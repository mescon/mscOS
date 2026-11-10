"""Spotify's volume slider drives the Mark Levinson 585.5; the signal stays bit-perfect.

Soloist applies the Spotify volume as PipeWire stream volume (channelVolumes on its
"spotify" stream node = (v/100)^3), not in its own samples. It reads channelVolumes back
(resetting them made it report 100 %), so the panel sets softVolumes to 1.0 instead: the
stream's channelVolumes stay what Soloist set, the samples pass untouched (verified
2026-11-10: Spotify 50 %, capture bit-perfect 0.0 dB, Spotify still 50 %). So the slider
is a remote control for the amplifier:

  phone slider -> Soloist volume_changed v -> amplifier at v/100 * ceiling (0.5 dB steps)
  new channelVolumes on the stream        -> softVolumes 1.0 (also for new streams)
  amplifier changed elsewhere (remote, HA, panel) -> Spotify volume follows

Safety: never above the panel ceiling (ml_max_db); no change on the Arcam bypass input,
in standby, in Roon mode, or while the amplifier is not on USB; Spotify restores its own
per-device volume when the device is selected, so reports during the first seconds after
it becomes active are not taken as moves: the slider is set from the amplifier instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import collect
from levinson import USB_INPUT, LevinsonError

log = logging.getLogger("volbridge")

GRACE_S = 2.0      # after the device becomes active: Spotify's own volume restore, not a move
ECHO_S = 4.0       # our own set_volume coming back as volume_changed
POLL_S = 5.0       # amplifier volume poll while Spotify is active
MAX_UP_DB = 6.0    # largest rise from one slider event (a jump is capped; the slider then follows)


class VolumeBridge:
    def __init__(self, hub):
        self.hub = hub
        self._active_t = 0.0
        self._sent: tuple[int, float] | None = None   # (spotify volume we set, when)
        self._amp_db: float | None = None             # last amplifier level we know of
        self._lock = asyncio.Lock()

    # ---- conditions
    def enabled(self) -> bool:
        h = self.hub
        return bool(h.settings.get("spotify_vol_to_amp", True)) and h.settings.get("source") == "spotify"

    def _amp_ok(self) -> bool:
        m = self.hub.ml_state or {}
        return bool(self.hub.ml.host and m.get("reachable") and m.get("power") is not False
                    and m.get("input") == USB_INPUT)

    def _max(self) -> float:
        return float(self.hub.settings.get("ml_max_db", 40.0))

    def to_db(self, v: int) -> float:
        return max(0.0, min(self._max(), round(v / 100 * self._max() * 2) / 2))

    def to_pct(self, db: float) -> int:
        return max(0, min(100, round(min(db, self._max()) / self._max() * 100)))

    # ---- events from app.py
    def on_active(self):
        """The device was just selected in Spotify: sync the slider from the amplifier."""
        self._active_t = time.time()
        if self.enabled():
            asyncio.ensure_future(self._sync_after_grace())

    def on_volume_changed(self, v):
        if not isinstance(v, (int, float)) or not self.enabled():
            return
        v = int(round(v))
        if self._sent and abs(v - self._sent[0]) <= 1 and time.time() - self._sent[1] < ECHO_S:
            return  # our own change
        if time.time() - self._active_t < GRACE_S:
            return  # Spotify restoring its stored device volume; _sync_after_grace fixes it
        asyncio.ensure_future(self._move_amp(v))

    # ---- actions
    async def _move_amp(self, v: int):
        async with self._lock:
            if not self._amp_ok():
                return
            db = self.to_db(v)
            cur = self.hub.ml.volume_db
            if cur is not None and abs(cur - db) < 0.25:
                return
            capped = cur is not None and db > cur + MAX_UP_DB
            if capped:
                db = cur + MAX_UP_DB
            try:
                await self.hub.ml.set_volume(db, self._max())
                self._amp_db = db
                log.info("Spotify %d %% -> 585.5 %.1f dB%s", v, db, " (capped)" if capped else "")
            except LevinsonError as e:
                log.warning("volume bridge: %s", e)
                return
        await self.hub.ml_refresh()
        if capped:
            await self._set_spotify(self.to_pct(db))  # the slider shows where the amplifier is

    async def _set_spotify(self, pct: int):
        if not self.hub.soloist_ws:
            return
        self._sent = (pct, time.time())
        try:
            await self.hub.soloist_cmd({"command": "set_volume", "volume": pct})
        except Exception as e:  # noqa: BLE001 - never break the panel over this
            log.warning("volume bridge: set_volume %s", e)

    async def _sync_after_grace(self):
        await asyncio.sleep(GRACE_S)
        await self.sync_from_amp(force=True)

    async def sync_from_amp(self, force: bool = False):
        """Amplifier level -> Spotify slider."""
        if not self.enabled() or not self._amp_ok() or not self.hub.auth.get("is_active"):
            return
        try:
            db = await self.hub.ml.read_volume()
        except LevinsonError:
            return
        if db is None:
            return
        if not force and self._amp_db is not None and abs(db - self._amp_db) < 0.25:
            return
        self._amp_db = db
        pct = self.to_pct(db)
        if force or pct != self.hub.playback.get("volume"):
            log.info("585.5 %.1f dB -> Spotify %d %%", db, pct)
            await self._set_spotify(pct)


    # ---- background: amplifier poll and PipeWire stream watch
    async def amp_loop(self):
        while True:
            try:
                if self.enabled() and self.hub.auth.get("is_active") and time.time() - self._active_t > GRACE_S:
                    await self.sync_from_amp()
            except Exception:
                log.exception("volume bridge poll")
            await asyncio.sleep(POLL_S)

    async def stream_loop(self):
        """Keep the Spotify stream at unity: watch PipeWire (pw-dump -m) and set softVolumes
        to 1.0 after every new channelVolumes from Soloist, also on a newly created stream."""
        while True:
            if not self.enabled():
                await asyncio.sleep(10)
                continue
            proc = None
            try:
                proc = await collect.spawn_as_user(["pw-dump", "-m", "-N"])
                dec, buf = json.JSONDecoder(), ""
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk.decode(errors="replace")
                    while True:
                        buf = buf.lstrip()
                        if not buf:
                            break
                        try:
                            objs, end = dec.raw_decode(buf)
                        except ValueError:
                            break  # incomplete
                        buf = buf[end:]
                        for o in objs:
                            oid = collect.spotify_stream_changed(o) if isinstance(o, dict) else None
                            if oid is not None:
                                await collect.spotify_stream_unity(oid)
                    if not self.enabled():
                        break
            except Exception:
                log.exception("volume bridge stream watch")
            finally:
                if proc and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            await asyncio.sleep(3)
