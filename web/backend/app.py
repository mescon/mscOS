#!/usr/bin/env python3
"""sMS-200Ultra control panel: Soloist, audio chain, Arcam AVR and system admin.

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
import subprocess
import sys
import tempfile
import time

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bitcheck  # noqa: E402
from auth import Auth  # noqa: E402
import collect  # noqa: E402
from arcam import Arcam, ArcamError, SOURCE_RC5  # noqa: E402
from levinson import INPUTS as ML_INPUTS, USB_INPUT, Levinson, LevinsonError, discover as ml_discover  # noqa: E402

STATIC = os.environ.get("SOTM_STATIC", os.path.join(os.path.dirname(__file__), "..", "static"))
STATE_DIR = os.environ.get("SOTM_STATE", "/var/lib/sotm-web")
SOLOIST_WS = "ws://127.0.0.1:9090"
ARCAM_HOST = os.environ.get("ARCAM_HOST", "192.168.1.176")
RO_LOWER = "/media/root-ro"
PUBLIC_URL = os.environ.get("SOTM_PUBLIC_URL", "http://mscos.local")
# Only the reverse proxy and the device itself may talk to the panel directly; everyone
# else is sent to the HTTPS name, so the password never crosses the LAN in clear text.
TRUSTED_PEERS = set(os.environ.get("SOTM_TRUSTED_PEERS", "192.168.1.42").split()) | {"127.0.0.1", "::1"}
HA_WEBHOOK_URL = os.environ.get("HA_WEBHOOK_URL", "")  # from /etc/sotm-web.env, never logged
SESSION_COOKIE = "sotm_session"
AUTO_VERIFY_EVERY = 3600
AUTO_VERIFY_SECS = 20

log = logging.getLogger("sotm-web")


# =========================================================================== helpers
def overlay_active() -> bool:
    return collect.system_info()["root_overlay"]


def persist(path: str) -> None:
    """With overlayroot active, also write a changed file to the real SD-card root."""
    if not overlay_active() or not os.path.isdir(RO_LOWER):
        return
    subprocess.run(["mount", "-o", "remount,rw", RO_LOWER], check=False)
    try:
        dst = RO_LOWER + path
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(path, dst)
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
        self.arcam = Arcam(ARCAM_HOST)
        self.arcam_state: dict = {"reachable": None}
        self.tests = collections.deque(maxlen=200)  # verification log, manual and automatic
        self.settings: dict = {"auto_verify": True, "alerts": True, "leds": True, "net_speed": 100,
                               # Mark Levinson 585.5: address ("" = find it), wake + USB on play,
                               # standby after N idle minutes (0 = never), panel volume ceiling
                               "ml_host": "", "ml_tv_input": "RCA3", "arcam_max_vol": 50, "ml_auto_on": True, "ml_auto_off_min": 0, "ml_max_db": 40.0,
                               "source": "spotify"}  # who owns the DAC: spotify | roon
        # 30 s samples, 24 h: the DAC's measured clock and the supply rail
        self.clock = collections.deque(maxlen=24 * 60 * 2)
        self.supply = collections.deque(maxlen=24 * 60 * 2)
        self.alert_sent: dict[str, float] = {}
        self.since: dict[str, float] = {}  # when a bad condition was first seen
        self.http: ClientSession | None = None
        self._load()
        self.ml = Levinson(self.settings.get("ml_host") or self.settings.get("ml_host_found") or None)
        self.ml_state: dict = {"reachable": None}
        self.ml_found = ""          # address found by scanning
        self._was_playing = False
        self._ml_woke_t = 0.0       # when we last woke it (auto standby only undoes our own wake)
        self._idle_since = time.time()
        self._ml_seen_t = time.time()  # last time the amplifier answered (or was found)
        self._dac_running = False
        self.source_lock = asyncio.Lock()
        self.ml_select_lock = asyncio.Lock()
        self.cmd_lock = asyncio.Lock()  # one Soloist command at a time: its errors carry no request id
        self._cmd_reply: asyncio.Future | None = None

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
                self.settings.update(json.load(f))
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
            self.playback["item"] = ev.get("item")
        elif t == "playback_changed":
            self.playback["status"] = ev.get("status")
        elif t == "volume_changed":
            self.playback["volume"] = ev.get("volume")
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
            raise web.HTTPServiceUnavailable(text="Soloist svarar inte")
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
                if self.settings.get("source") == "roon":
                    self._set_playing(collect.dac_info().get("state") == "RUNNING")
                if n % 720 == 0 and n:  # hourly: keep SD-card writes rare
                    await asyncio.get_running_loop().run_in_executor(None, self.save)
            except Exception:  # keep sampling no matter what
                log.exception("sampling failed")
            n += 1
            await asyncio.sleep(5)

    async def refresh_status(self):
        pw, solo = await asyncio.gather(collect.pipewire_info(), collect.soloist_info())
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
            "source": self.settings.get("source", "spotify"),
            "roon": {"installed": await collect.roon_installed(), "active": await collect.roon_active()},
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
            await self.alert("dropouts", "Avbrott i ljudet",
                             f"{n} avbrott under uppspelning kl {time.strftime('%H:%M')}. Se avbrottshistoriken i panelen.",
                             cooldown=3600)

    # ---- alerts to Home Assistant (webhook -> phone notification)
    async def alert(self, kind: str, title: str, message: str, cooldown: float = 6 * 3600, force=False) -> bool:
        if not HA_WEBHOOK_URL or (not self.settings.get("alerts") and not force):
            return False
        now = time.time()
        if not force and now - self.alert_sent.get(kind, 0) < cooldown:
            return False
        try:
            if self.http is None:
                self.http = ClientSession(timeout=ClientTimeout(total=10))
            async with self.http.post(HA_WEBHOOK_URL, json={"title": title, "message": message, "tag": f"sotm-{kind}"}) as r:
                ok_ = r.status < 300
        except Exception as e:  # never let alerting break sampling
            log.warning("alert %s failed: %s", kind, type(e).__name__)
            return False
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
            await self.alert("dac", "DAC:en hittas inte",
                             "Mark Levinson 585.5 syns inte längre på USB. Kontrollera kabeln och att förstärkaren är på.")
        if self._held("soloist", self.settings.get("source") == "spotify"
                      and solo.get("service") not in (None, "active"), 120):
            await self.alert("soloist", "Soloist har stannat",
                             f"Spotify-tjänsten är {solo.get('service')}. Starta om den under System i panelen.")
        days = solo.get("days_left")
        if days is not None and days <= 14:
            await self.alert("expiry", "Soloist-bygget går snart ut",
                             f"{days} dagar kvar. Nattens uppdatering har inte hittat en nyare version; prova Sök ny version i panelen.",
                             cooldown=24 * 3600)

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
            raise web.HTTPConflict(text="Ingen USB-DAC hittades i PipeWire")
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
                        what = "avbrott" if res.get("verdict") == "bitperfect" else f"resultat: {res.get('verdict')}"
                        await self.alert("verify", "Verifieringen hittade avvikelser",
                                         f"{'Automatisk' if auto else 'Manuell'} verifiering kl {time.strftime('%H:%M')}: {what}.")
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

    # ---- Mark Levinson 585.5
    def _on_playback_change(self):
        if self.settings.get("source") != "spotify":
            return
        # Choosing "Mark Levinson 585.5" in the Spotify app already wakes the amplifier and
        # selects USB, before anything plays (ml_on_play has a 90 s guard, so play right after
        # does not send it twice).
        if "is_active" not in self.auth:
            return
        active = bool(self.auth.get("is_active"))
        prev = getattr(self, "_was_active", None)  # None: first report after (re)connecting
        if active and prev is False:
            asyncio.ensure_future(self.ml_on_play())
        self._was_active = active
        playing = self.playback.get("status") == "playing"
        if not getattr(self, "_playing_known", True) and "status" in self.playback:
            # first report after (re)connecting: music already playing is not a new start,
            # so a panel restart never wakes the amplifier
            self._playing_known = True
            self._was_playing = playing
            if not playing:
                self._idle_since = time.time()
            return
        self._set_playing(playing)

    def _set_playing(self, playing: bool):
        # A real start, not a track change: Soloist pauses for a moment between tracks, and
        # re-selecting the input then makes the amplifier relock (audible glitch).
        if playing and not self._was_playing and time.time() - self._idle_since > 30:
            asyncio.ensure_future(self.ml_on_play())
        if not playing and self._was_playing:
            self._idle_since = time.time()
        self._was_playing = playing

    async def ml_on_play(self, force: bool = False):
        """Spotify started: wake the amplifier and select the USB input. force: an explicit
        request to play on the stereo (from HA), so the guard against fighting a manual
        input change does not apply."""
        if not self.settings.get("ml_auto_on") or not self.ml.host:
            return
        if not force and time.time() - getattr(self, "_ml_play_t", 0) < 90:  # don't fight a manual input change
            return
        self._ml_play_t = time.time()
        try:
            await self.ml_select(USB_INPUT, unmute=force)
        except LevinsonError as e:
            log.warning("585.5 auto-on failed: %s", e)
        await self.ml_refresh()

    async def ml_select(self, name: str, unmute: bool = False) -> dict:
        """Wake the amplifier if needed and put it on an input, without re-selecting the input
        it is already on (that makes it relock: audible). Raises LevinsonError."""
        async with self.ml_select_lock:
            # Read the display now: the remote may have changed power or input since the last poll.
            d = await self.ml.read_display()
            if d:
                self.ml.power = d["on"]
            woke = self.ml.power is not True
            if woke:
                await self.ml.set_power(True)
                self._ml_woke_t = time.time()
                await asyncio.sleep(6)  # leaving standby takes a few seconds; inputs are ignored meanwhile
            label = next((k for k, v in (self.settings.get("ml_labels") or {}).items() if v == name), None)
            already = not woke and d is not None and label is not None and d["label"] == label
            if not already:
                await self.ml.set_input(name)
                asyncio.ensure_future(self.ml_learn_label(name))
                if label:  # let the display catch up, so the answer shows the new input
                    for _ in range(10):
                        await asyncio.sleep(0.5)
                        d2 = await self.ml.read_display()
                        if d2 and d2["label"] == label and d2.get("level"):
                            break
            log.info("585.5: %s, %s", "woken" if woke else "already on",
                     f"already on {name}" if already else f"input {name}")
            if unmute:  # an explicit "TV/play on the stereo": mute set with the remote should not leave it silent
                await self.ml.set_mute(False)
            return {"woke": woke, "switched": not already, "unmuted": unmute}

    async def ml_learn_label(self, name: str):
        """After selecting an input, remember what the display calls it (e.g. USB = "SOtM")."""
        self._learn_gen = gen = getattr(self, "_learn_gen", 0) + 1
        for _ in range(6):
            await asyncio.sleep(1.5)
            if gen != self._learn_gen:
                return  # another input was selected meanwhile; that one learns its own name
            d = await self.ml.read_display()
            if d and d["label"]:
                labels = dict(self.settings.get("ml_labels") or {})
                if d["label"].lower() == "unused":
                    break  # the factory name of every unused input says nothing
                if labels.get(d["label"]) not in (None, name):
                    continue  # still showing the previous input (slow, or this input is disabled)
                if labels.get(d["label"]) != name:
                    labels = {k: v for k, v in labels.items() if v != name}
                    labels[d["label"]] = name
                    self.settings["ml_labels"] = labels
                    await asyncio.get_running_loop().run_in_executor(None, self.save_settings)
                break
        await self.ml_refresh()

    async def ml_refresh(self):
        self.ml_state = await self.ml.status() if self.ml.host else {"reachable": False, "host": None, "searching": True}
        lab = self.ml_state.get("label")
        if lab:  # the display is the truth, also after the remote was used
            known = (self.settings.get("ml_labels") or {}).get(lab)
            self.ml.input = self.ml_state["input"] = known
        self.ml_state["labels"] = {v: k for k, v in (self.settings.get("ml_labels") or {}).items()}
        self.ml_state["found"] = self.ml_found
        await self.broadcast({"type": "levinson", "levinson": self.ml_state})

    async def levinson_loop(self):
        last_scan = 0.0
        while True:
            try:
                if not self.ml.host and time.time() - last_scan > 600:
                    last_scan = time.time()
                    ip = await ml_discover(skip={ARCAM_HOST, "192.168.1.1", "192.168.1.42", "192.168.1.100", "192.168.1.200"})
                    if ip:
                        log.info("585.5 found at %s", ip)
                        self.ml_found = self.ml.host = ip
                        self._ml_seen_t = time.time()
                        self.settings["ml_host_found"] = ip
                        await asyncio.get_running_loop().run_in_executor(None, self.save_settings)
                await self.ml_refresh()
                # an automatically found address that stops answering may have moved (DHCP): search again
                if self.ml_state.get("reachable"):
                    self._ml_seen_t = time.time()
                elif (self.ml.host and not self.settings.get("ml_host")
                      and time.time() - self._ml_seen_t > 900):
                    await self.ml.close()
                    self.ml.host = self.ml_found = ""
                    self._ml_seen_t = time.time()
                    last_scan = 0.0
                # auto standby: only after we woke it, and after N minutes without playback
                mins = int(self.settings.get("ml_auto_off_min") or 0)
                if (mins and self._ml_woke_t and not self._was_playing
                        and time.time() - max(self._idle_since, self._ml_woke_t) > mins * 60):
                    await self.ml.set_power(False)
                    self._ml_woke_t = 0.0
                    log.info("585.5: standby after %d idle minutes", mins)
            except Exception:
                log.exception("585.5 loop")
            await asyncio.sleep(10 if self.clients else 60)

    async def arcam_loop(self):
        """Read the Arcam once at start, then only on demand (/api/arcam). The AVR10 serves one
        TCP client at a time and Home Assistant (arcam_fmj) keeps a connection open: every
        connection from here throws HA off for ~5 s, and an off->on missed in that gap is a
        TV mode that never starts."""
        try:
            self.arcam_state = await self.arcam.status()
        except ArcamError as e:
            self.arcam_state = {"reachable": False, "error": str(e)}

    # ---- jobs: long-running commands with streamed output
    async def run_job(self, name: str, cmd: list[str] | str, as_user=False):
        if self.jobs.get(name, {}).get("running"):
            raise web.HTTPConflict(text=f"{name} pågår redan")
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
        raise web.HTTPBadRequest(text=f"{what}: ange ett tal")
    if not math.isfinite(x):
        raise web.HTTPBadRequest(text=f"{what}: ange ett tal")
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
        "arcam": hub.arcam_state, "arcam_sources": list(SOURCE_RC5),
        "levinson": hub.ml_state, "ml_inputs": ML_INPUTS,
        "tests": list(hub.tests)[-30:], "settings": {**hub.settings, "alerts_configured": bool(HA_WEBHOOK_URL)},
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
        raise web.HTTPBadRequest(text="okänt kommando")
    uri = b.get("uri")
    if c == "add_to_queue" or uri is not None:
        m = SPOTIFY_URI.fullmatch(uri) if isinstance(uri, str) else None
        if not m:
            raise web.HTTPBadRequest(text="Ogiltig Spotify-adress. Ange till exempel spotify:album:<22 tecken>.")
        if c == "add_to_queue" and m.group(1) != "track":
            raise web.HTTPBadRequest(text="Bara enskilda spår kan läggas i kön (spotify:track:…).")
    msg = {k: v for k, v in b.items() if k in ("command", "position_ms", "volume", "enabled", "uri", "limit")}
    if not (c == "play" and uri) and c not in ("add_to_queue", "deactivate", "activate"):
        await hub.soloist_cmd(msg)  # the panel's own buttons: fire and forget, as before
        return ok()
    # From HA/the voice assistant: say what actually happened.
    if hub.settings.get("source") == "roon":
        raise web.HTTPConflict(text="Stereon är inställd på Roon")
    if not hub.soloist_ws:
        raise web.HTTPServiceUnavailable(text="Soloist svarar inte")
    if not hub.auth.get("logged_in"):
        raise web.HTTPConflict(text="Soloist är inte inloggad på Spotify. Välj enheten i Spotify-appen en gång.")
    if c == "play":
        if not hub.auth.get("is_active"):
            r = await hub.soloist_cmd({"command": "activate"}, wait=1.5)
            if r and r.get("type") == "error":
                raise web.HTTPBadGateway(text=f"Soloist: {r.get('message')}")
            if not await hub.wait_active():
                raise web.HTTPGatewayTimeout(text="Soloist blev inte aktiv på Spotify")
        r = await hub.soloist_cmd(msg, wait=2.0)
        if not (r and r.get("type") == "error") and time.time() - getattr(hub, "_ml_play_t", 0) > 10:
            asyncio.ensure_future(hub.ml_on_play(force=True))  # wake the amplifier, select SOtM
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
        "position_ms": pos.get("position_ms"),
        "position_ts": pos["timestamp_ms"] / 1000 if pos.get("timestamp_ms") else None,
        "shuffle": opts.get("shuffle"),
        "repeat": opts.get("repeat"),
        "device_name": a.get("device_name"),
    }


def _ml_bypass(text: str | None = None) -> bool:
    """The display shows "Line" instead of a level when the input is in processor bypass
    (fixed gain, e.g. "Arcam      Line"): the AV receiver sets the volume then."""
    t = text if text is not None else (hub.ml_state or {}).get("display") or ""
    parts = re.split(r"\s{2,}", t, maxsplit=1)
    return len(parts) > 1 and parts[1].strip().lower() == "line"


def _ha_amp() -> dict:
    m = hub.ml_state or {}
    reach = bool(m.get("reachable"))
    bypass = reach and _ml_bypass()
    inp = m.get("input") if reach else None
    labels = m.get("labels") or {}
    return {
        "reachable": reach,
        "power": ("on" if m.get("power") else "standby" if m.get("power") is False else "unknown") if reach else "unknown",
        "input": (labels.get(inp) or m.get("label") or inp) if reach else None,  # what the display calls it, e.g. "SOtM"
        "input_id": inp,  # the panel's name: USB, XLR, ...
        "input_no": ML_INPUTS.index(inp) + 1 if inp in ML_INPUTS else None,  # as on the remote, 1..11
        "volume_db": m.get("volume_db") if reach and not bypass else None,
        "bypass": bypass if reach else None,  # fixed gain: the volume is the Arcam's
        "mute": m.get("muted") if reach else None,
        "max_db": float(hub.settings.get("ml_max_db", 40.0)),
    }


def _ha_mode() -> str | None:
    """Living-room mode from what the 585.5 is on: tv (the AV receiver's input), stereo (SOtM),
    off (standby), other, or None when unknown. Follows the remote as well."""
    m = hub.ml_state or {}
    if not m.get("reachable") or m.get("power") is None:
        return None
    if m.get("power") is False:
        return "off"
    inp = m.get("input")
    return "tv" if inp == hub.settings.get("ml_tv_input", "RCA3") else "stereo" if inp == USB_INPUT else "other"


@routes.get("/api/ha")
async def ha_summary(request):
    """One cheap snapshot for Home Assistant: in-memory state only, plus the DAC's /proc state."""
    d = collect.dac_info()
    src = hub.settings.get("source", "spotify")
    solo = _ha_soloist()
    # PipeWire keeps the DAC open (ALSA RUNNING) also in silence, so with Spotify "running"
    # means Soloist is playing here; Roon Bridge opens the DAC only while it plays.
    running = (d.get("state") == "RUNNING") if src == "roon" else (solo["status"] == "playing" if solo["reachable"] else None)
    return web.json_response({
        "ts": time.time(),
        "source": src,
        "soloist": solo,
        "amp": _ha_amp(),
        "mode": _ha_mode(),
        "dac": {"present": bool(d.get("present")), "running": running if d.get("present") else None},
    })


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
        raise web.HTTPBadRequest(text="Namnet får vara 1–40 tecken: bokstäver, siffror, mellanslag, punkt och bindestreck.")
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
    p = collect.prefs_path()
    if not p:
        raise web.HTTPConflict(text="Soloist har inte loggat in än. Anslut från Spotify-appen först.")
    await user_systemctl("stop", "soloist.service")
    text = collect.read(p)
    line = f"audio.normalize_v2={'true' if enabled else 'false'}"
    text = re.sub(r"^audio\.normalize_v2=.*$", line, text, flags=re.M) if "audio.normalize_v2=" in text \
        else text.rstrip("\n") + "\n" + line + "\n"
    with open(p, "w") as f:
        f.write(text)
    persist(p)
    await user_systemctl("start", "soloist.service")
    return ok(enabled=enabled)


@routes.post("/api/soloist/apikey")
async def soloist_apikey(request):
    """Replace the Spotify developer key. Never echoed or logged. Rolled back if Soloist
    does not stay up with the new key."""
    key = str((await body(request)).get("key", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-.:+/=]{16,512}", key):
        raise web.HTTPBadRequest(text="Nyckeln ser inte giltig ut: 16–512 tecken, bokstäver, siffror och _-.:+/=.")
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
        raise web.HTTPUnprocessableEntity(text="Soloist startade inte med den nya nyckeln. Den tidigare nyckeln är återställd.")
    await hub.refresh_status()
    return ok(changed=True)


@routes.get("/api/optimizations")
async def get_optimizations(request):
    items = await asyncio.get_running_loop().run_in_executor(
        None, collect.optimizations, hub.status, hub.playback)
    return web.json_response({"t": int(time.time()), "items": items})


# ---- Audio test (capture what goes to the DAC, look for dropouts and bit-perfectness)
@routes.post("/api/audio/test")
async def audio_test(request):
    secs = max(5, min(int((await body(request)).get("seconds", 30)), 120))
    if hub.jobs.get("audio-test", {}).get("running"):
        raise web.HTTPConflict(text="En verifiering pågår redan")
    await hub.run_audio_test(secs)
    return ok(seconds=secs)


# ---- Source switch: Spotify | Roon
@routes.post("/api/source")
async def set_source(request):
    src = (await body(request)).get("source")
    if src not in ("spotify", "roon"):
        raise web.HTTPBadRequest(text="Välj spotify eller roon")
    if hub.source_lock.locked():
        raise web.HTTPConflict(text="Byter redan källa")
    async with hub.source_lock:
        try:
            r = await collect.set_source(src)
        except RuntimeError as e:
            raise web.HTTPConflict(text=str(e))
        if not r["ok"]:
            raise web.HTTPInternalServerError(text=f"Bytet misslyckades: {r['error']}")
        hub.settings["source"] = src
        await asyncio.get_running_loop().run_in_executor(None, hub.save_settings)
        hub._was_playing = False
        hub._idle_since = time.time()
        if src == "roon":
            hub.playback, hub.queue = {}, {}
    await hub.broadcast({"type": "settings", "settings": hub.settings})
    await hub.refresh_status()
    return ok(**r)


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
    return web.json_response({**hub.settings, "alerts_configured": bool(HA_WEBHOOK_URL)})


@routes.post("/api/settings")
async def set_settings(request):
    b = await body(request)
    upd: dict = {k: bool(b[k]) for k in ("auto_verify", "alerts", "leds", "ml_auto_on") if k in b}
    if "ml_auto_off_min" in b:
        upd["ml_auto_off_min"] = num(b["ml_auto_off_min"], 0, 240, "Standby efter", integer=True)
    if "ml_max_db" in b:
        upd["ml_max_db"] = num(b["ml_max_db"], 20, 80, "Högsta volym")
    if "ml_host" in b:
        h = str(b["ml_host"]).strip()
        if h:
            try:
                ipaddress.IPv4Address(h)
            except ValueError:
                raise web.HTTPBadRequest(text="Ange en IP-adress, till exempel 192.168.1.150, eller lämna tomt.")
        upd["ml_host"] = h
    # all valid: apply
    hub.settings.update(upd)
    if "leds" in upd:
        collect.apply_leds(upd["leds"])
    if "ml_host" in upd:
        await hub.ml.close()
        hub.ml.host = upd["ml_host"] or hub.ml_found or None
        hub._ml_seen_t = time.time()
    if b.get("net_speed") in (100, 1000) and b["net_speed"] != hub.settings.get("net_speed"):
        hub.settings["net_speed"] = b["net_speed"]
        # the link renegotiates (~3 s); answer first so the reply is not lost
        asyncio.get_running_loop().call_later(1, lambda: asyncio.ensure_future(collect.apply_net_speed(b["net_speed"])))
    await asyncio.get_running_loop().run_in_executor(None, hub.save_settings)
    await hub.broadcast({"type": "settings", "settings": hub.settings})
    return ok(settings=hub.settings)


@routes.post("/api/alerts/test")
async def alerts_test(request):
    if not HA_WEBHOOK_URL:
        raise web.HTTPConflict(text="Larm är inte kopplade till Home Assistant")
    sent = await hub.alert("test", "Testlarm från Mark Levinson 585.5",
                           "Larmen fungerar. Så här ser det ut när något händer med ljudkedjan.", force=True)
    if not sent:
        raise web.HTTPBadGateway(text="Home Assistant tog inte emot larmet")
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
    return request.cookies.get(SESSION_COOKIE)


@routes.get("/api/auth")
async def auth_state(request):
    return web.json_response({"configured": AUTH.configured, "logged_in": AUTH.valid(_token(request))})


def _login_response(request, token: str):
    resp = ok(logged_in=True)
    resp.set_cookie(SESSION_COOKIE, token, max_age=90 * 86400, httponly=True, samesite="Strict", path="/",
                    secure=request.headers.get("X-Forwarded-Proto", request.scheme) == "https")
    return resp


def _check_new_password(pw: str):
    if len(pw) < 8:
        raise web.HTTPBadRequest(text="Lösenordet behöver minst 8 tecken.")


@routes.post("/api/auth/setup")
async def auth_setup(request):
    if AUTH.configured:
        raise web.HTTPConflict(text="Ett lösenord finns redan. Logga in.")
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
            raise web.HTTPTooManyRequests(text="För många felaktiga försök. Vänta tio minuter.")
        good = await asyncio.get_running_loop().run_in_executor(None, AUTH.verify, pw)
        if not good:
            AUTH.failed()
            await asyncio.sleep(1)
            raise web.HTTPUnauthorized(text="Fel lösenord.")
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
        raise web.HTTPUnauthorized(text="Nuvarande lösenord stämmer inte.")
    pw = str(b.get("password", ""))
    _check_new_password(pw)
    await asyncio.get_running_loop().run_in_executor(None, AUTH.set_password, pw)
    return _login_response(request, AUTH.new_session())


# ---- Arcam
@routes.post("/api/levinson")
async def levinson_action(request):
    b = await body(request)
    try:
        if "power" in b:
            await hub.ml.set_power(bool(b["power"]))
            if b["power"]:
                await asyncio.sleep(0.5)
        if "input" in b:
            await hub.ml.set_input(str(b["input"]))
            asyncio.ensure_future(hub.ml_learn_label(str(b["input"])))
        if "volume_db" in b or "volume_step_db" in b:
            # read fresh, and by input too: right after a switch the display shows no level yet
            d = await hub.ml.read_display()
            tv_label = next((k for k, v in (hub.settings.get("ml_labels") or {}).items()
                             if v == hub.settings.get("ml_tv_input", "RCA3")), None)
            if _ml_bypass(d["text"] if d else None) or (d and tv_label and d["label"] == tv_label):
                raise web.HTTPConflict(text="Ingången står i bypass. Volymen ställs på Arcam.")
        if "volume_db" in b:
            await hub.ml.set_volume(num(b["volume_db"], 0, 80, "Volym"), hub.settings.get("ml_max_db", 40.0))
        if "volume_step_db" in b:
            step = num(b["volume_step_db"], -20, 20, "Volymsteg")
            mx = float(hub.settings.get("ml_max_db", 40.0))
            cur = await hub.ml.read_volume()  # fresh: the remote may have changed it
            if cur is None:
                raise LevinsonError("volymen gick inte att läsa")
            # never raise past the ceiling, and a level set higher with the remote only goes down
            if not (step > 0 and cur >= mx):
                await hub.ml.set_volume(max(0.0, min(mx, cur + step)) if step > 0 else max(0.0, cur + step), max(mx, cur))
        if "mute" in b:
            await hub.ml.set_mute(bool(b["mute"]))
    except LevinsonError as e:
        raise web.HTTPBadGateway(text=f"585.5: {e}")
    await hub.ml_refresh()
    return ok(levinson=hub.ml_state, amp=_ha_amp())


@routes.post("/api/mode")
async def living_room_mode(request):
    """The 585.5's part of the living-room modes (HA does the TV, the Shield and the Arcam):
    tv      wake it and select the AV receiver's input (bypass); pause Soloist, which would
            otherwise play into an input nobody hears
    stereo  wake it and select SOtM (USB), as when the device is chosen in Spotify
    tv_off  standby, but only if it is on the receiver's input: music playing stays on"""
    b = await body(request)
    mode = b.get("mode")
    tv = hub.settings.get("ml_tv_input", "RCA3")
    if mode not in ("tv", "stereo", "tv_off"):
        raise web.HTTPBadRequest(text="mode ska vara tv, stereo eller tv_off")
    if not hub.ml.host:
        raise web.HTTPServiceUnavailable(text="Förstärkaren är inte hittad")
    did: dict = {}
    try:
        if mode == "tv":
            if hub.soloist_ws and hub.auth.get("is_active") and hub.playback.get("status") == "playing":
                await hub.soloist_cmd({"command": "pause"})
                did["paused_soloist"] = True
            did |= await hub.ml_select(tv, unmute=True)
        elif mode == "stereo":
            did |= await hub.ml_select(USB_INPUT, unmute=True)  # Spotify and Roon both play through USB
            hub._ml_play_t = time.time()  # the next Spotify start need not do it again
        else:
            d = await hub.ml.read_display()
            label = next((k for k, v in (hub.settings.get("ml_labels") or {}).items() if v == tv), None)
            if d and d["on"] and label and d["label"] == label:
                await hub.ml.set_power(False)
                did["standby"] = True
            else:
                did["standby"] = False
                did["reason"] = "förstärkaren är inte på tv-ingången" if d and d["on"] else "redan i standby"
    except LevinsonError as e:
        raise web.HTTPBadGateway(text=f"585.5: {e}")
    await hub.ml_refresh()
    return ok(mode=mode, did=did, amp=_ha_amp())


@routes.post("/api/arcam")
async def arcam_action(request):
    b = await body(request)
    try:
        if "power" in b:
            await hub.arcam.set_power(bool(b["power"]))
        if "source" in b:
            await hub.arcam.set_source(str(b["source"]))
        if "volume" in b:
            # same rule as the 585.5: the panel never goes above the ceiling; the remote can
            await hub.arcam.set_volume(min(num(b["volume"], 0, 99, "Volym", integer=True),
                                           int(hub.settings.get("arcam_max_vol", 50))))
        if "mute" in b:
            await hub.arcam.set_mute(bool(b["mute"]))
        await asyncio.sleep(0.6 if "power" not in b else 3)
        hub.arcam_state = await hub.arcam.status()
    except ArcamError as e:
        raise web.HTTPBadGateway(text=f"Arcam: {e}")
    await hub.broadcast({"type": "arcam", "arcam": hub.arcam_state})
    return ok(arcam=hub.arcam_state)


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
    rc, _, err = await collect.run(["systemctl", "--user", "restart", "pipewire.service", "wireplumber.service",
                                    "soloist.service"], as_user=True, timeout=60)
    if rc:
        raise web.HTTPInternalServerError(text=f"Omstarten misslyckades: {err.strip()[:200]}")
    return ok()


USB_LOCK = asyncio.Lock()


@routes.post("/api/system/usb-reconnect")
async def usb_reconnect(request):
    if USB_LOCK.locked():
        raise web.HTTPConflict(text="DAC:n ansluts redan på nytt")
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
        raise web.HTTPConflict(text="Stäng av skrivskyddet först. Med skrivskyddat system försvinner uppdateringar vid omstart.")
    await hub.run_job("system-upgrade", "apt-get update && apt-get -y -o Dpkg::Options::=--force-confold full-upgrade && apt-get -y autoremove")
    return ok()


@routes.post("/api/system/overlay")
async def overlay(request):
    enabled = bool((await body(request)).get("enabled"))
    if enabled:
        cmd = ("set -e; dpkg -s overlayroot >/dev/null 2>&1 || apt-get install -y overlayroot; "
               "sed -i 's/^overlayroot=.*/overlayroot=\"tmpfs:recurse=0\"/' /etc/overlayroot.conf; "
               "grep -q '^overlayroot=\"tmpfs' /etc/overlayroot.conf; "
               "update-initramfs -u; echo 'Klart. Starta om enheten för att aktivera skrivskyddet.'")
    else:
        cmd = ("set -e; if [ -d /media/root-ro/etc ]; then mount -o remount,rw /media/root-ro; "
               "sed -i 's/^overlayroot=.*/overlayroot=\"\"/' /media/root-ro/etc/overlayroot.conf; "
               "mount -o remount,ro /media/root-ro; else sed -i 's/^overlayroot=.*/overlayroot=\"\"/' /etc/overlayroot.conf; fi; "
               "echo 'Klart. Starta om enheten för att stänga av skrivskyddet.'")
    await hub.run_job("overlay", cmd)
    return ok()


@routes.get("/api/logs")
async def get_logs(request):
    q = request.query
    text = await collect.logs(q.get("source", "soloist"), int(q.get("lines", 200)), q.get("errors") == "1")
    return web.Response(text=text, content_type="text/plain", charset="utf-8")


# ---- static PWA
@routes.get("/")
async def index(request):
    return web.FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


PROTECTED = ("/api/system/", "/api/logs", "/api/soloist/apikey", "/api/soloist/name", "/api/soloist/normalization",
             "/api/soloist/restart", "/api/soloist/update", "/api/settings", "/api/alerts/", "/api/tests/",
             "/api/auth/password",
             "/api/source")  # stops/starts the audio stacks as root


def _peer(request) -> str:
    return (request.transport.get_extra_info("peername") or ("",))[0] if request.transport else ""


@web.middleware
async def only_via_proxy(request, handler):
    if _peer(request) not in TRUSTED_PEERS:
        if request.path.startswith("/api/"):
            raise web.HTTPForbidden(text=f"Använd {PUBLIC_URL}")
        raise web.HTTPPermanentRedirect(PUBLIC_URL + request.path_qs)
    return await handler(request)


@web.middleware
async def same_origin_json(request, handler):
    """CSRF guard for every state-changing request, protected or not (amplifier volume,
    playback, Arcam). A cross-site page can only send "simple" requests without a CORS
    preflight, which cannot be application/json; browsers also send Origin on POST."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != PUBLIC_URL.rstrip("/"):
            raise web.HTTPForbidden(text="Begäran kommer från en annan webbplats.")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Begäran måste vara JSON.")
    return await handler(request)


@web.middleware
async def require_login(request, handler):
    p = request.path
    guarded = any(p.startswith(x) for x in PROTECTED) and not (p == "/api/settings" and request.method == "GET")
    if guarded and not AUTH.valid(_token(request)):
        raise web.HTTPUnauthorized(text="Logga in under System för att göra det här.")
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
        src = hub.settings.get("source", "spotify")
        if src == "spotify":
            rc, out, _ = await collect.run(["systemctl", "--user", "is-active", "soloist.service"], as_user=True, timeout=10)
            if out.strip() == "active":
                return
        async with hub.source_lock:
            r = await collect.set_source(src)
            log.info("source at startup: %s", r)
    asyncio.ensure_future(restore())
    app["tasks"] = [asyncio.create_task(c) for c in (hub.soloist_loop(), hub.sample_loop(), hub.arcam_loop(), hub.levinson_loop())]


async def on_cleanup(app):
    for t in app["tasks"]:
        t.cancel()
    if hub.http:
        await hub.http.close()
    hub.save()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    app = web.Application(middlewares=[only_via_proxy, same_origin_json, require_login, cache_headers])
    app.add_routes(routes)
    app.router.add_static("/", STATIC, show_index=False)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", 80)), access_log=None)


if __name__ == "__main__":
    main()
