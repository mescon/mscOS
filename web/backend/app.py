#!/usr/bin/env python3
"""mscOS control panel: sources, audio chain, modules and system admin.

aiohttp server on :80. Browsers get one WebSocket (/api/ws) that carries status
snapshots, Soloist playback events and job output; actions are plain POSTs.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
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
import tempfile
import time

from aiohttp import WSMsgType, web
import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bitcheck  # noqa: E402
from auth import Auth  # noqa: E402
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
SOLOIST_WS = "ws://127.0.0.1:9090"
RO_LOWER = "/media/root-ro"
# Site settings (/etc/mscos/site.env). Default: local mode, the panel answers everyone on the
# LAN at http://<hostname>.local. With MSCOS_PUBLIC_URL set (an HTTPS reverse proxy in front),
# only the proxy (MSCOS_TRUSTED_PEERS) and the device itself may talk to the panel directly;
# everyone else is sent to the HTTPS name, so the password never crosses the LAN in clear text.
PUBLIC_URL = env("PUBLIC_URL").strip().rstrip("/")
TRUSTED_PEERS = set(env("TRUSTED_PEERS").split()) | {"127.0.0.1", "::1"}
SESSION_COOKIE = "mscos_session"
OLD_SESSION_COOKIE = "sotm_session"  # still accepted, so nobody is logged out by the rename
AUTO_VERIFY_EVERY = 3600
AUTO_VERIFY_SECS = 20

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
        self.playback: dict = {}
        self.auth: dict = {}
        self.queue: dict = {}
        self.soloist_ws = None
        self.status: dict = {}
        self.xruns = collections.deque(maxlen=24 * 60 * 2)  # 30 s samples, 24 h
        self.last_err: dict = {}
        self.last_test: dict | None = None
        self.jobs: dict[str, dict] = {}
        self.tests = collections.deque(maxlen=200)  # verification log, manual and automatic
        self.settings: dict = {"auto_verify": True, "alerts": True, "leds": True, "net_speed": 100,
                               "spotify_vol_to_amp": True, "soloist_normalization": False,
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
        self.track_quality: dict = {}  # measured format of the current track
        self._norm_failed: tuple | None = None   # last set of accounts a write did not fix
        self._accounts: set[str] | None = None  # Spotify accounts seen (guests add new ones)
        self.cmd_lock = asyncio.Lock()  # one Soloist command at a time: its errors carry no request id
        self._cmd_reply: asyncio.Future | None = None

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
            if os.path.exists(collect.SOLOIST_UNIT) and "spotify" not in en:
                en.append("spotify")
                self.settings.setdefault("source", "spotify")
                self.settings["source"] = self.settings["source"] or "spotify"
            if os.path.exists("/opt/RoonBridge/start.sh") and "roon" not in en:
                en.append("roon")
            self.settings["plugins_v"] = 2
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
        if src != "spotify":
            self.playback, self.queue = {}, {}
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

    @property
    def playing_here(self) -> bool:
        return self._was_playing

    @property
    def idle_since(self) -> float:
        return self._idle_since

    num = staticmethod(lambda *a, **k: num(*a, **k))
    body = staticmethod(lambda request: body(request))
    ok = staticmethod(lambda **kw: ok(**kw))

    def play_started(self, force: bool = False):
        for p in self.plugins.values():
            asyncio.ensure_future(p.on_play_start(force))

    async def pause_if_playing(self) -> bool:
        if self.soloist_ws and self.auth.get("is_active") and self.playback.get("status") == "playing":
            await self.soloist_cmd({"command": "pause"})
            return True
        return False

    def names(self) -> dict:
        """What to call things. Each is a setting; empty means the detected default:
        output    what this device plays into: the amplifier module's model, else the DAC
        streamer  this device: the board name, PC model or device tree model"""
        s, a = self.settings, self.amp
        det_out = (getattr(a, "model", "") if a else "") or collect.detect_dac() or "DAC"
        det_str = collect.detect_streamer()
        return {"output": s.get("output_name") or det_out, "streamer": s.get("streamer_name") or det_str,
                "detected": {"output": det_out, "streamer": det_str}}

    def features(self) -> dict:
        """What this installation has, so the panel shows only that."""
        f = {"amp": "none", "arcam": False, "plugins": list(self.plugins),
             "sources": [{"id": i, "label": p.label, "name": p.name, "protocol": p.protocol, "pipewire": p.pipewire,
                          "hint": p.hint, "terms": p.terms, "terms_url": p.terms_url} for i, p in self.sources.items()]}
        for p in self.plugins.values():
            f.update(p.features())
        return f

    def summary(self) -> dict:
        """One cheap snapshot (in-memory state only, plus the DAC's /proc state) for automations."""
        d = collect.dac_info()
        src = self.settings.get("source", "")
        solo = _ha_soloist()
        # PipeWire keeps the DAC open (ALSA RUNNING) also in silence, so with a source that
        # plays through it "running" is what the source says; Roon Bridge and NAA open the
        # DAC only while they play.
        if src == "spotify":
            running = solo["status"] == "playing" if solo["reachable"] else None
        elif self.source and self.source.pipewire:
            running = self._was_playing
        else:
            running = d.get("state") == "RUNNING"
        out = {"ts": time.time(), "source": src, "soloist": solo}
        for p in self.plugins.values():
            out.update(p.summary())
        out["dac"] = {"present": bool(d.get("present")), "running": running if d.get("present") else None}
        return out

    # ---- persistence of small stats (rarely written, SD-friendly)
    def _load(self):
        try:
            with open(os.path.join(STATE_DIR, "stats.json")) as f:
                d = json.load(f)
            self.xruns.extend(d.get("xruns", []))
            self.last_test = d.get("last_test")
            self.tests.extend(d.get("tests", []))
            lt = self.last_test
            if not self.tests and lt and lt.get("t"):  # seed the log with the pre-log result
                self.tests.append({k: lt.get(k) for k in ("t", "verdict", "bits", "gain_db", "seconds", "track")}
                                  | {"auto": False, "gaps": len(lt.get("gaps") or [])})
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
            json.dump({"xruns": list(self.xruns), "last_test": self.last_test, "tests": list(self.tests),
                       "alert_sent": self.alert_sent, "clock": list(self.clock), "supply": list(self.supply)}, f)
        os.replace(tmp, os.path.join(STATE_DIR, "stats.json"))

    def save_settings(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        p = os.path.join(STATE_DIR, "settings.json")
        with open(p + ".tmp", "w") as f:
            json.dump(self.settings, f)
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
    async def soloist_loop(self):
        while True:
            try:
                async with websockets.connect(SOLOIST_WS, max_size=None, open_timeout=5) as ws:
                    self.soloist_ws = ws
                    self._was_active = None  # the first auth_state only records the current state
                    self._playing_known = False  # likewise the first playback_state
                    await ws.send(json.dumps({"type": "command", "command": "get_auth_state"}))
                    async for raw in ws:
                        ev = json.loads(raw)
                        if ev.get("type") in ("error", "command_result") and self._cmd_reply and not self._cmd_reply.done():
                            self._cmd_reply.set_result(ev)
                        if ev.get("type") in ("device_changed", "playback_changed"):
                            log.info("soloist %s: active=%s status=%s", ev["type"], ev.get("is_active", self.auth.get("is_active")),
                                     ev.get("status", self.playback.get("status")))
                        self._apply_soloist(ev)
                        self._on_playback_change()
                        await self.broadcast({"type": "soloist", "event": ev})
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError):
                pass
            self.soloist_ws = None
            self.auth = {}
            await self.broadcast({"type": "soloist", "event": {"type": "disconnected"}})
            await asyncio.sleep(3)

    def _apply_soloist(self, ev: dict):
        t = ev.get("type")
        if t == "auth_state":
            self.auth = ev
        elif t == "playback_state":
            self.playback = ev
        elif t == "track_changed":
            asyncio.ensure_future(self.measure_track_quality())
            self.playback["item"] = ev.get("item")
        elif t == "playback_changed":
            self.playback["status"] = ev.get("status")
        elif t == "volume_changed":
            self.playback["volume"] = ev.get("volume")
            self.vol.on_volume_changed(ev.get("volume"))
        elif t == "position_sync":
            self.playback["position"] = ev.get("position")
        elif t == "options_changed":
            self.playback["options"] = ev.get("options")
        elif t == "queue_changed":
            self.queue = ev
        elif t == "device_changed":
            self.auth = {**self.auth, "is_active": ev.get("is_active"), "device_name": ev.get("device_name")}

    async def soloist_cmd(self, msg: dict, wait: float = 0.0) -> dict | None:
        """Send a command. With wait, return Soloist's error or command_result if one arrives
        within that time (None: accepted without complaint)."""
        if not self.soloist_ws:
            raise web.HTTPServiceUnavailable(text=T("Soloist svarar inte"))
        if not wait:
            await self.soloist_ws.send(json.dumps({"type": "command", **msg}))
            return None
        async with self.cmd_lock:
            self._cmd_reply = asyncio.get_running_loop().create_future()
            try:
                await self.soloist_ws.send(json.dumps({"type": "command", **msg}))
                return await asyncio.wait_for(asyncio.shield(self._cmd_reply), wait)
            except asyncio.TimeoutError:
                return None
            finally:
                self._cmd_reply = None

    async def wait_active(self, secs: float = 8.0) -> bool:
        end = time.time() + secs
        while time.time() < end:
            if self.auth.get("is_active"):
                return True
            await asyncio.sleep(0.25)
        return bool(self.auth.get("is_active"))

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
                await self.maybe_auto_verify()
                cur = self.source
                if cur and not self.soloist:
                    p = await cur.playing()
                    self._set_playing(collect.dac_info().get("state") == "RUNNING" if p is None else p)
                await self.keep_normalization()
                if n % 720 == 0 and n:  # hourly: keep SD-card writes rare
                    await asyncio.get_running_loop().run_in_executor(None, self.save)
            except Exception:  # keep sampling no matter what
                log.exception("sampling failed")
            n += 1
            await asyncio.sleep(5)

    async def keep_normalization(self):
        """A guest's new account starts with Spotify's normalization on (-5 dB, not
        bit-perfect). Fix every account's prefs, but only after 10 quiet minutes: Soloist
        reads them at start, and a restart would cut off someone listening."""
        if not self.soloist:
            return
        accounts = set(collect.account_dirs())
        if self._accounts is None:  # first look: what is there now is not "new"
            self._accounts = accounts
        new = accounts - self._accounts
        self._accounts |= accounts
        want = bool(self.settings.get("soloist_normalization", False))
        wrong = collect.normalization_mismatch(want)
        if not wrong:
            self._norm_failed = None
            return
        # never restart Soloist again for the same accounts after a write that did not stick
        # (one attempt per set of accounts; a new account, or a manual change, retries)
        key = (want, tuple(wrong))
        if key == self._norm_failed:
            return
        if new and any(w.startswith(tuple(new)) for w in wrong):
            # A guest's first connection: fix it now and bring their playback straight back,
            # so it is bit-perfect from the first song (a few seconds' pause, once).
            playing, active = self._was_playing, bool(self.auth.get("is_active"))
            log.info("new Spotify account: normalization %s, restarting Soloist", "on" if want else "off")
            async with self.source_lock:
                ok_ = await apply_normalization(want)
            if not ok_:
                self._norm_failed = (want, tuple(collect.normalization_mismatch(want)))
                log.warning("normalization could not be written for %s", self._norm_failed[1])
            asyncio.ensure_future(self._resume_after_restart(active, playing))
            return
        if self._was_playing or time.time() - self._idle_since < 600:
            return  # an existing account: only when nobody is listening
        log.info("normalization %s for %d account(s); restarting Soloist", "on" if want else "off", len(wrong))
        async with self.source_lock:
            ok_ = await apply_normalization(want)
        if not ok_:
            self._norm_failed = (want, tuple(collect.normalization_mismatch(want)))
            log.warning("normalization could not be written for %s", self._norm_failed[1])

    async def _resume_after_restart(self, active: bool, playing: bool):
        """After a Soloist restart: take the device back (activate) and resume, as before."""
        if not active:
            return
        for _ in range(40):  # Soloist logs in again with the stored credentials
            await asyncio.sleep(0.5)
            if self.soloist_ws and self.auth.get("logged_in"):
                break
        else:
            return
        await asyncio.sleep(1)
        try:
            await self.soloist_cmd({"command": "activate"}, wait=2)
            if playing:
                await self.wait_active(8)
                await self.soloist_cmd({"command": "play"}, wait=2)
            log.info("Soloist back: active%s", ", playing" if playing else "")
        except Exception as e:  # noqa: BLE001
            log.warning("resume after restart: %s", e)

    async def refresh_status(self):
        pw = await collect.pipewire_info()
        solo = await collect.soloist_info() if "spotify" in self.plugins else {}
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
            "soloist": solo,
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
    async def alert(self, kind: str, title: str, message: str, cooldown: float = 6 * 3600, force=False) -> bool:
        if not self.settings.get("alerts") and not force:
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
        solo = st.get("soloist") or {}
        if self._held("dac", not dac.get("present"), 60):
            await self.alert("dac", T("DAC:en hittas inte"),
                             T("DAC:en syns inte längre på USB. Kontrollera kabeln och att DAC:en är på."))
        if self._held("soloist", self.soloist and collect.api_key_info()["set"]
                      and solo.get("service") not in (None, "active"), 120):
            await self.alert("soloist", T("Soloist har stannat"),
                             T("Spotify-tjänsten är {state}. Starta om den under System i panelen.", state=solo.get("service")))
        days = solo.get("days_left")
        if days is not None and days <= 14:
            await self.alert("expiry", T("Soloist-bygget går snart ut"),
                             T("{days} dagar kvar. Nattens uppdatering har inte hittat en nyare version; prova Sök ny version i panelen.", days=days),
                             cooldown=24 * 3600)

    # ---- what the current track really is (Soloist does not say: measure it)
    async def measure_track_quality(self):
        """6 s of what goes to the DAC, a few seconds into each track: on the 16-bit lattice
        means a 16-bit file, content below it 24-bit, no lattice at all lossy (or processed).
        Read-only tap on the sink monitor; one at a time; skipped while a verification runs."""
        if getattr(self, "_quality_busy", False):
            return
        uri = (self.playback.get("item") or {}).get("uri")
        if not uri or not self.soloist:
            log.info("track quality: no track")
            return
        self._quality_busy = True
        try:
            await asyncio.sleep(8)  # past the track start (gapless join, fades)
            if (self.playback.get("item") or {}).get("uri") != uri or self.playback.get("status") != "playing" or not self.auth.get("is_active") \
                    or self.jobs.get("audio-test", {}).get("running"):
                log.info("track quality: skipped (track changed, paused, played elsewhere or a verification running)")
                return
            sink = (self.status.get("pipewire") or {}).get("sink") or {}
            if not sink.get("name"):
                return
            uid = await collect.soloist_uid()
            fd, wav = tempfile.mkstemp(suffix=".wav", dir="/tmp")
            os.close(fd)
            os.chown(wav, uid, -1)
            try:
                await collect.run(["timeout", "7", "pw-record", "-P", "{ stream.capture.sink=true }",
                                   "--target", sink["name"], "--rate", "44100", "--channels", "2",
                                   "--format", "f32", wav], as_user=True, timeout=20)

                def analyse():
                    x = bitcheck.read_f32_wav(wav)
                    return bitcheck.lattice(x[22050:]) if len(x) > 3 * 44100 else {"verdict": "too_short"}
                r = await asyncio.get_running_loop().run_in_executor(None, analyse)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(wav)
            if (self.playback.get("item") or {}).get("uri") != uri:
                return  # the track changed while measuring
            v = r.get("verdict")
            if v in ("silent", "too_short"):
                log.info("track quality: %s, not recorded", v)
                return
            self.track_quality = {"uri": uri, "t": int(time.time()), "verdict": v, "bits": r.get("bits"),
                                  "gain_db": r.get("gain_db"), "rate": 44100}
            log.info("track quality: %s %s bit", v, r.get("bits"))
            await self.broadcast({"type": "quality", "quality": self.track_quality})
        except Exception as e:  # noqa: BLE001 - never disturb playback over this
            log.warning("track quality: %s", e)
        finally:
            self._quality_busy = False

    # ---- verification (manual or automatic)
    async def maybe_auto_verify(self):
        if not self.settings.get("auto_verify") or self.jobs.get("audio-test", {}).get("running"):
            return
        last_auto = max((t["t"] for t in self.tests if t.get("auto")), default=0)
        if time.time() - last_auto < AUTO_VERIFY_EVERY:
            return
        p = self.playback
        if p.get("status") != "playing" or (self.status.get("dac") or {}).get("state") != "RUNNING":
            return
        pos = p.get("position") or {}
        dur = (((p.get("item") or {}).get("decorations") or {}).get("playback") or {}).get("duration_ms") or 0
        at = pos.get("position_ms", 0) + (time.time() * 1000 - pos.get("timestamp_ms", time.time() * 1000))
        # Mid-track only: a track change inside the window would show up as a gap.
        if at < 15000 or dur - at < (AUTO_VERIFY_SECS + 15) * 1000:
            return
        await self.run_audio_test(AUTO_VERIFY_SECS, auto=True)

    async def run_audio_test(self, secs: int, auto: bool = False):
        sink = (self.status.get("pipewire") or {}).get("sink") or {}
        if not sink.get("name"):
            raise web.HTTPConflict(text=T("Ingen USB-DAC hittades i PipeWire"))
        job = {"name": "audio-test", "running": True, "output": "", "rc": None, "started": time.time(),
               "seconds": secs, "auto": auto}
        self.jobs["audio-test"] = job
        await self.broadcast({"type": "job", "name": "audio-test", "started": True, "auto": auto, "seconds": secs})

        async def go():
            uid = await collect.soloist_uid()
            fd, wav = tempfile.mkstemp(suffix=".wav", dir="/tmp")
            os.close(fd)
            os.chown(wav, uid, -1)
            try:
                await collect.run(["timeout", str(secs + 1), "pw-record", "-P", "{ stream.capture.sink=true }",
                                   "--target", sink["name"], "--rate", "44100", "--channels", "2",
                                   "--format", "f32", wav], as_user=True, timeout=secs + 15)

                def analyse():
                    x = bitcheck.read_f32_wav(wav)
                    r = bitcheck.lattice(x[44100:]) if len(x) > 2 * 44100 else {"verdict": "too_short"}
                    r["gaps"] = bitcheck.find_gaps(x)
                    r["seconds"] = round(len(x) / 44100, 1)
                    r["rms"] = round(float((x.astype("float64") ** 2).mean() ** 0.5), 4) if len(x) else 0
                    return r

                res = await asyncio.get_running_loop().run_in_executor(None, analyse)
                res["t"] = int(time.time())
                res["auto"] = auto
                item = self.playback.get("item") or {}
                deco = item.get("decorations") or {}
                res["track"] = deco.get("identity", {}).get("name")
                silent = res.get("verdict") in ("silent", "too_short") or res.get("rms", 1) < 1e-4
                if not silent:  # a silent capture says nothing about the chain
                    self.last_test = res
                    self.tests.append({k: res.get(k) for k in ("t", "auto", "verdict", "bits", "gain_db", "seconds", "track")}
                                      | {"gaps": len(res.get("gaps") or [])})
                    if res.get("verdict") != "bitperfect" or res.get("gaps"):
                        what = T("avbrott") if res.get("verdict") == "bitperfect" else T("resultat: {verdict}", verdict=res.get("verdict"))
                        await self.alert("verify", T("Verifieringen hittade avvikelser"),
                                         T("Automatisk verifiering kl {time}: {what}." if auto else "Manuell verifiering kl {time}: {what}.",
                                           time=time.strftime("%H:%M"), what=what))
                job["result"] = res
                job["rc"] = 0
            except Exception as e:  # report, never crash the server
                job["rc"] = 1
                job["output"] = str(e)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(wav)
                job["running"] = False
                await self.broadcast({"type": "job", "name": "audio-test", "done": True, "rc": job["rc"], "auto": auto,
                                      "result": job.get("result"), "error": job["output"],
                                      "tests": list(self.tests)[-30:]})
                await asyncio.get_running_loop().run_in_executor(None, self.save)

        asyncio.create_task(go())
        return job

    # ---- playback on this device: modules react (the amplifier wakes and selects its input)
    def _on_playback_change(self):
        if not self.soloist:
            return
        # Choosing "Mark Levinson 585.5" in the Spotify app already wakes the amplifier and
        # selects its input, before anything plays (the module has a 90 s guard, so play right after
        # does not send it twice).
        if "is_active" not in self.auth:
            return
        active = bool(self.auth.get("is_active"))
        prev = getattr(self, "_was_active", None)  # None: first report after (re)connecting
        if active and prev is False:
            self.play_started()
        if active and prev is not True:
            self.vol.on_active()  # slider from the amplifier, after Spotify restored its own
        self._was_active = active
        # Soloist mirrors the account's playback: "playing" also arrives while another device
        # (the phone) plays. Only playback on this device counts as a start.
        playing = active and self.playback.get("status") == "playing"
        if not getattr(self, "_playing_known", True) and "status" in self.playback:
            # first report after (re)connecting: music already playing is not a new start,
            # so a panel restart never wakes the amplifier
            self._playing_known = True
            self._was_playing = playing
            if playing:  # already playing when we (re)connected: still measure this track
                asyncio.ensure_future(self.measure_track_quality())
            if not playing:
                self._idle_since = time.time()
            return
        self._set_playing(playing)

    def _set_playing(self, playing: bool):
        if playing and not self._was_playing:
            asyncio.ensure_future(self.measure_track_quality())
        # A real start, not a track change: Soloist pauses for a moment between tracks, and
        # re-selecting the input then makes the amplifier relock (audible glitch).
        if playing and not self._was_playing and time.time() - self._idle_since > 30:
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
        "type": "hello", "status": hub.status, "playback": hub.playback, "auth": hub.auth,
        "queue": hub.queue, "xruns": list(hub.xruns)[-2880:], "last_test": hub.last_test,
        "features": hub.features(), "names": hub.names(), **{k: v for p in hub.plugins.values() for k, v in p.hello().items()}, "quality": hub.track_quality,
        "now": hub.source.now_playing() if hub.source else None,
        "tests": list(hub.tests)[-30:], "settings": {**hub.settings, "alerts_configured": bool(hub.features().get("ha_alerts"))},
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
SPOTIFY_URI = re.compile(r"spotify:(track|album|playlist|artist|episode|show):[A-Za-z0-9]{22}")


@routes.post("/api/soloist/command")
async def soloist_command(request):
    b = await body(request)
    c = b.get("command")
    allowed = {"play", "pause", "skip_next", "skip_prev", "seek", "set_volume", "set_shuffle",
               "set_repeat_context", "set_repeat_track", "get_state", "get_queue", "activate",
               "deactivate", "add_to_queue"}
    if c not in allowed:
        raise web.HTTPBadRequest(text=T("okänt kommando"))
    uri = b.get("uri")
    if c == "add_to_queue" or uri is not None:
        m = SPOTIFY_URI.fullmatch(uri) if isinstance(uri, str) else None
        if not m:
            raise web.HTTPBadRequest(text=T("Ogiltig Spotify-adress. Ange till exempel spotify:album:<22 tecken>."))
        if c == "add_to_queue" and m.group(1) != "track":
            raise web.HTTPBadRequest(text=T("Bara enskilda spår kan läggas i kön (spotify:track:…)."))
    msg = {k: v for k, v in b.items() if k in ("command", "position_ms", "volume", "enabled", "uri", "limit")}
    if not (c == "play" and uri) and c not in ("add_to_queue", "deactivate", "activate"):
        await hub.soloist_cmd(msg)  # the panel's own buttons: fire and forget, as before
        return ok()
    # From Home Assistant (voice assistants): say what actually happened.
    if not hub.soloist:
        raise web.HTTPConflict(text=T("Källan är inte Spotify Lossless"))
    if not hub.soloist_ws:
        raise web.HTTPServiceUnavailable(text=T("Soloist svarar inte"))
    if not hub.auth.get("logged_in"):
        raise web.HTTPConflict(text=T("Soloist är inte inloggad på Spotify. Välj enheten i Spotify-appen en gång."))
    if c == "play":
        if not hub.auth.get("is_active"):
            r = await hub.soloist_cmd({"command": "activate"}, wait=1.5)
            if r and r.get("type") == "error":
                raise web.HTTPBadGateway(text=f"Soloist: {r.get('message')}")
            if not await hub.wait_active():
                raise web.HTTPGatewayTimeout(text=T("Soloist blev inte aktiv på Spotify"))
        r = await hub.soloist_cmd(msg, wait=2.0)
        if not (r and r.get("type") == "error"):
            hub.play_started(force=True)  # wake the amplifier, select this device's input
    else:
        r = await hub.soloist_cmd(msg, wait=2.0)
    if r and r.get("type") == "error":
        raise web.HTTPBadGateway(text=f"Soloist: {r.get('message')}")
    return ok(result=r)


def _ha_soloist() -> dict:
    connected = hub.soloist_ws is not None
    a, p = hub.auth, hub.playback
    it = p.get("item") or {}
    deco = it.get("decorations") or {}
    ctx = p.get("context") or {}
    name = lambda e: (((e or {}).get("decorations") or {}).get("identity") or {}).get("name")  # noqa: E731

    def cover(e):
        c = (((e or {}).get("decorations") or {}).get("visual_identity") or {}).get("cover") or []
        best = next((x for x in c if x.get("size") == "large"), None) or (c[-1] if c else {})
        return best.get("url")

    active = bool(a.get("is_active")) if connected and "is_active" in a else None
    st = p.get("status")
    pos = p.get("position") or {}
    opts = p.get("options") or {}
    return {
        "reachable": connected,
        "logged_in": a.get("logged_in") if connected else None,
        "active": active,
        # what this device does; the account playing on another device is "stopped" here
        "status": None if not connected else (st if active and st in ("playing", "paused") else "stopped"),
        "title": name(it) if it else None,
        "artist": ", ".join(filter(None, (name(c.get("entity")) for c in deco.get("creators") or []))) or None,
        "album": name((deco.get("parent") or {}).get("entity")),
        "uri": it.get("uri"),
        "context_uri": ctx.get("uri"),
        "image_url": cover(it) or cover(ctx),
        "duration_ms": (deco.get("playback") or {}).get("duration_ms"),
        # measured from the samples, for this track only: "flac24", "flac16", "lossy" or None
        "quality": _ha_quality(it.get("uri")),
        "position_ms": pos.get("position_ms"),
        "position_ts": pos["timestamp_ms"] / 1000 if pos.get("timestamp_ms") else None,
        "shuffle": opts.get("shuffle"),
        "repeat": opts.get("repeat"),
        "device_name": a.get("device_name"),
    }


def _ha_quality(uri) -> str | None:
    q = hub.track_quality
    if not uri or q.get("uri") != uri:
        return None
    if q.get("verdict") == "bitperfect":
        return "flac24" if q.get("bits") == 24 else "flac16" if q.get("bits") == 16 else None
    return "lossy" if q.get("verdict") == "lossy_or_processed" else None


@routes.post("/api/soloist/restart")
async def soloist_restart(request):
    rc, out, err = await user_systemctl("restart", "soloist.service")
    return ok(rc=rc, error=err.strip())


@routes.post("/api/soloist/update")
async def soloist_update(request):
    await hub.run_job("soloist-update", ["/usr/local/sbin/soloist-update"])
    return ok()


@routes.post("/api/soloist/name")
async def soloist_name(request):
    name = str((await body(request)).get("name", "")).strip()
    if not re.fullmatch(r"[\w .\-åäöÅÄÖ]{1,40}", name):
        raise web.HTTPBadRequest(text=T("Namnet får vara 1–40 tecken: bokstäver, siffror, mellanslag, punkt och bindestreck."))
    p = collect.SOLOIST_UNIT
    text = collect.read(p)
    text = re.sub(r'--device-name\s+"[^"]*"', f'--device-name "{name}"', text)
    with open(p, "w") as f:
        f.write(text)
    persist(p)
    await user_systemctl("daemon-reload")
    await user_systemctl("restart", "soloist.service")
    return ok(name=name)


@routes.post("/api/soloist/normalization")
async def soloist_normalization(request):
    enabled = bool((await body(request)).get("enabled"))
    if not collect.prefs_paths():
        raise web.HTTPConflict(text=T("Soloist har inte loggat in än. Anslut från Spotify-appen först."))
    hub.settings["soloist_normalization"] = enabled  # kept for every account, guests too
    await asyncio.get_running_loop().run_in_executor(None, hub.save_settings)
    await apply_normalization(enabled)
    return ok(enabled=enabled)


async def apply_normalization(enabled: bool) -> bool:
    """Write the setting into every account's prefs (Soloist reads them at start).
    Returns whether every account now has it."""
    await user_systemctl("stop", "soloist.service")
    for d in collect.account_dirs():
        if await collect.set_normalization_file(d + "prefs", enabled):
            persist(d + "prefs")
    await user_systemctl("start", "soloist.service")
    return not collect.normalization_mismatch(enabled)


@routes.post("/api/soloist/apikey")
async def soloist_apikey(request):
    """Replace the Spotify developer key. Never echoed or logged. Rolled back if Soloist
    does not stay up with the new key."""
    key = str((await body(request)).get("key", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-.:+/=]{16,512}", key):
        raise web.HTTPBadRequest(text=T("Nyckeln ser inte giltig ut: 16–512 tecken, bokstäver, siffror och _-.:+/=."))
    path = collect.SOLOIST_ENV
    old = collect.read(path)
    if f"SOLOIST_API_KEY={key}" in old:
        return ok(changed=False)
    st = os.stat(path) if os.path.exists(path) else None

    def write(text: str):
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        if st:
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
        persist(path)

    new_text = re.sub(r"^SOLOIST_API_KEY=.*$", f"SOLOIST_API_KEY={key}", old, flags=re.M) \
        if re.search(r"^SOLOIST_API_KEY=", old, re.M) else old.rstrip("\n") + f"\nSOLOIST_API_KEY={key}\n"
    write(new_text)
    await user_systemctl("restart", "soloist.service")
    # Baseline after the manual restart (which resets the automatic-restart counter).
    _, before, _ = await user_systemctl("show", "soloist.service", "-p", "NRestarts", "--value")
    await asyncio.sleep(12)  # Soloist restarts after 5 s if the key is rejected
    _, active, _ = await user_systemctl("is-active", "soloist.service")
    _, after, _ = await user_systemctl("show", "soloist.service", "-p", "NRestarts", "--value")
    if active.strip() != "active" or after.strip() != before.strip():
        write(old)
        await user_systemctl("restart", "soloist.service")
        raise web.HTTPUnprocessableEntity(text=T("Soloist startade inte med den nya nyckeln. Den tidigare nyckeln är återställd."))
    await hub.refresh_status()
    return ok(changed=True)


@routes.get("/api/optimizations")
async def get_optimizations(request):
    items = await asyncio.to_thread(collect.optimizations, hub.status, hub.playback, hub.vol.enabled(),
                                    hub.names()["output"])
    return web.json_response({"t": int(time.time()), "items": items})


# ---- Audio test (capture what goes to the DAC, look for dropouts and bit-perfectness)
@routes.post("/api/audio/test")
async def audio_test(request):
    secs = max(5, min(int((await body(request)).get("seconds", 30)), 120))
    if hub.jobs.get("audio-test", {}).get("running"):
        raise web.HTTPConflict(text=T("En verifiering pågår redan"))
    await hub.run_audio_test(secs)
    return ok(seconds=secs)


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
    script = os.path.join(collect.LIB, "sources", p.id, "install.sh")
    if not os.path.exists(script):
        raise web.HTTPNotFound(text=T("Installationsskriptet saknas: {path}", path=script))
    if overlay_active():
        raise web.HTTPConflict(text=T("Stäng av skrivskyddet först. Med skrivskyddat system försvinner installationen vid omstart."))
    await hub.run_job(f"install-{p.id}", [script])
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


# ---- Settings, alerts
@routes.get("/api/settings")
async def get_settings(request):
    return web.json_response({**hub.settings, "alerts_configured": bool(hub.features().get("ha_alerts"))})


@routes.post("/api/settings")
async def set_settings(request):
    b = await body(request)
    upd: dict = {k: bool(b[k]) for k in ("auto_verify", "alerts", "leds", "spotify_vol_to_amp") if k in b}
    for k in ("output_name", "streamer_name"):
        if k in b:
            v = re.sub(r"\s+", " ", str(b[k])).strip()
            if len(v) > 60 or any(ord(c) < 32 for c in v):
                raise web.HTTPBadRequest(text=T("Namnet får vara högst 60 tecken."))
            upd[k] = v  # "" = the detected name
    if "language" in b:  # the system language: alerts and other texts written without a browser
        if b["language"] not in i18n.LANGS:
            raise web.HTTPBadRequest(text=T("Okänt språk"))
        upd["language"] = b["language"]
    for p in hub.plugins.values():
        p.update_settings(b, upd)
    # all valid: apply
    hub.settings.update(upd)
    if "leds" in upd:
        collect.apply_leds(upd["leds"])
    for p in hub.plugins.values():
        if any(k in upd for k in p.defaults):
            await p.settings_applied(upd)
    if b.get("net_speed") in (100, 1000) and b["net_speed"] != hub.settings.get("net_speed"):
        hub.settings["net_speed"] = b["net_speed"]
        # the link renegotiates (~3 s); answer first so the reply is not lost
        asyncio.get_running_loop().call_later(1, lambda: asyncio.ensure_future(collect.apply_net_speed(b["net_speed"])))
    await asyncio.get_running_loop().run_in_executor(None, hub.save_settings)
    await hub.broadcast({"type": "settings", "settings": hub.settings, "features": hub.features(), "names": hub.names()})
    return ok(settings=hub.settings)


# ---- modules: which are installed, which are on
KIND_ORDER = {"source": 0, "amp": 1, "device": 2, "integration": 3}


async def _plugin_list() -> list[dict]:
    out = []
    for pid, cls in sorted(hub.available_plugins.items(), key=lambda kv: (KIND_ORDER.get(kv[1].kind, 9), kv[0])):
        d = {"id": pid, "kind": cls.kind, "name": cls.name, "description": cls.description,
             "requires": list(cls.requires), "enabled": pid in hub.settings.get("plugins", []),
             "running": pid in hub.plugins}
        if cls.kind == "source":  # its player is installed on the device, from its publisher
            d.update(terms=cls.terms, terms_url=cls.terms_url,
                     installed=await hub.plugins[pid].installed() if pid in hub.plugins else None)
        out.append(d)
    return out


@routes.get("/api/plugins")
async def plugins_list(request):
    return web.json_response({"plugins": await _plugin_list(), "restart_needed": _plugins_restart_needed()})


def _plugins_restart_needed() -> bool:
    return set(hub.settings.get("plugins", [])) != set(hub.plugins)


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


@routes.post("/api/alerts/test")
async def alerts_test(request):
    if not any(p.features().get("ha_alerts") for p in hub.plugins.values()):
        raise web.HTTPConflict(text=T("Ingen modul skickar larm (slå på Home Assistant under Moduler)"))
    sent = await hub.alert("test", T("Testlarm från {name}", name=hub.names()["output"]),
                           T("Larmen fungerar. Så här ser det ut när något händer med ljudkedjan."), force=True)
    if not sent:
        raise web.HTTPBadGateway(text=T("Home Assistant tog inte emot larmet"))
    return ok()


@routes.post("/api/tests/clear")
async def tests_clear(request):
    hub.tests.clear()
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


PROTECTED = ("/api/system/", "/api/logs", "/api/soloist/apikey", "/api/soloist/name", "/api/soloist/normalization",
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
        raise web.HTTPUnauthorized(text=T("Logga in under System för att göra det här."))
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
    if "spotify" in hub.plugins:  # the Soloist relay and its volume bridge
        core += [hub.soloist_loop(), hub.vol.amp_loop(), hub.vol.stream_loop()]
    app["tasks"] += [asyncio.create_task(c) for c in core]


async def on_cleanup(app):
    for t in app["tasks"]:
        t.cancel()
    for p in hub.plugins.values():
        http = getattr(p, "http", None)
        if http:
            await http.close()
    hub.save()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    i18n.setup(STATIC, lambda: hub.settings.get("language", "en"))
    app = web.Application(middlewares=[request_language, only_via_proxy, same_origin_json, require_login, cache_headers])
    for p in hub.plugins.values():
        p.routes(routes)
    app.add_routes(routes)
    app.router.add_static("/", STATIC, show_index=False)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", 80)), access_log=None)


if __name__ == "__main__":
    main()
