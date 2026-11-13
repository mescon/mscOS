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

    options = (
        {"key": "arcam_host", "type": "text", "label": {"sv": "Adress", "en": "Address"}, "placeholder": "192.168.1.176",
         "help": {"sv": "Receiverns IP-adress. IP-styrning ska vara aktiverad i dess meny.", "en": "The receiver's IP address. IP control must be enabled in its menu."}},
        {"key": "arcam_max_vol", "type": "number", "min": 0, "max": 99, "step": 1,
         "label": {"sv": "Högsta volym från panelen", "en": "Highest volume from the panel"}},
    )

    def features(self) -> dict:
        return {"arcam": bool(self.host)}

    def status(self) -> dict | None:
        if not self.host:
            return {"state": "warn", "text": {"sv": "Ange receiverns adress", "en": "Enter the receiver's address"}}
        return None

    def panel(self) -> dict | None:
        if not self.host:
            return None
        a, title = self.state, {"sv": "Arcam", "en": "Arcam"}
        if a.get("reachable") is None:  # read when the card is opened: the receiver takes one connection at a time
            return {"title": title, "lead": {"sv": "Hämtar läget från receivern…", "en": "Asking the receiver…"}, "controls": []}
        if a.get("reachable") is False:
            return {"title": title, "summary": {"sv": "Svarar inte", "en": "No answer"}, "warn": True, "controls": [],
                    "lead": {"sv": "Receivern svarar inte. Kontrollera att den är på nätverket och att IP-styrning är aktiverad.",
                             "en": "The receiver does not answer. Check that it is on the network and that IP control is enabled."}}
        on, src = bool(a.get("power")), a.get("source")
        if not on:
            return {"title": title, "summary": {"sv": "I standby", "en": "In standby"},
                    "controls": [{"type": "button", "name": "power", "value": True, "label": {"sv": "Slå på", "en": "Turn on"}}]}
        return {"title": title, "summary": {"sv": f"På, {src}", "en": f"On, {src}"}, "controls": [
            {"type": "button", "name": "power", "value": False, "label": {"sv": "Stäng av", "en": "Turn off"}},
            {"type": "button", "name": "mute", "value": not a.get("muted"), "pressed": bool(a.get("muted")),
             "label": {"sv": "Ljud av", "en": "Mute"}},
            {"type": "range", "name": "volume", "label": {"sv": "Volym", "en": "Volume"}, "min": 0,
             "max": int(self.hub.settings.get("arcam_max_vol", 50)), "step": 1, "value": a.get("volume")},
            {"type": "choice", "name": "source", "label": {"sv": "Källa", "en": "Source"}, "value": src,
             "options": [[s, s] for s in SOURCE_RC5]}]}

    async def panel_action(self, name: str, value) -> None:
        if name != "refresh" and name not in ("power", "mute", "volume", "source"):
            raise RuntimeError(T("Okänt reglage"))
        await self._act({} if name == "refresh" else {name: value})

    async def _act(self, b: dict) -> None:
        hub = self.hub
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
            if b:
                await asyncio.sleep(0.6 if "power" not in b else 3)
            self.state = await self.dev.status()
        except ArcamError as e:
            raise RuntimeError(f"Arcam: {e}")
        hub.panel_changed(self)

    def routes(self, routes) -> None:
        @routes.post("/api/arcam")
        async def arcam_action(request):
            hub = self.hub
            if not self.host:
                raise web.HTTPNotFound(text=T("Arcams adress är inte inställd."))
            try:
                await self._act(await hub.body(request))
            except RuntimeError as e:
                raise web.HTTPBadGateway(text=str(e))
            return hub.ok(arcam=self.state)


PLUGIN = ArcamPlugin
