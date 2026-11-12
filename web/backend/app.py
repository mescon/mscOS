#!/usr/bin/env python3
"""mscOS control panel: sources, audio chain, modules and system admin.

aiohttp server on :80. Browsers get one WebSocket (/api/ws) that carries status
snapshots, Soloist playback events and job output; actions are plain POSTs.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import ipaddress
import logging
import math
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auth import Auth  # noqa: E402
import board  # noqa: E402
import extmods  # noqa: E402
import collect  # noqa: E402
import i18n  # noqa: E402
from i18n import T  # noqa: E402
import plugins as pluginlib  # noqa: E402
from volbridge import VolumeBridge  # noqa: E402

def env(name: str, default: str = "") -> str:
    """MSCOS_<name> from the environment (SOTM_<name>: the name before the panel was mscos-panel)."""
    return os.environ.get(f"MSCOS_{name}", os.environ.get(f"SOTM_{name}", default))


STATIC = env("STATIC", os.path.join(os.path.dirname(__file__), "..", "static"))
STATE_DIR = env("STATE", "/var/lib/mscos")
RO_LOWER = "/media/root-ro"
# Site settings (/etc/mscos/site.env). Default: local mode, the panel answers everyone on the
# LAN at http://<hostname>.local. With MSCOS_PUBLIC_URL set (an HTTPS reverse proxy in front),
# only the proxy (MSCOS_TRUSTED_PEERS) and the device itself may talk to the panel directly;
# everyone else is sent to the HTTPS name, so the password never crosses the LAN in clear text.
PUBLIC_URL = env("PUBLIC_URL").strip().rstrip("/")
TRUSTED_PEERS = set(env("TRUSTED_PEERS").split()) | {"127.0.0.1", "::1"}
SESSION_COOKIE = "mscos_session"
OLD_SESSION_COOKIE = "sotm_session"  # still accepted, so nobody is logged out by the rename

log = logging.getLogger("mscos-panel")


# =========================================================================== helpers
def overlay_active() -> bool:
    return collect.system_info()["root_overlay"]


def _walk_nofollow(root: str, rel: str, create: bool) -> int:
    """An fd for directory root/rel, walking one component at a time with O_NOFOLLOW (no
    symlink anywhere on the way), creating missing directories if asked. Caller closes it."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in [p for p in rel.split("/") if p]:
            if part in (".", ".."):
                raise OSError(f"refusing path component {part!r}")
            try:
                nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=fd)
                nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def persist(path: str) -> None:
    """With overlayroot active, also write a changed file to the real SD-card root.
    Some of these files live in the soloist user's tree, so neither end may follow a symlink
    at any level, and only permission bits (no setuid/setgid) and the owner are carried over."""
    if not overlay_active() or not os.path.isdir(RO_LOWER):
        return
    if not os.path.isabs(path):
        return
    d, name = os.path.split(path)
    subprocess.run(["mount", "-o", "remount,rw", RO_LOWER], check=False)
    try:
        sdir = _walk_nofollow("/", d, create=False)
        try:
            src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=sdir)
        finally:
            os.close(sdir)
        try:
            st = os.fstat(src)
            if not stat.S_ISREG(st.st_mode):
                return
            ddir = _walk_nofollow(RO_LOWER, d, create=True)
            try:
                out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=ddir)
            finally:
                os.close(ddir)
            try:
                os.fchown(out, st.st_uid, st.st_gid)
                os.fchmod(out, st.st_mode & 0o777)
                with os.fdopen(src, "rb", closefd=False) as fi, os.fdopen(out, "wb", closefd=False) as fo:
                    shutil.copyfileobj(fi, fo)
            finally:
                os.close(out)
        finally:
            os.close(src)
    except OSError as e:
        log.warning("persist %s: %s", path, e)
    finally:
        subprocess.run(["mount", "-o", "remount,ro", RO_LOWER], check=False)


async def user_systemctl(*args: str):
    return await collect.run(["systemctl", "--user", *args], as_user=True, timeout=30)


