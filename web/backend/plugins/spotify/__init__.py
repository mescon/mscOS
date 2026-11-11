"""Spotify Lossless through Soloist, Spotify's own Connect endpoint for developers.

Soloist needs a personal developer key from a Premium account; Spotify's terms say the key
may not be shared and the program may not be redistributed, so the install script downloads
it from Spotify on the device and the key is entered in the panel (System -> Spotify).
The Soloist relay itself (playback state, queue, measured format, the volume bridge) is
part of the core; this module is what puts it on the source switch.
"""
from __future__ import annotations

import os

from aiohttp import web

import collect
from plugins import SourcePlugin

BIN = "/usr/local/bin/soloist"


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

    async def command(self, cmd: str, **kw) -> None:
        h = self.hub
        send = lambda c, **a: h.soloist_cmd({"command": c, **a})  # noqa: E731
        try:
            if cmd == "toggle":
                await send("pause" if h.playback.get("status") == "playing" else "play")
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

    async def version(self) -> str | None:
        v = ((self.hub.status.get("soloist") or {}).get("version") or "").replace("soloist ", "")
        return v.split(" ")[0] or None

    async def installed(self) -> bool:
        return os.path.exists(BIN) and os.path.exists(collect.SOLOIST_UNIT)

    async def start(self) -> str:
        if not collect.api_key_info()["set"]:
            return ""  # installed but no key yet: nothing to start, the panel asks for the key
        return await super().start()

    async def names_changed(self) -> None:
        if await self.installed():
            await self.hub.set_soloist_name(self.hub.names()["network"])

    def slider(self) -> dict | None:
        h = self.hub
        return {"active": bool(h.soloist_ws and h.auth.get("is_active")), "volume": h.playback.get("volume"),
                "digital": not h.vol.enabled()}

    async def set_slider(self, pct: int) -> None:
        if self.hub.soloist_ws:
            await self.hub.soloist_cmd({"command": "set_volume", "volume": pct})

    async def state(self) -> dict:
        return {"key_set": collect.api_key_info()["set"]}


PLUGIN = Spotify
