"""Lyrics with times from NetEase Cloud Music, for when LRCLIB has none.

LRCLIB (which "About the music" asks first) is open and kept by its users, and many of its
songs have their words but no times, so the words stand still. NetEase Cloud Music, a Chinese
music service, has timed words for a great many songs. It has no public API: this asks the
same addresses its own web player uses, which is not something NetEase has agreed to and may
stop working any day. The device tells NetEase the name of the song and the artist, nothing
else, and only when LRCLIB had no times for it. That is why the module is off until its owner
turns it on.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata

from aiohttp import ClientSession, ClientTimeout

from plugins import Plugin

log = logging.getLogger("netease")
SITE = "https://music.163.com"
HEADERS = {"Referer": SITE + "/", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) mscOS", "Accept": "application/json"}
STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# NetEase puts who wrote and made the song first, as lines of their own ("作词 : Paul McCartney"): not words of the song
CREDIT = re.compile(r"^[^:：]{0,24}[\u3400-\u9fff][^:：]{0,24}\s?[:：]")
NOTE = re.compile(r"\s*[\(\[][^)\]]*(remaster|version|mix|edit|deluxe|live|mono|stereo|bonus|anniversary)[^)\]]*[\)\]]|\s+-\s+.*(remaster|version|mix|edit|live|mono|stereo).*$", re.I)


def fold(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).casefold()
    return "".join(c for c in s if c.isalnum())


def bare(title) -> str:
    """The title without its edition note: "Yesterday (Remastered)" and "Yesterday - 2009 Mix" are Yesterday."""
    return NOTE.sub("", str(title or "")).strip()


def lines(lrc: str) -> list:
    """[[ms, line], ...] from LRC text, in order, without the credits NetEase puts first."""
    out = []
    for raw in str(lrc or "").splitlines()[:2000]:
        stamps = list(STAMP.finditer(raw))
        if not stamps:
            continue
        text = raw[stamps[-1].end():].strip()[:300]
        if CREDIT.match(text):
            continue
        for m in stamps:
            frac = m.group(3) or "0"
            out.append([int(m.group(1)) * 60000 + int(m.group(2)) * 1000 + int(frac.ljust(3, "0")[:3]), text])
    out.sort(key=lambda l: l[0])
    while out and not out[0][1]:          # (the empty lines the credits left at the start)
        out.pop(0)
    return out[:600]


def pick(songs, artist: str, title: str, duration_ms: int) -> int | None:
    """The id of the song that is this recording: the same title, the artist among its artists,
    and the same length within three seconds (the times must fit what plays). None: no such."""
    want, who, best = fold(bare(title)), fold(artist), None
    for s in songs or []:
        if not isinstance(s, dict) or not isinstance(s.get("id"), int) or fold(bare(s.get("name"))) != want:
            continue
        names = [fold(a.get("name")) for a in s.get("artists") or [] if isinstance(a, dict)]
        if who not in names:
            continue
        off = abs((s.get("duration") or 0) - duration_ms) if duration_ms else 0
        if off < 3000 and (best is None or off < best[0]):
            best = (off, s["id"])
    return best[1] if best else None


class NetEase(Plugin):
    id = "netease"
    kind = "integration"
    name = {"sv": "Låttexter från NetEase", "en": "Lyrics from NetEase"}
    description = {"sv": "Hämtar låttexter med tider från NetEase Cloud Music när LRCLIB saknar dem. Enheten berättar då för NetEase (en kinesisk musiktjänst) vilken låt som spelas.",
                   "en": "Fetches lyrics with times from NetEase Cloud Music when LRCLIB has none. The device then tells NetEase (a Chinese music service) which song is playing."}

    def __init__(self, hub):
        super().__init__(hub)
        self.http: ClientSession | None = None

    async def _json(self, path: str, params: dict) -> dict:
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=10))
        try:
            async with self.http.get(SITE + path, params=params, headers=HEADERS, allow_redirects=False) as r:
                if r.status != 200:
                    return {}
                body = await r.content.read(600_000)      # (an answer is a few kilobytes: more is not one)
                d = json.loads(body)
                return d if isinstance(d, dict) else {}
        except (OSError, asyncio.TimeoutError, ValueError):
            return {}

    async def words(self, artist: str, title: str, album: str, duration_ms: int) -> dict | None:
        """The song's words with their times: the shape "About the music" shows, or None.
        (Asked by that module when its own source had no times; it keeps the answer.)"""
        artist, title = " ".join(str(artist or "").split())[:120], " ".join(str(title or "").split())[:160]
        if not artist or not title or not duration_ms:     # (without the length the times cannot be trusted to fit)
            return None
        found = await self._json("/api/search/get", {"s": f"{bare(title)} {artist}"[:200], "type": 1, "limit": 12})
        song = pick((found.get("result") or {}).get("songs"), artist, title, int(duration_ms))
        if song is None:
            return None
        d = await self._json("/api/song/lyric", {"id": song, "lv": 1, "kv": 1, "tv": -1})
        synced = lines((d.get("lrc") or {}).get("lyric") or "")
        if len(synced) < 3:                                # ("pure music, enjoy" and the like)
            return None
        return {"synced": synced, "words": None, "plain": "\n".join(t for _, t in synced)[:8000], "instrumental": False,
                "from": {"name": "NetEase Cloud Music", "url": f"{SITE}/song?id={song}"}}


PLUGIN = NetEase