# =========================================================================== hub
class Hub:
    def __init__(self):
        self.clients: set[web.WebSocketResponse] = set()
        self.status: dict = {}
        self.xruns = collections.deque(maxlen=24 * 60 * 2)  # 30 s samples, 24 h
        self.last_err: dict = {}
        self.jobs: dict[str, dict] = {}
        self.saved_stats: dict = {}  # the stats file as read, for the modules that keep state in it
        self.settings: dict = {"alerts": True, "leds": True, "net_speed": 100, "spotify_vol_to_amp": True,
                               "source": "",  # the source module that owns the DAC (one at a time)
                               "language": "en"}  # system language (alerts); the panel follows the browser
        # 30 s samples, 24 h: the DAC's measured clock and the supply rail
        self.clock = collections.deque(maxlen=24 * 60 * 2)
        self.supply = collections.deque(maxlen=24 * 60 * 2)
        self.alert_sent: dict[str, float] = {}
        self.since: dict[str, float] = {}  # when a bad condition was first seen
        self._load()
        self.plugins: dict[str, pluginlib.Plugin] = {}
        self._load_plugins()
        self.vol = VolumeBridge(self)  # Spotify slider -> the amplifier, stream kept at unity
        self._was_playing = False
        self._idle_since = time.time()
        self._dac_running = False
        self.source_lock = asyncio.Lock()

    # ---- modules
    def _load_plugins(self):
        """Start the enabled modules. Installs from before modules get what they used."""
        found = pluginlib.discover()
        if "plugins" not in self.settings:
            s, en = self.settings, []
            if s.get("amp") == "levinson" or s.get("ml_host") or s.get("ml_host_found"):
                en.append("levinson")
            if os.environ.get("ARCAM_HOST") or s.get("arcam_host"):
                en.append("arcam")
            if "levinson" in en and ("ml_tv_input" in s or "arcam" in en):
                en.append("tvmode")
            if os.environ.get("HA_WEBHOOK_URL"):
                en.append("homeassistant")
            self.settings["plugins"] = en
            try:
                self.save_settings()
            except OSError:
                pass
        if self.settings.get("plugins_v", 1) < 2:  # sources became modules: turn on what is installed
            en = self.settings["plugins"]
            if os.path.exists(f"{collect.AUDIO_HOME}/.config/systemd/user/soloist.service") and "spotify" not in en:
                en.append("spotify")
                self.settings.setdefault("source", "spotify")
                self.settings["source"] = self.settings["source"] or "spotify"
            if os.path.exists("/opt/RoonBridge/start.sh") and "roon" not in en:
                en.append("roon")
            if os.path.exists("/sys/module/snd_usb_audio/parameters/mscos_dsd") and "cm6632a" not in en:
                en.append("cm6632a")
            self.settings["plugins_v"] = 3
        if self.settings.get("plugins_v", 1) < 3:  # (v2 was sources only)
            en = self.settings["plugins"]
            if os.path.exists("/sys/module/snd_usb_audio/parameters/mscos_dsd") and "cm6632a" not in en:
                en.append("cm6632a")
            self.settings["plugins_v"] = 3
            try:
                self.save_settings()
            except OSError:
                pass
        self.available_plugins = found
        for pid in self.settings["plugins"]:
            cls = found.get(pid)
            if cls is None:
                log.warning("module %s is enabled but not installed", pid)
                continue
            for k, v in cls.defaults.items():
                self.settings.setdefault(k, v)
            try:
                self.plugins[pid] = cls(self)
            except Exception:
                log.exception("module %s failed to start", pid)
        log.info("modules: %s", ", ".join(self.plugins) or "none")
        if self.settings.get("source") not in self.sources:  # its module was turned off
            self.settings["source"] = next(iter(self.sources), "")

    @property
    def sources(self) -> dict:
        """The enabled source modules, by id."""
        return {i: p for i, p in self.plugins.items() if p.kind == "source"}

    @property
    def source(self):
        """The source that owns the DAC now, or None."""
        return self.sources.get(self.settings.get("source") or "")

    @property
    def soloist(self) -> bool:
        """Is Spotify Lossless (Soloist) the current source?"""
        return self.settings.get("source") == "spotify"

    async def switch_source(self, src: str) -> dict:
        """Hand the DAC to one source: stop every other one, then PipeWire if the new one
        opens the DAC itself, wait for the DAC to be free, and start it."""
        new = self.sources.get(src)
        if src and new is None:
            raise RuntimeError(T("Okänd källa"))
        if new and not await new.installed():
            raise RuntimeError(T("{name} är inte installerat", name=new.name.get(i18n.current(), new.label)))
        err, direct_before = "", False
        for s in self.sources.values():
            if s is not new and await s.active():
                direct_before = direct_before or not s.pipewire
                err += await s.stop()
        via_pw = bool(new and new.pipewire)
        released = True
        if not via_pw:
            err += await collect.pipewire("stop")
        if not via_pw or direct_before:
            released = await collect._wait_released()
        if via_pw:
            err += await collect.pipewire("start")
        e2 = await new.start() if new else ""
        ok_ = not e2
        self.settings["source"] = src
        await asyncio.get_running_loop().run_in_executor(None, self.save_settings)
        self._was_playing = False
        self._idle_since = time.time()
        for p in self.sources.values():  # the ones that lost the DAC forget what they played
            if p is not new:
                p.lost_dac()
        return {"ok": ok_, "source": src, "dac_released": released, "error": (err + e2)[:300]}

    def source_changed(self, plugin) -> None:
        """A source module reports new state (what plays): tell the panel, note play starts."""
        if plugin is not self.source:
            return
        np = plugin.now_playing()
        if np is not None:
            self._set_playing(bool(np.get("playing")))
        asyncio.ensure_future(self.broadcast({"type": "now", "source": plugin.id, "now": np}))

    @property
    def amp(self):
        """The volume: the first enabled amplifier module, or None."""
        return next((p for p in self.plugins.values() if p.kind == "amp"), None)

    def plugin(self, pid: str):
        return self.plugins.get(pid)

    # ---- cards the modules describe (Plugin.panel)
    def panels(self) -> dict:
        out = {}
        for pid, p in self.plugins.items():
            try:
                d = p.panel()
            except Exception:  # noqa: BLE001 - a module's card must not take the panel down
                log.exception("panel of %s", pid)
                d = None
            if d:
                out[pid] = d
        return out

    def panel_changed(self, plugin=None) -> None:
        """A module's card changed (or any may have): tell the open panels."""
        asyncio.ensure_future(self.broadcast({"type": "panels", "panels": self.panels()}))

    @property
    def playing_here(self) -> bool:
        return self._was_playing

    @property
    def idle_since(self) -> float:
        return self._idle_since

    num = staticmethod(lambda *a, **k: num(*a, **k))
    user_systemctl = staticmethod(lambda *a, **k: user_systemctl(*a, **k))
    persist = staticmethod(lambda *a, **k: persist(*a, **k))
    overlay_active = staticmethod(lambda: overlay_active())
    body = staticmethod(lambda request: body(request))
    ok = staticmethod(lambda **kw: ok(**kw))

    def play_started(self, force: bool = False):
        for p in self.plugins.values():
            asyncio.ensure_future(p.on_play_start(force))

    async def pause_if_playing(self) -> bool:
        """Pause the source if it plays here and can be paused; True if it was."""
        cur = self.source
        np = cur.now_playing() if cur else None
        if cur and "pause" in cur.transport and np and np.get("playing") and (cur.slider() or {"active": True}).get("active"):
            try:
                await cur.command("pause")
            except RuntimeError:
                return False
            return True
        return False

    def names(self) -> dict:
        """What to call things. Each is a setting; empty means the detected default:
        output    what this device plays into: the amplifier module's model, else the DAC
        streamer  this device: the board name, PC model or device tree model"""
        s, a = self.settings, self.amp
        det_out = (getattr(a, "model", "") if a else "") or collect.detect_dac() or "DAC"
        det_str = collect.detect_streamer()
        out, strm = s.get("output_name") or det_out, s.get("streamer_name") or det_str
        # what the device is called on the network (Spotify Connect, ...): one of the two, or a name of its own
        mode = s.get("network_name", "streamer")
        net = (s.get("network_name_custom") if mode == "custom" else out if mode == "output" else strm) or strm or out
        return {"output": out, "streamer": strm, "network": net, "detected": {"output": det_out, "streamer": det_str}}

    def features(self) -> dict:
        """What this installation has, so the panel shows only that."""
        f = {"amp": "none", "arcam": False, "plugins": list(self.plugins), "leds": bool(collect._leds()),
             "sources": [{"id": i, "label": p.label, "name": p.name, "protocol": p.protocol, "pipewire": p.pipewire,
                          "transport": list(p.transport),
                          "hint": p.hint, "terms": p.terms, "terms_url": p.terms_url} for i, p in self.sources.items()]}
        bp = board.profile()
        speed = collect.read(f"/sys/class/net/{collect.net_if()}/speed").strip() if collect.net_if() else ""
        f["board"] = {"power_button": bp["POWER_BUTTON"] != "no", "sd": bp["STORAGE"] == "sd",
                      # a wired link whose speed can be chosen (the control is hidden otherwise)
                      "link_mbps": int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else None}
        for p in self.plugins.values():
            f.update(p.features())
        return f

    def summary(self) -> dict:
        """One cheap snapshot (in-memory state only, plus the DAC's /proc state) for automations."""
        d = collect.dac_info()
        src = self.settings.get("source", "")
        # PipeWire keeps the DAC open (ALSA RUNNING) also in silence, so with a source that
        # plays through it "running" is what the source says; Roon Bridge and NAA open the
        # DAC only while they play.
        if self.source and self.source.pipewire:
            running = self._was_playing
        else:
            running = d.get("state") == "RUNNING"
        out = {"ts": time.time(), "source": src}
        for p in self.plugins.values():
            out.update(p.summary())
        out["dac"] = {"present": bool(d.get("present")), "running": running if d.get("present") else None}
        return out

    # ---- persistence of small stats (rarely written, SD-friendly)
    def _load(self):
        try:
            with open(os.path.join(STATE_DIR, "stats.json")) as f:
                d = json.load(f)
            self.saved_stats = d
            self.xruns.extend(d.get("xruns", []))
            self.alert_sent.update(d.get("alert_sent", {}))
            self.clock.extend(r for r in d.get("clock", []) if len(r) == 5)
            self.supply.extend(d.get("supply", []))
        except (OSError, ValueError):
            pass
        try:
            with open(os.path.join(STATE_DIR, "settings.json")) as f:
                saved = json.load(f)
            self.settings.update(saved)
            if "language" not in saved:  # an install from before the panel had languages
                self.settings["language"] = "sv"
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = os.path.join(STATE_DIR, "stats.json.tmp")
        with open(tmp, "w") as f:
            d = {}
            for p in self.plugins.values():  # what the modules keep (the verification log, ...)
                d.update(p.save_state())
            d.update({"xruns": list(self.xruns), "alert_sent": self.alert_sent, "clock": list(self.clock),
                      "supply": list(self.supply)})
            json.dump(d, f)
        os.replace(tmp, os.path.join(STATE_DIR, "stats.json"))

    def save_settings(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        p = os.path.join(STATE_DIR, "settings.json")
        with open(p + ".tmp", "w") as f:
            json.dump(self.settings, f)
        os.replace(p + ".tmp", p)
        persist(p)

    # ---- secrets: addresses with tokens, keys. A root-only file; never in the settings
    # (those are sent to the panel) and never sent anywhere but to the service they are for.
    def secret(self, key: str) -> str:
        if not hasattr(self, "_secrets"):
            try:
                with open(os.path.join(STATE_DIR, "secrets.json")) as f:
                    self._secrets = json.load(f)
            except (OSError, ValueError):
                self._secrets = {}
        return self._secrets.get(key, "")

    def set_secret(self, key: str, value: str) -> None:
        self.secret(key)
        if value:
            self._secrets[key] = value
        else:
            self._secrets.pop(key, None)
        os.makedirs(STATE_DIR, exist_ok=True)
        p = os.path.join(STATE_DIR, "secrets.json")
        fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._secrets, f)
        os.replace(p + ".tmp", p)
        persist(p)

    async def broadcast(self, msg: dict):
        data = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except (ConnectionError, RuntimeError):
                self.clients.discard(ws)

    # ---- Soloist WebSocket relay
    # ---- periodic sampling
    async def sample_loop(self):
        """Status every 5 s while someone has the panel open, else once a minute.
        Dropout counters every 30 s regardless, for the 24 h history."""
        n = 0
        while True:
            try:
                if self.clients or n % 12 == 0:
                    await self.refresh_status()
                    await self.check_health()
                if n % 6 == 0:
                    await self.sample_xruns()
                cur = self.source
                # nothing chosen, or what is chosen has no player yet (a new installation): take the
                # first source that is installed
                if n % 2 == 0 and (cur is None or not await cur.installed()):
                    for sid, p in self.sources.items():
                        if p is not cur and await p.installed():
                            log.info("no source chosen: taking %s", sid)
                            await self.switch_source(sid)
                            break
                if cur and not cur.pushes_playing:
                    p = await cur.playing()
                    self._set_playing(collect.dac_info().get("state") == "RUNNING" if p is None else p)
                for p in list(self.plugins.values()):
                    await p.tick()
                if n % 720 == 0 and n:  # hourly: keep SD-card writes rare
                    await asyncio.get_running_loop().run_in_executor(None, self.save)
            except Exception:  # keep sampling no matter what
                log.exception("sampling failed")
            n += 1
            await asyncio.sleep(5)

    async def refresh_status(self):
        pw = await collect.pipewire_info()
        extra: dict = {}
        for p in self.plugins.values():
            extra.update(await p.status_fields())
        srcs = {}
        for i, p in self.sources.items():
            srcs[i] = {"installed": await p.installed(), "active": await p.active(), **await p.state()}
        if time.monotonic() - getattr(self, "_usb_err_t", -1e9) > 60:
            self._usb_err = await collect.usb_errors_since_boot()
            self._usb_err_t = time.monotonic()
        self.status = {
            "ts": time.time(),
            "system": collect.system_info(),
            "dac": collect.dac_info(),
            "pipewire": pw,
            **extra,
            "usb_errors": self._usb_err,
            "supply": collect.supply(),
            "source": self.settings.get("source", ""),
            "sources": srcs,
            "now": self.source.now_playing() if self.source else None,
            "leds": collect.leds_state(),
            "pw_top": self.last_err,
        }
        await self.broadcast({"type": "status", "status": self.status})

    async def sample_xruns(self):
        top = await collect.pw_top_errors()
        sink = next((v for k, v in top.items() if k.startswith("alsa_output.usb")), None)
        stream = next((v for k, v in top.items() if k == "spotify"), None)
        prev = self.last_err
        playing = bool(stream and stream["state"] == "R")
        point = {"t": int(time.time()), "playing": playing,
                 "dac": 0, "stream": 0}
        if sink and prev.get("sink") is not None and sink["err"] >= prev["sink"]:
            point["dac"] = sink["err"] - prev["sink"]
        if stream and prev.get("stream") is not None and stream["err"] >= prev["stream"]:
            point["stream"] = stream["err"] - prev["stream"]
        self.last_err = {"sink": sink["err"] if sink else None,
                         "stream": stream["err"] if stream else None,
                         "quantum": sink["quantum"] if sink else None}
        self.xruns.append(point)
        ck, sp = collect.pcm_counter(), collect.supply()
        if ck:
            self.clock.append([point["t"], ck["ts"], ck["hw"], ck["nominal"], ck["trig"]])
        if sp:
            self.supply.append([point["t"], sp["v"], sp["a"]])
        await self.broadcast({"type": "xrun", "point": point})
        # Only DAC-side xruns are dropouts. The Spotify stream's own counter also ticks when
        # Soloist (re)starts a stream or changes volume; recordings of the sink show no gaps then.
        n = point["dac"]
        if playing and n:
            await self.alert("dropouts", T("Avbrott i ljudet"),
                             T("{n} avbrott under uppspelning kl {time}. Se avbrottshistoriken i panelen.",
                               n=n, time=time.strftime("%H:%M")),
                             cooldown=3600)

    # ---- alerts, delivered by the modules that are a transport (Home Assistant, ...)
    def can_alert(self) -> bool:
        """Is any module that delivers alerts on and set up?"""
        return any(p.can_notify() for p in self.plugins.values())

    async def alert(self, kind: str, title: str, message: str, cooldown: float = 6 * 3600, force=False) -> bool:
        if (not self.settings.get("alerts") or kind in self.settings.get("alerts_off", [])) and not force:
            return False
        now = time.time()
        if not force and now - self.alert_sent.get(kind, 0) < cooldown:
            return False
        ok_ = False
        for p in self.plugins.values():
            ok_ = await p.notify(kind, title, message) or ok_
        if ok_ and not force:
            self.alert_sent[kind] = now
        return ok_

    def _held(self, key: str, bad: bool, secs: float) -> bool:
        """True once a condition has been bad continuously for `secs`."""
        if not bad:
            self.since.pop(key, None)
            return False
        first = self.since.setdefault(key, time.time())
        return time.time() - first >= secs

    async def check_health(self):
        st = self.status
        dac = st.get("dac") or {}
        if self._held("dac", not dac.get("present"), 60):
            await self.alert("dac", T("DAC:en hittas inte"),
                             T("DAC:en syns inte längre på USB. Kontrollera kabeln och att DAC:en är på."))
        for p in list(self.plugins.values()):
            await p.check_health()

    # ---- playback on this device: modules react (the amplifier wakes and selects its input)
    def _set_playing(self, playing: bool):
        if playing and not self._was_playing and self.source:
            self.source.play_began()
        # A real start, not a track change: Soloist pauses for a moment between tracks, and
        # re-selecting the input then makes the amplifier relock (audible glitch).
        if playing and not self._was_playing and time.time() - self._idle_since > 30:
            self.vol.settle()  # the app may now push its stored volume: not a move of the slider
            self.play_started()
        if not playing and self._was_playing:
            self._idle_since = time.time()
        self._was_playing = playing

    # ---- jobs: long-running commands with streamed output
    async def run_job(self, name: str, cmd: list[str] | str, as_user=False):
        if self.jobs.get(name, {}).get("running"):
            raise web.HTTPConflict(text=T("{name} pågår redan", name=name))
        job = {"name": name, "running": True, "output": "", "rc": None, "started": time.time()}
        self.jobs[name] = job

        async def go():
            if isinstance(cmd, str):
                argv = ["bash", "-c", cmd]
            else:
                argv = list(cmd)
            p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT,
                                                     env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
            async for line in p.stdout:
                text = line.decode(errors="replace")
                job["output"] = (job["output"] + text)[-20000:]
                await self.broadcast({"type": "job", "name": name, "line": text})
            job["rc"] = await p.wait()
            job["running"] = False
            await self.broadcast({"type": "job", "name": name, "done": True, "rc": job["rc"]})
            await self.refresh_status()

        asyncio.create_task(go())
        return job


