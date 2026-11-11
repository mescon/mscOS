"""Spotify Connect without a key: go-librespot, an open-source Spotify Connect client.

No developer key and no setup: the device shows up in the Spotify app on the same network
(Zeroconf) and the listener's own Premium account logs in by choosing it. Spotify only
sends Ogg Vorbis to such clients, 320 kbit/s at most: not Lossless. For Lossless, use the
Spotify Lossless module (Soloist) with a key of your own instead.

go-librespot is a reimplementation of Spotify's protocol, not made or approved by Spotify.
The install script downloads its release from GitHub on the device. It plays through
PipeWire like Soloist, at 44.1 kHz, and by default leaves the samples alone: the Spotify
volume slider does nothing and the volume is set on the amplifier ("fixed"); "software"
lets the slider scale the samples instead.

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
    label = "Spotify"
    protocol = "Spotify Connect"
    name = {"sv": "Spotify Connect (librespot)", "en": "Spotify Connect (librespot)"}
    description = {"sv": "Spotify Connect utan nyckel: välj enheten i Spotify-appen. Ogg Vorbis 320 kbit/s, inte Lossless. Kräver Premium.",
                   "en": "Spotify Connect without a key: choose the device in the Spotify app. Ogg Vorbis 320 kbit/s, not Lossless. Needs Premium."}
    user_units = ("librespot.service",)
    hint = {"sv": "Välj {name} i Spotify-appen", "en": "Choose {name} in the Spotify app"}
    terms = {"sv": "go-librespot hämtas från GitHub till den här enheten. Det är ett fristående program med öppen källkod, inte gjort eller godkänt av Spotify, och spelar högst 320 kbit/s.",
             "en": "go-librespot is downloaded from GitHub to this device. It is an independent open-source program, not made or approved by Spotify, and plays 320 kbit/s at most."}
    terms_url = "https://github.com/devgianlu/go-librespot"
    defaults = {"librespot_name": "", "librespot_bitrate": 320, "librespot_volume": "fixed"}

    def __init__(self, hub):
        super().__init__(hub)
        self.status: dict | None = None   # GET /status of go-librespot, None: no session
        self.http: ClientSession | None = None

    # ---- configuration (go-librespot reads config.yml at start)
    def device_name(self) -> str:
        return self.hub.settings.get("librespot_name") or self.hub.names()["output"]

    def config(self) -> str:
        s = self.hub.settings
        soft = s.get("librespot_volume") == "software"
        avahi = shutil.which("avahi-daemon") is not None
        q = json.dumps  # a JSON string is a valid YAML string
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
            f"external_volume: {'false' if soft else 'true'}",
            "initial_volume: 100",
            "ignore_last_volume: true",
            "server:",
            "  enabled: true",
            "  address: 127.0.0.1",
            "  port: 3678",
            "",
        ])

    def write_config(self) -> bool:
        """Write config.yml if it differs; True if it changed."""
        path = f"{CONF_DIR}/config.yml"
        text = self.config()
        if collect.read(path) == text:
            return False
        import pwd
        pw = pwd.getpwnam(collect.SVC_USER)
        os.makedirs(CONF_DIR, exist_ok=True)
        os.chown(CONF_DIR, pw.pw_uid, pw.pw_gid)
        with open(path + ".tmp", "w") as f:
            f.write(text)
        os.chown(path + ".tmp", pw.pw_uid, pw.pw_gid)
        os.replace(path + ".tmp", path)
        return True

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
        if await self.installed() and await asyncio.to_thread(self.write_config) and await self.active():
            await collect.run(["systemctl", "--user", "restart", "librespot.service"], as_user=True, timeout=30)

    # ---- source
    async def installed(self) -> bool:
        return os.path.exists(BIN) and os.path.exists(UNIT)

    async def start(self) -> str:
        await asyncio.to_thread(self.write_config)
        return await super().start()

    async def stop(self) -> str:
        self.status = None
        return await super().stop()

    async def playing(self) -> bool | None:
        st = self.status
        return bool(st and st.get("track") and not st.get("paused") and not st.get("stopped"))

    def now_playing(self) -> dict | None:
        st = self.status
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
        return {"device_name": self.device_name(), "session": self.status is not None}

    # ---- follow go-librespot while it is the source
    async def _fetch(self) -> None:
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=4))
        async with self.http.get(f"http://{API}/status") as r:
            self.status = await r.json() if r.status == 200 else None
        self._at = time.time()
        self.hub.source_changed(self)

    async def _follow(self):
        while True:
            if self.hub.settings.get("source") != self.id:
                self.status = None
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
            if self.status is not None:
                self.status = None
                self.hub.source_changed(self)
            await asyncio.sleep(4)

    def tasks(self) -> list:
        return [self._follow()]


PLUGIN = Librespot
