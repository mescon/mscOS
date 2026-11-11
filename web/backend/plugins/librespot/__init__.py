"""Spotify Connect without a key: go-librespot, an open-source Spotify Connect client.

No developer key and no setup: the device shows up in the Spotify app on the same network
(Zeroconf) and the listener's own Premium account logs in by choosing it. Spotify only
sends Ogg Vorbis to such clients, 320 kbit/s at most: not Lossless. For Lossless, use the
Spotify Lossless module (Soloist) with a key of your own instead.

go-librespot is a reimplementation of Spotify's protocol, not made or approved by Spotify.
The install script downloads its release from GitHub on the device. It plays through
PipeWire like Soloist, at 44.1 kHz.

Volume, three ways:
  amp       with an amplifier module and the bridge setting on: go-librespot runs with
            external volume (it reports the slider, the samples pass untouched) and the
            core's volume bridge moves the amplifier, as with Soloist
  software  no amplifier module: the Spotify slider scales the samples (the default)
  fixed     the slider does nothing, the samples pass untouched, volume is set elsewhere

Its local API (127.0.0.1:3678) gives the panel what plays: GET /status and a WebSocket
with events.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time

from aiohttp import ClientSession, ClientTimeout, web
import websockets

import collect
from i18n import T
from plugins import SourcePlugin

log = logging.getLogger("librespot")
BIN = "/usr/local/bin/go-librespot"
API = "127.0.0.1:3678"
CONF_DIR = f"{collect.AUDIO_HOME}/.config/go-librespot"
UNIT = f"{collect.AUDIO_HOME}/.config/systemd/user/librespot.service"


class Librespot(SourcePlugin):
    id = "librespot"
    label = "Spotify Connect"
    protocol = "Spotify Connect"
    name = {"sv": "Spotify Connect (librespot)", "en": "Spotify Connect (librespot)"}
    description = {"sv": "Spotify Connect utan nyckel: välj enheten i Spotify-appen. Ogg Vorbis 320 kbit/s, inte Lossless. Kräver Premium.",
                   "en": "Spotify Connect without a key: choose the device in the Spotify app. Ogg Vorbis 320 kbit/s, not Lossless. Needs Premium."}
    user_units = ("librespot.service",)
    hint = {"sv": "Välj {name} i Spotify-appen", "en": "Choose {name} in the Spotify app"}
    terms = {"sv": "go-librespot hämtas från GitHub till den här enheten. Det är ett fristående program med öppen källkod, inte gjort eller godkänt av Spotify, och spelar högst 320 kbit/s.",
             "en": "go-librespot is downloaded from GitHub to this device. It is an independent open-source program, not made or approved by Spotify, and plays 320 kbit/s at most."}
    terms_url = "https://github.com/devgianlu/go-librespot"
    has_slider = True
    defaults = {"librespot_name": "", "librespot_bitrate": 320, "librespot_volume": "software"}
    options = (
        {"key": "librespot_name", "type": "text", "label": {"sv": "Namn i Spotify-appen", "en": "Name in the Spotify app"},
         "help": {"sv": "Tomt: samma namn som det enheten spelar till.", "en": "Empty: the name of what this device plays into."}},
        {"key": "librespot_bitrate", "type": "select", "label": {"sv": "Kvalitet", "en": "Quality"},
         "options": [[320, {"sv": "320 kbit/s", "en": "320 kbit/s"}], [160, {"sv": "160 kbit/s", "en": "160 kbit/s"}],
                     [96, {"sv": "96 kbit/s", "en": "96 kbit/s"}]]},
        {"key": "librespot_volume", "type": "select",
         "label": {"sv": "Volym utan förstärkarmodul", "en": "Volume without an amplifier module"},
         "options": [["software", {"sv": "Spotify-reglaget (digital volym)", "en": "The Spotify slider (digital volume)"}],
                     ["fixed", {"sv": "Fast: reglaget gör inget", "en": "Fixed: the slider does nothing"}]],
         "help": {"sv": "Med en förstärkarmodul styr Spotify-reglaget förstärkaren och samplen lämnas orörda; det ställs in under Förstärkaren. Det här valet gäller annars.",
                  "en": "With an amplifier module the Spotify slider drives the amplifier and the samples are left untouched; that is set under Amplifier. This choice applies otherwise."}},
    )

    def __init__(self, hub):
        super().__init__(hub)
        self.playstate: dict | None = None   # GET /status of go-librespot, None: no session
        self.http: ClientSession | None = None
        self._was_active = False
        self._vol: int | None = None

    # ---- volume
    def volume_mode(self) -> str:
        s = self.hub.settings
        if self.hub.amp is not None and s.get("spotify_vol_to_amp", True):
            return "amp"
        return "fixed" if s.get("librespot_volume") == "fixed" else "software"

    def slider(self) -> dict | None:
        mode = self.volume_mode()
        if mode == "fixed":
            return None
        st = self.playstate
        vol = round(st["volume"] * 100 / st["volume_steps"]) if st and st.get("volume_steps") and st.get("volume") is not None else None
        return {"active": bool(st) and not st.get("stopped"), "volume": vol, "digital": mode == "software"}

    async def set_slider(self, pct: int) -> None:
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=4))
        async with self.http.post(f"http://{API}/player/volume", json={"volume": max(0, min(100, int(pct)))}) as r:
            await r.read()

    def _bridge(self) -> None:
        """Tell the volume bridge what the slider did since the last look."""
        sl = self.slider() or {}
        active, vol = bool(sl.get("active")), sl.get("volume")
        if active and not self._was_active:
            self.hub.vol.on_active()  # the slider is set from the amplifier, not the other way
        elif active and vol is not None and vol != self._vol:
            self.hub.vol.on_volume_changed(vol)
        self._was_active, self._vol = active, vol

    # ---- configuration (go-librespot reads config.yml at start)
    def device_name(self) -> str:
        return self.hub.settings.get("librespot_name") or self.hub.names()["output"]

    def config(self) -> str:
        s = self.hub.settings
        mode = self.volume_mode()
        avahi = shutil.which("avahi-daemon") is not None
        q = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731 - a JSON string is a valid YAML string
        return "\n".join([
            "# Written by the mscOS panel (module Spotify Connect). Changes here are overwritten.",
            f"device_name: {q(self.device_name())}",
            "device_type: speaker",
            "zeroconf_enabled: true",
            f"zeroconf_backend: {'avahi' if avahi else 'builtin'}",
            "credentials:",
            "  type: zeroconf",
            "  zeroconf:",
            "    persist_credentials: true",
            # through PipeWire (pipewire-alsa): the same 44.1 kHz path to the DAC as Soloist
            "audio_backend: alsa",
            "audio_device: pipewire",
            f"bitrate: {int(s.get('librespot_bitrate', 320))}",
            "normalisation_disabled: true",
            "volume_steps: 100",
            # amp: the slider is only reported, and starts low until the bridge has set it from
            # the amplifier. fixed: full scale, untouched. software: go-librespot scales, and
            # starts where it was left, at a modest level the first time.
            f"external_volume: {'false' if mode == 'software' else 'true'}",
            f"initial_volume: {dict(amp=0, fixed=100, software=30)[mode]}",
            f"ignore_last_volume: {'false' if mode == 'software' else 'true'}",
            "server:",
            "  enabled: true",
            "  address: 127.0.0.1",
            "  port: 3678",
            "",
        ])

    async def write_config(self) -> bool:
        """Write config.yml if it differs; True if it changed. Written as the audio user, never
        as root: the directory is theirs, and root must not follow whatever they put there."""
        script = ('umask 077; mkdir -p "$1" && cat > "$1/config.yml.tmp" || exit 1; '
                  'if cmp -s "$1/config.yml.tmp" "$1/config.yml"; then rm -f "$1/config.yml.tmp"; exit 3; fi; '
                  'mv -f "$1/config.yml.tmp" "$1/config.yml"')
        rc, _, err = await collect.run(["sh", "-c", script, "sh", CONF_DIR], as_user=True, timeout=10,
                                       input_=self.config().encode())
        if rc not in (0, 3):
            log.warning("config.yml: %s", err.strip())
        return rc == 0

    def update_settings(self, b: dict, upd: dict) -> None:
        if "librespot_name" in b:
            v = re.sub(r"\s+", " ", str(b["librespot_name"])).strip()
            if len(v) > 40 or any(ord(c) < 32 for c in v):
                raise web.HTTPBadRequest(text=T("Namnet får vara högst 40 tecken."))
            upd["librespot_name"] = v
        if "librespot_bitrate" in b:
            if b["librespot_bitrate"] not in (96, 160, 320):
                raise web.HTTPBadRequest(text=T("Välj 96, 160 eller 320 kbit/s"))
            upd["librespot_bitrate"] = b["librespot_bitrate"]
        if "librespot_volume" in b:
            if b["librespot_volume"] not in ("fixed", "software"):
                raise web.HTTPBadRequest(text=T("Okänt volymläge"))
            upd["librespot_volume"] = b["librespot_volume"]

    async def settings_applied(self, upd: dict) -> None:
        if await self.installed() and await self.write_config() and await self.active():
            await self._restart()

    async def _restart(self) -> None:
        await collect.run(["systemctl", "--user", "restart", "librespot.service"], as_user=True, timeout=30)

    # ---- source
    async def installed(self) -> bool:
        return os.path.exists(BIN) and os.path.exists(UNIT)

    async def version(self) -> str | None:
        return self.recorded_version()  # go-librespot cannot tell its own

    async def latest(self) -> str | None:
        """The tag GitHub's "latest release" link leads to."""
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=4))
        async with self.http.get("https://github.com/devgianlu/go-librespot/releases/latest", allow_redirects=False,
                                 timeout=ClientTimeout(total=15)) as r:
            m = re.search(r"/releases/tag/([\w.+-]+)$", r.headers.get("Location", ""))
        return m.group(1) if m else None

    async def start(self) -> str:
        if await self.write_config() and await self.active():
            await self._restart()  # already running with an older configuration
        return await super().start()

    async def stop(self) -> str:
        self.playstate, self._was_active = None, False
        return await super().stop()

    async def playing(self) -> bool | None:
        st = self.playstate
        return bool(st and st.get("track") and not st.get("paused") and not st.get("stopped"))

    def now_playing(self) -> dict | None:
        st = self.playstate
        t = (st or {}).get("track")
        if not t:
            return None
        return {"title": t.get("name"), "artist": ", ".join(t.get("artist_names") or []), "album": t.get("album_name"),
                "image": t.get("album_cover_url"), "uri": t.get("uri"),
                "playing": not st.get("paused") and not st.get("stopped"),
                "duration_ms": t.get("duration"), "position_ms": t.get("position"), "position_ts": getattr(self, "_at", None),
                "codec": t.get("codec") or "vorbis", "bitrate": t.get("bitrate") or self.hub.settings.get("librespot_bitrate"),
                "user": st.get("username")}

    async def state(self) -> dict:
        return {"device_name": self.device_name(), "session": self.playstate is not None}

    # ---- follow go-librespot while it is the source
    async def _fetch(self) -> None:
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=4))
        async with self.http.get(f"http://{API}/status") as r:
            self.playstate = await r.json() if r.status == 200 else None
        self._at = time.time()
        self._bridge()
        self.hub.source_changed(self)

    async def _follow(self):
        try:  # a new panel version or another set of modules may want another configuration
            if self.hub.settings.get("source") == self.id and await self.installed() \
                    and await self.write_config() and await self.active():
                await self._restart()
        except Exception:  # noqa: BLE001
            log.exception("configuration at start")
        while True:
            if self.hub.settings.get("source") != self.id:
                self.playstate = None
                await asyncio.sleep(5)
                continue
            try:
                async with websockets.connect(f"ws://{API}/events", open_timeout=4) as ws:
                    await self._fetch()
                    async for _ in ws:  # every event (playing, paused, metadata, ...): read the state again
                        await self._fetch()
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError, ValueError):
                pass
            except Exception:  # noqa: BLE001 - never let this loop die
                log.exception("following go-librespot")
            if self.playstate is not None:
                self.playstate = None
                self._bridge()
                self.hub.source_changed(self)
            await asyncio.sleep(4)

    def tasks(self) -> list:
        return [self._follow()]


PLUGIN = Librespot