hub = Hub()
AUTH = Auth(os.path.join(STATE_DIR, "auth.json"), on_write=persist)
routes = web.RouteTableDef()


def ok(**kw):
    return web.json_response({"ok": True, **kw})


async def body(request) -> dict:
    try:
        b = await request.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return b if isinstance(b, dict) else {}


def num(v, lo: float, hi: float, what: str, integer: bool = False):
    """A finite number within [lo, hi] from JSON input, else 400 (never 500, never NaN)."""
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        raise web.HTTPBadRequest(text=T("{what}: ange ett tal", what=T(what)))
    if not math.isfinite(x):
        raise web.HTTPBadRequest(text=T("{what}: ange ett tal", what=T(what)))
    x = max(lo, min(hi, x))
    return int(round(x)) if integer else round(x, 1)


# =========================================================================== routes
@routes.get("/api/ws")
async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    hub.clients.add(ws)
    await ws.send_str(json.dumps({
        "type": "hello", "status": hub.status, "xruns": list(hub.xruns)[-2880:],
        "features": hub.features(), "names": hub.names(), **{k: v for p in hub.plugins.values() for k, v in p.hello().items()},
        "now": hub.source.now_playing() if hub.source else None, "panels": hub.panels(),
        "settings": {**hub.settings, "alerts_configured": hub.can_alert()},
        "jobs": {k: {kk: vv for kk, vv in v.items()} for k, v in hub.jobs.items()},
    }))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT and msg.data == "ping":
                await ws.send_str('{"type":"pong"}')
    finally:
        hub.clients.discard(ws)
    return ws


