"""ListenBrainz: what is listened to here is sent to the listener's ListenBrainz account
(listenbrainz.org, the open listening diary of the MetaBrainz Foundation).

Every source's tracks go there, as the panel's own listening history has them: the track
that begins is announced as "playing now", and it becomes a listen once it has played for
half its length or four minutes, whichever comes first (ListenBrainz' own rule). A track
that is skipped before that is not sent. The account's token is kept root-only on the
device and goes nowhere but to ListenBrainz.
"""
from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import ClientSession, ClientTimeout

from plugins import Plugin

log = logging.getLogger("listenbrainz")
API = "https://api.listenbrainz.org/1/submit-listens"
SERVICE = {"spotify": "spotify.com", "librespot": "spotify.com"}   # where a source's music comes from, when that is one service


def listen_after(duration_ms) -> float:
    """Seconds a track must have played to count: half of it, four minutes at most."""
    half = (duration_ms or 0) / 2000
    return min(240.0, half) if half >= 15 else 240.0 if not duration_ms else 15.0


def payload(kind: str, e: dict) -> dict:
    info = {"media_player": "mscOS", "submission_client": "mscOS"}
    if e.get("duration_ms"):
        info["duration_ms"] = int(e["duration_ms"])
    if e.get("source") in SERVICE:
        info["music_service"] = SERVICE[e["source"]]
    meta = {"artist_name": e["artist"], "track_name": e["title"], "additional_info": info}
    if e.get("album"):
        meta["release_name"] = e["album"]
    item = {"track_metadata": meta}
    if kind == "single":
        item["listened_at"] = int(e["t"])
    return {"listen_type": kind, "payload": [item]}


class ListenBrainz(Plugin):
    id = "listenbrainz"
    name = {"sv": "ListenBrainz", "en": "ListenBrainz"}
    description = {"sv": "Skickar det som spelas här till ditt ListenBrainz-konto, från alla källor.",
                   "en": "Sends what is played here to your ListenBrainz account, from every source."}
    options = (
        {"key": "listenbrainz_token", "type": "secret", "needed": True, "guide": {"sv": ["Öppna [ListenBrainz](https://listenbrainz.org) och logga in. Inloggningen går via ett MusicBrainz-konto, som du skapar gratis på vägen om du inte har ett.", "Öppna [Settings](https://listenbrainz.org/settings/).", "Kopiera det som står under User token till fältet här och tryck Spara ändringar.", "Din token ger rätt att skriva lyssningar till ditt konto. Dela den inte."],
                   "en": ["Open [ListenBrainz](https://listenbrainz.org) and log in. The login goes by a MusicBrainz account, which you create for free on the way if you have none.", "Open [Settings](https://listenbrainz.org/settings/).", "Copy what is under User token into the field here and press Save changes.", "Your token gives leave to write listens to your account. Do not share it."]},
         "label": {"sv": "Användartoken", "en": "User token"},
         "help": {"sv": "Finns under Settings på listenbrainz.org. Den sparas bara på enheten och visas inte igen.",
                  "en": "Found under Settings at listenbrainz.org. It is kept on the device only and not shown again."},
         "link": {"url": "https://listenbrainz.org/settings/", "label": {"sv": "Hämta din token hos ListenBrainz", "en": "Get your token at ListenBrainz"}}},
    )

    def __init__(self, hub):
        super().__init__(hub)
        self.http: ClientSession | None = None

    async def _send(self, body: dict) -> bool:
        token = self.hub.secret("listenbrainz_token")
        if not token:
            return False
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=15))
        try:
            async with self.http.post(API, json=body, headers={"Authorization": f"Token {token}"}) as r:
                if r.status != 200:
                    log.warning("ListenBrainz said %s to a %s", r.status, body["listen_type"])
                return r.status == 200
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("ListenBrainz: %s", type(e).__name__)
            return False

    async def _count(self, entry: dict, wait: float) -> None:
        """Once the track has played long enough, and is still the one that plays: a listen."""
        await asyncio.sleep(max(0.0, wait))
        heard = self.hub.listened()
        if heard and heard[-1] is entry and not entry.get("lb") and await self._send(payload("single", entry)):
            entry["lb"] = 1

    async def on_listened(self, entry: dict) -> None:
        """A track began here (the hub's listening history). Announce it, and count it once it has played long enough."""
        if not entry.get("artist") or not entry.get("title") or not self.hub.secret("listenbrainz_token"):
            return
        # The one before, if it played long enough and was not counted (its own timer was
        # lost to a restart of the panel): the time between the two starts says so.
        heard = self.hub.listened()
        prev = heard[-2] if len(heard) > 1 and heard[-1] is entry else None
        if prev and not prev.get("lb") and prev.get("artist") and entry["t"] - prev["t"] >= listen_after(prev.get("duration_ms")) \
                and entry["t"] - prev["t"] < 6 * 3600 and await self._send(payload("single", prev)):
            prev["lb"] = 1
        await self._send(payload("playing_now", entry))
        await self._count(entry, listen_after(entry.get("duration_ms")))

    async def _resume(self):
        """The panel started while a track played: its timer starts again where it was."""
        await asyncio.sleep(20)   # the sources have said what plays by now
        heard = self.hub.listened()
        e = heard[-1] if heard else None
        if not e or e.get("lb") or not e.get("artist") or not self.hub.secret("listenbrainz_token"):
            return
        played = time.time() - e["t"]
        if played < (e.get("duration_ms") or 600000) / 1000:   # it can still be playing
            await self._count(e, listen_after(e.get("duration_ms")) - played)

    def tasks(self) -> list:
        return [self._resume()]


PLUGIN = ListenBrainz
