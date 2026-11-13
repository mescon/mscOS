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


def playlist_row(p: dict) -> dict:
    """A playlist as the search and its own page give it. Spotify tells an app like this one what
    a public playlist is called, whose it is and how long, but not what is in it."""
    count = (p.get("items") or p.get("tracks") or {}).get("total")
    return {"uri": p.get("uri"), "id": p.get("id"), "name": p.get("name", ""), "owner": (p.get("owner") or {}).get("display_name") or "",
            "count": count if isinstance(count, int) else None, "cover": image(p.get("images")),
            "about": re.sub(r"<[^>]+>", "", p.get("description") or "")[:400]}


def artist_row(a: dict) -> dict:
    return {"uri": a.get("uri"), "id": a.get("id"), "name": a.get("name", ""), "cover": image(a.get("images"), 160)}


class WebApi:
    def __init__(self, credentials, lastfm_key=lambda: ""):
        self.credentials = credentials      # () -> (client id, client secret)
        self.lastfm_key = lastfm_key        # () -> the listener's Last.fm key, when there is one
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
        d = await self.get("search", q=q, type="album,artist,track,playlist", limit=8)
        items = lambda k: [x for x in ((d.get(k) or {}).get("items") or []) if isinstance(x, dict) and x.get("uri")]   # noqa: E731
        return {"artists": [artist_row(a) for a in items("artists")][:6], "albums": [album_row(a) for a in items("albums")],
                "playlists": [playlist_row(p) for p in items("playlists")], "tracks": [track_row(t) for t in items("tracks")]}

    async def playlist(self, playlist_id: str) -> dict | None:
        d = await self.get(f"playlists/{playlist_id}")
        if not d.get("uri"):
            return None
        return {**playlist_row(d), "cover": image(d.get("images"), 640), "followers": (d.get("followers") or {}).get("total")}

    async def tracks(self, ids: list[str]) -> dict:
        """{id: row} for tracks known only by their id (the far end of the queue, which the
        player hands over as bare addresses). One question each, kept; those not found are left out."""
        async def one(i):
            try:
                t = await self.get(f"tracks/{i}")
            except WebApiError:
                return None
            return {**track_row(t), "cover_big": image((t.get("album") or {}).get("images"), 640)} if t.get("name") else None
        rows = await asyncio.gather(*(one(i) for i in ids))
        return {i: r for i, r in zip(ids, rows) if r}

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
        albums, seen = [], set()
        for offset in range(0, 60, 10):         # ten at a time is what Spotify gives
            al = await self.get(f"artists/{artist_id}/albums", include_groups="album", limit=10, offset=offset)
            for a in al.get("items") or []:
                key = (a.get("name", "").casefold(), (a.get("release_date") or "")[:4])
                if a.get("uri") and key not in seen:    # the same album listed for several markets
                    seen.add(key)
                    albums.append(album_row(a))
            if offset + 10 >= (al.get("total") or 0):
                break
        albums.sort(key=lambda a: a["year"] or "9999")   # in the order they came out
        return {**artist_row(d), "cover": image(d.get("images"), 320), "albums": albums, **(await self.top_tracks(d.get("name", "")))}

    async def top_tracks(self, name: str) -> dict:
        """The artist's best-known tracks: {"top": [track rows], "top_from": "lastfm" | "spotify"}.
        Last.fm counts listeners all over the world (with the listener's own key); without it,
        Spotify's own order of the artist's tracks, which follows how much they are played."""
        if not name:
            return {"top": [], "top_from": ""}
        key = self.lastfm_key()
        if key:
            try:
                async with self.http.get("https://ws.audioscrobbler.com/2.0/", params={
                        "method": "artist.gettoptracks", "artist": name, "api_key": key, "format": "json", "limit": 5, "autocorrect": 1}) as r:
                    d = await r.json(content_type=None) if r.status == 200 else {}
            except (OSError, asyncio.TimeoutError, ValueError):
                d = {}
            top = []
            for t in ((d.get("toptracks") or {}).get("track") or [])[:5]:
                title = str(t.get("name") or "")
                hit = await self.get("search", q=f'track:"{title}" artist:"{name}"', type="track", limit=3)
                rows = [x for x in ((hit.get("tracks") or {}).get("items") or []) if x.get("uri")]
                row = next((x for x in rows if name.casefold() in names(x.get("artists")).casefold()), None)
                if row:
                    top.append({**track_row(row), "listeners": int(t["listeners"]) if str(t.get("listeners", "")).isdigit() else None})
            if top:
                return {"top": top, "top_from": "lastfm"}
        d = await self.get("search", q=f'artist:"{name}"', type="track", limit=5)
        rows = [x for x in ((d.get("tracks") or {}).get("items") or []) if x.get("uri") and name.casefold() in names(x.get("artists")).casefold()]
        return {"top": [track_row(x) for x in rows[:5]], "top_from": "spotify" if rows else ""}