@routes.get("/api/status")
async def status(request):
    return web.json_response(hub.status)


# ---- Soloist


@routes.post("/api/player")
async def player_command(request):
    """The panel's transport buttons, for whichever source plays: toggle, play, pause, next,
    prev, seek (position_ms), shuffle (enabled), repeat (mode)."""
    b = await body(request)
    src, c = hub.source, b.get("command")
    if src is None or c not in src.transport:
        raise web.HTTPConflict(text=T("Källan tar inte det kommandot."))
    kw: dict = {}
    if c == "seek":
        kw["position_ms"] = int(num(b.get("position_ms"), 0, 24 * 3600 * 1000, "position"))
    if c == "shuffle":
        kw["enabled"] = bool(b.get("enabled"))
    if c == "repeat":
        if b.get("mode") not in ("off", "context", "track"):
            raise web.HTTPBadRequest(text=T("Okänt val"))
        kw["mode"] = b["mode"]
    try:
        await src.command(c, **kw)
    except RuntimeError as e:
        raise web.HTTPBadGateway(text=str(e))
    return ok()


@routes.get("/api/optimizations")
async def get_optimizations(request):
    cur = hub.source
    items = await asyncio.to_thread(collect.optimizations, hub.status, {}, hub.vol.enabled(),
                                    hub.names()["output"],
                                    {"id": cur.id, "pipewire": cur.pipewire, "label": cur.label, "slider": cur.slider(),
                                     "rt_proc": cur.rt_proc, "rows": cur.optimization_rows()} if cur else None)
    return web.json_response({"t": int(time.time()), "items": items})


# ---- Audio test (capture what goes to the DAC, look for dropouts and bit-perfectness)
# ---- Source switch: one source module at a time owns the DAC
@routes.post("/api/source")
async def set_source(request):
    src = (await body(request)).get("source")
    if src not in hub.sources:
        raise web.HTTPBadRequest(text=T("Okänd källa"))
    if hub.source_lock.locked():
        raise web.HTTPConflict(text=T("Byter redan källa"))
    async with hub.source_lock:
        try:
            r = await hub.switch_source(src)
        except RuntimeError as e:
            raise web.HTTPConflict(text=str(e))
        if not r["ok"]:
            raise web.HTTPInternalServerError(text=T("Bytet misslyckades: {error}", error=r["error"]))
    await hub.broadcast({"type": "settings", "settings": hub.settings})
    await hub.refresh_status()
    return ok(**r)


@routes.post("/api/sources/install")
async def source_install(request):
    """Download and set up a source's player on this device, from its publisher (the
    module's install script). Output is streamed to the panel as a job."""
    b = await body(request)
    p = hub.sources.get(b.get("id"))
    if p is None:
        raise web.HTTPNotFound(text=T("Okänd källa"))
    script = os.path.join(p.module_dir, "install.sh")
    if not os.path.exists(script):
        raise web.HTTPNotFound(text=T("Installationsskriptet saknas: {path}", path=script))
    if overlay_active():
        raise web.HTTPConflict(text=T("Stäng av skrivskyddet först. Med skrivskyddat system försvinner installationen vid omstart."))
    # the shared helpers come with mscOS, also for a module installed beside it
    await hub.run_job(f"install-{p.id}", ["env", f"MSCOS_COMMON={os.path.join(pluginlib._modules_dir(), 'common.sh')}", "bash", script])
    return ok()


# ---- DAC detail (Eunhasu's /dacinfo/, interpreted)
@routes.get("/api/dac")
async def dac(request):
    c = getattr(hub, "_dac_cache", (0.0, None))
    if time.monotonic() - c[0] < 8 and c[1]:
        return web.json_response(c[1])
    d = await collect.dac_detail()
    if d.get("present"):
        d["clock_avg"] = collect.clock_stats(list(hub.clock), collect.pcm_counter())
    d["supply_history"] = list(hub.supply)
    hub._dac_cache = (time.monotonic(), d)
    return web.json_response(d)


@routes.get("/api/names")
async def get_names(request):
    return web.json_response(hub.names())


# ---- Settings, alerts
ALERT_KINDS = ("dac", "soloist", "dropouts", "verify", "expiry")  # what hub.alert is called with


@routes.get("/api/settings")
async def get_settings(request):
    return web.json_response({**hub.settings, "alerts_configured": hub.can_alert()})


