"""Spotify's volume slider drives the amplifier module (e.g. Mark Levinson 585.5); the signal stays bit-perfect.

The bridge works with any source module that has a slider (SourcePlugin.slider): Soloist,
and go-librespot with external volume, which reports the slider and never scales samples.
What follows about PipeWire stream volume is Soloist's case only.

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
import logging
import time


log = logging.getLogger("volbridge")

GRACE_S = 2.0      # after the device becomes active: then the slider is set from the amplifier
SETTLE_S = 7.0     # after it becomes active or playback starts: the app pushes the volume it has
                   # stored for the device, sometimes seconds later. That is never a move: the
                   # slider is put back where the amplifier is. (Found 2026-11-12: starting playback
                   # with the amplifier at 1 dB raised it 6 dB.)
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
        self._resync = False

    # ---- conditions
    def enabled(self) -> bool:
        h = self.hub
        return (h.amp is not None and bool(h.settings.get("spotify_vol_to_amp", True))
                and getattr(type(h.source), "has_slider", False))

    def _slider(self) -> dict:
        src = self.hub.source
        return (src.slider() if src else None) or {}

    def _active(self) -> bool:
        return bool(self._slider().get("active"))

    def _amp_ok(self) -> bool:
        a = self.hub.amp
        return bool(a and a.ready())

    def _max(self) -> float:
        a = self.hub.amp
        return a.max_db if a else 40.0

    def to_db(self, v: int) -> float:
        """Slider percent -> dB under the ceiling, as the amplifier will take it (its steps)."""
        db = max(0.0, min(self._max(), v / 100 * self._max()))
        return self.hub.amp.quantize(db) if self.hub.amp else db

    def to_pct(self, db: float) -> int:
        return max(0, min(100, round(min(db, self._max()) / self._max() * 100)))

    def _errors(self) -> tuple:
        return getattr(self.hub.amp, "errors", (OSError,))

    # ---- events from app.py
    def on_active(self):
        """The device was just selected in Spotify: sync the slider from the amplifier."""
        self._active_t = time.time()
        if self.enabled():
            asyncio.ensure_future(self._sync_after_grace())

    def settle(self):
        """Playback is starting here: what the app reports in the next seconds is its stored
        volume, not the listener's hand."""
        self._active_t = time.time()

    async def _resync_soon(self):
        await asyncio.sleep(1.0)
        self._resync = False
        await self.sync_from_amp(force=True)

    def on_volume_changed(self, v):
        if not isinstance(v, (int, float)) or not self.enabled():
            return
        v = int(round(v))
        if self._sent and abs(v - self._sent[0]) <= 1 and time.time() - self._sent[1] < ECHO_S:
            return  # our own change
        if time.time() - self._active_t < SETTLE_S:
            # the app restoring its stored device volume: the amplifier stays, the slider goes back
            if not self._resync:
                self._resync = True
                asyncio.ensure_future(self._resync_soon())
            return
        asyncio.ensure_future(self._move_amp(v))

    # ---- actions
    async def _move_amp(self, v: int):
        async with self._lock:
            if not self._amp_ok():
                return
            db = self.to_db(v)
            cur = self.hub.amp.volume_db
            if cur is not None and abs(cur - db) < 0.25:
                return
            capped = cur is not None and db > cur + MAX_UP_DB
            if capped:
                db = cur + MAX_UP_DB
            try:
                await self.hub.amp.set_volume(db, self._max())
                actual = await self.hub.amp.read_volume()  # what it actually took
                self._amp_db = actual if actual is not None else db
                log.info("Spotify %d %% -> amplifier %.1f dB%s", v, db, " (capped)" if capped else "")
            except self._errors() as e:
                log.warning("volume bridge: %s", e)
                return
        await self.hub.amp.refresh()
        if capped:
            await self._set_spotify(self.to_pct(db))  # the slider shows where the amplifier is

    async def _set_spotify(self, pct: int):
        src = self.hub.source
        if src is None:
            return
        self._sent = (pct, time.time())
        try:
            await src.set_slider(pct)
        except Exception as e:  # noqa: BLE001 - never break the panel over this
            log.warning("volume bridge: set_volume %s", e)

    async def _sync_after_grace(self):
        await asyncio.sleep(GRACE_S)
        await self.sync_from_amp(force=True)

    async def sync_from_amp(self, force: bool = False):
        """Amplifier level -> Spotify slider."""
        if not self.enabled() or not self._amp_ok() or not self._active():
            return
        try:
            db = await self.hub.amp.read_volume()
        except self._errors():
            return
        if db is None:
            return
        if not force and self._amp_db is not None and abs(db - self._amp_db) < 0.25:
            return
        self._amp_db = db
        pct = self.to_pct(db)
        if force or pct != self._slider().get("volume"):
            log.info("amplifier %.1f dB -> Spotify %d %%", db, pct)
            await self._set_spotify(pct)


    # ---- background: amplifier poll
    async def amp_loop(self):
        while True:
            try:
                if self.enabled() and self._active() and time.time() - self._active_t > SETTLE_S:
                    await self.sync_from_amp()
            except Exception:
                log.exception("volume bridge poll")
            await asyncio.sleep(POLL_S)
