"""About the music: what Wikipedia, Discogs and MusicBrainz say about the artist, the album
and the song that is playing, shown under the sleeve in the Listen view.

The device asks those three services by the names of what is playing, which tells them what
is listened to here: that is why this is a module to switch on, and off by default. Answers
are kept on the device, so each artist, album and song is asked about once."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time

from aiohttp import ClientSession, ClientTimeout, web

import collect
from plugins import Plugin

from . import lookup

log = logging.getLogger("about")
KEEP, KEEP_EMPTY, MOST = 60 * 86400, 2 * 86400, 600   # seconds found / not found is kept; files


class About(Plugin):
    id = "about"
    name = {"sv": "Om musiken", "en": "About the music"}
    description = {"sv": "Text om artisten, albumet och låten som spelas, från Wikipedia, Discogs och MusicBrainz. Enheten frågar de tjänsterna om det som spelas.",
                   "en": "Notes on the artist, the album and the song that is playing, from Wikipedia, Discogs and MusicBrainz. The device asks those services about what is playing."}
    options = ({"key": "about_discogs_token", "type": "secret",
                "label": {"sv": "Discogs-token (valfritt)", "en": "Discogs token (optional)"},
                "help": {"sv": "Behövs inte. Med en egen token får enheten fråga Discogs oftare.",
                         "en": "Not needed. With a token of your own the device may ask Discogs more often."},
                "link": {"url": "https://www.discogs.com/settings/developers", "label": {"sv": "Skapa en token hos Discogs", "en": "Create a token at Discogs"}}},)

    def __init__(self, hub):
        super().__init__(hub)
        self.dir = os.path.join(collect.STATE_DIR, "about")
        self.http: ClientSession | None = None
        self.busy: dict[str, asyncio.Task] = {}

    # ---- kept answers
    def _file(self, kind: str, *names: str) -> str:
        key = "\0".join(" ".join(n.casefold().split()) for n in names)
        return os.path.join(self.dir, f"{kind}-{hashlib.sha1(key.encode()).hexdigest()[:20]}.json")

    def _kept(self, path: str):
        """(found, value): found is False when nothing usable is kept."""
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - d["at"] < (KEEP if d["value"] else KEEP_EMPTY):
                return True, d["value"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return False, None

    def _keep(self, path: str, value) -> None:
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(path + ".new", "w", encoding="utf-8") as f:
                json.dump({"at": int(time.time()), "value": value}, f, ensure_ascii=False)
            os.replace(path + ".new", path)
            names = sorted((n for n in os.listdir(self.dir) if n.endswith(".json")), key=lambda n: os.path.getmtime(os.path.join(self.dir, n)))
            for n in names[:-MOST]:
                os.unlink(os.path.join(self.dir, n))
        except OSError as e:
            log.warning("keeping an answer: %s", e)

    async def _one(self, path: str, ask):
        """The kept answer, or ask() and keep what it says. One question at a time per subject."""
        found, value = self._kept(path)
        if found:
            return value
        if path not in self.busy:
            async def run():
                try:
                    v = await ask()
                    await asyncio.to_thread(self._keep, path, v)
                    return v
                finally:
                    self.busy.pop(path, None)
            self.busy[path] = asyncio.create_task(run())
        return await asyncio.shield(self.busy[path])

    async def about(self, artist: str, album: str, title: str, lang: str) -> dict:
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=15))
        langs = tuple(dict.fromkeys((lang, "en")))
        token = self.hub.secret("about_discogs_token") or ""
        out = {"artist": None, "album": None, "track": None}
        try:
            async def find_artist():
                # "A, B" is several artists on one track: the first of them, when the whole is nobody's name
                found = await lookup.artist(self.http, artist, langs, token)
                first = artist.split(", ")[0]
                return found or (await lookup.artist(self.http, first, langs, token) if first != artist else None)
            a = out["artist"] = await self._one(self._file("artist", lang, artist), find_artist)
            jobs = {}
            if a and album:
                jobs["album"] = self._one(self._file("album", lang, artist, album), lambda: lookup.album(self.http, a["mbid"], artist, album, langs, token))
            if title:
                jobs["track"] = self._one(self._file("track", lang, artist, title), lambda: lookup.track(self.http, a["name"] if a else artist, title, langs))
            for k, v in zip(jobs, await asyncio.gather(*jobs.values(), return_exceptions=True)):
                if isinstance(v, lookup.Unavailable):
                    out["unavailable"] = True
                elif isinstance(v, Exception):
                    log.warning("%s: %r", k, v)
                else:
                    out[k] = v
        except lookup.Unavailable:
            out["unavailable"] = True
        return out

    def routes(self, routes) -> None:
        @routes.post("/api/about")
        async def about(request):
            try:
                b = await request.json()
                artist, album, title = (" ".join(str(b.get(k) or "").split())[:200] for k in ("artist", "album", "title"))
                lang = str(b.get("lang") or "en")
            except (ValueError, AttributeError):
                raise web.HTTPBadRequest()
            if not artist or lang not in ("sv", "en"):
                raise web.HTTPBadRequest()
            return web.json_response(await self.about(artist, album, title, lang))


PLUGIN = About
