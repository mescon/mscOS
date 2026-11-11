"""Home Assistant: a compact state API for REST sensors, and alerts as phone notifications.

  GET /api/ha   one cheap snapshot (in-memory state only): source, what plays and its
                measured format, the DAC, and what the other modules add (amp, mode)
  alerts        the core's alerts (DAC gone, Soloist stopped, dropouts, ...) are posted to
                a Home Assistant webhook, which an automation turns into a notification

The webhook address is a secret: entered in the panel it is kept in the root-only secrets
file, never in the settings (those are shown to the panel). HA_WEBHOOK_URL in
/etc/mscos/panel.env is used when none is entered (installs from before it could be).
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

    options = (
        {"key": "ha_webhook", "type": "secret", "label": {"sv": "Webhook-adress för larm", "en": "Webhook address for alerts"},
         "placeholder": "http://homeassistant.local:8123/api/webhook/…",
         "help": {"sv": "En automation i Home Assistant med webhook-utlösare gör larmet till en avisering. Den får title, message och tag. Adressen visas inte igen.",
                  "en": "An automation in Home Assistant with a webhook trigger turns the alert into a notification. It receives title, message and tag. The address is not shown again."}},
    )

    def __init__(self, hub):
        super().__init__(hub)
        self.http: ClientSession | None = None

    @property
    def webhook(self) -> str:
        return self.hub.secret("ha_webhook") or os.environ.get("HA_WEBHOOK_URL", "")

    def can_notify(self) -> bool:
        return self.webhook.startswith(("http://", "https://"))

    def status(self) -> dict | None:
        return {"state": "ok", "text": {"sv": "Larm skickas via webhook", "en": "Alerts go through the webhook"}} if self.can_notify() \
            else {"state": "idle", "text": {"sv": "Ingen webhook för larm är angiven", "en": "No webhook for alerts is entered"}}

    async def notify(self, kind: str, title: str, message: str) -> bool:
        if not self.can_notify():
            return False
        try:
            if self.http is None:
                self.http = ClientSession(timeout=ClientTimeout(total=10))
            async with self.http.post(self.webhook, json={"title": title, "message": message, "tag": f"mscos-{kind}"}) as r:
                return r.status < 300
        except Exception as e:  # never let alerting break sampling; never log the address
            log.warning("alert %s failed: %s", kind, type(e).__name__)
            return False

    def routes(self, routes) -> None:
        @routes.get("/api/ha")
        async def ha_summary(request):
            return web.json_response(self.hub.summary())


PLUGIN = HomeAssistant