@routes.post("/api/settings")
async def set_settings(request):
    b = await body(request)
    upd: dict = {k: bool(b[k]) for k in ("alerts", "leds", "spotify_vol_to_amp", "setup_done") if k in b}
    for k in ("output_name", "streamer_name"):
        if k in b:
            v = re.sub(r"\s+", " ", str(b[k])).strip()
            if len(v) > 60 or any(ord(c) < 32 for c in v):
                raise web.HTTPBadRequest(text=T("Namnet får vara högst 60 tecken."))
            upd[k] = v  # "" = the detected name
    if "alerts_off" in b:  # the kinds of alert the user does not want
        if not isinstance(b["alerts_off"], list) or any(k not in ALERT_KINDS for k in b["alerts_off"]):
            raise web.HTTPBadRequest(text=T("Okänt val"))
        upd["alerts_off"] = sorted(set(b["alerts_off"]))
    if "network_name" in b:
        if b["network_name"] not in ("streamer", "output", "custom"):
            raise web.HTTPBadRequest(text=T("Okänt val"))
        upd["network_name"] = b["network_name"]
    if "network_name_custom" in b:
        v = re.sub(r"\s+", " ", str(b["network_name_custom"])).strip()
        if len(v) > 40 or any(ord(c) < 32 for c in v):
            raise web.HTTPBadRequest(text=T("Namnet får vara högst 40 tecken."))
        upd["network_name_custom"] = v
    if upd.get("network_name", hub.settings.get("network_name")) == "custom" \
            and not upd.get("network_name_custom", hub.settings.get("network_name_custom")):
        raise web.HTTPBadRequest(text=T("Ange ett namn."))
    if "language" in b:  # the system language: alerts and other texts written without a browser
        if b["language"] not in i18n.LANGS:
            raise web.HTTPBadRequest(text=T("Okänt språk"))
        upd["language"] = b["language"]
    tz = b.get("timezone")
    if tz is not None:  # the device's clock zone (logs, the nightly maintenance): the guide sends the browser's
        if not (isinstance(tz, str) and re.fullmatch(r"[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}", tz)
                and os.path.isfile(f"/usr/share/zoneinfo/{tz}")):
            raise web.HTTPBadRequest(text=T("Okänd tidszon"))
    for p in hub.plugins.values():
        p.update_settings(b, upd)
    secrets = {o["key"]: p for p in hub.plugins.values() for o in p.option_list() if o["type"] == "secret" and o["key"] in b}
    for k in secrets:
        v = str(b[k]).strip()
        if len(v) > 2000 or any(ord(c) < 32 for c in v):
            raise web.HTTPBadRequest(text=T("Ogiltigt värde"))
    # all valid: apply
    for k, p in secrets.items():
        await p.set_secret(k, str(b[k]).strip())
        await p.settings_applied({k: True})
    if tz is not None:
        await collect.run(["timedatectl", "set-timezone", tz], timeout=10)
    hub.settings.update(upd)
    if "leds" in upd:
        collect.apply_leds(upd["leds"])
    for p in hub.plugins.values():
        # the bridge setting decides how a source with a slider handles volume
        if any(k in upd for k in p.defaults) or ("spotify_vol_to_amp" in upd and getattr(type(p), "has_slider", False)):
            await p.settings_applied(upd)
    if any(k in upd for k in ("output_name", "streamer_name", "network_name", "network_name_custom")):
        for p in hub.sources.values():  # the players take the device's name on the network
            try:
                await p.names_changed()
            except RuntimeError as e:
                log.warning("name for %s: %s", p.id, e)
    if b.get("net_speed") in (100, 1000) and b["net_speed"] != hub.settings.get("net_speed"):
        hub.settings["net_speed"] = b["net_speed"]
        # the link renegotiates (~3 s); answer first so the reply is not lost
        asyncio.get_running_loop().call_later(1, lambda: asyncio.ensure_future(collect.apply_net_speed(b["net_speed"])))
    await asyncio.get_running_loop().run_in_executor(None, hub.save_settings)
    shown = {**hub.settings, "alerts_configured": hub.can_alert()}
    await hub.broadcast({"type": "settings", "settings": shown, "features": hub.features(), "names": hub.names()})
    return ok(settings=shown)


# ---- the amplifier (whichever module is on) and the modules' own cards
@routes.post("/api/amp")
async def amp_action(request):
    a = hub.amp
    if a is None:
        raise web.HTTPNotFound(text=T("Ingen förstärkarmodul är på."))
    await a.amp_action(await body(request))
    hub.panel_changed()  # cards that show the amplifier's input (TV mode) follow
    return ok(amp=a.amp_state())


@routes.post("/api/panels/{id}")
async def panel_action(request):
    p = hub.plugins.get(request.match_info["id"])
    if p is None or p.panel() is None:
        raise web.HTTPNotFound(text=T("Okänd modul"))
    b = await body(request)
    try:
        await p.panel_action(str(b.get("name", "")), b.get("value"))
    except RuntimeError as e:
        raise web.HTTPBadGateway(text=str(e))
    return ok(panel=p.panel())


# ---- modules: which are installed, which are on
KIND_ORDER = {"source": 0, "amp": 1, "device": 2, "notify": 3, "integration": 4}


async def _plugin_list() -> list[dict]:
    out = []
    for pid, cls in sorted(hub.available_plugins.items(), key=lambda kv: (KIND_ORDER.get(kv[1].kind, 9), kv[0])):
        d = {"id": pid, "kind": cls.kind, "name": cls.name, "description": cls.description, "external": cls.external,
             "module_version": cls.module_version, "module_latest": _module_latest.get(pid),
             "latest": _latest.get(pid),
             "requires": list(cls.requires), "enabled": pid in hub.settings.get("plugins", []),
             "running": pid in hub.plugins}
        if pid in hub.plugins:
            d["status"] = hub.plugins[pid].status()
        if pid in hub.plugins:
            opts = hub.plugins[pid].option_list()
            if opts:  # a secret's value never leaves the device: only whether one is set
                d["options"] = [{**o, "value": None, "set": hub.plugins[pid].secret_set(o["key"])} if o["type"] == "secret"
                                else {**o, "value": hub.settings.get(o["key"])} for o in opts]
            d["log"] = hub.plugins[pid].log()
            d["facts"] = hub.plugins[pid].facts()
            d["actions"] = list(hub.plugins[pid].actions)
        if cls.kind == "source":  # its player is installed on the device, from its publisher
            d.update(terms=cls.terms, terms_url=cls.terms_url, updates=cls.updates,
                     installed=await hub.plugins[pid].installed() if pid in hub.plugins else None)
            if d["installed"]:
                d["version"] = await hub.plugins[pid].version()
                d["latest"] = _latest.get(pid)  # from the last check, None: not checked
        out.append(d)
    return out


_latest: dict[str, str] = {}   # source id -> newest version at its publisher
_module_latest: dict[str, str] = {}  # module id -> a newer version of the module in the catalogue
_latest_t = 0.0


@routes.post("/api/plugins/check")
async def plugins_check(request):
    """Ask each installed player's publisher for its newest version. Only on request: the
    device does not call out by itself. Players that update themselves are not asked."""
    global _latest_t
    failed = []
    for pid, p in hub.sources.items():
        if p.updates != "manual" or not await p.installed():
            continue
        try:
            v = await p.latest()
        except Exception as e:  # noqa: BLE001 - no network, or the publisher's site changed
            log.warning("newest version of %s: %s", pid, e)
            v = None
        if v:
            _latest[pid] = v
        else:
            failed.append(pid)
    async with ClientSession(timeout=ClientTimeout(total=15)) as http:  # modules installed beside mscOS
        for pid, cls in hub.available_plugins.items():
            src = (cls.external or {}).get("source")
            if not src or src == "catalogue":
                continue
            try:
                async with http.get(src) as r:
                    _latest[pid] = extmods.check_manifest(json.loads(await r.text()), remote=True)["version"]
            except Exception as e:  # noqa: BLE001
                log.warning("newest version of %s: %s", pid, type(e).__name__)
                failed.append(pid)
        try:  # the project's catalogue: a newer version of a module than this device has
            for m in await fetch_catalogue(http):
                cls = hub.available_plugins.get(m["id"])
                if cls and pluginlib._vkey(m["version"]) > pluginlib._vkey(cls.module_version):
                    _module_latest[m["id"]] = m["version"]
                else:
                    _module_latest.pop(m["id"], None)
        except (OSError, ValueError, asyncio.TimeoutError) as e:
            log.warning("catalogue: %s", e)
            failed.append("catalogue")
    _latest_t = time.time()
    return web.json_response({"plugins": await _plugin_list(), "restart_needed": _plugins_restart_needed(),
                              "checked": int(_latest_t), "failed": failed, "mscos": hub.status.get("system", {}).get("mscos", "")})


@routes.get("/api/plugins")
async def plugins_list(request):
    return web.json_response({"plugins": await _plugin_list(), "restart_needed": _plugins_restart_needed(),
                              "checked": int(_latest_t) or None, "mscos": hub.status.get("system", {}).get("mscos", "")})


_ext_changed = False  # a module was installed, updated or removed: the panel must start again to load it


def _plugins_restart_needed() -> bool:
    return _ext_changed or set(hub.settings.get("plugins", [])) != set(hub.plugins)


