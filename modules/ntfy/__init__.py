"""Alerts to a phone through ntfy (ntfy.sh, or a server of your own).

The core's alerts (the DAC gone, a source stopped, dropouts, a verification that differs)
are published to one ntfy topic; the ntfy app on the phone subscribes to it. The topic
address works like a password (anyone who knows it can read and write the topic), so it is
kept as a secret: stored root-only on the device and never shown again.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout

from plugins import Plugin

log = logging.getLogger("ntfy")


class Ntfy(Plugin):
    id = "ntfy"
    kind = "notify"
    name = {"sv": "ntfy", "en": "ntfy"}
    description = {"sv": "Larm till telefonen via ntfy-appen. Kräver inget konto: välj ett svårgissat ämne.",
                   "en": "Alerts to the phone through the ntfy app. No account needed: choose a topic that is hard to guess."}
    options = (
        {"key": "ntfy_url", "type": "secret", "label": {"sv": "Ämnets adress", "en": "Topic address"},
         "placeholder": "https://ntfy.sh/my-topic-4f9a2c",
         "help": {"sv": "Prenumerera på samma ämne i ntfy-appen. Adressen fungerar som ett lösenord och visas inte igen.",
                  "en": "Subscribe to the same topic in the ntfy app. The address works like a password and is not shown again."}},
    )

    def __init__(self, hub):
        super().__init__(hub)
        self.http: ClientSession | None = None

    def _target(self) -> tuple[str, str] | None:
        u = urlsplit(self.hub.secret("ntfy_url"))
        topic = u.path.strip("/")
        if u.scheme not in ("http", "https") or not u.netloc or not topic or "/" in topic:
            return None
        return f"{u.scheme}://{u.netloc}/", topic

    def can_notify(self) -> bool:
        return self._target() is not None

    def status(self) -> dict | None:
        if self.hub.secret("ntfy_url") and not self.can_notify():
            return {"state": "warn", "text": {"sv": "Adressen ska se ut som https://ntfy.sh/ämne", "en": "The address should look like https://ntfy.sh/topic"}}
        if not self.can_notify():
            return {"state": "warn", "text": {"sv": "Ange ämnets adress", "en": "Enter the topic address"}}
        return {"state": "ok", "text": {"sv": "Inställt", "en": "Set up"}}

    async def notify(self, kind: str, title: str, message: str) -> bool:
        t = self._target()
        if t is None:
            return False
        try:
            if self.http is None:
                self.http = ClientSession(timeout=ClientTimeout(total=10))
            async with self.http.post(t[0], json={"topic": t[1], "title": title, "message": message, "tags": ["loud_sound"]}) as r:
                return r.status < 300
        except Exception as e:  # noqa: BLE001 - never let alerting break sampling; never log the address
            log.warning("alert %s failed: %s", kind, type(e).__name__)
            return False


PLUGIN = Ntfy
