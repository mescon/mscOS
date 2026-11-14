"""Spotify's Web API, for finding music: search, an album's tracks, an artist's albums.

Soloist plays what it is told to (a track, an album, an artist, a playlist) but cannot search.
The Web API can, with the credentials of an app the listener creates in Spotify's developer
dashboard: its Client ID and Client secret, kept root-only on the device. Nothing here needs
the listener's own account: the app's own token ("client credentials") reads the catalogue.

The listener may also sign in with their Spotify account (authorization code with PKCE: the
Client ID alone, no secret). Then the questions are asked as the listener, which adds what
belongs to the account: where it plays, the playlists, the saved albums and the liked tracks.
What is kept is a refresh token, root-only on the device. Its leave is to read the playback
state and the library, and to save to and remove from the library (a liked track, a saved
album): the device never does anything else with it.
"""
from __future__ import annotations

import asyncio
import atexit
import base64
import collections
import hashlib
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from aiohttp import ClientSession, ClientTimeout

log = logging.getLogger("spotify")

API = "https://api.spotify.com/v1"
ID = re.compile(r"[A-Za-z0-9]{22}")
ACCOUNTS = "https://accounts.spotify.com"
# what the sign-in asks leave for: to see where the account plays and what is in its library
# Spotify counts an app's questions over a rolling half minute and does not say how many it
# allows (few, for an app in development mode): some two hundred in five minutes brought a
# pause of well over an hour. So the device holds itself to half of that over time, one
# question every three seconds, with room for a burst when a page needs several at once
# (an artist's page asks a dozen). When Spotify says wait (429 with Retry-After), nothing at
# all is asked until that time has passed, also if the panel starts again meanwhile.
BURST = 10              # questions that may be asked at once, after a quiet while
EVERY = 6.0             # seconds for one more to become available
HOUR = 240              # and no more than this many in an hour, whatever the rest says
QUIET = 60              # what runs by itself (nobody is waiting for it) stops at this many in an hour
# For how long an answer kept on the device is good, in seconds. Kept across restarts of the
# panel: what Spotify counts is every question, also the ones asked a second time.
DAY = 86400
GOOD = {"library": 6 * 3600, "me": 30 * DAY, "playlist": 3600, "track": 30 * DAY, "album": 7 * DAY, "artist": DAY, "saved": 6 * 3600, "search": DAY}
PER_HALF_MINUTE = int(30 / EVERY)    # what that comes to over time (shown in the module's card)
# To read: where it plays, the playlists, the library. To change: the library alone, which is
# what liking a track and saving an album are. Spotify's one call for saving also covers artists
# and playlists and its description lists the leave for those too; the device never saves
# those, so it does not ask for that leave. (Should Spotify refuse a track or an album without
# it, the listener is told that it is not allowed, and nothing more is taken.)
SCOPES = ("user-read-playback-state playlist-read-private playlist-read-collaborative user-library-read"
          " user-library-modify")
SAVES = re.compile(r"spotify:(track|album):[A-Za-z0-9]{22}")


LOOPBACK = "http://127.0.0.1:8888/callback"     # the one kind of plain-http address Spotify sends a sign-in's answer to


def answer_parts(pasted: str) -> tuple[str, str, str]:
    """(code, state, error) from the address the browser ended up at after the sign-in, as the
    listener copied it from the address field (the whole address, or what follows the "?")."""
    text = "".join(str(pasted or "").split())[:4000]
    try:
        query = urlsplit(text).query if "?" in text else text
        q = {k: v[0] for k, v in parse_qs(query).items() if v}
    except ValueError:          # (not an address at all)
        return "", "", ""
    return q.get("code", "")[:2000], q.get("state", "")[:200], q.get("error", "")[:100]


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


def apart(artists) -> list:
    """The artists one by one, for a page that lets each name lead to the artist."""
    return [{"id": a.get("id"), "name": a["name"]} for a in artists or [] if a.get("name")]


