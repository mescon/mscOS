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
import re
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
    description = {"sv": "Text om artisten, albumet och låten som spelas, vilka som medverkar, och låttexten i takt med musiken. Från Wikipedia, MusicBrainz, Wikidata, Discogs, Last.fm och LRCLIB. Enheten frågar de tjänsterna om det som spelas.",
                   "en": "Notes on the artist, the album and the song that is playing, who plays on it, and the words in time with the music. From Wikipedia, MusicBrainz, Wikidata, Discogs, Last.fm and LRCLIB. The device asks those services about what is playing."}
    defaults = {"about_lyrics": False}
    options = ({"key": "about_lyrics", "type": "toggle", "label": {"sv": "Visa låttexten", "en": "Show the words of the song"},
                "help": {"sv": "Texten hämtas från LRCLIB, en öppen databas som användare fyller på, och följer med i musiken när den är tidssatt.",
                         "en": "The words come from LRCLIB, an open database kept by its users, and follow the music when they are timed."}},
               {"key": "about_lastfm_key", "type": "secret", "label": {"sv": "Last.fm API-nyckel (valfritt)", "en": "Last.fm API key (optional)"},
                "help": {"sv": "Med en egen nyckel blir Last.fm en källa till texterna, ofta den enda för mindre kända artister.",
                         "en": "With a key of your own Last.fm becomes a source for the notes, often the only one for lesser-known artists."},
                "link": {"url": "https://www.last.fm/api/account/create", "label": {"sv": "Skapa en nyckel hos Last.fm", "en": "Create a key at Last.fm"}}},
               {"key": "about_discogs_token", "type": "secret",
                "label": {"sv": "Discogs-token (valfritt)", "en": "Discogs token (optional)"},
                "help": {"sv": "Behövs inte. Med en egen token får enheten fråga Discogs oftare.",
                         "en": "Not needed. With a token of your own the device may ask Discogs more often."},
                "link": {"url": "https://www.discogs.com/settings/developers", "label": {"sv": "Skapa en token hos Discogs", "en": "Create a token at Discogs"}}},)

    def update_settings(self, b: dict, upd: dict) -> None:
        if "about_lyrics" in b:
            upd["about_lyrics"] = bool(b["about_lyrics"])

    def __init__(self, hub):
        super().__init__(hub)
        self.dir = os.path.join(collect.STATE_DIR, "about")
        self.http: ClientSession | None = None
        self.busy: dict[str, asyncio.Task] = {}

    # ---- kept answers
    def _file(self, kind: str, *names: str) -> str:
        key = "\0".join(" ".join(n.casefold().split()) for n in names)
        return os.path.join(self.dir, f"{kind}6-{hashlib.sha1(key.encode()).hexdigest()[:20]}.json")   # 2: with pictures

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

    async def about(self, artist: str, album: str, title: str, lang: str, spotify_artist: str = "") -> dict:
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=15))
        langs = tuple(dict.fromkeys((lang, "en")))
        token = self.hub.secret("about_discogs_token") or ""
        lfm = self.hub.secret("about_lastfm_key") or ""
        src = "l" if lfm else ""   # with a Last.fm key the answers hold more: kept apart from those without
        out = {"artist": None, "album": None, "track": None}
        try:
            async def who():
                # "A, B" is several artists on one track: the first of them, when the whole is nobody's name
                first = artist.split(", ")[0]
                return await lookup.find_artist(self.http, artist, album, title, spotify_artist) \
                    or (await lookup.find_artist(self.http, first, album, title) if first != artist else "")
            # Which artist: by the player's id for it when there is one, else by the name together
            # with the album (several artists share a name; an album tells them apart).
            mbid = await self._one(self._file("who", spotify_artist) if spotify_artist else self._file("who", artist, album), who)
            a = out["artist"] = await self._one(self._file("artist" + src, lang, mbid), lambda: lookup.artist(self.http, mbid, langs, token, lfm)) if mbid else None
            jobs = {}
            if a and album:
                jobs["album"] = self._one(self._file("album" + src, lang, mbid, album), lambda: lookup.album(self.http, a["mbid"], a["name"], album, langs, token, lfm))
            if title:
                jobs["track"] = self._one(self._file("track" + src, lang, artist, title), lambda: lookup.track(self.http, a["name"] if a else artist, title, langs, lfm))
            for k, v in zip(jobs, await asyncio.gather(*jobs.values(), return_exceptions=True)):
                if isinstance(v, lookup.Unavailable):
                    out["unavailable"] = True
                elif isinstance(v, Exception):
                    log.warning("%s: %r", k, v)
                else:
                    out[k] = v
        except lookup.Unavailable:
            out["unavailable"] = True
        # a title track's search can land on the album's own article: that text is said once, as the album's
        al, tr = out["album"], out["track"]
        if al and tr:
            theirs = {t["url"] for t in al.get("texts", [])}
            tr["texts"] = [t for t in tr.get("texts", []) if t["url"] not in theirs]
            if (tr.get("review") or {}).get("url") in theirs:
                tr.pop("review", None)
        # who plays on this track: MusicBrainz credits each recording of the album's release
        if al:
            al = out["album"] = dict(al)
            mine = (al.pop("track_credits", None) or {}).get(lookup._fold(lookup.plain(title)))
            if mine and title:
                tr = out["track"] = dict(tr or {"name": lookup.plain(title), "texts": [], "facts": [], "links": []})
                tr["credits"] = mine["credits"]
        if tr is not None and not (tr.get("texts") or tr.get("credits") or tr.get("review")):
            out["track"] = None
        return out

    async def lyrics(self, artist: str, title: str, album: str, duration_ms: int) -> dict:
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=15))
        v = await self._one(self._file("words", artist, title, album), lambda: lookup.lyrics(self.http, artist, title, album, duration_ms))
        return v or {"synced": [], "plain": "", "instrumental": False, "none": True}

    def routes(self, routes) -> None:
        @routes.post("/api/about")
        async def about(request):
            try:
                b = await request.json()
                artist, album, title = (" ".join(str(b.get(k) or "").split())[:200] for k in ("artist", "album", "title"))
                lang = str(b.get("lang") or "en")
                # the player's own id for the artist, when it has one ("spotify:artist:<id>")
                m = re.fullmatch(r"spotify:artist:([A-Za-z0-9]{22})", str(b.get("artist_uri") or ""))
            except (ValueError, AttributeError):
                raise web.HTTPBadRequest()
            if not artist or lang not in ("sv", "en"):
                raise web.HTTPBadRequest()
            return web.json_response(await self.about(artist, album, title, lang, m.group(1) if m else ""))

        @routes.post("/api/about/lyrics")
        async def lyrics(request):
            if not self.hub.settings.get("about_lyrics"):
                raise web.HTTPNotFound()
            try:
                b = await request.json()
                artist, album, title = (" ".join(str(b.get(k) or "").split())[:200] for k in ("artist", "album", "title"))
                dur = int(b.get("duration_ms") or 0)
            except (ValueError, AttributeError, TypeError):
                raise web.HTTPBadRequest()
            if not artist or not title or not 0 <= dur < 4 * 3600 * 1000:
                raise web.HTTPBadRequest()
            return web.json_response(await self.lyrics(artist.split(", ")[0], title, album, dur))


PLUGIN = About
