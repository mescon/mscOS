"""Arcam AV receivers over IP (port 50000): power, source, volume, mute.

Read only on demand (when its row in the panel is opened), never at start or in the
background: an AVR serves one TCP client at a time, and another controller holding the
connection (Home Assistant's arcam_fmj) is thrown off for ~5 s by every connection from here.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os

from aiohttp import web

from i18n import N_, T
from plugins import Plugin
from .driver import SOURCE_RC5, Arcam, ArcamError


class ArcamPlugin(Plugin):
    id = "arcam"
    kind = "integration"
    name = {"sv": "Arcam AV-receiver", "en": "Arcam AV receiver"}
    description = {"sv": "Ström, källa, volym och mute för en Arcam-receiver i nätverket.",
                   "en": "Power, source, volume and mute for an Arcam receiver on the network."}
    defaults = {"arcam_host": "", "arcam_max_vol": 50}

    def __init__(self, hub):
        super().__init__(hub)
        # older installs had the address in the environment (ARCAM_HOST)
        if not hub.settings.get("arcam_host") and os.environ.get("ARCAM_HOST"):
            hub.settings["arcam_host"] = os.environ["ARCAM_HOST"].strip()
        self.dev = Arcam(hub.settings.get("arcam_host") or "")
        self.state: dict = {"reachable": None}

    @property
    def host(self):
        return self.hub.settings.get("arcam_host") or None

    def update_settings(self, b: dict, upd: dict) -> None:
        if "arcam_max_vol" in b:
            upd["arcam_max_vol"] = self.hub.num(b["arcam_max_vol"], 0, 99, N_("Arcams volymtak"), integer=True)
        if "arcam_host" in b:
            h = str(b["arcam_host"]).strip()
            if h:
                try:
                    ipaddress.IPv4Address(h)
                except ValueError:
                    raise web.HTTPBadRequest(text=T("Ange Arcams IP-adress, till exempel 192.168.1.176."))
            upd["arcam_host"] = h

    async def settings_applied(self, upd: dict) -> None:
        if "arcam_host" in upd:
            self.dev = Arcam(upd["arcam_host"])
            self.state = {"reachable": None}

    def hello(self) -> dict:
        return {"arcam": self.state, "arcam_sources": list(SOURCE_RC5)}

    def features(self) -> dict:
        return {"arcam": bool(self.host)}

    def routes(self, routes) -> None:
        @routes.post("/api/arcam")
        async def arcam_action(request):
            hub = self.hub
            if not self.host:
                raise web.HTTPNotFound(text=T("Arcams adress är inte inställd."))
            b = await hub.body(request)
            try:
                if "power" in b:
                    await self.dev.set_power(bool(b["power"]))
                if "source" in b:
                    await self.dev.set_source(str(b["source"]))
                if "volume" in b:
                    # the panel never goes above the ceiling; the remote can
                    await self.dev.set_volume(min(hub.num(b["volume"], 0, 99, N_("Volym"), integer=True),
                                                  int(hub.settings.get("arcam_max_vol", 50))))
                if "mute" in b:
                    await self.dev.set_mute(bool(b["mute"]))
                await asyncio.sleep(0.6 if "power" not in b else 3)
                self.state = await self.dev.status()
            except ArcamError as e:
                raise web.HTTPBadGateway(text=f"Arcam: {e}")
            await hub.broadcast({"type": "arcam", "arcam": self.state})
            return hub.ok(arcam=self.state)


PLUGIN = ArcamPlugin