@routes.post("/api/plugins/set")
async def plugins_set(request):
    """Turn a module on or off. Takes effect when the panel restarts (restart: true does it
    now): a module adds routes and tasks, which are set up once at start."""
    b = await body(request)
    pid = b.get("id")
    if pid not in hub.available_plugins:
        raise web.HTTPNotFound(text=T("Okänd modul"))
    en = [p for p in hub.settings.get("plugins", []) if p != pid]
    if b.get("enabled"):
        cls = hub.available_plugins[pid]
        if "amp" in cls.requires and not any(hub.available_plugins[p].kind == "amp" for p in en if p in hub.available_plugins):
            raise web.HTTPConflict(text=T("Modulen behöver en förstärkarmodul. Slå på en förstärkare först."))
        if cls.kind == "amp":  # one volume: another amplifier module goes off
            en = [p for p in en if hub.available_plugins.get(p) is None or hub.available_plugins[p].kind != "amp"]
        en.append(pid)
    else:
        cur = hub.plugins.get(pid)
        if cur is not None and cur.kind == "source" and await cur.active():
            async with hub.source_lock:  # a source that is turned off stops playing now
                await cur.stop()
        # what needs an amplifier goes with the last one
        if hub.available_plugins[pid].kind == "amp":
            en = [p for p in en if "amp" not in hub.available_plugins.get(p, pluginlib.Plugin).requires]
    hub.settings["plugins"] = en
    await asyncio.get_running_loop().run_in_executor(None, hub.save_settings)
    if b.get("restart"):
        log.info("modules now %s; restarting the panel", en)
        asyncio.get_running_loop().call_later(0.5, lambda: os._exit(0))  # systemd starts it again
    return ok(plugins=await _plugin_list(), restart_needed=_plugins_restart_needed())


# ---- the catalogue: modules/index.json in the project's repository
def catalogue_url() -> str:
    """MSCOS_CATALOGUE, else modules/index.json on the main branch of the repository named
    in /etc/mscos-release (HOME_URL)."""
    if env("CATALOGUE"):
        return env("CATALOGUE")
    home = dict(re.findall(r'^(\w+)="?([^"\n]*)"?', collect.read("/etc/mscos-release"), re.M)).get("HOME_URL", "")
    m = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+?)/?", home)
    return f"https://raw.githubusercontent.com/{m.group(1)}/main/modules/index.json" if m else ""


_catalogue: tuple[float, list] = (0.0, [])


async def fetch_catalogue(http) -> list[dict]:
    global _catalogue
    url = catalogue_url()
    # the project's own address is always HTTPS; one set by the device's owner (MSCOS_CATALOGUE,
    # a mirror on the local network) is taken as given
    if not url.startswith("https://") and not (env("CATALOGUE") and url.startswith("http://")):
        raise OSError("no catalogue address")
    async with http.get(url) as r:
        if r.status != 200:
            raise OSError(f"HTTP {r.status}")
        d = json.loads(await r.text())
    mods = [m for m in d.get("modules", []) if isinstance(m, dict) and extmods.ID.fullmatch(str(m.get("id", "")))
            and extmods.VERSION.fullmatch(str(m.get("version", ""))) and isinstance(m.get("files"), list)]
    _catalogue = (time.time(), mods)
    return mods


@routes.post("/api/plugins/action")
async def plugins_action(request):
    """A button in a module's card (Plugin.actions)."""
    b = await body(request)
    p = hub.plugins.get(str(b.get("id", "")))
    if p is None or b.get("name") not in {a["name"] for a in p.actions}:
        raise web.HTTPNotFound(text=T("Okänd modul"))
    try:
        r = await p.action(b["name"])
    except RuntimeError as e:
        raise web.HTTPBadGateway(text=str(e))
    return ok(**(r or {}))


@routes.post("/api/plugins/catalogue")
async def plugins_catalogue(request):
    """What the project's catalogue has: for each module, whether this device has it and
    whether the catalogue's is newer. Asks the repository; only when the user does."""
    try:
        async with ClientSession(timeout=ClientTimeout(total=20)) as http:
            mods = await fetch_catalogue(http)
    except (OSError, ValueError, asyncio.TimeoutError) as e:
        raise web.HTTPBadGateway(text=T("Katalogen kunde inte hämtas: {why}", why=e if isinstance(e, OSError) else type(e).__name__))
    out = []
    for m in mods:
        cls = hub.available_plugins.get(m["id"])
        out.append({k: m.get(k) for k in ("id", "version", "kind", "name", "description")}
                   | {"installed": cls.module_version if cls else None,
                      "newer": cls is None or pluginlib._vkey(m["version"]) > pluginlib._vkey(cls.module_version)})
    return ok(modules=out)


@routes.post("/api/plugins/install")
async def plugins_install(request):
    """Install or update a module: from the project's catalogue ({"id": ...}), or one that
    is published elsewhere, from the address of its module.json ({"url": ...}). A module
    runs inside the panel as root: the page says so before this."""
    b = await body(request)
    if overlay_active():
        raise web.HTTPConflict(text=T("Stäng av skrivskyddet först. Med skrivskyddat system försvinner installationen vid omstart."))
    global _ext_changed
    if "id" in b:
        try:
            async with ClientSession(timeout=ClientTimeout(total=60)) as http:
                m = next((x for x in await fetch_catalogue(http) if x["id"] == b["id"]), None)
                if m is None:
                    raise web.HTTPNotFound(text=T("Modulen finns inte i katalogen."))
                base, files, total = catalogue_url().rsplit("/", 1)[0], [], 0
                for f in m["files"][:200]:
                    path, want = str(f.get("path", "")), str(f.get("sha256", ""))
                    if not re.fullmatch(r"[\w.-]+(/[\w.-]+)*", path) or ".." in path.split("/"):
                        raise ValueError("path")
                    async with http.get(f"{base}/{m['id']}/{path}") as r:
                        data = await r.content.read(extmods.MAX_BYTES + 1) if r.status == 200 else None
                    if data is None or hashlib.sha256(data).hexdigest() != want:
                        raise ValueError(f"{path}: sha256")
                    total += len(data)
                    if total > extmods.MAX_BYTES:
                        raise ValueError("too large")
                    files.append((path, data, bool(f.get("exec"))))
            await asyncio.to_thread(extmods.install_files, m["id"], m["version"], "catalogue", files)
        except ValueError as e:
            raise web.HTTPBadRequest(text=T("Modulen kunde inte installeras: {why}", why=e))
        except (OSError, asyncio.TimeoutError) as e:
            raise web.HTTPBadGateway(text=T("Modulen kunde inte hämtas: {why}", why=type(e).__name__))
        log.info("module %s %s installed from the catalogue", m["id"], m["version"])
        _ext_changed = True
        return ok(id=m["id"], version=m["version"])
    url = str(b.get("url", "")).strip()
    if not url.startswith("https://") or len(url) > 500:
        raise web.HTTPBadRequest(text=T("Ange adressen till modulens module.json (https)."))
    try:
        async with ClientSession(timeout=ClientTimeout(total=60)) as http:
            async with http.get(url) as r:
                m = extmods.check_manifest(json.loads(await r.text()), remote=True)
            async with http.get(m["archive"]) as r:
                data = await r.content.read(extmods.MAX_BYTES + 1)
        await asyncio.to_thread(extmods.unpack, data, m["sha256"], m["id"], url, m["version"])
    except ValueError as e:
        raise web.HTTPBadRequest(text=T("Modulen kunde inte installeras: {why}", why=e))
    except (OSError, asyncio.TimeoutError) as e:
        raise web.HTTPBadGateway(text=T("Modulen kunde inte hämtas: {why}", why=type(e).__name__))
    log.info("module %s %s installed from its publisher", m["id"], m["version"])
    _ext_changed = True
    return ok(id=m["id"], version=m["version"])