def album_row(a: dict) -> dict:
    return {"uri": a.get("uri"), "id": a.get("id"), "name": a.get("name", ""), "artist": names(a.get("artists")), "artists": apart(a.get("artists")),
            "year": (a.get("release_date") or "")[:4], "kind": a.get("album_type") or "", "cover": image(a.get("images"))}


def track_row(t: dict, album: dict | None = None) -> dict:
    al = t.get("album") or album or {}
    return {"uri": t.get("uri"), "name": t.get("name", ""), "artist": names(t.get("artists")), "artists": apart(t.get("artists")), "album": al.get("name", ""),
            "album_id": al.get("id"), "cover": image(al.get("images"), 64), "duration_ms": t.get("duration_ms"), "n": t.get("track_number")}


def playlist_row(p: dict) -> dict:
    """A playlist as the search and its own page give it. Spotify tells an app like this one what
    a public playlist is called, whose it is and how long, but not what is in it."""
    count = (p.get("items") or p.get("tracks") or {}).get("total")
    return {"uri": p.get("uri"), "id": p.get("id"), "name": p.get("name", ""), "owner": (p.get("owner") or {}).get("display_name") or "",
            "owner_id": (p.get("owner") or {}).get("id") or "", "snap": p.get("snapshot_id") or "",
            "count": count if isinstance(count, int) else None, "cover": image(p.get("images")),
            "about": re.sub(r"<[^>]+>", "", p.get("description") or "")[:400]}


def artist_row(a: dict) -> dict:
    return {"uri": a.get("uri"), "id": a.get("id"), "name": a.get("name", ""), "cover": image(a.get("images"), 160)}


