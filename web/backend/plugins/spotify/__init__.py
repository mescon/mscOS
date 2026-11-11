"""Spotify Lossless through Soloist, Spotify's own Connect endpoint for developers.

Soloist needs a personal developer key from a Premium account; Spotify's terms say the key
may not be shared and the program may not be redistributed, so the install script downloads
it from Spotify on the device and the key is entered in the panel.

Everything about Soloist lives here: the relay to its WebSocket (what plays, the queue,
commands), its key, name and volume normalization (also for guests' accounts), what the
current track really is (measured from the samples), the sample-exact verification, and
the alerts about it. The core only sees a source module.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import re
import tempfile
import time

from aiohttp import web
import websockets

import bitcheck
import collect
from i18n import T
from plugins import SourcePlugin

log = logging.getLogger("spotify")
BIN = "/usr/local/bin/soloist"
SOLOIST_WS = "ws://127.0.0.1:9090"
SPOTIFY_URI = re.compile(r"spotify:(track|album|playlist|artist|episode|show):[A-Za-z0-9]{22}")
AUTO_VERIFY_EVERY = 3600
AUTO_VERIFY_SECS = 20


class Spotify(SourcePlugin):
    id = "spotify"
    label = "Spotify Lossless"
    protocol = "Spotify Connect"
    name = {"sv": "Spotify Lossless (Soloist)", "en": "Spotify Lossless (Soloist)"}
    description = {"sv": "Spotify Connect i Lossless, bitperfekt. Kräver Premium och en egen utvecklarnyckel från Spotify.",
                   "en": "Spotify Connect in Lossless, bit-perfect. Needs Premium and your own developer key from Spotify."}
    user_units = ("soloist.service", "soloist-rt.service")
    hint = {"sv": "Välj {name} i Spotify-appen", "en": "Choose {name} in the Spotify app"}
    terms = {"sv": "Soloist hämtas från Spotify till den här enheten. Du behöver Spotify Premium och en egen nyckel, som du skapar på Spotifys utvecklarsida och anger här efteråt. Nyckeln är personlig.",
             "en": "Soloist is downloaded from Spotify to this device. You need Spotify Premium and a key of your own, which you create on Spotify's developer site and enter here afterwards. The key is personal."}
    terms_url = "https://developer.spotify.com/documentation/soloist"
    has_slider = True
    updates = "self"  # soloist-update.timer, nightly: Spotify's builds expire
    transport = ("toggle", "play", "pause", "next", "prev", "seek", "shuffle", "repeat")
    pushes_playing = True  # Soloist's events say when playback starts and stops here
    defaults = {"auto_verify": True, "soloist_normalization": False}

    def __init__(self, hub):
        super().__init__(hub)
        self.playback: dict = {}
        self.auth: dict = {}
        self.queue: dict = {}
        self.soloist_ws = None
        self.track_quality: dict = {}  # measured format of the current track
        self._norm_failed: tuple | None = None   # last set of accounts a write did not fix
        self._accounts: set[str] | None = None  # Spotify accounts seen (guests add new ones)
        self.cmd_lock = asyncio.Lock()  # one Soloist command at a time: its errors carry no request id
        self._cmd_reply: asyncio.Future | None = None
        # the verification log, manual and automatic (kept across restarts with the core's stats)
        d = hub.saved_stats
        self.last_test: dict | None = d.get("last_test")
        self.tests = collections.deque(d.get("tests", []), maxlen=200)
        lt = self.last_test
        if not self.tests and lt and lt.get("t"):  # seed the log with the pre-log result
            self.tests.append({k: lt.get(k) for k in ("t", "verdict", "bits", "gain_db", "seconds", "track")}
                              | {"auto": False, "gaps": len(lt.get("gaps") or [])})

    # ---- what the core asks of a module
    def tasks(self) -> list:
        return [self.soloist_loop()]

    def hello(self) -> dict:
        return {"playback": self.playback, "auth": self.auth, "queue": self.queue, "last_test": self.last_test,
                "quality": self.track_quality, "tests": list(self.tests)[-30:]}

    def summary(self) -> dict:
        return {"soloist": self._ha_soloist()}

    def save_state(self) -> dict:
        return {"last_test": self.last_test, "tests": list(self.tests)}

    def clear_history(self) -> None:
        self.tests.clear()

    async def tick(self) -> None:
        await self.maybe_auto_verify()
        await self.keep_normalization()

    def lost_dac(self) -> None:
        self.playback, self.queue = {}, {}

    def play_began(self) -> None:
        asyncio.ensure_future(self.measure_track_quality())

    async def check_health(self) -> None:
        hub, solo = self.hub, self.hub.status.get("soloist") or {}
        if hub._held("soloist", hub.soloist and collect.api_key_info()["set"]
                     and solo.get("service") not in (None, "active"), 120):
            await hub.alert("soloist", T("Soloist har stannat"),
                            T("Spotify-tjänsten är {state}. Starta om den under Moduler i panelen.", state=solo.get("service")))
        days = solo.get("days_left")
        if days is not None and days <= 14:
            await hub.alert("expiry", T("Soloist-bygget går snart ut"),
                            T("{days} dagar kvar. Nattens uppdatering har inte hittat en nyare version; prova Sök ny version i panelen.", days=days),
                            cooldown=24 * 3600)

    def update_settings(self, b: dict, upd: dict) -> None:
        if "auto_verify" in b:
            upd["auto_verify"] = bool(b["auto_verify"])

    # ---- source
    async def installed(self) -> bool:
        return os.path.exists(BIN) and os.path.exists(collect.SOLOIST_UNIT)

    async def start(self) -> str:
        if not collect.api_key_info()["set"]:
            return ""  # installed but no key yet: nothing to start, the panel asks for the key
        return await super().start()

    async def state(self) -> dict:
        return {"key_set": collect.api_key_info()["set"]}

    async def version(self) -> str | None:
        v = ((self.hub.status.get("soloist") or {}).get("version") or "").replace("soloist ", "")
        return v.split(" ")[0] or None

    def now_playing(self) -> dict | None:
        s = self._ha_soloist()
        if not s["reachable"] or not s["title"]:
            return None
        return {"title": s["title"], "artist": s["artist"], "album": s["album"], "image": s["image_url"], "uri": s["uri"],
                "playing": s["status"] == "playing", "duration_ms": s["duration_ms"], "position_ms": s["position_ms"],
                "position_ts": s["position_ts"], "shuffle": bool(s["shuffle"]), "repeat": s["repeat"] or "off"}

    async def command(self, cmd: str, **kw) -> None:
        send = lambda c, **a: self.soloist_cmd({"command": c, **a})  # noqa: E731
        try:
            if cmd == "toggle":
                await send("pause" if self.playback.get("status") == "playing" else "play")
            elif cmd in ("play", "pause"):
                await send(cmd)
            elif cmd in ("next", "prev"):
                await send(f"skip_{cmd}")
            elif cmd == "seek":
                await send("seek", position_ms=int(kw["position_ms"]))
            elif cmd == "shuffle":
                await send("set_shuffle", enabled=bool(kw.get("enabled")))
            elif cmd == "repeat":
                await send("set_repeat_track", enabled=kw.get("mode") == "track")
                await send("set_repeat_context", enabled=kw.get("mode") == "context")
        except web.HTTPException as e:
            raise RuntimeError(e.text)

    async def names_changed(self) -> None:
        if await self.installed():
            await self.set_soloist_name(self.hub.names()["network"])

    def slider(self) -> dict | None:
        return {"active": bool(self.soloist_ws and self.auth.get("is_active")), "volume": self.playback.get("volume"),
                "digital": not self.hub.vol.enabled()}

    async def set_slider(self, pct: int) -> None:
        if self.soloist_ws:
            await self.soloist_cmd({"command": "set_volume", "volume": pct})

    # ---- the relay to Soloist's WebSocket
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
                        await self.hub.broadcast({"type": "soloist", "event": ev})
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError):
                pass
            self.soloist_ws = None
            self.auth = {}
            await self.hub.broadcast({"type": "soloist", "event": {"type": "disconnected"}})
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
            self.hub.vol.on_volume_changed(ev.get("volume"))
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

    async def keep_normalization(self):
        """A guest's new account starts with Spotify's normalization on (-5 dB, not
        bit-perfect). Fix every account's prefs, but only after 10 quiet minutes: Soloist
        reads them at start, and a restart would cut off someone listening."""
        if not self.hub.soloist:
            return
        accounts = set(collect.account_dirs())
        if self._accounts is None:  # first look: what is there now is not "new"
            self._accounts = accounts
        new = accounts - self._accounts
        self._accounts |= accounts
        want = bool(self.hub.settings.get("soloist_normalization", False))
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
            playing, active = self.hub._was_playing, bool(self.auth.get("is_active"))
            log.info("new Spotify account: normalization %s, restarting Soloist", "on" if want else "off")
            async with self.hub.source_lock:
                ok_ = await self.apply_normalization(want)
            if not ok_:
                self._norm_failed = (want, tuple(collect.normalization_mismatch(want)))
                log.warning("normalization could not be written for %s", self._norm_failed[1])
            asyncio.ensure_future(self._resume_after_restart(active, playing))
            return
        if self.hub._was_playing or time.time() - self.hub._idle_since < 600:
            return  # an existing account: only when nobody is listening
        log.info("normalization %s for %d account(s); restarting Soloist", "on" if want else "off", len(wrong))
        async with self.hub.source_lock:
            ok_ = await self.apply_normalization(want)
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

    async def measure_track_quality(self):
        """6 s of what goes to the DAC, a few seconds into each track: on the 16-bit lattice
        means a 16-bit file, content below it 24-bit, no lattice at all lossy (or processed).
        Read-only tap on the sink monitor; one at a time; skipped while a verification runs."""
        if getattr(self, "_quality_busy", False):
            return
        uri = (self.playback.get("item") or {}).get("uri")
        if not uri or not self.hub.soloist:
            log.info("track quality: no track")
            return
        self._quality_busy = True
        try:
            await asyncio.sleep(8)  # past the track start (gapless join, fades)
            if (self.playback.get("item") or {}).get("uri") != uri or self.playback.get("status") != "playing" or not self.auth.get("is_active") \
                    or self.hub.jobs.get("audio-test", {}).get("running"):
                log.info("track quality: skipped (track changed, paused, played elsewhere or a verification running)")
                return
            sink = (self.hub.status.get("pipewire") or {}).get("sink") or {}
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
            await self.hub.broadcast({"type": "quality", "quality": self.track_quality})
        except Exception as e:  # noqa: BLE001 - never disturb playback over this
            log.warning("track quality: %s", e)
        finally:
            self._quality_busy = False

    async def maybe_auto_verify(self):
        if not self.hub.settings.get("auto_verify") or self.hub.jobs.get("audio-test", {}).get("running"):
            return
        last_auto = max((t["t"] for t in self.tests if t.get("auto")), default=0)
        if time.time() - last_auto < AUTO_VERIFY_EVERY:
            return
        p = self.playback
        if p.get("status") != "playing" or (self.hub.status.get("dac") or {}).get("state") != "RUNNING":
            return
        pos = p.get("position") or {}
        dur = (((p.get("item") or {}).get("decorations") or {}).get("playback") or {}).get("duration_ms") or 0
        at = pos.get("position_ms", 0) + (time.time() * 1000 - pos.get("timestamp_ms", time.time() * 1000))
        # Mid-track only: a track change inside the window would show up as a gap.
        if at < 15000 or dur - at < (AUTO_VERIFY_SECS + 15) * 1000:
            return
        await self.run_audio_test(AUTO_VERIFY_SECS, auto=True)

    async def run_audio_test(self, secs: int, auto: bool = False):
        sink = (self.hub.status.get("pipewire") or {}).get("sink") or {}
        if not sink.get("name"):
            raise web.HTTPConflict(text=T("Ingen USB-DAC hittades i PipeWire"))
        job = {"name": "audio-test", "running": True, "output": "", "rc": None, "started": time.time(),
               "seconds": secs, "auto": auto}
        self.hub.jobs["audio-test"] = job
        await self.hub.broadcast({"type": "job", "name": "audio-test", "started": True, "auto": auto, "seconds": secs})

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
                        await self.hub.alert("verify", T("Verifieringen hittade avvikelser"),
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
                await self.hub.broadcast({"type": "job", "name": "audio-test", "done": True, "rc": job["rc"], "auto": auto,
                                      "result": job.get("result"), "error": job["output"],
                                      "tests": list(self.tests)[-30:]})
                await asyncio.get_running_loop().run_in_executor(None, self.hub.save)

        asyncio.create_task(go())
        return job

    def _on_playback_change(self):
        if not self.hub.soloist:
            return
        # Choosing "Mark Levinson 585.5" in the Spotify app already wakes the amplifier and
        # selects its input, before anything plays (the module has a 90 s guard, so play right after
        # does not send it twice).
        if "is_active" not in self.auth:
            return
        active = bool(self.auth.get("is_active"))
        prev = getattr(self, "_was_active", None)  # None: first report after (re)connecting
        if active and prev is False:
            self.hub.play_started()
        if active and prev is not True:
            self.hub.vol.on_active()  # slider from the amplifier, after Spotify restored its own
        self._was_active = active
        # Soloist mirrors the account's playback: "playing" also arrives while another device
        # (the phone) plays. Only playback on this device counts as a start.
        playing = active and self.playback.get("status") == "playing"
        if not getattr(self, "_playing_known", True) and "status" in self.playback:
            # first report after (re)connecting: music already playing is not a new start,
            # so a panel restart never wakes the amplifier
            self._playing_known = True
            self.hub._was_playing = playing
            if playing:  # already playing when we (re)connected: still measure this track
                asyncio.ensure_future(self.measure_track_quality())
            if not playing:
                self.hub._idle_since = time.time()
            return
        self.hub._set_playing(playing)

    async def set_soloist_name(self, name: str) -> bool:
        """Soloist's name in the Spotify app (an argument in its unit). True if it changed;
        Soloist is restarted then. Read and written as the audio user: the unit lives in its
        home, and root follows nothing there."""
        name = re.sub(r"[^\w .\-]", "", name)[:40].strip() or "mscOS"  # what Soloist takes
        p = collect.SOLOIST_UNIT
        rc, text, _ = await collect.run(["cat", p], as_user=True, timeout=5)
        if rc or "--device-name" not in text:
            raise RuntimeError(T("Soloist är inte installerat"))
        new = re.sub(r'--device-name\s+"[^"]*"', lambda m: f'--device-name "{name}"', text)
        if new == text:
            return False
        rc, _, err = await collect.run(["sh", "-c", 'cat > "$1.tmp" && mv -f "$1.tmp" "$1"', "sh", p], as_user=True,
                                       timeout=5, input_=new.encode())
        if rc:
            raise RuntimeError(err.strip()[:200])
        self.hub.persist(p)
        await self.hub.user_systemctl("daemon-reload")
        if self.hub.soloist:
            await self.hub.user_systemctl("restart", "soloist.service")
        return True

    # ---- state for automations, and the normalization writer
    def _ha_soloist(self) -> dict:
        connected = self.soloist_ws is not None
        a, p = self.auth, self.playback
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
            "quality": self._ha_quality(it.get("uri")),
            "position_ms": pos.get("position_ms"),
            "position_ts": pos["timestamp_ms"] / 1000 if pos.get("timestamp_ms") else None,
            "shuffle": opts.get("shuffle"),
            "repeat": opts.get("repeat"),
            "device_name": a.get("device_name"),
        }

    def _ha_quality(self, uri) -> str | None:
        q = self.track_quality
        if not uri or q.get("uri") != uri:
            return None
        if q.get("verdict") == "bitperfect":
            return "flac24" if q.get("bits") == 24 else "flac16" if q.get("bits") == 16 else None
        return "lossy" if q.get("verdict") == "lossy_or_processed" else None

    async def apply_normalization(self, enabled: bool) -> bool:
        """Write the setting into every account's prefs (Soloist reads them at start).
        Returns whether every account now has it."""
        await self.hub.user_systemctl("stop", "soloist.service")
        for d in collect.account_dirs():
            if await collect.set_normalization_file(d + "prefs", enabled):
                self.hub.persist(d + "prefs")
        await self.hub.user_systemctl("start", "soloist.service")
        return not collect.normalization_mismatch(enabled)

    # ---- HTTP API
    def routes(self, routes) -> None:
        hub, sp = self.hub, self

        @routes.post("/api/soloist/command")
        async def soloist_command(request):
            b = await hub.body(request)
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
                await sp.soloist_cmd(msg)  # the panel's own buttons: fire and forget, as before
                return hub.ok()
            # From Home Assistant (voice assistants): say what actually happened.
            if not hub.soloist:
                raise web.HTTPConflict(text=T("Källan är inte Spotify Lossless"))
            if not sp.soloist_ws:
                raise web.HTTPServiceUnavailable(text=T("Soloist svarar inte"))
            if not sp.auth.get("logged_in"):
                raise web.HTTPConflict(text=T("Soloist är inte inloggad på Spotify. Välj enheten i Spotify-appen en gång."))
            if c == "play":
                if not sp.auth.get("is_active"):
                    r = await sp.soloist_cmd({"command": "activate"}, wait=1.5)
                    if r and r.get("type") == "error":
                        raise web.HTTPBadGateway(text=f"Soloist: {r.get('message')}")
                    if not await sp.wait_active():
                        raise web.HTTPGatewayTimeout(text=T("Soloist blev inte aktiv på Spotify"))
                r = await sp.soloist_cmd(msg, wait=2.0)
                if not (r and r.get("type") == "error"):
                    hub.play_started(force=True)  # wake the amplifier, select this device's input
            else:
                r = await sp.soloist_cmd(msg, wait=2.0)
            if r and r.get("type") == "error":
                raise web.HTTPBadGateway(text=f"Soloist: {r.get('message')}")
            return hub.ok(result=r)

        @routes.post("/api/soloist/restart")
        async def soloist_restart(request):
            rc, out, err = await hub.user_systemctl("restart", "soloist.service")
            return hub.ok(rc=rc, error=err.strip())

        @routes.post("/api/soloist/update")
        async def soloist_update(request):
            await hub.run_job("soloist-update", ["/usr/local/sbin/soloist-update"])
            return hub.ok()

        @routes.post("/api/soloist/normalization")
        async def soloist_normalization(request):
            enabled = bool((await hub.body(request)).get("enabled"))
            if not collect.prefs_paths():
                raise web.HTTPConflict(text=T("Soloist har inte loggat in än. Anslut från Spotify-appen först."))
            hub.settings["soloist_normalization"] = enabled  # kept for every account, guests too
            await asyncio.get_running_loop().run_in_executor(None, hub.save_settings)
            await sp.apply_normalization(enabled)
            return hub.ok(enabled=enabled)

        @routes.post("/api/soloist/apikey")
        async def soloist_apikey(request):
            """Replace the Spotify developer key. Never echoed or logged. Rolled back if Soloist
            does not stay up with the new key."""
            key = str((await hub.body(request)).get("key", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_\-.:+/=]{16,512}", key):
                raise web.HTTPBadRequest(text=T("Nyckeln ser inte giltig ut: 16–512 tecken, bokstäver, siffror och _-.:+/=."))
            path = collect.SOLOIST_ENV
            old = collect.read(path)
            if f"SOLOIST_API_KEY={key}" in old:
                return hub.ok(changed=False)
            st = os.stat(path) if os.path.exists(path) else None

            def write(text: str):
                tmp = path + ".tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                if st:
                    os.chown(tmp, st.st_uid, st.st_gid)
                os.replace(tmp, path)
                hub.persist(path)

            new_text = re.sub(r"^SOLOIST_API_KEY=.*$", f"SOLOIST_API_KEY={key}", old, flags=re.M) \
                if re.search(r"^SOLOIST_API_KEY=", old, re.M) else old.rstrip("\n") + f"\nSOLOIST_API_KEY={key}\n"
            write(new_text)
            if not hub.soloist:  # another source owns the DAC: the key is used when Spotify is chosen
                return hub.ok(changed=True, started=False)
            await hub.user_systemctl("restart", "soloist.service")
            # Baseline after the manual restart (which resets the automatic-restart counter).
            _, before, _ = await hub.user_systemctl("show", "soloist.service", "-p", "NRestarts", "--value")
            await asyncio.sleep(12)  # Soloist restarts after 5 s if the key is rejected
            _, active, _ = await hub.user_systemctl("is-active", "soloist.service")
            _, after, _ = await hub.user_systemctl("show", "soloist.service", "-p", "NRestarts", "--value")
            if active.strip() != "active" or after.strip() != before.strip():
                write(old)
                await hub.user_systemctl("restart", "soloist.service")
                raise web.HTTPUnprocessableEntity(text=T("Soloist startade inte med den nya nyckeln. Den tidigare nyckeln är återställd."))
            await hub.refresh_status()
            return hub.ok(changed=True)

        @routes.post("/api/audio/test")
        async def audio_test(request):
            secs = max(5, min(int((await hub.body(request)).get("seconds", 30)), 120))
            if hub.jobs.get("audio-test", {}).get("running"):
                raise web.HTTPConflict(text=T("En verifiering pågår redan"))
            await sp.run_audio_test(secs)
            return hub.ok(seconds=secs)


PLUGIN = Spotify
