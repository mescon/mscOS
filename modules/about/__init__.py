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
import threading
import time

from aiohttp import ClientSession, ClientTimeout, web

import collect
from plugins import Plugin

from . import lookup

log = logging.getLogger("about")
_KEEPING = threading.Lock()
KEEP, KEEP_EMPTY, MOST = 60 * 86400, 2 * 86400, 600   # seconds found / not found is kept; files


class About(Plugin):
    id = "about"
    name = {"sv": "Om musiken", "en": "About the music"}
    description = {"sv": "Text om artisten, albumet och låten som spelas, vilka som medverkar, och låttexten i takt med musiken. Ett omslag när spelaren saknar ett. Från Wikipedia, MusicBrainz, Cover Art Archive, Wikidata, Discogs, Last.fm och LRCLIB. Enheten frågar de tjänsterna om det som spelas.",
                   "en": "Notes on the artist, the album and the song that is playing, who plays on it, and the lyrics in time with the music. A sleeve when the player has none. From Wikipedia, MusicBrainz, the Cover Art Archive, Wikidata, Discogs, Last.fm and LRCLIB. The device asks those services about what is playing."}
    defaults = {"about_lyrics": False}
    options = ({"key": "about_lyrics", "type": "toggle", "label": {"sv": "Visa låttexten", "en": "Show the lyrics"},
                "help": {"sv": "Texten hämtas från LRCLIB, en öppen databas som användare fyller på, och följer med i musiken när den är tidssatt.",
                         "en": "The lyrics come from LRCLIB, an open database kept by its users, and follow the music when they are timed."}},
               {"key": "about_lastfm_key", "type": "secret", "guide": {"sv": ["Öppna [Last.fm: Create API account](https://www.last.fm/api/account/create) och logga in. Har du inget konto skapar du ett först, det är gratis.", "Skriv ett namn under Application name, till exempel mscOS. Övriga fält kan lämnas tomma.", "Tryck Submit.", "Kopiera API key (inte Shared secret) till fältet här och tryck Spara ändringar.", "Tappar du bort nyckeln finns den kvar under [dina API-konton hos Last.fm](https://www.last.fm/api/accounts)."],
                   "en": ["Open [Last.fm: Create API account](https://www.last.fm/api/account/create) and log in. If you have no account, create one first; it is free.", "Enter a name under Application name, mscOS for example. The other fields can be left empty.", "Press Submit.", "Copy the API key (not the Shared secret) into the field here and press Save changes.", "If you lose the key, it is still there under [your API accounts at Last.fm](https://www.last.fm/api/accounts)."]},
         "label": {"sv": "Last.fm API-nyckel (valfritt)", "en": "Last.fm API key (optional)"},
                "help": {"sv": "Med en egen nyckel blir Last.fm en källa till texterna, ofta den enda för mindre kända artister.",
                         "en": "With a key of your own Last.fm becomes a source for the notes, often the only one for lesser-known artists."},
                "link": {"url": "https://www.last.fm/api/account/create", "label": {"sv": "Skapa en nyckel hos Last.fm", "en": "Create a key at Last.fm"}}},
               {"key": "about_discogs_token", "type": "secret", "guide": {"sv": ["Öppna [Discogs: Developers](https://www.discogs.com/settings/developers) och logga in. Har du inget konto skapar du ett först, det är gratis.", "Tryck Generate new token.", "Kopiera din token till fältet här och tryck Spara ändringar."],
                   "en": ["Open [Discogs: Developers](https://www.discogs.com/settings/developers) and log in. If you have no account, create one first; it is free.", "Press Generate new token.", "Copy your token into the field here and press Save changes."]},
         
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
        # One at a time: the cards are fetched side by side, and two clearings-out of the oldest
        # files at once would each find the other's files gone.
        with _KEEPING:
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

    async def about(self, artist: str, album: str, title: str, lang: str, spotify_artist: str = "", part: str = "") -> dict:
        """The notes for one card (part: "artist", "album" or "track"), or for all three. The
        page asks for each by itself, so that each shows the moment it is ready; whichever way,
        the three are worked on at the same time."""
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=15))
        langs = tuple(dict.fromkeys((lang, "en")))
        token = self.hub.secret("about_discogs_token") or ""
        lfm = self.hub.secret("about_lastfm_key") or ""
        src = "l" if lfm else ""   # with a Last.fm key the answers hold more: kept apart from those without
        first = re.split(r", | / ", artist)[0]   # "A, B" and "A / B" are several artists on one track
        out: dict = {}

        async def who():
            return await lookup.spot(self.http, artist, album, title, spotify_artist) \
                or (await lookup.spot(self.http, first, album, title) if first != artist else {})

        async def get_artist(mbid):
            return await self._one(self._file("artist" + src, lang, mbid), lambda: lookup.artist(self.http, mbid, langs, token, lfm)) if mbid else None

        async def get_album(mbid):
            if not mbid or not album:
                return None
            return await self._one(self._file("album" + src, lang, mbid, album),
                                   lambda: lookup.album(self.http, mbid, first, album, langs, token, lfm, (found or {}).get("release", "")))

        async def get_track():
            return await self._one(self._file("track" + src, lang, first, title), lambda: lookup.track(self.http, first, title, langs, lfm)) if title else None

        try:
            # Which artist: by the player's id for it when there is one, else by the name together
            # with the album (several artists share a name; an album tells them apart).
            need_who = part in ("", "artist", "album")
            # One search says both who it is and which album: kept per artist and album.
            found = await self._one(self._file("spot", spotify_artist or artist, album), who) if need_who else None
            mbid = (found or {}).get("artist", "")
            jobs = {k: f for k, f in (("artist", lambda: get_artist(mbid)), ("album", lambda: get_album(mbid)), ("track", get_track)) if part in ("", k)}
            for k, v in zip(jobs, await asyncio.gather(*(f() for f in jobs.values()), return_exceptions=True)):
                if isinstance(v, lookup.Unavailable):
                    out["unavailable"] = True
                    out[k] = None
                elif isinstance(v, Exception):
                    log.warning("%s: %r", k, v)
                    out[k] = None
                else:
                    out[k] = v
        except lookup.Unavailable:
            out["unavailable"] = True
        # who plays on this track: MusicBrainz credits each recording of the album's release.
        # It comes with the album, for the track's card.
        if out.get("album"):
            al = out["album"] = dict(out["album"])
            mine = (al.pop("track_credits", None) or {}).get(lookup._fold(lookup.plain(title)))
            if mine and title:
                out["track_credits"] = mine["credits"]
        if part == "":   # all three at once: put together here as the page does it for itself
            al, tr = out.get("album"), out.get("track")
            if al and tr:
                theirs = {t["url"] for t in al.get("texts", [])}
                tr = out["track"] = {**tr, "texts": [t for t in tr.get("texts", []) if t["url"] not in theirs]}
            if out.get("track_credits"):
                tr = out["track"] = {**(tr or {"name": lookup.plain(title), "texts": [], "facts": [], "links": []}), "credits": out.pop("track_credits")}
            if tr is not None and not (tr.get("texts") or tr.get("credits") or tr.get("review")):
                out["track"] = None
            for k in ("artist", "album", "track"):
                out.setdefault(k, None)
        return out

    async def lyrics(self, artist: str, title: str, album: str, duration_ms: int) -> dict:
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=15))
        # Other modules that run may know the words with their times (a `words()` of their own):
        # they are asked only when LRCLIB has none, and which of them there are is part of what
        # the answer is kept under, so that turning one on asks again.
        others = sorted((p for p in self.hub.plugins.values() if p is not self and callable(getattr(p, "words", None))), key=lambda p: p.id)

        async def find():
            v = await lookup.lyrics(self.http, artist, title, album, duration_ms)
            if (v and (v.get("synced") or v.get("instrumental"))) or not others:
                return v
            for p in others:
                try:
                    w = await p.words(artist, title, album, duration_ms)
                except Exception:                      # (another module's trouble is not this one's)
                    log.exception("words from %s", p.id)
                    w = None
                if w and w.get("synced"):
                    return w
            return v
        v = await self._one(self._file("words2" + "".join("+" + p.id for p in others), artist, title, album), find)
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
            part = str(b.get("part") or "")
            if part not in ("", "artist", "album", "track"):
                raise web.HTTPBadRequest()
            return web.json_response(await self.about(artist, album, title, lang, m.group(1) if m else "", part))

        @routes.post("/api/about/years")
        async def years(request):
            """When albums first came out: [{"artist", "title"}] in, the years (or null) in the
            same order out. One question to MusicBrainz for each artist, kept."""
            try:
                b = await request.json()
                albums = [(" ".join(str(a.get("artist") or "").split())[:200], " ".join(str(a.get("title") or "").split())[:200]) for a in b.get("albums", [])[:120]]
            except (ValueError, AttributeError, TypeError):
                raise web.HTTPBadRequest()
            if self.http is None:
                self.http = ClientSession(timeout=ClientTimeout(total=15))
            first = lambda a: re.split(r", | / ", a)[0]      # noqa: E731
            by: dict[str, list[str]] = {}
            for a, t in albums:
                if a and t:
                    by.setdefault(first(a), []).append(t)
            known: dict[str, dict] = {}
            for artist, titles in list(by.items())[:8]:      # (a list is seldom of more artists; the rest stay without)
                names = sorted({lookup._fold(lookup.bare(t)) for t in titles})
                try:
                    known[artist] = await self._one(self._file("years3", artist, *names), lambda artist=artist, titles=titles: lookup.album_years(self.http, artist, titles)) or {}
                except lookup.Unavailable:
                    break
            return web.json_response({"years": [known.get(first(a), {}).get(lookup._fold(lookup.bare(t))) for a, t in albums]})

        @routes.get("/api/about/cover")
        async def cover(request):
            """A sleeve for when the player has none to show (its own picture is missing or
            broken): the album's front from the Cover Art Archive, passed on by the device."""
            q = request.query
            artist, album, title = (" ".join(str(q.get(k) or "").split())[:200] for k in ("artist", "album", "title"))
            if not artist or not (album or title):
                raise web.HTTPBadRequest()
            if self.http is None:
                self.http = ClientSession(timeout=ClientTimeout(total=15))
            first = re.split(r", | / ", artist)[0]
            try:
                url = await self._one(self._file("cover2", first, album or "\0" + title), lambda: lookup.cover(self.http, first, album, "" if album else title))
            except lookup.Unavailable:
                raise web.HTTPServiceUnavailable()
            if not url:
                return web.Response(status=204)   # no picture: said plainly, and not as an error in the browser's log
            try:
                async with self.http.get(url, headers={"User-Agent": lookup.UA}) as r:
                    data = await collect.read_capped(r, 3 * 1024 * 1024)
                    ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    if r.status != 200 or len(data) > 3 * 1024 * 1024 or ctype not in ("image/jpeg", "image/png", "image/webp"):
                        return web.Response(status=204)   # no picture: said plainly, and not as an error in the browser's log
            except (OSError, asyncio.TimeoutError):
                raise web.HTTPBadGateway()
            return web.Response(body=data, content_type=ctype, headers={
                "Cache-Control": "private, max-age=604800", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox"})

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
            return web.json_response(await self.lyrics(re.split(r", | / ", artist)[0], title, album, dur))


PLUGIN = About