class WebApi:
    def __init__(self, credentials, lastfm_key=lambda: "", refresh=lambda: "", keep_refresh=lambda token: None, wait_file: str = "",
                 granted=lambda: "", keep_granted=lambda scope: None, store_file: str = ""):
        self.store_file = store_file        # where finished answers are kept across restarts (the listener's own: for root only)
        self.long: dict[str, list] = {}     # those answers: key -> [when, what]
        self.long_dirty = False
        try:
            with open(store_file) as f:
                d = json.load(f)
            self.long = {k: v for k, v in d.items() if isinstance(v, list) and len(v) == 2} if isinstance(d, dict) else {}
        except (OSError, ValueError):
            pass
        if store_file:
            atexit.register(self.write_long)   # (what the last seconds brought is not lost when the panel stops)
        self.granted = granted              # () -> what the sign-in gave leave for, as Spotify wrote it
        self.keep_granted = keep_granted
        self.credentials = credentials      # () -> (client id, client secret)
        self.refresh = refresh              # () -> the signed-in listener's refresh token, or ""
        self.keep_refresh = keep_refresh    # (token) -> keeps it; "" forgets it
        self.pending: tuple | None = None   # a sign-in under way: (state, verifier, redirect address, until)
        self.sent: collections.deque = collections.deque(maxlen=5000)   # each question of the last hour: (when, what about)
        self.wait_file = wait_file          # where the time of a pause Spotify asked for is kept
        self.wait_until = 0.0               # Spotify asked for a pause until then
        try:
            with open(wait_file) as f:
                self.wait_until = min(float(f.read().strip()), time.time() + 86400)
        except (OSError, ValueError):
            pass
        self.refused = 0                    # how many times it has (since the panel started)
        self.turn = asyncio.Lock()
        self.left, self.left_at = float(BURST), time.time()      # how many may be asked right now, and as of when
        self.flying: dict[str, asyncio.Future] = {}              # questions under way: the same one is not asked twice at once
        self.lastfm_key = lastfm_key        # () -> the listener's Last.fm key, when there is one
        self.http: ClientSession | None = None
        self.token, self.until = "", 0.0
        self.kept: dict[str, tuple[float, dict]] = {}   # answers of the last minutes: typing asks the same again and again
        self.lock = asyncio.Lock()

    def _pause(self, after: str) -> None:
        """Spotify asked for a pause (429): nothing is asked until it has passed, kept across a restart."""
        self.wait_until = time.time() + min(86400, int(after) if after.isdigit() else 60)
        self.refused += 1
        if self.wait_file:
            try:
                with open(self.wait_file, "w") as f:
                    f.write(str(int(self.wait_until)))
            except OSError:
                pass

    # ---- answers kept on the device
    def known(self, kind: str, key: str, old: bool = False):
        """A kept answer that is still good; with old, or while nothing may be asked, also one that is not."""
        hit = self.long.get(f"{kind}:{key}")
        if hit and (old or time.time() - hit[0] < GOOD[kind] or not self.may_ask()):
            return hit[1]
        return None

    def keep(self, kind: str, key: str, what) -> None:
        self.long[f"{kind}:{key}"] = [time.time(), what]
        if len(self.long) > 3000:              # (the oldest go)
            for k in sorted(self.long, key=lambda k: self.long[k][0])[:500]:
                del self.long[k]
        self._long_changed()

    def forget(self, *starts: str) -> None:
        for k in [k for k in self.long if k.startswith(starts)]:
            del self.long[k]
        self._long_changed()

    def _long_changed(self) -> None:
        if self.long_dirty or not self.store_file:
            return
        self.long_dirty = True
        try:
            asyncio.get_running_loop().call_later(20, self.write_long)     # (written at most every twenty seconds)
        except RuntimeError:
            self.write_long()

    def write_long(self) -> None:
        if not self.long_dirty or not self.store_file:
            return
        self.long_dirty = False
        try:
            fd = os.open(self.store_file + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.long, f, separators=(",", ":"))
            os.replace(self.store_file + ".tmp", self.store_file)
        except OSError:
            pass

    def in_hour(self) -> int:
        now = time.time()
        return sum(1 for t, _ in self.sent if now - t < 3600)

    def may_ask(self, quiet: bool = False) -> bool:
        """Whether a question would be let through now. quiet: for what runs by itself."""
        return time.time() >= self.wait_until and self.in_hour() < (QUIET if quiet else HOUR)

    def about(self) -> str:
        """What the last hour's questions were about, the most asked first: for the log."""
        return ", ".join(f"{k} {n}" for k, n in collections.Counter(w for t, w in self.sent if time.time() - t < 3600).most_common(8))

    async def _my_turn(self, what: str = "") -> None:
        """Wait until a question may be asked (BURST, EVERY), or refuse while Spotify has asked
        for a pause or the hour is full (HOUR)."""
        async with self.turn:
            while True:
                now = time.time()
                if now < self.wait_until:
                    raise WebApiError("busy")
                if self.in_hour() >= HOUR:
                    if now - getattr(self, "full_said", 0) > 600:
                        self.full_said = now
                        log.warning("spotify web api: %s questions in an hour, no more for now (%s)", HOUR, self.about())
                    raise WebApiError("busy")
                self.left = min(float(BURST), self.left + (now - self.left_at) / EVERY)
                self.left_at = now
                if self.left >= 1:
                    break
                await asyncio.sleep(min(EVERY, (1 - self.left) * EVERY) + 0.01)
            self.left -= 1
            self.sent.append((time.time(), what))

    def asked(self) -> dict:
        """How much has been asked of Spotify: for the module's card, so that it can be seen."""
        now = time.time()
        return {"half_minute": sum(1 for t, _ in self.sent if now - t < 30), "hour": self.in_hour(), "hour_limit": HOUR,
                "limit": PER_HALF_MINUTE, "refused": self.refused, "wait": max(0, int(self.wait_until - now)), "until": self.wait_until}

    def ready(self) -> bool:
        cid, secret = self.credentials()
        return bool(cid and (secret or self.refresh()))

    def signed_in(self) -> bool:
        return bool(self.credentials()[0] and self.refresh())

    def sign_in_address(self, redirect: str) -> str:
        """Where the listener's browser goes to give its leave. The answer comes back to
        `redirect` with a code, which finish() trades for the tokens."""
        cid = self.credentials()[0].strip()
        if not cid:
            raise WebApiError("credentials")
        state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(64)
        self.pending = (state, verifier, redirect, time.time() + 600)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return f"{ACCOUNTS}/authorize?" + urlencode({"client_id": cid, "response_type": "code", "redirect_uri": redirect, "state": state,
                                                     "scope": SCOPES, "code_challenge_method": "S256", "code_challenge": challenge})

    async def finish(self, code: str, state: str) -> None:
        """The code Spotify sent back, traded for the tokens. Only for the sign-in that was begun here."""
        p = self.pending
        if not p or not secrets.compare_digest(p[0].encode(), str(state or "").encode()):
            raise WebApiError("credentials")       # (not the sign-in that was begun: it stays as it was)
        self.pending = None
        if time.time() > p[3] or not code:
            raise WebApiError("credentials")
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=10))
        try:
            async with self.http.post(f"{ACCOUNTS}/api/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": p[2],
                                                                    "client_id": self.credentials()[0].strip(), "code_verifier": p[1]}) as r:
                d = await r.json(content_type=None) if r.status == 200 else None
        except (OSError, asyncio.TimeoutError, ValueError):
            raise WebApiError("down")
        if not d or not d.get("refresh_token") or not d.get("access_token"):
            raise WebApiError("credentials")
        async with self.lock:
            self.keep_granted(str(d.get("scope") or "")[:400])
            self.keep_refresh(d["refresh_token"])
            self.token, self.until = d["access_token"], time.time() + max(60, int(d.get("expires_in", 3600)) - 120)
            self.kept.clear()

    def can_save(self) -> bool:
        return self.signed_in() and "user-library-modify" in self.granted().split()

    def sign_out(self) -> None:
        self.keep_granted("")
        self.keep_refresh("")
        self.token, self.until = "", 0.0
        self.kept.clear()
        self.forget("library:", "saved:", "playlist:", "me:")      # (the listener's own: gone with the sign-in)

    async def _user_token(self, cid: str, refresh: str) -> str | None:
        """A token as the listener, from the refresh token. None when Spotify no longer takes
        it (leave withdrawn in the account): it is forgotten then."""
        try:
            async with self.http.post(f"{ACCOUNTS}/api/token", data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": cid}) as r:
                if r.status in (400, 401):
                    self.keep_refresh("")
                    return None
                d = await r.json(content_type=None) if r.status == 200 else None
        except (OSError, asyncio.TimeoutError, ValueError):
            raise WebApiError("down")
        if not d or not d.get("access_token"):
            raise WebApiError("down")
        if d.get("refresh_token") and d["refresh_token"] != refresh:     # (Spotify may hand out a new one: the old one is spent then)
            self.keep_refresh(d["refresh_token"])
        self.token, self.until = d["access_token"], time.time() + max(60, int(d.get("expires_in", 3600)) - 120)
        return self.token

    async def _token(self) -> str:
        async with self.lock:
            if self.token and time.time() < self.until:
                return self.token
            cid, secret = (x.strip() for x in self.credentials())
            refresh = self.refresh() if cid else ""
            if refresh:
                token = await self._user_token(cid, refresh)
                if token:
                    return token
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

    async def get(self, path: str, keep: float = 600, **params) -> dict:
        """keep: for how many seconds the answer is good to give again."""
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        hit = self.kept.get(key)
        if hit and (time.time() - hit[0] < keep or time.time() < self.wait_until):   # (while Spotify asks for a pause: the older answer)
            return hit[1]
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=10))
        if key in self.flying:                 # (the same question is under way: its answer is this one's too)
            return await asyncio.shield(self.flying[key])
        fut = self.flying[key] = asyncio.get_running_loop().create_future()
        try:
            d = await self._get(path, key, keep, params)
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e)
                fut.exception()                # (looked at here, so that an answer nobody else waited for is not reported as lost)
            raise
        finally:
            self.flying.pop(key, None)
        if not fut.done():
            fut.set_result(d)
        return d

    async def _get(self, path: str, key: str, keep: float, params: dict) -> dict:
        for attempt in (0, 1):
            token = await self._token()
            await self._my_turn(re.sub(r"/[A-Za-z0-9]{22}", "", path))
            try:
                async with self.http.get(f"{API}/{path}", params=params, headers={"Authorization": f"Bearer {token}"}) as r:
                    if r.status == 401 and attempt == 0:     # the token went stale early: a new one, once
                        self.token = ""
                        continue
                    if r.status == 429:
                        after = r.headers.get("Retry-After", "")
                        self._pause(after)
                        log.warning("spotify web api: asked to wait (429) at %s, Retry-After %s; the last hour: %s", path.split("/")[0], after or "?", self.about())
                        raise WebApiError("busy")
                    if r.status in (204, 404):               # (204: nothing plays anywhere)
                        return {}
                    if r.status == 403:
                        raise WebApiError("leave")
                    if r.status != 200:
                        log.warning("spotify web api: %s at %s", r.status, path.split("/")[0])
                        raise WebApiError("down")
                    d = await r.json(content_type=None)
            except (OSError, asyncio.TimeoutError, ValueError) as e:
                log.warning("spotify web api: no answer at %s (%s)", path.split("/")[0], type(e).__name__)
                raise WebApiError("down")
            if keep <= 0:           # (asked once and not again: not worth keeping)
                return d
            if len(self.kept) > 300:
                for k in sorted(self.kept, key=lambda k: self.kept[k][0])[:100]:
                    del self.kept[k]
            self.kept[key] = (time.time(), d)
            return d
        raise WebApiError("credentials")

    async def search(self, q: str) -> dict:
        hit = self.known("search", q.casefold())
        if hit:
            return hit
        d = await self.get("search", q=q, type="album,artist,track,playlist", limit=8)
        items = lambda k: [x for x in ((d.get(k) or {}).get("items") or []) if isinstance(x, dict) and x.get("uri")]   # noqa: E731
        out = self._search_rows(d, items)
        self.keep("search", q.casefold(), out)
        return out

    @staticmethod
    def _search_rows(d: dict, items) -> dict:
        return {"artists": [artist_row(a) for a in items("artists")][:6], "albums": [album_row(a) for a in items("albums")],
                "playlists": [playlist_row(p) for p in items("playlists")], "tracks": [track_row(t) for t in items("tracks")]}

    async def playlist(self, playlist_id: str, items: bool = False) -> dict | None:
        """items: also what is in it, for the owner (asked as the signed-in listener)."""
        key = f"{playlist_id}:{int(bool(items and self.signed_in()))}"
        hit = self.long.get(f"playlist:{key}")
        if hit:
            # as long as the library names the same snapshot, nothing in it has changed: no question at all
            snap = next((p.get("snap") for p in (self.known("library", "playlists", old=True) or []) if p.get("id") == playlist_id), None)
            if (snap and snap == hit[1].get("snap")) or time.time() - hit[0] < GOOD["playlist"] or not self.may_ask():
                return hit[1]
        d = await self.get(f"playlists/{playlist_id}", keep=60)
        if not d.get("uri"):
            return None
        out = {**playlist_row(d), "cover": image(d.get("images"), 640), "followers": (d.get("followers") or {}).get("total")}
        # what is in it: Spotify says so for the signed-in listener's own playlists only
        if items and self.signed_in():
            rows, total, hidden = [], None, 0
            for offset in range(0, 300, 50):
                try:
                    page = await self.get(f"playlists/{playlist_id}/items", keep=60, limit=50, offset=offset)
                except WebApiError:
                    break
                for it in page.get("items") or []:
                    t = it.get("item") or it.get("track") or {}
                    if t.get("uri", "").startswith("spotify:track:"):
                        rows.append(track_row(t))
                    else:                      # a podcast episode, a file of the listener's own, or a track Spotify no longer has
                        hidden += 1
                total = page.get("total")
                if not page.get("items") or offset + 50 >= (total or 0):
                    break
            if rows:
                out["tracks"], out["count"] = rows, total if isinstance(total, int) else out.get("count")
                out["hidden"] = hidden
                out["more"] = max(0, (total or 0) - len(rows) - hidden) if isinstance(total, int) else 0     # (beyond the three hundred that are read)
        self.keep("playlist", key, out)
        return out

    async def me(self) -> dict:
        hit = self.known("me", "")
        if not hit:
            d = await self.get("me")
            hit = {"name": d.get("display_name") or d.get("id") or "", "id": d.get("id") or ""}
            self.keep("me", "", hit)
        return hit

    async def where(self) -> dict | None:
        """The device the account plays on right now: {"name", "type"}, or None when nothing plays."""
        try:
            d = await self.get("me/player", keep=30)
        except WebApiError:           # (not worth an error on the page: the device is just not named)
            return None
        dev = d.get("device") or {}
        return {"name": str(dev.get("name") or "")[:80], "type": str(dev.get("type") or "")[:30]} if dev.get("name") else None

    async def library(self, fresh: bool = False) -> dict:
        """What the listener has gathered: every playlist (their own and those they follow), the
        saved albums, and the latest two hundred liked tracks. "me": the listener's id, to tell their own
        playlists from the followed ones. Each part is kept on the device for hours (a large
        library is dozens of questions); fresh asks Spotify again."""
        whole = True

        async def part(path, row, most):
            nonlocal whole
            name = path.split("/")[1]
            was_whole = whole_before.get(name, True)
            if not (self.may_ask() and (fresh or not was_whole)):       # (one cut short is asked again as soon as it may be)
                hit = self.known("library", name)
                if hit is not None:
                    whole = whole and was_whole
                    return hit
            out, cut = [], False
            for offset in range(0, most, 50):
                try:
                    d = await self.get(path, keep=900, limit=50, offset=offset)     # (a quarter of an hour: a large library is many questions)
                except WebApiError as e:
                    if e.kind == "leave" or out:      # (not allowed, or cut short: what there is)
                        whole = False
                        cut = True
                        break
                    older = self.known("library", name, old=True)
                    if older is not None:             # (no answer now: what was known before)
                        return older
                    raise
                out += [x for x in (row(i) for i in d.get("items") or [] if isinstance(i, dict)) if x and x.get("uri")]
                if not d.get("items") or offset + 50 >= (d.get("total") or 0):
                    break
            whole_before[name] = not cut
            self.keep("library", "whole", whole_before)
            self.keep("library", name, out)
            return out
        whole_before = dict(self.known("library", "whole", old=True) or {})
        playlists, albums, tracks, me = await asyncio.gather(
            part("me/playlists", playlist_row, 1000),
            part("me/albums", lambda i: album_row(i.get("album") or {}), 500),
            part("me/tracks", lambda i: track_row(i.get("track") or {}), 200),
            self.me())
        at = min((self.long.get(f"library:{n}") or [time.time()])[0] for n in ("playlists", "albums", "tracks"))
        return {"playlists": playlists, "albums": albums, "tracks": tracks, "me": me.get("id") or "", "whole": whole, "at": int(at)}

    async def _change(self, method: str, path: str, **params) -> None:
        """A change on the account (PUT or DELETE). Raises as get() does."""
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=10))
        for attempt in (0, 1):
            token = await self._token()
            await self._my_turn(method.lower() + " " + path)
            try:
                async with self.http.request(method, f"{API}/{path}", params=params, headers={"Authorization": f"Bearer {token}"}) as r:
                    if r.status == 401 and attempt == 0:
                        self.token = ""
                        continue
                    if r.status == 429:
                        self._pause(r.headers.get("Retry-After", ""))
                        raise WebApiError("busy")
                    if r.status in (401, 403):
                        raise WebApiError("leave")
                    if r.status not in (200, 201, 204):
                        log.warning("spotify web api: %s at %s %s", r.status, method, path.split("/")[0])
                        raise WebApiError("down")
                    return
            except (OSError, asyncio.TimeoutError):
                raise WebApiError("down")
        raise WebApiError("credentials")

    async def saved(self, uris: list[str]) -> dict:
        """{uri: whether it is in the listener's library} for tracks and albums."""
        uris = [u for u in dict.fromkeys(uris) if SAVES.fullmatch(u)][:40]
        if not uris:
            return {}
        out = {u: self.known("saved", u) for u in uris}
        ask = [u for u in uris if out[u] is None]
        if ask:                                # (only those not known already)
            d = await self.get("me/library/contains", keep=20, uris=",".join(ask))
            if isinstance(d, list) and len(d) == len(ask):
                for u, x in zip(ask, d):
                    out[u] = bool(x)
                    self.keep("saved", u, bool(x))
        return {u: v for u, v in out.items() if v is not None}

    async def save(self, uri: str, on: bool) -> None:
        """Put a track or an album in the listener's library, or take it out."""
        if not SAVES.fullmatch(uri):
            raise WebApiError("down")
        await self._change("PUT" if on else "DELETE", "me/library", uris=uri)
        for k in [k for k in self.kept if k.startswith(("me/library/contains", "me/tracks", "me/albums"))]:   # (what was known of the library is no longer so)
            del self.kept[k]
        self.keep("saved", uri, on)
        self.forget("library:tracks" if uri.startswith("spotify:track:") else "library:albums")

    async def last_added(self, playlist_id: str, total: int) -> str:
        """When a track was last added to the playlist (one of the listener's own), as Spotify
        writes the time; "" for an empty one. The newest are looked for at the end, where they
        are put unless the listener sorts the list by hand."""
        d = await self.get(f"playlists/{playlist_id}/items", keep=0, limit=50, offset=max(0, int(total or 0) - 50), fields="items(added_at)")
        return max((str(i.get("added_at") or "") for i in d.get("items") or [] if isinstance(i, dict)), default="")[:20]

    async def tracks(self, ids: list[str]) -> dict:
        """{id: row} for tracks known only by their id (the far end of the queue, which the
        player hands over as bare addresses). One question each, kept; those not found are left out."""
        async def one(i):
            hit = self.known("track", i)
            if hit:
                return hit
            try:
                t = await self.get(f"tracks/{i}")
            except WebApiError:
                return None
            if not t.get("name"):
                return None
            row = {**track_row(t), "cover_big": image((t.get("album") or {}).get("images"), 640)}
            self.keep("track", i, row)
            return row
        rows = await asyncio.gather(*(one(i) for i in ids))
        return {i: r for i, r in zip(ids, rows) if r}

    async def album(self, album_id: str) -> dict | None:
        hit = self.known("album", album_id)
        if hit:
            return hit
        d = await self.get(f"albums/{album_id}")
        if not d.get("uri"):
            return None
        out = {**album_row(d), "cover": image(d.get("images"), 640), "label": d.get("label") or "",
               "tracks": [track_row(t, d) for t in ((d.get("tracks") or {}).get("items") or []) if t.get("uri")]}
        self.keep("album", album_id, out)
        return out

    async def artist(self, artist_id: str) -> dict | None:
        hit = self.known("artist", artist_id)
        if hit:
            return hit
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
        out = {**artist_row(d), "cover": image(d.get("images"), 320), "albums": albums, **(await self.top_tracks(d.get("name", "")))}
        self.keep("artist", artist_id, out)
        return out

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
