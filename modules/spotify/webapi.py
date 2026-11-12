"""Spotify's Web API, for finding music: search, an album's tracks, an artist's albums.

Soloist plays what it is told to (a track, an album, an artist, a playlist) but cannot search.
The Web API can, with the credentials of an app the listener creates in Spotify's developer
dashboard: its Client ID and Client secret, kept root-only on the device. Nothing here needs
the listener's own account: the app's own token ("client credentials") reads the catalogue.
"""
from __future__ import annotations

import asyncio
import base64
import re
import time

from aiohttp import ClientSession, ClientTimeout

API = "https://api.spotify.com/v1"
ID = re.compile(r"[A-Za-z0-9]{22}")


class WebApiError(Exception):
    """What went wrong, in words for the listener: `kind` is "credentials", "busy" or "down"."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def image(images, want: int = 300) -> str | None:
    """The picture nearest the wanted width, from Spotify's list of sizes."""
    pics = [i for i in images or [] if isinstance(i, dict) and str(i.get("url", "")).startswith("https://")]
    if not pics:
        return None
    return min(pics, key=lambda i: abs((i.get("width") or 640) - want))["url"]


def names(artists) -> str:
    return ", ".join(a.get("name", "") for a in artists or [] if a.get("name"))


def album_row(a: dict) -> dict:
    return {"uri": a.get("uri"), "id": a.get("id"), "name": a.get("name", ""), "artist": names(a.get("artists")),
            "year": (a.get("release_date") or "")[:4], "kind": a.get("album_type") or "", "cover": image(a.get("images"))}


def track_row(t: dict, album: dict | None = None) -> dict:
    al = t.get("album") or album or {}
    return {"uri": t.get("uri"), "name": t.get("name", ""), "artist": names(t.get("artists")), "album": al.get("name", ""),
            "album_id": al.get("id"), "cover": image(al.get("images"), 64), "duration_ms": t.get("duration_ms"), "n": t.get("track_number")}


def artist_row(a: dict) -> dict:
    return {"uri": a.get("uri"), "id": a.get("id"), "name": a.get("name", ""), "cover": image(a.get("images"), 160)}


class WebApi:
    def __init__(self, credentials):
        self.credentials = credentials      # () -> (client id, client secret)
        self.http: ClientSession | None = None
        self.token, self.until = "", 0.0
        self.kept: dict[str, tuple[float, dict]] = {}   # answers of the last minutes: typing asks the same again and again
        self.lock = asyncio.Lock()

    def ready(self) -> bool:
        cid, secret = self.credentials()
        return bool(cid and secret)

    async def _token(self) -> str:
        async with self.lock:
            if self.token and time.time() < self.until:
                return self.token
            cid, secret = (x.strip() for x in self.credentials())
            if not cid or not secret:
                raise WebApiError("credentials")
            basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
            try:
                async with self.http.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                                          headers={"Authorization": f"Basic {basic}"}) as r:
                    d = await r.json(content_type=None) if r.status == 200 else None
                    if r.status in (400, 401):
                        raise WebApiError("credentials")
            except (OSError, asyncio.TimeoutError, ValueError):
                raise WebApiError("down")
            if not d or not d.get("access_token"):
                raise WebApiError("down")
            self.token, self.until = d["access_token"], time.time() + max(60, int(d.get("expires_in", 3600)) - 120)
            return self.token

    async def get(self, path: str, **params) -> dict:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        hit = self.kept.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=10))
        for attempt in (0, 1):
            token = await self._token()
            try:
                async with self.http.get(f"{API}/{path}", params=params, headers={"Authorization": f"Bearer {token}"}) as r:
                    if r.status == 401 and attempt == 0:     # the token went stale early: a new one, once
                        self.token = ""
                        continue
                    if r.status == 429:
                        raise WebApiError("busy")
                    if r.status == 404:
                        return {}
                    if r.status != 200:
                        raise WebApiError("down")
                    d = await r.json(content_type=None)
            except (OSError, asyncio.TimeoutError, ValueError):
                raise WebApiError("down")
            if len(self.kept) > 300:
                for k in sorted(self.kept, key=lambda k: self.kept[k][0])[:100]:
                    del self.kept[k]
            self.kept[key] = (time.time(), d)
            return d
        raise WebApiError("credentials")

    async def search(self, q: str) -> dict:
        d = await self.get("search", q=q, type="album,artist,track", limit=8)
        items = lambda k: [x for x in ((d.get(k) or {}).get("items") or []) if isinstance(x, dict) and x.get("uri")]   # noqa: E731
        return {"artists": [artist_row(a) for a in items("artists")][:6], "albums": [album_row(a) for a in items("albums")],
                "tracks": [track_row(t) for t in items("tracks")]}

    async def album(self, album_id: str) -> dict | None:
        d = await self.get(f"albums/{album_id}")
        if not d.get("uri"):
            return None
        return {**album_row(d), "cover": image(d.get("images"), 640), "label": d.get("label") or "",
                "tracks": [track_row(t, d) for t in ((d.get("tracks") or {}).get("items") or []) if t.get("uri")]}

    async def artist(self, artist_id: str) -> dict | None:
        d = await self.get(f"artists/{artist_id}")
        if not d.get("uri"):
            return None
        al = await self.get(f"artists/{artist_id}/albums", include_groups="album,single", limit=10)
        return {**artist_row(d), "cover": image(d.get("images"), 320),
                "albums": [album_row(a) for a in (al.get("items") or []) if a.get("uri")]}
