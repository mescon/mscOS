"""Home Assistant: a compact state API for REST sensors, and alerts as phone notifications.

  GET /api/ha   one cheap snapshot (in-memory state only): source, what plays and its
                measured format, the DAC, and what the other modules add (amp, mode)
  alerts        the core's alerts (DAC gone, Soloist stopped, dropouts, ...) are posted to
                a Home Assistant webhook, which an automation turns into a notification

The webhook address is a secret: it is read from HA_WEBHOOK_URL in the root-only
/etc/sotm-web.env, never stored in the settings (those are shown to the panel).
Playback control for HA uses the core's own API (/api/soloist/command, with a Spotify URI).
"""
from __future__ import annotations

import logging
import os

from aiohttp import ClientSession, ClientTimeout, web

from plugins import Plugin

log = logging.getLogger("homeassistant")


class HomeAssistant(Plugin):
    id = "homeassistant"
    kind = "integration"
    name = {"sv": "Home Assistant", "en": "Home Assistant"}
    description = {"sv": "Tillstånd för sensorer (/api/ha) och larm till telefonen via en webhook.",
                   "en": "State for sensors (/api/ha) and alerts to the phone through a webhook."}

    def __init__(self, hub):
        super().__init__(hub)
        self.webhook = os.environ.get("HA_WEBHOOK_URL", "")
        self.http: ClientSession | None = None

    def features(self) -> dict:
        return {"ha_alerts": bool(self.webhook)}

    async def notify(self, kind: str, title: str, message: str) -> bool:
        if not self.webhook:
            return False
        try:
            if self.http is None:
                self.http = ClientSession(timeout=ClientTimeout(total=10))
            async with self.http.post(self.webhook, json={"title": title, "message": message, "tag": f"sotm-{kind}"}) as r:
                return r.status < 300
        except Exception as e:  # never let alerting break sampling; never log the address
            log.warning("alert %s failed: %s", kind, type(e).__name__)
            return False

    def routes(self, routes) -> None:
        @routes.get("/api/ha")
        async def ha_summary(request):
            return web.json_response(self.hub.summary())


PLUGIN = HomeAssistant