@routes.post("/api/plugins/remove")
async def plugins_remove(request):
    pid = str((await body(request)).get("id", ""))
    cls = hub.available_plugins.get(pid)
    if cls is None or cls.external is None:
        raise web.HTTPNotFound(text=T("Okänd modul"))
    hub.settings["plugins"] = [p for p in hub.settings.get("plugins", []) if p != pid]
    await asyncio.get_running_loop().run_in_executor(None, hub.save_settings)
    await asyncio.to_thread(extmods.remove, pid)
    global _ext_changed
    _ext_changed = True
    return ok()


# ---- mscOS itself: newer packages from its releases
MSCOS_UPDATE = os.path.join(collect.LIB, "bin", "mscos-update")


@routes.get("/api/system/mscos-update")
async def mscos_update_check(request):
    rc, out, err = await collect.run([MSCOS_UPDATE, "--check"], timeout=40)
    try:
        d = json.loads(out)
    except ValueError:
        raise web.HTTPBadGateway(text=T("Kunde inte söka efter en ny version av mscOS: {why}", why=(err or out).strip()[-200:] or rc))
    return ok(**d)


@routes.post("/api/system/mscos-update")
async def mscos_update_run(request):
    if overlay_active():
        raise web.HTTPConflict(text=T("Stäng av skrivskyddet först. Med skrivskyddat system försvinner uppdateringar vid omstart."))
    # in a unit of its own: the update restarts the panel, which must not take the update with it
    rc, _, err = await collect.run(["systemd-run", "--collect", "--unit", "mscos-self-update", MSCOS_UPDATE], timeout=20)
    if rc:
        raise web.HTTPConflict(text=T("Uppdateringen kunde inte startas: {why}", why=err.strip()[-200:]))
    return ok()


@routes.post("/api/alerts/test")
async def alerts_test(request):
    if not hub.can_alert():
        raise web.HTTPConflict(text=T("Ingen modul som skickar larm är inställd. Slå på en under Moduler."))
    sent = await hub.alert("test", T("Testlarm från {name}", name=hub.names()["output"]),
                           T("Larmen fungerar. Så här ser det ut när något händer med ljudkedjan."), force=True)
    if not sent:
        raise web.HTTPBadGateway(text=T("Larmet kunde inte skickas. Kontrollera modulens inställningar."))
    return ok()


@routes.post("/api/tests/clear")
async def tests_clear(request):
    for p in hub.plugins.values():
        p.clear_history()
    hub.xruns.clear()
    await asyncio.get_running_loop().run_in_executor(None, hub.save)
    await hub.broadcast({"type": "history_cleared"})
    return ok()


# ---- Login
def _token(request):
    return request.cookies.get(SESSION_COOKIE) or request.cookies.get(OLD_SESSION_COOKIE)


@routes.get("/api/auth")
async def auth_state(request):
    return web.json_response({"configured": AUTH.configured, "logged_in": AUTH.valid(_token(request))})


def _login_response(request, token: str):
    resp = ok(logged_in=True)
    resp.set_cookie(SESSION_COOKIE, token, max_age=90 * 86400, httponly=True, samesite="Strict", path="/",
                    secure=(request.headers.get("X-Forwarded-Proto") if _peer(request) in TRUSTED_PEERS else None
                            or request.scheme) == "https")
    return resp


def _check_new_password(pw: str):
    if len(pw) < 8:
        raise web.HTTPBadRequest(text=T("Lösenordet behöver minst 8 tecken."))


@routes.post("/api/auth/setup")
async def auth_setup(request):
    if AUTH.configured:
        raise web.HTTPConflict(text=T("Ett lösenord finns redan. Logga in."))
    pw = str((await body(request)).get("password", ""))
    _check_new_password(pw)
    await asyncio.get_running_loop().run_in_executor(None, AUTH.set_password, pw)
    return _login_response(request, AUTH.new_session())


LOGIN_LOCK = asyncio.Lock()  # one password check at a time, so parallel guesses cannot outrun the throttle


@routes.post("/api/auth/login")
async def auth_login(request):
    pw = str((await body(request)).get("password", ""))
    async with LOGIN_LOCK:
        if AUTH.throttled():
            raise web.HTTPTooManyRequests(text=T("För många felaktiga försök. Vänta tio minuter."))
        good = await asyncio.get_running_loop().run_in_executor(None, AUTH.verify, pw)
        if not good:
            AUTH.failed()
            await asyncio.sleep(1)
            raise web.HTTPUnauthorized(text=T("Fel lösenord."))
    return _login_response(request, AUTH.new_session())


@routes.post("/api/auth/logout")
async def auth_logout(request):
    AUTH.revoke(_token(request))
    resp = ok(logged_in=False)
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


@routes.post("/api/auth/password")
async def auth_password(request):
    b = await body(request)
    if not await asyncio.get_running_loop().run_in_executor(None, AUTH.verify, str(b.get("current", ""))):
        AUTH.failed()
        raise web.HTTPUnauthorized(text=T("Nuvarande lösenord stämmer inte."))
    pw = str(b.get("password", ""))
    _check_new_password(pw)
    await asyncio.get_running_loop().run_in_executor(None, AUTH.set_password, pw)
    return _login_response(request, AUTH.new_session())


# ---- Arcam
# ---- System
@routes.post("/api/system/reboot")
async def reboot(request):
    asyncio.get_running_loop().call_later(1.0, lambda: subprocess.Popen(["systemctl", "reboot"]))
    hub.save()
    return ok()


@routes.post("/api/system/poweroff")
async def poweroff(request):
    asyncio.get_running_loop().call_later(1.0, lambda: subprocess.Popen(["systemctl", "poweroff"]))
    hub.save()
    return ok()


@routes.post("/api/system/restart-audio")
async def restart_audio(request):
    cur = hub.source
    async with hub.source_lock:
        err = await cur.stop() if cur else ""
        if not cur or cur.pipewire:
            rc, _, e = await collect.run(["systemctl", "--user", "restart", "pipewire.service", "wireplumber.service"],
                                         as_user=True, timeout=60)
            err += e.strip() if rc else ""
        if cur:
            err += await cur.start()
    if err:
        raise web.HTTPInternalServerError(text=T("Omstarten misslyckades: {error}", error=err[:200]))
    return ok()


USB_LOCK = asyncio.Lock()


@routes.post("/api/system/usb-reconnect")
async def usb_reconnect(request):
    if USB_LOCK.locked():
        raise web.HTTPConflict(text=T("DAC:n ansluts redan på nytt"))
    async with USB_LOCK:
        r = await collect.usb_reconnect()
    hub._dac_cache = (0.0, None)
    await hub.refresh_status()
    return ok(**r)


@routes.get("/api/system/updates")
async def updates(request):
    rc, _, _ = await collect.run("apt-get update -qq", timeout=180)
    pk = await collect.apt_upgradable()
    return ok(packages=pk, refreshed=rc == 0)


@routes.post("/api/system/upgrade")
async def upgrade(request):
    if overlay_active():
        raise web.HTTPConflict(text=T("Stäng av skrivskyddet först. Med skrivskyddat system försvinner uppdateringar vid omstart."))
    await hub.run_job("system-upgrade", "apt-get update && apt-get -y -o Dpkg::Options::=--force-confold full-upgrade && apt-get -y autoremove")
    return ok()


@routes.post("/api/system/overlay")
async def overlay(request):
    enabled = bool((await body(request)).get("enabled"))
    if enabled:
        cmd = ("set -e; dpkg -s overlayroot >/dev/null 2>&1 || apt-get install -y overlayroot; "
               "sed -i 's/^overlayroot=.*/overlayroot=\"tmpfs:recurse=0\"/' /etc/overlayroot.conf; "
               "grep -q '^overlayroot=\"tmpfs' /etc/overlayroot.conf; "
               "update-initramfs -u; echo '" + T("Klart. Starta om enheten för att aktivera skrivskyddet.") + "'")
    else:
        cmd = ("set -e; if [ -d /media/root-ro/etc ]; then mount -o remount,rw /media/root-ro; "
               "sed -i 's/^overlayroot=.*/overlayroot=\"\"/' /media/root-ro/etc/overlayroot.conf; "
               "mount -o remount,ro /media/root-ro; else sed -i 's/^overlayroot=.*/overlayroot=\"\"/' /etc/overlayroot.conf; fi; "
               "echo '" + T("Klart. Starta om enheten för att stänga av skrivskyddet.") + "'")
    await hub.run_job("overlay", cmd)
    return ok()


@routes.get("/api/logs")
async def get_logs(request):
    q = request.query
    cur = hub.source
    text = await collect.logs(q.get("source", "player"), int(q.get("lines", 200)), q.get("errors") == "1",
                              (cur.user_units, cur.system_units) if cur else ((), ()))
    return web.Response(text=text, content_type="text/plain", charset="utf-8")


# ---- static PWA
@routes.get("/")
async def index(request):
    return web.FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


PROTECTED = ("/api/system/", "/api/logs", "/api/soloist/apikey", "/api/soloist/normalization",
             "/api/soloist/restart", "/api/soloist/update", "/api/settings", "/api/alerts/", "/api/tests/",
             "/api/auth/password",
             "/api/source",  # stops/starts the audio stacks as root (also /api/sources/install)
             "/api/plugins/")  # turns modules on and off (restarts the panel)


def _peer(request) -> str:
    return (request.transport.get_extra_info("peername") or ("",))[0] if request.transport else ""


ALLOWED_HOSTS_EXTRA = {h.lower() for h in env("ALLOWED_HOSTS").split()}
_own_names: tuple[float, set[str]] = (0.0, set())


async def own_names() -> set[str]:
    """Names and addresses this device answers to (local mode): hostname, hostname.local,
    its IPs, localhost, plus MSCOS_ALLOWED_HOSTS. Refreshed every 30 s (DHCP)."""
    global _own_names
    if time.time() - _own_names[0] < 30:
        return _own_names[1]
    host = socket.gethostname().lower()
    names = {host, f"{host}.local", "localhost", "127.0.0.1", "::1"} | ALLOWED_HOSTS_EXTRA
    rc, out, _ = await collect.run(["ip", "-j", "addr", "show"], timeout=5)
    try:
        names |= {a["local"].lower() for i in json.loads(out) for a in i.get("addr_info", []) if a.get("local")}
    except (ValueError, KeyError, TypeError):
        pass
    _own_names = (time.time(), names)
    return names


def _host_only(host: str) -> str:
    """'name:80' -> 'name', '[fe80::1]:80' -> 'fe80::1'."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:].split("]")[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


@web.middleware
async def request_language(request, handler):
    """Texts in the answer follow the panel's language (its cookie) or the browser's."""
    token = i18n.use(i18n.from_request(request))
    try:
        return await handler(request)
    finally:
        i18n._lang.reset(token)


@web.middleware
async def only_via_proxy(request, handler):
    if not PUBLIC_URL:
        # Local mode. DNS rebinding defence: a page on another site can point its own name at
        # this device; then Origin and Host agree, so the Host itself must be one of ours.
        if _host_only(request.host or "") not in await own_names():
            raise web.HTTPMisdirectedRequest(text=T("Okänt värdnamn. Öppna panelen med enhetens namn eller IP-adress."))
        return await handler(request)
    if _peer(request) not in TRUSTED_PEERS:
        if request.path.startswith("/api/"):
            raise web.HTTPForbidden(text=T("Använd {url}", url=PUBLIC_URL))
        raise web.HTTPPermanentRedirect(PUBLIC_URL + request.path_qs)
    return await handler(request)


@web.middleware
async def same_origin_json(request, handler):
    """CSRF guard for every state-changing request, protected or not (amplifier volume,
    playback, Arcam). A cross-site page can only send "simple" requests without a CORS
    preflight, which cannot be application/json; browsers also send Origin on POST."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        # proxy mode: the public name; local mode: the Host (already checked to be ours) as
        # plain http, the only scheme the panel serves itself
        own = PUBLIC_URL or f"http://{request.host}"
        if origin and origin.rstrip("/").lower() != own.lower():
            raise web.HTTPForbidden(text=T("Begäran kommer från en annan webbplats."))
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text=T("Begäran måste vara JSON."))
    return await handler(request)


@web.middleware
async def require_login(request, handler):
    p = request.path
    guarded = any(p.startswith(x) for x in PROTECTED) and not (p == "/api/settings" and request.method == "GET")
    if guarded and not AUTH.valid(_token(request)):
        raise web.HTTPUnauthorized(text=T("Logga in under Profil för att göra det här."))
    return await handler(request)


@web.middleware
async def cache_headers(request, handler):
    resp = await handler(request)
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif not request.path.startswith("/fonts/"):
        # Always revalidate the app shell so updates show up immediately (tiny files, LAN).
        resp.headers["Cache-Control"] = "no-cache"
    return resp


async def on_startup(app):
    collect.apply_leds(hub.settings.get("leds", True))
    if "network_name" not in hub.settings:  # from before the name on the network was a choice: keep Soloist's
        sp = hub.plugins.get("spotify")
        cur, n = (sp.current_name() if sp else ""), hub.names()
        if cur and cur == n["output"]:
            hub.settings["network_name"] = "output"
        elif cur and cur != n["streamer"]:
            hub.settings.update(network_name="custom", network_name_custom=cur)
        else:
            hub.settings["network_name"] = "streamer"
        hub.save_settings()
    async def restore():
        """Bring up the saved source. Spotify normally starts with the soloist session by itself;
        start it only if it is not running (e.g. Roon mode was ended by editing settings)."""
        await asyncio.sleep(5)  # let the user session come up first
        cur = hub.source
        others = [s for s in hub.sources.values() if s is not cur and await s.active()]
        if cur is None or (not others and await cur.active()) or not await cur.installed():
            return
        async with hub.source_lock:
            try:
                r = await hub.switch_source(cur.id)
                log.info("source at startup: %s", r)
            except RuntimeError as e:
                log.warning("source at startup: %s", e)
    asyncio.ensure_future(restore())
    app["tasks"] = [asyncio.create_task(c) for p in hub.plugins.values() for c in p.tasks()]
    core = [hub.sample_loop()]
    if any(getattr(type(p), "has_slider", False) for p in hub.sources.values()):  # the volume bridge
        core += [hub.vol.amp_loop()]
    app["tasks"] += [asyncio.create_task(c) for c in core]


async def on_cleanup(app):
    for t in app["tasks"]:
        t.cancel()
    for p in hub.plugins.values():
        http = getattr(p, "http", None)
        if http:
            await http.close()
    hub.save()


def make_app(background: bool = True) -> web.Application:
    """The panel as an aiohttp application. background=False leaves out the sampling loops and
    the modules' tasks (for tests of the HTTP API)."""
    i18n.setup(STATIC, lambda: hub.settings.get("language", "en"))
    app = web.Application(middlewares=[request_language, only_via_proxy, same_origin_json, require_login, cache_headers])
    for p in hub.plugins.values():
        p.routes(routes)
    app.add_routes(routes)
    app.router.add_static("/", STATIC, show_index=False)
    if background:
        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    web.run_app(make_app(), host="0.0.0.0", port=int(os.environ.get("PORT", 80)), access_log=None)


if __name__ == "__main__":
    main()
