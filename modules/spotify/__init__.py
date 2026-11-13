"""Spotify Lossless through Soloist, Spotify's own Connect endpoint for developers.

Soloist needs a personal developer key from a Premium account; Spotify's terms say the key
may not be shared and the program may not be redistributed, so the install script downloads
it from Spotify on the device and the key is entered in the panel.

Everything about Soloist lives here: the relay to its WebSocket (what plays, the queue,
commands), its key, name and volume normalization (also for guests' accounts), what the
current track really is (measured from the samples), the sample-exact verification, and
the alerts about it. The core only sees a source module.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import re
import tempfile
import time

from aiohttp import web
import websockets

import bitcheck
import collect
from . import folders
from . import webapi
from i18n import T
from plugins import SourcePlugin
from . import soloist as so

log = logging.getLogger("spotify")
BIN = "/usr/local/bin/soloist"
SOLOIST_WS = "ws://127.0.0.1:9090"
SPOTIFY_URI = re.compile(r"spotify:(track|album|playlist|artist|episode|show):[A-Za-z0-9]{22}")
AUTO_VERIFY_EVERY = 3600
AUTO_VERIFY_SECS = 20


class Spotify(SourcePlugin):
    id = "spotify"
    label = "Spotify Lossless"
    protocol = "Spotify Connect"
    name = {"sv": "Spotify Lossless (Soloist)", "en": "Spotify Lossless (Soloist)"}
    description = {"sv": "Spotify Connect i Lossless, bitperfekt. Kräver Premium och en egen utvecklarnyckel från Spotify.",
                   "en": "Spotify Connect in Lossless, bit-perfect. Needs Premium and your own developer key from Spotify."}
    user_units = ("soloist.service", "soloist-rt.service")
    hint = {"sv": "Välj {name} i Spotify-appen", "en": "Choose {name} in the Spotify app"}
    terms = {"sv": "Soloist hämtas från Spotify till den här enheten. Du behöver Spotify Premium och en egen nyckel, som du skapar på Spotifys utvecklarsida och anger här efteråt. Nyckeln är personlig.",
             "en": "Soloist is downloaded from Spotify to this device. You need Spotify Premium and a key of your own, which you create on Spotify's developer site and enter here afterwards. The key is personal."}
    terms_url = "https://developer.spotify.com/documentation/soloist"
    has_slider = True
    updates = "self"  # soloist-update.timer, nightly: Spotify's builds expire
    transport = ("toggle", "play", "pause", "next", "prev", "seek", "shuffle", "repeat")
    pushes_playing = True  # Soloist's events say when playback starts and stops here
    rt_proc = "soloist"   # its rendering threads run with realtime priority (soloist-rt)
    defaults = {"auto_verify": True, "soloist_normalization": False}
    alert_kinds = ({"id": "soloist", "label": {"sv": "Soloist har stannat", "en": "Soloist has stopped"}},
                   {"id": "verify", "label": {"sv": "En verifiering avviker", "en": "A verification deviates"}},
                   {"id": "expiry", "label": {"sv": "Spotify-bygget går snart ut", "en": "The Spotify build expires soon"}})

    def __init__(self, hub):
        super().__init__(hub)
        self.playback: dict = {}
        self.auth: dict = {}
        self.queue: dict = {}
        self.soloist_ws = None
        self.track_quality: dict = {}  # measured format of the current track
        self._norm_failed: tuple | None = None   # last set of accounts a write did not fix
        self._accounts: set[str] | None = None  # Spotify accounts seen (guests add new ones)
        self.cmd_lock = asyncio.Lock()  # one Soloist command at a time: its errors carry no request id
        self._cmd_reply: asyncio.Future | None = None
        # the verification log, manual and automatic (kept across restarts with the core's stats)
        d = hub.saved_stats
        self.last_test: dict | None = d.get("last_test")
        self.tests = collections.deque(d.get("tests", []), maxlen=200)
        lt = self.last_test
        if not self.tests and lt and lt.get("t"):  # seed the log with the pre-log result
            self.tests.append({k: lt.get(k) for k in ("t", "verdict", "bits", "gain_db", "seconds", "track")}
                              | {"auto": False, "gaps": len(lt.get("gaps") or [])})

    # ---- what the core asks of a module
    def tasks(self) -> list:
        return [self.soloist_loop(), self.stream_loop()]

    async def status_fields(self) -> dict:
        return {"soloist": await so.soloist_info()}

    def optimization_rows(self) -> list[dict]:
        norm = (self.hub.status.get("soloist") or {}).get("normalization")
        return [{"id": "norm", "title": T("Volymnormalisering av"), "why": T("Spotify sänker annars vissa låtar med flera dB."),
                 "ok": norm is False, "value": T("Av") if norm is False else (T("På") if norm else T("Okänt"))}]

    def current_name(self) -> str:
        return so.soloist_device_name()

    def hello(self) -> dict:
        return {"playback": self.playback, "auth": self.auth, "queue": self.queue, "last_test": self.last_test,
                "quality": self.track_quality, "tests": list(self.tests)[-30:], "spotify_search": self.web.ready(), "spotify_user": self.web.signed_in()}

    @property
    def web(self):
        """Spotify's Web API (search), with the listener's own app credentials."""
        if not hasattr(self, "_web"):
            self._web = webapi.WebApi(lambda: (self.hub.secret("spotify_client_id") or "", self.hub.secret("spotify_client_secret") or ""),
                                      lambda: self.hub.secret("about_lastfm_key") or "",
                                      refresh=lambda: self.hub.secret("spotify_refresh") or "", keep_refresh=self._keep_refresh,
                                      wait_file=os.path.join(collect.STATE_DIR, "spotify-webapi-wait"))
        return self._web

    # When a track was last added to each of the listener's own playlists, so that the ones in
    # use come first. Spotify says it only inside each playlist, so they are read off one at a
    # time, slowly, and again only when a playlist has changed (its snapshot is another).
    ADDED = os.path.join(collect.STATE_DIR, "spotify-playlist-added.json")

    def added(self) -> dict:
        if not hasattr(self, "_added"):
            try:
                with open(self.ADDED) as f:
                    d = json.load(f)
                self._added = {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}
            except (OSError, ValueError):
                self._added = {}
        return self._added

    def _added_save(self) -> None:
        fd = os.open(self.ADDED + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._added, f)
        os.replace(self.ADDED + ".tmp", self.ADDED)
        self.hub.persist(self.ADDED)

    def read_added(self, own: list[dict]) -> int:
        """Start reading off the playlists that are new or have changed; how many there are."""
        known = self.added()
        todo = [(p["id"], p.get("snap") or "", p.get("count") or 0) for p in own if p.get("id") and known.get(p["id"], {}).get("snap") != (p.get("snap") or "")]
        if todo and not self.web.asked()["wait"] and not (getattr(self, "_added_task", None) and not self._added_task.done()):
            self._added_task = asyncio.create_task(self._read_added(todo, {p["id"] for p in own if p.get("id")}))
        return len(todo)

    async def _read_added(self, todo: list, own: set) -> None:
        known, n = self.added(), 0
        try:
            for pid, snap, count in todo:
                if not self.web.signed_in():
                    break
                try:
                    known[pid] = {"snap": snap, "added": await self.web.last_added(pid, count)}
                except webapi.WebApiError as e:
                    if e.kind in ("busy", "credentials"):      # (Spotify asks for a pause: the rest another time)
                        break
                    known[pid] = {"snap": snap, "added": known.get(pid, {}).get("added", "")}
                n += 1
                if n % 40 == 0:
                    self._added_save()
                    await self.hub.broadcast({"type": "spotify_added"})
                await asyncio.sleep(12)        # (very slowly, two or three in a half minute: Spotify allows an app like this one little)
        finally:
            for pid in [k for k in known if k not in own]:        # (playlists that are no more)
                del known[pid]
            if n:
                self._added_save()
                await self.hub.broadcast({"type": "spotify_added"})

    def _keep_refresh(self, token: str) -> None:
        self.hub.set_secret("spotify_refresh", token)
        if not token:
            self.hub.set_secret("spotify_user", "")

    def summary(self) -> dict:
        return {"soloist": self._ha_soloist()}

    def save_state(self) -> dict:
        return {"last_test": self.last_test, "tests": list(self.tests)}

    def clear_history(self) -> None:
        self.tests.clear()
        self.forget_played()

    def forget_played(self) -> None:
        self._played = []
        with contextlib.suppress(OSError):
            os.unlink(self.HISTORY)

    async def tick(self) -> None:
        await self.maybe_auto_verify()
        await self.keep_normalization()

    def lost_dac(self) -> None:
        self.playback, self.queue = {}, {}

    def play_began(self) -> None:
        asyncio.ensure_future(self.measure_track_quality())

    async def check_health(self) -> None:
        hub, solo = self.hub, self.hub.status.get("soloist") or {}
        if hub._held("soloist", hub.soloist and so.api_key_info()["set"]
                     and solo.get("service") not in (None, "active"), 120):
            await hub.alert("soloist", T("Soloist har stannat"),
                            T("Spotify-tjänsten är {state}. Starta om den under Moduler i panelen.", state=solo.get("service")))
        days = solo.get("days_left")
        if days is not None and days <= 14:
            await hub.alert("expiry", T("Soloist-bygget går snart ut"),
                            T("{days} dagar kvar. Nattens uppdatering har inte hittat en nyare version; prova Sök ny version i panelen.", days=days),
                            cooldown=24 * 3600)

    options = (
        {"key": "spotify_client_id", "type": "secret", "needed": True, "guide": {"sv": ["Öppna [Spotifys utvecklarportal](https://developer.spotify.com/dashboard) och logga in med ditt Spotify-konto.", "Tryck Create app.", "Skriv ett namn och en beskrivning. Vad som helst går bra, till exempel mscOS.", "Under Redirect URIs, skriv `{panel}/api/spotify/callback` och tryck Add. Adressen måste stämma tecken för tecken.", "Börjar adressen ovan med http och inte https godtar Spotify den inte. Skriv då `http://127.0.0.1:8888/callback` i stället: sökningen fungerar, men inte Logga in med Spotify.", "Under frågan om vilka API:er du tänker använda, kryssa i Web API.", "Godkänn villkoren och tryck Save.", "Öppna appens Settings. Kopiera Client ID till fältet här.", "Tryck View client secret och kopiera den till fältet Client secret här under.", "Tryck Spara ändringar. Tryck sedan Logga in med Spotify och godkänn hos Spotify."],
                   "en": ["Open [Spotify's developer dashboard](https://developer.spotify.com/dashboard) and log in with your Spotify account.", "Press Create app.", "Enter a name and a description. Anything will do, mscOS for example.", "Under Redirect URIs, enter `{panel}/api/spotify/callback` and press Add. The address must match character for character.", "If the address above begins with http and not https, Spotify does not take it. Enter `http://127.0.0.1:8888/callback` instead: the search works, but Sign in with Spotify does not.", "Under the question of which APIs you plan to use, tick Web API.", "Accept the terms and press Save.", "Open the app's Settings. Copy the Client ID into the field here.", "Press View client secret and copy it into the Client secret field below.", "Press Save changes. Then press Sign in with Spotify and approve at Spotify."]},
         "label": {"sv": "Webb-API: Client ID", "en": "Web API: Client ID"},
         "help": {"sv": "För att söka i hela Spotify under Musik. Skapa en app i Spotifys utvecklarportal (Web API) och klistra in dess Client ID här och dess Client secret nedan.",
                  "en": "For searching all of Spotify under Music. Create an app in Spotify's developer dashboard (Web API) and paste its Client ID here and its Client secret below."},
         "link": {"url": "https://developer.spotify.com/dashboard", "label": {"sv": "Spotifys utvecklarportal", "en": "Spotify's developer dashboard"}}},
        {"key": "spotify_client_secret", "type": "secret", "label": {"sv": "Webb-API: Client secret", "en": "Web API: Client secret"},
         "help": {"sv": "Sparas bara på enheten och visas inte igen. Behövs inte när du loggar in med Spotify (knappen nedan): då räcker Client ID, och Musik visar även dina spellistor, sparade album och gillade låtar, och Lyssna säger vilken enhet som spelar. För inloggningen ska panelens adress med /api/spotify/callback på slutet stå som Redirect URI i appen hos Spotify.",
                  "en": "Kept on the device only and not shown again. Not needed when you sign in with Spotify (the button below): the Client ID is enough then, and Music also shows your playlists, saved albums and liked tracks, and Listen says which device plays. For the sign-in, the panel's address with /api/spotify/callback at the end must be entered as a Redirect URI in the app at Spotify."}},
        {"key": "soloist_normalization", "type": "toggle", "label": {"sv": "Normalisera volymen", "en": "Normalize the volume"},
         "help": {"sv": "Av ger bitperfekt ljud. Spotify-appens reglage når inte Soloist, så det styrs här. Soloist startas om när du ändrar.",
                  "en": "Off gives bit-perfect audio. The Spotify app's own switch does not reach Soloist, so it is set here. Soloist restarts when you change it."}},
        {"key": "soloist_key", "type": "secret", "needed": True, "guide": {"sv": ["Kontot som skapar nyckeln måste ha Spotify Premium.", "Öppna [Spotify for Developers](https://developer.spotify.com/dashboard) och logga in med ditt Spotify-konto. Första gången får du godkänna utvecklarvillkoren.", "Gå till [sidan för Soloist-nycklar](https://developer.spotify.com/dashboard/soloist) och skapa en nyckel där.", "Kopiera nyckeln, klistra in den i fältet här och tryck Spara ändringar. Soloist startas om och kontrolleras, vilket tar ungefär 15 sekunder.", "Nyckeln är personlig. Dela den inte, och klistra inte in den i inlägg eller loggar. Mer står i [Spotifys beskrivning av Soloist](https://developer.spotify.com/documentation/soloist)."],
                   "en": ["The account that creates the key must have Spotify Premium.", "Open [Spotify for Developers](https://developer.spotify.com/dashboard) and log in with your Spotify account. The first time you are asked to accept the developer terms.", "Go to [the page for Soloist keys](https://developer.spotify.com/dashboard/soloist) and create a key there.", "Copy the key, paste it into the field here and press Save changes. Soloist is restarted and checked, which takes about 15 seconds.", "The key is personal. Do not share it, and do not paste it into posts or logs. More is in [Spotify's description of Soloist](https://developer.spotify.com/documentation/soloist)."]},
         "label": {"sv": "Soloist: utvecklarnyckel", "en": "Soloist: developer key"},
         "placeholder": {"sv": "Klistra in en ny nyckel", "en": "Paste a new key"},
         "help": {"sv": "Nyckeln lagras bara på enheten och visas aldrig. Efter bytet startas Soloist om och kontrolleras, vilket tar ungefär 15 sekunder. Startar den inte, återställs den tidigare nyckeln.",
                  "en": "The key is stored on the device only and never shown. After the change Soloist is restarted and checked, which takes about 15 seconds. If it does not start, the previous key is put back."},
         "link": {"url": "https://developer.spotify.com/documentation/soloist",
                  "label": {"sv": "Skapa en egen nyckel på Spotifys utvecklarsida", "en": "Create a key of your own on Spotify's developer site"}}},
    )
    actions = (
        {"name": "restart", "label": {"sv": "Starta om Soloist", "en": "Restart Soloist"}},
        {"name": "update", "label": {"sv": "Sök ny version", "en": "Look for a new version"}},
        # ("account": shown under Profile too, with the keys)
        {"name": "login", "account": True, "label": {"sv": "Logga in med Spotify", "en": "Sign in with Spotify"}},
        {"name": "logout", "account": True, "label": {"sv": "Logga ut från Spotify", "en": "Sign out of Spotify"}},
    )

    def actions_now(self) -> list[dict]:
        gone = "login" if self.web.signed_in() else "logout"      # (one of the two at a time)
        return [a for a in self.actions if a["name"] != gone]

    def update_settings(self, b: dict, upd: dict) -> None:
        if "auto_verify" in b:
            upd["auto_verify"] = bool(b["auto_verify"])
        if "soloist_normalization" in b:
            if not so.prefs_paths():
                raise web.HTTPConflict(text=T("Soloist har inte loggat in än. Anslut från Spotify-appen först."))
            upd["soloist_normalization"] = bool(b["soloist_normalization"])

    async def settings_applied(self, upd: dict) -> None:
        if "soloist_normalization" in upd:
            await self.apply_normalization(upd["soloist_normalization"])

    async def set_secret(self, key: str, value: str) -> None:
        if key == "soloist_key":       # Soloist's own key lives in its own file
            if value:
                await self.set_key(value)
        else:
            # (Spotify's Client ID and secret are 32 hexadecimal characters each: anything else is a slip)
            if key.startswith("spotify_client") and value and not re.fullmatch(r"[0-9a-f]{32}", value.strip()):
                raise web.HTTPBadRequest(text=T("Det ser inte ut som ett Client ID eller en Client secret från Spotify: de är 32 tecken, siffror och a till f."))
            await super().set_secret(key, value.strip() if key.startswith("spotify_client") else value)
            if key.startswith("spotify_client"):
                self.web.token = ""
                if key == "spotify_client_id":       # (a sign-in belongs to the app it was made with)
                    self.web.sign_out()

    def secret_set(self, key: str) -> bool:
        return so.api_key_info()["set"] if key == "soloist_key" else super().secret_set(key)

    async def action(self, name: str) -> dict | None:
        if name == "restart":
            rc, _, err = await self.hub.user_systemctl("restart", "soloist.service")
            if rc:
                raise RuntimeError(err.strip()[-200:])
            return {"toast": {"sv": "Soloist startades om", "en": "Soloist was restarted"}}
        if name == "login":
            # Spotify sends the answer back to an address it has been told of beforehand, and
            # takes none but https ones: the panel's own, when it is reached over https.
            public = os.environ.get("MSCOS_PUBLIC_URL", "").strip().rstrip("/")
            if not public.lower().startswith("https://"):
                raise RuntimeError(T("Inloggning med Spotify kräver att panelen nås över https (MSCOS_PUBLIC_URL). Spotify skickar inte svaret till en vanlig http-adress."))
            if not (self.hub.secret("spotify_client_id") or "").strip():
                raise RuntimeError(T("Ange appens Client ID först, här ovanför."))
            return {"open": self.web.sign_in_address(public + "/api/spotify/callback")}
        if name == "logout":
            self.web.sign_out()
            await self.hub.broadcast({"type": "spotify_user", "on": False})
            return {"toast": {"sv": "Utloggad från Spotify på den här enheten", "en": "Signed out of Spotify on this device"}}
        await self.hub.run_job("soloist-update", ["/usr/local/sbin/soloist-update"])
        return None

    def status(self) -> dict | None:
        solo = self.hub.status.get("soloist") or {}
        days = solo.get("days_left")
        if not so.api_key_info()["set"]:
            return {"state": "warn", "text": {"sv": "Ingen nyckel är inställd", "en": "No key is entered"}}
        if solo.get("service") not in (None, "active"):
            return {"state": "warn", "text": {"sv": "Soloist kör inte", "en": "Soloist is not running"}}
        if days is not None:
            return {"state": "warn" if days < 14 else "ok",
                    "text": {"sv": f"Bygget gäller {days} dagar till", "en": f"The build is valid for {days} more days"}}
        return None

    def facts(self) -> list[dict]:
        solo, k = self.hub.status.get("soloist") or {}, so.api_key_info()
        if not solo:
            return []
        yes = lambda ok, sv, en, sv2, en2: {"value": {"sv": sv if ok else sv2, "en": en if ok else en2}, "state": "good" if ok else "warn"}  # noqa: E731
        nxt = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", str(solo.get("update_next") or ""))
        changed = time.strftime("%Y-%m-%d %H:%M", time.localtime(k["changed"])) if k.get("changed") else ""
        who = str(self.hub.secret("spotify_user") or "")[:60]
        asked = self.web.asked()
        return [
            {"label": {"sv": "Tjänst", "en": "Service"}, **yes(solo.get("service") == "active", "Igång", "Running", str(solo.get("service") or "Okänd"), str(solo.get("service") or "Unknown"))},
            {"label": {"sv": "Spotify", "en": "Spotify"}, **yes(bool(self.auth.get("logged_in")), "Inloggad", "Signed in", "Inte inloggad: välj enheten i Spotify-appen", "Not signed in: choose the device in the Spotify app")},
            {"label": {"sv": "Spotify-konto", "en": "Spotify account"}, "account": True, "state": "good" if self.web.signed_in() else "",
             "value": ({"sv": f"Inloggad{' som ' + who if who else ''}", "en": f"Signed in{' as ' + who if who else ''}"} if self.web.signed_in()
                       else {"sv": "Inte inloggad: Musik visar inte ditt bibliotek", "en": "Not signed in: Music does not show your library"})},
            {"label": {"sv": "Frågor till Spotifys webb-API", "en": "Questions to Spotify's Web API"}, "state": "warn" if asked["wait"] or asked["refused"] else "",
             "value": {"sv": f"{asked['half_minute']} senaste halvminuten (högst {asked['limit']}), {asked['hour']} senaste timmen"
                             + (f". Spotify har bett om paus {asked['refused']} gånger" if asked["refused"] else "") + (f", {asked['wait']} s kvar" if asked["wait"] else ""),
                       "en": f"{asked['half_minute']} in the last half minute (at most {asked['limit']}), {asked['hour']} in the last hour"
                             + (f". Spotify has asked for a pause {asked['refused']} times" if asked["refused"] else "") + (f", {asked['wait']} s left" if asked["wait"] else "")}},
            {"label": {"sv": "Realtidsprioritet", "en": "Realtime priority"}, **yes(solo.get("rt_watcher") == "active", "Aktiv", "On", "Av", "Off")},
            {"label": {"sv": "Nyckel", "en": "Key"}, **yes(k["set"], f"Inställd{', ändrad ' + changed if changed else ''}", f"Entered{', changed ' + changed if changed else ''}", "Saknas: Soloist kan inte starta", "Missing: Soloist cannot start")},
            {"label": {"sv": "Nästa versionskoll", "en": "Next version check"}, "value": nxt.group(0) if nxt else "–", "state": ""},
        ]

    # ---- source
    async def installed(self) -> bool:
        return os.path.exists(BIN) and os.path.exists(so.SOLOIST_UNIT)

    async def start(self) -> str:
        if not so.api_key_info()["set"]:
            return ""  # installed but no key yet: nothing to start, the panel asks for the key
        return await super().start()

    async def state(self) -> dict:
        return {"key_set": so.api_key_info()["set"]}

    async def version(self) -> str | None:
        v = ((self.hub.status.get("soloist") or {}).get("version") or "").replace("soloist ", "")
        return v.split(" ")[0] or None

    def now_playing(self) -> dict | None:
        s = self._ha_soloist()
        if not s["reachable"] or not s["title"]:
            return None
        return {"title": s["title"], "artist": s["artist"], "album": s["album"], "image": s["image_url"], "uri": s["uri"],
                "playing": s["status"] == "playing", "duration_ms": s["duration_ms"], "position_ms": s["position_ms"],
                "position_ts": s["position_ts"], "shuffle": bool(s["shuffle"]), "repeat": s["repeat"] or "off"}

    async def command(self, cmd: str, **kw) -> None:
        send = lambda c, **a: self.soloist_cmd({"command": c, **a})  # noqa: E731
        try:
            if cmd == "toggle":
                await send("pause" if self.playback.get("status") == "playing" else "play")
            elif cmd in ("play", "pause"):
                await send(cmd)
            elif cmd in ("next", "prev"):
                await send(f"skip_{cmd}")
            elif cmd == "seek":
                await send("seek", position_ms=int(kw["position_ms"]))
            elif cmd == "shuffle":
                await send("set_shuffle", enabled=bool(kw.get("enabled")))
            elif cmd == "repeat":
                await send("set_repeat_track", enabled=kw.get("mode") == "track")
                await send("set_repeat_context", enabled=kw.get("mode") == "context")
        except web.HTTPException as e:
            raise RuntimeError(e.text)

    async def names_changed(self) -> None:
        if await self.installed():
            await self.set_soloist_name(self.hub.names()["network"])

    def slider(self) -> dict | None:
        return {"active": bool(self.soloist_ws and self.auth.get("is_active")), "volume": self.playback.get("volume"),
                "digital": not self.hub.vol.enabled()}

    async def set_slider(self, pct: int) -> None:
        if self.soloist_ws:
            await self.soloist_cmd({"command": "set_volume", "volume": pct})

    # ---- the relay to Soloist's WebSocket
    async def soloist_loop(self):
        while True:
            try:
                async with websockets.connect(SOLOIST_WS, max_size=None, open_timeout=5) as ws:
                    self.soloist_ws = ws
                    self._was_active = None  # the first auth_state only records the current state
                    self._playing_known = False  # likewise the first playback_state
                    await ws.send(json.dumps({"type": "command", "command": "get_auth_state"}))
                    async for raw in ws:
                        ev = json.loads(raw)
                        if ev.get("type") in ("error", "command_result") and self._cmd_reply and not self._cmd_reply.done():
                            self._cmd_reply.set_result(ev)
                        if ev.get("type") in ("device_changed", "playback_changed"):
                            log.info("soloist %s: active=%s status=%s", ev["type"], ev.get("is_active", self.auth.get("is_active")),
                                     ev.get("status", self.playback.get("status")))
                        self._apply_soloist(ev)
                        self._on_playback_change()
                        await self.hub.broadcast({"type": "soloist", "event": ev})
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError):
                pass
            self.soloist_ws = None
            self.auth = {}
            await self.hub.broadcast({"type": "soloist", "event": {"type": "disconnected"}})
            await asyncio.sleep(3)

    def _apply_soloist(self, ev: dict):
        t = ev.get("type")
        if t == "auth_state":
            self.auth = ev
        elif t == "playback_state":
            self.playback = ev
            if ev.get("status") == "playing":   # already playing when the panel (re)connected
                self.note_played(ev.get("item"))
        elif t == "track_changed":
            asyncio.ensure_future(self.measure_track_quality())
            self.playback["item"] = ev.get("item")
            self.note_played(ev.get("item"))
        elif t == "playback_changed":
            self.playback["status"] = ev.get("status")
        elif t == "volume_changed":
            self.playback["volume"] = ev.get("volume")
            self.hub.vol.on_volume_changed(ev.get("volume"))
        elif t == "position_sync":
            self.playback["position"] = ev.get("position")
        elif t == "options_changed":
            self.playback["options"] = ev.get("options")
        elif t == "queue_changed":
            self.queue = ev
        elif t == "device_changed":
            self.auth = {**self.auth, "is_active": ev.get("is_active"), "device_name": ev.get("device_name")}

    async def soloist_cmd(self, msg: dict, wait: float = 0.0) -> dict | None:
        """Send a command. With wait, return Soloist's error or command_result if one arrives
        within that time (None: accepted without complaint)."""
        if not self.soloist_ws:
            raise web.HTTPServiceUnavailable(text=T("Soloist svarar inte"))
        if not wait:
            await self.soloist_ws.send(json.dumps({"type": "command", **msg}))
            return None
        async with self.cmd_lock:
            self._cmd_reply = asyncio.get_running_loop().create_future()
            try:
                await self.soloist_ws.send(json.dumps({"type": "command", **msg}))
                return await asyncio.wait_for(asyncio.shield(self._cmd_reply), wait)
            except asyncio.TimeoutError:
                return None
            finally:
                self._cmd_reply = None

    async def wait_active(self, secs: float = 8.0) -> bool:
        end = time.time() + secs
        while time.time() < end:
            if self.auth.get("is_active"):
                return True
            await asyncio.sleep(0.25)
        return bool(self.auth.get("is_active"))

    async def keep_normalization(self):
        """A guest's new account starts with Spotify's normalization on (-5 dB, not
        bit-perfect). Fix every account's prefs, but only after 10 quiet minutes: Soloist
        reads them at start, and a restart would cut off someone listening."""
        if not self.hub.soloist:
            return
        accounts = set(so.account_dirs())
        if self._accounts is None:  # first look: what is there now is not "new"
            self._accounts = accounts
        new = accounts - self._accounts
        self._accounts |= accounts
        want = bool(self.hub.settings.get("soloist_normalization", False))
        wrong = so.normalization_mismatch(want)
        if not wrong:
            self._norm_failed = None
            return
        # never restart Soloist again for the same accounts after a write that did not stick
        # (one attempt per set of accounts; a new account, or a manual change, retries)
        key = (want, tuple(wrong))
        if key == self._norm_failed:
            return
        if new and any(w.startswith(tuple(new)) for w in wrong):
            # A guest's first connection: fix it now and bring their playback straight back,
            # so it is bit-perfect from the first song (a few seconds' pause, once).
            playing, active = self.hub._was_playing, bool(self.auth.get("is_active"))
            log.info("new Spotify account: normalization %s, restarting Soloist", "on" if want else "off")
            async with self.hub.source_lock:
                ok_ = await self.apply_normalization(want)
            if not ok_:
                self._norm_failed = (want, tuple(so.normalization_mismatch(want)))
                log.warning("normalization could not be written for %s", self._norm_failed[1])
            asyncio.ensure_future(self._resume_after_restart(active, playing))
            return
        if self.hub._was_playing or time.time() - self.hub._idle_since < 600:
            return  # an existing account: only when nobody is listening
        log.info("normalization %s for %d account(s); restarting Soloist", "on" if want else "off", len(wrong))
        async with self.hub.source_lock:
            ok_ = await self.apply_normalization(want)
        if not ok_:
            self._norm_failed = (want, tuple(so.normalization_mismatch(want)))
            log.warning("normalization could not be written for %s", self._norm_failed[1])

    async def _resume_after_restart(self, active: bool, playing: bool):
        """After a Soloist restart: take the device back (activate) and resume, as before."""
        if not active:
            return
        for _ in range(40):  # Soloist logs in again with the stored credentials
            await asyncio.sleep(0.5)
            if self.soloist_ws and self.auth.get("logged_in"):
                break
        else:
            return
        await asyncio.sleep(1)
        try:
            await self.soloist_cmd({"command": "activate"}, wait=2)
            if playing:
                await self.wait_active(8)
                await self.soloist_cmd({"command": "play"}, wait=2)
            log.info("Soloist back: active%s", ", playing" if playing else "")
        except Exception as e:  # noqa: BLE001
            log.warning("resume after restart: %s", e)

    # ---- what has been played here: the albums, tracks and artists, to find and play again
    HISTORY = os.path.join(collect.STATE_DIR, "spotify-history.json")

    def _history(self) -> list:
        if not hasattr(self, "_played"):
            try:
                with open(self.HISTORY, encoding="utf-8") as f:
                    d = json.load(f)
                self._played = d if isinstance(d, list) else []
            except (OSError, ValueError):
                self._played = []
        return self._played

    def note_played(self, item) -> None:
        """A track began on this device: remember it, with the album and the artists it belongs to."""
        try:
            deco = lambda e: (e or {}).get("decorations") or {}   # noqa: E731
            name = lambda e: (deco(e).get("identity") or {}).get("name") or ""   # noqa: E731
            uri = (item or {}).get("uri") or ""
            if not SPOTIFY_URI.fullmatch(uri) or self.auth.get("is_active") is False or not self.hub.settings.get("listen_history", True):
                return
            who = ", ".join(name(c.get("entity")) for c in (deco(item).get("creators") or [])[:6] if name(c.get("entity")))
            self.hub.note_listened(self.id, name(item), who, name((deco(item).get("parent") or {}).get("entity")),
                                   (deco(item).get("playback") or {}).get("duration_ms"))
            h = self._history()
            if h and h[-1].get("uri") == uri:
                return
            album = (deco(item).get("parent") or {}).get("entity") or {}
            covers = (deco(item).get("visual_identity") or {}).get("cover") or []
            cover = next((c.get("url") for c in covers if c.get("size") == "large"), None) or (covers[-1].get("url") if covers else None)
            h.append({"t": int(time.time()), "uri": uri, "title": name(item)[:200],
                      "artists": [{"name": name(c.get("entity"))[:120], "uri": (c.get("entity") or {}).get("uri") or ""}
                                  for c in (deco(item).get("creators") or [])[:6] if name(c.get("entity"))],
                      "album": {"name": name(album)[:200], "uri": album.get("uri") or ""},
                      "cover": cover if isinstance(cover, str) and cover.startswith("https://") else None})
            del h[:-600]
            with open(self.HISTORY + ".new", "w", encoding="utf-8") as f:
                json.dump(h, f, ensure_ascii=False)
            os.replace(self.HISTORY + ".new", self.HISTORY)
        except Exception as e:  # noqa: BLE001 - a list of what was played must never get in the way of playing
            log.warning("history: %s", e)

    def played(self) -> dict:
        """The albums, tracks and artists played here, the latest first, each once."""
        albums, tracks, artists = {}, {}, {}
        for e in reversed(self._history()):
            a = e.get("album") or {}
            who = ", ".join(x.get("name", "") for x in e.get("artists", []))
            if SPOTIFY_URI.fullmatch(a.get("uri") or "") and a["uri"] not in albums:
                albums[a["uri"]] = {"uri": a["uri"], "name": a.get("name", ""), "artist": who, "cover": e.get("cover"), "t": e.get("t")}
            if e.get("uri") not in tracks:
                tracks[e["uri"]] = {"uri": e["uri"], "name": e.get("title", ""), "artist": who, "album": a.get("name", ""), "cover": e.get("cover"), "t": e.get("t")}
            for x in e.get("artists", []):
                if SPOTIFY_URI.fullmatch(x.get("uri") or "") and x["uri"] not in artists:
                    artists[x["uri"]] = {"uri": x["uri"], "name": x.get("name", ""), "t": e.get("t")}
        return {"albums": list(albums.values())[:120], "tracks": list(tracks.values())[:200], "artists": list(artists.values())[:120]}

    async def measure_track_quality(self):
        """Measure the track that plays, and then the one that plays when that is done: a track
        that began while another was being measured (or waited for) is not left unmeasured.
        One measurement at a time; a few tries for a track, then it is left alone."""
        if getattr(self, "_quality_busy", False):
            return
        self._quality_busy = True
        try:
            tries: dict[str, int] = {}
            while True:
                uri = (self.playback.get("item") or {}).get("uri")
                # (whether it plays is looked at after the wait in _measure_once: a track is
                # still buffering when it is announced)
                tq = self.track_quality or {}
                if not uri or not self.hub.soloist:
                    return
                if tq.get("uri") == uri:
                    # Measured. A track found lossy is looked at again every half minute while it
                    # plays (a few times): the page says how it is now, not how it began.
                    if tq.get("verdict") != "lossy_or_processed" or tries.get(uri, 0) >= 8:
                        return
                    await asyncio.sleep(25)
                    if (self.playback.get("item") or {}).get("uri") != uri:
                        continue
                elif tries.get(uri, 0) >= 3:
                    return
                tries[uri] = tries.get(uri, 0) + 1
                await self._measure_once(uri)
        finally:
            self._quality_busy = False

    async def _measure_once(self, uri: str):
        """3 s of what goes to the DAC, a few seconds into the track: on the 16-bit lattice
        means a 16-bit file, content below it 24-bit, no lattice at all lossy (or processed).
        Read-only tap on the sink monitor; skipped while a verification runs."""
        try:
            await asyncio.sleep(4)  # past the track start (gapless join, fades)
            if (self.playback.get("item") or {}).get("uri") != uri or self.playback.get("status") != "playing" or not self.auth.get("is_active") \
                    or self.hub.jobs.get("audio-test", {}).get("running"):
                log.info("track quality: skipped (track changed, paused, played elsewhere or a verification running)")
                return
            sink = (self.hub.status.get("pipewire") or {}).get("sink") or {}
            if not sink.get("name"):
                return
            uid = await collect.audio_uid()
            fd, wav = tempfile.mkstemp(suffix=".wav", dir="/tmp")
            os.close(fd)
            os.chown(wav, uid, -1)
            try:
                await collect.run(["timeout", "4", "pw-record", "-P", "{ stream.capture.sink=true }",
                                   "--target", sink["name"], "--rate", "44100", "--channels", "2",
                                   "--format", "f32", wav], as_user=True, timeout=20)

                def analyse():
                    x = bitcheck.read_f32_wav(wav)
                    # half a second in, and two seconds at least (x is frames, 44100 a second)
                    return bitcheck.lattice(x[22050:]) if len(x) > 2 * 44100 else {"verdict": "too_short"}
                r = await asyncio.get_running_loop().run_in_executor(None, analyse)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(wav)
            if (self.playback.get("item") or {}).get("uri") != uri:
                return  # the track changed while measuring
            v = r.get("verdict")
            if v in ("silent", "too_short"):
                log.info("track quality: %s, not recorded", v)
                return
            if v == "lossy_or_processed" and getattr(self, "_lossy_once", None) != uri and (self.track_quality or {}).get("uri") != uri:
                # One look can be wrong (a dropout or a seek inside the three seconds breaks the
                # pattern as lossy audio does): said only when a second look agrees.
                self._lossy_once = uri
                log.info("track quality: lossy at the first look, measuring once more")
                return
            self.track_quality = {"uri": uri, "t": int(time.time()), "verdict": v, "bits": r.get("bits"),
                                  "gain_db": r.get("gain_db"), "rate": 44100}
            log.info("track quality: %s %s bit", v, r.get("bits"))
            await self.hub.broadcast({"type": "quality", "quality": self.track_quality})
        except Exception as e:  # noqa: BLE001 - never disturb playback over this
            log.warning("track quality: %s", e)

    async def maybe_auto_verify(self):
        if not self.hub.settings.get("auto_verify") or self.hub.jobs.get("audio-test", {}).get("running"):
            return
        last_auto = max((t["t"] for t in self.tests if t.get("auto")), default=0)
        if time.time() - last_auto < AUTO_VERIFY_EVERY:
            return
        p = self.playback
        if p.get("status") != "playing" or (self.hub.status.get("dac") or {}).get("state") != "RUNNING":
            return
        if self.auth.get("is_active") is False:  # the account plays on another device: nothing to record here
            return
        pos = p.get("position") or {}
        dur = (((p.get("item") or {}).get("decorations") or {}).get("playback") or {}).get("duration_ms") or 0
        at = pos.get("position_ms", 0) + (time.time() * 1000 - pos.get("timestamp_ms", time.time() * 1000))
        # Mid-track only: a track change inside the window would show up as a gap.
        if at < 15000 or dur - at < (AUTO_VERIFY_SECS + 15) * 1000:
            return
        await self.run_audio_test(AUTO_VERIFY_SECS, auto=True)

    async def run_audio_test(self, secs: int, auto: bool = False):
        sink = (self.hub.status.get("pipewire") or {}).get("sink") or {}
        if not sink.get("name"):
            raise web.HTTPConflict(text=T("Ingen USB-DAC hittades i PipeWire"))
        job = {"name": "audio-test", "running": True, "output": "", "rc": None, "started": time.time(),
               "seconds": secs, "auto": auto}
        self.hub.jobs["audio-test"] = job
        await self.hub.broadcast({"type": "job", "name": "audio-test", "started": True, "auto": auto, "seconds": secs})

        async def go():
            uid = await collect.audio_uid()
            fd, wav = tempfile.mkstemp(suffix=".wav", dir="/tmp")
            os.close(fd)
            os.chown(wav, uid, -1)
            try:
                await collect.run(["timeout", str(secs + 1), "pw-record", "-P", "{ stream.capture.sink=true }",
                                   "--target", sink["name"], "--rate", "44100", "--channels", "2",
                                   "--format", "f32", wav], as_user=True, timeout=secs + 15)

                def analyse():
                    x = bitcheck.read_f32_wav(wav)
                    r = bitcheck.lattice(x[44100:]) if len(x) > 2 * 44100 else {"verdict": "too_short"}
                    r["gaps"] = bitcheck.find_gaps(x)
                    r["seconds"] = round(len(x) / 44100, 1)
                    r["rms"] = round(float((x.astype("float64") ** 2).mean() ** 0.5), 4) if len(x) else 0
                    return r

                res = await asyncio.get_running_loop().run_in_executor(None, analyse)
                res["t"] = int(time.time())
                res["auto"] = auto
                item = self.playback.get("item") or {}
                deco = item.get("decorations") or {}
                res["track"] = deco.get("identity", {}).get("name")
                silent = res.get("verdict") in ("silent", "too_short") or res.get("rms", 1) < 1e-4
                if not silent:  # a silent capture says nothing about the chain
                    self.last_test = res
                    self.tests.append({k: res.get(k) for k in ("t", "auto", "verdict", "bits", "gain_db", "seconds", "track")}
                                      | {"gaps": len(res.get("gaps") or [])})
                    if res.get("verdict") != "bitperfect" or res.get("gaps"):
                        what = T("avbrott") if res.get("verdict") == "bitperfect" else T("resultat: {verdict}", verdict=res.get("verdict"))
                        await self.hub.alert("verify", T("Verifieringen hittade avvikelser"),
                                         T("Automatisk verifiering kl {time}: {what}." if auto else "Manuell verifiering kl {time}: {what}.",
                                           time=time.strftime("%H:%M"), what=what))
                job["result"] = res
                job["rc"] = 0
            except Exception as e:  # report, never crash the server
                job["rc"] = 1
                job["output"] = str(e)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(wav)
                job["running"] = False
                await self.hub.broadcast({"type": "job", "name": "audio-test", "done": True, "rc": job["rc"], "auto": auto,
                                      "result": job.get("result"), "error": job["output"],
                                      "tests": list(self.tests)[-30:]})
                await asyncio.get_running_loop().run_in_executor(None, self.hub.save)

        asyncio.create_task(go())
        return job

    def _on_playback_change(self):
        if not self.hub.soloist:
            return
        # Choosing "Mark Levinson 585.5" in the Spotify app already wakes the amplifier and
        # selects its input, before anything plays (the module has a 90 s guard, so play right after
        # does not send it twice).
        if "is_active" not in self.auth:
            return
        active = bool(self.auth.get("is_active"))
        prev = getattr(self, "_was_active", None)  # None: first report after (re)connecting
        if active and prev is False:
            self.hub.play_started()
        if active and prev is not True:
            self.hub.vol.on_active()  # slider from the amplifier, after Spotify restored its own
        self._was_active = active
        # Soloist mirrors the account's playback: "playing" also arrives while another device
        # (the phone) plays. Only playback on this device counts as a start.
        playing = active and self.playback.get("status") == "playing"
        if not getattr(self, "_playing_known", True) and "status" in self.playback:
            # first report after (re)connecting: music already playing is not a new start,
            # so a panel restart never wakes the amplifier
            self._playing_known = True
            self.hub._was_playing = playing
            if playing:  # already playing when we (re)connected: still measure this track
                asyncio.ensure_future(self.measure_track_quality())
            if not playing:
                self.hub._idle_since = time.time()
            return
        self.hub._set_playing(playing)

    async def set_soloist_name(self, name: str) -> bool:
        """Soloist's name in the Spotify app (an argument in its unit). True if it changed;
        Soloist is restarted then. Read and written as the audio user: the unit lives in its
        home, and root follows nothing there."""
        name = re.sub(r"[^\w .\-]", "", name)[:40].strip() or "mscOS"  # what Soloist takes
        p = so.SOLOIST_UNIT
        rc, text, _ = await collect.run(["cat", p], as_user=True, timeout=5)
        if rc or "--device-name" not in text:
            raise RuntimeError(T("Soloist är inte installerat"))
        new = re.sub(r'--device-name\s+"[^"]*"', lambda m: f'--device-name "{name}"', text)
        if new == text:
            return False
        rc, _, err = await collect.run(["sh", "-c", 'cat > "$1.tmp" && mv -f "$1.tmp" "$1"', "sh", p], as_user=True,
                                       timeout=5, input_=new.encode())
        if rc:
            raise RuntimeError(err.strip()[:200])
        self.hub.persist(p)
        await self.hub.user_systemctl("daemon-reload")
        if self.hub.soloist:
            await self.hub.user_systemctl("restart", "soloist.service")
        return True

    # ---- the Spotify stream is kept at unity while the slider drives the amplifier
    async def stream_loop(self):
        """Keep the Spotify stream at unity: watch PipeWire (pw-dump -m) and set softVolumes
        to 1.0 after every new channelVolumes from Soloist, also on a newly created stream.
        Only Soloist needs this; other sources leave the samples alone themselves."""
        while True:
            if not (self.hub.vol.enabled() and self.hub.soloist):
                await asyncio.sleep(10)
                continue
            proc = None
            try:
                proc = await collect.spawn_as_user(["pw-dump", "-m", "-N"])
                dec, buf = json.JSONDecoder(), ""
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk.decode(errors="replace")
                    while True:
                        buf = buf.lstrip()
                        if not buf:
                            break
                        try:
                            objs, end = dec.raw_decode(buf)
                        except ValueError:
                            break  # incomplete
                        buf = buf[end:]
                        for o in objs:
                            oid = so.spotify_stream_changed(o) if isinstance(o, dict) else None
                            if oid is not None:
                                await so.spotify_stream_unity(oid)
                    if not (self.hub.vol.enabled() and self.hub.soloist):
                        break
            except Exception:
                log.exception("stream watch")
            finally:
                if proc and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            await asyncio.sleep(3)

    # ---- state for automations, and the normalization writer
    def _ha_soloist(self) -> dict:
        connected = self.soloist_ws is not None
        a, p = self.auth, self.playback
        it = p.get("item") or {}
        deco = it.get("decorations") or {}
        ctx = p.get("context") or {}
        name = lambda e: (((e or {}).get("decorations") or {}).get("identity") or {}).get("name")  # noqa: E731

        def cover(e):
            c = (((e or {}).get("decorations") or {}).get("visual_identity") or {}).get("cover") or []
            best = next((x for x in c if x.get("size") == "large"), None) or (c[-1] if c else {})
            return best.get("url")

        active = bool(a.get("is_active")) if connected and "is_active" in a else None
        st = p.get("status")
        pos = p.get("position") or {}
        opts = p.get("options") or {}
        return {
            "reachable": connected,
            "logged_in": a.get("logged_in") if connected else None,
            "active": active,
            # what this device does; the account playing on another device is "stopped" here
            "status": None if not connected else (st if active and st in ("playing", "paused") else "stopped"),
            "title": name(it) if it else None,
            "artist": ", ".join(filter(None, (name(c.get("entity")) for c in deco.get("creators") or []))) or None,
            "album": name((deco.get("parent") or {}).get("entity")),
            "uri": it.get("uri"),
            "context_uri": ctx.get("uri"),
            "image_url": cover(it) or cover(ctx),
            "duration_ms": (deco.get("playback") or {}).get("duration_ms"),
            # measured from the samples, for this track only: "flac24", "flac16", "lossy" or None
            "quality": self._ha_quality(it.get("uri")),
            "position_ms": pos.get("position_ms"),
            "position_ts": pos["timestamp_ms"] / 1000 if pos.get("timestamp_ms") else None,
            "shuffle": opts.get("shuffle"),
            "repeat": opts.get("repeat"),
            "device_name": a.get("device_name"),
        }

    def _ha_quality(self, uri) -> str | None:
        q = self.track_quality
        if not uri or q.get("uri") != uri:
            return None
        if q.get("verdict") == "bitperfect":
            return "flac24" if q.get("bits") == 24 else "flac16" if q.get("bits") == 16 else None
        return "lossy" if q.get("verdict") == "lossy_or_processed" else None

    async def apply_normalization(self, enabled: bool) -> bool:
        """Write the setting into every account's prefs (Soloist reads them at start).
        Returns whether every account now has it."""
        await self.hub.user_systemctl("stop", "soloist.service")
        for d in so.account_dirs():
            if await so.set_normalization_file(d + "prefs", enabled):
                self.hub.persist(d + "prefs")
        await self.hub.user_systemctl("start", "soloist.service")
        return not so.normalization_mismatch(enabled)

    async def set_key(self, key: str) -> dict:
        """Replace the Spotify developer key. Never echoed or logged. Rolled back if Soloist
        does not stay up with the new key."""
        if not re.fullmatch(r"[A-Za-z0-9_\-.:+/=]{16,512}", key):
            raise web.HTTPBadRequest(text=T("Nyckeln ser inte giltig ut: 16–512 tecken, bokstäver, siffror och _-.:+/=."))
        path = so.SOLOIST_ENV
        old = collect.read(path)
        if f"SOLOIST_API_KEY={key}" in old:
            return {"changed": False}
        st = os.stat(path) if os.path.exists(path) else None

        def write(text: str):
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            if st:
                os.chown(tmp, st.st_uid, st.st_gid)
            os.replace(tmp, path)
            self.hub.persist(path)

        new_text = re.sub(r"^SOLOIST_API_KEY=.*$", f"SOLOIST_API_KEY={key}", old, flags=re.M) \
            if re.search(r"^SOLOIST_API_KEY=", old, re.M) else old.rstrip("\n") + f"\nSOLOIST_API_KEY={key}\n"
        write(new_text)
        if not self.hub.soloist:  # another source owns the DAC: the key is used when Spotify is chosen
            return {"changed": True, "started": False}
        await self.hub.user_systemctl("restart", "soloist.service")
        # Baseline after the manual restart (which resets the automatic-restart counter).
        _, before, _ = await self.hub.user_systemctl("show", "soloist.service", "-p", "NRestarts", "--value")
        await asyncio.sleep(12)  # Soloist restarts after 5 s if the key is rejected
        _, active, _ = await self.hub.user_systemctl("is-active", "soloist.service")
        _, after, _ = await self.hub.user_systemctl("show", "soloist.service", "-p", "NRestarts", "--value")
        if active.strip() != "active" or after.strip() != before.strip():
            write(old)
            await self.hub.user_systemctl("restart", "soloist.service")
            raise web.HTTPUnprocessableEntity(text=T("Soloist startade inte med den nya nyckeln. Den tidigare nyckeln är återställd."))
        await self.hub.refresh_status()
        return {"changed": True}

    # ---- HTTP API
    def routes(self, routes) -> None:
        hub, sp = self.hub, self

        @routes.post("/api/soloist/command")
        async def soloist_command(request):
            b = await hub.body(request)
            c = b.get("command")
            allowed = {"play", "pause", "skip_next", "skip_prev", "seek", "set_volume", "set_shuffle",
                       "set_repeat_context", "set_repeat_track", "get_state", "get_queue", "activate",
                       "deactivate", "add_to_queue"}
            if c not in allowed:
                raise web.HTTPBadRequest(text=T("okänt kommando"))
            uri = b.get("uri")
            if c == "add_to_queue" or uri is not None:
                m = SPOTIFY_URI.fullmatch(uri) if isinstance(uri, str) else None
                if not m:
                    raise web.HTTPBadRequest(text=T("Ogiltig Spotify-adress. Ange till exempel spotify:album:<22 tecken>."))
                if c == "add_to_queue" and m.group(1) != "track":
                    raise web.HTTPBadRequest(text=T("Bara enskilda spår kan läggas i kön (spotify:track:…)."))
            msg = {k: v for k, v in b.items() if k in ("command", "position_ms", "volume", "enabled", "uri", "limit")}
            if not (c == "play" and uri) and c not in ("add_to_queue", "deactivate", "activate"):
                await sp.soloist_cmd(msg)  # the panel's own buttons: fire and forget, as before
                return hub.ok()
            # From Home Assistant (voice assistants): say what actually happened.
            if not hub.soloist:
                raise web.HTTPConflict(text=T("Källan är inte Spotify Lossless"))
            if not sp.soloist_ws:
                raise web.HTTPServiceUnavailable(text=T("Soloist svarar inte"))
            if not sp.auth.get("logged_in"):
                raise web.HTTPConflict(text=T("Soloist är inte inloggad på Spotify. Välj enheten i Spotify-appen en gång."))
            if c == "play":
                if not sp.auth.get("is_active"):
                    r = await sp.soloist_cmd({"command": "activate"}, wait=1.5)
                    if r and r.get("type") == "error":
                        raise web.HTTPBadGateway(text=f"Soloist: {r.get('message')}")
                    if not await sp.wait_active():
                        raise web.HTTPGatewayTimeout(text=T("Soloist blev inte aktiv på Spotify"))
                r = await sp.soloist_cmd(msg, wait=2.0)
                if not (r and r.get("type") == "error"):
                    hub.play_started(force=True)  # wake the amplifier, select this device's input
            else:
                r = await sp.soloist_cmd(msg, wait=2.0)
            if r and r.get("type") == "error":
                raise web.HTTPBadGateway(text=f"Soloist: {r.get('message')}")
            return hub.ok(result=r)

        def failed(e: webapi.WebApiError):
            text = {"credentials": T("Spotify godtog inte appens Client ID och Client secret. Kontrollera dem under Moduler, Spotify Lossless."),
                    "leave": T("Spotify tillåter inte det för den här appen."),
                    "busy": T("Spotify tar inte emot fler frågor just nu. Försök igen om en stund.")}.get(e.kind, T("Spotify svarar inte."))
            return web.HTTPBadGateway(text=text)

        @routes.get("/api/spotify/search")
        async def spotify_search(request):
            q = " ".join(request.query.get("q", "").split())[:100]
            if len(q) < 2:
                raise web.HTTPBadRequest()
            try:
                return web.json_response(await sp.web.search(q))
            except webapi.WebApiError as e:
                raise failed(e)

        @routes.get("/api/spotify/callback")
        async def spotify_callback(request):
            """Where Spotify sends the listener back after the sign-in, with a code. It is taken
            only with the state of the sign-in that was begun from the panel (by someone logged
            in to it), within ten minutes."""
            q = request.query
            try:
                await sp.web.finish(q.get("code", "")[:2000], q.get("state", "")[:200])
                try:
                    hub.set_secret("spotify_user", (await sp.web.me())["name"][:60])
                except webapi.WebApiError:
                    pass
                await hub.broadcast({"type": "spotify_user", "on": True})
            except webapi.WebApiError:
                log.warning("spotify sign-in was not completed (%s)", "refused" if q.get("error") else "no valid code")
            raise web.HTTPFound("/system/modules/spotify")

        @routes.get("/api/spotify/where")
        async def spotify_where(request):
            """The device the account plays on, for when it is not this one."""
            if not sp.web.signed_in():
                return web.json_response({"device": None})
            try:
                return web.json_response({"device": await sp.web.where()})
            except webapi.WebApiError as e:
                raise failed(e)

        @routes.get("/api/spotify/library")
        async def spotify_library(request):
            """The signed-in listener's own playlists, saved albums and liked tracks: for
            someone logged in to the panel, not for everyone on the network."""
            if not hub.logged_in(request):
                raise web.HTTPUnauthorized(text=T("Logga in under Profil för att göra det här."))
            if not sp.web.signed_in():
                raise web.HTTPConflict(text=T("Logga in med Spotify under Moduler, Spotify Lossless."))
            try:
                d = await sp.web.library()
            except webapi.WebApiError as e:
                raise failed(e)
            known = sp.added()
            d = {**d, "playlists": [{**p, "added": known.get(p.get("id"), {}).get("added", "")} for p in d["playlists"]]}
            d["reading"] = sp.read_added([p for p in d["playlists"] if p.get("owner_id") == d["me"]])     # (how many are still to be read off)
            return web.json_response(d)

        FOLDERS = os.path.join(collect.STATE_DIR, "spotify-folders.json")

        @routes.get("/api/spotify/folders")
        async def spotify_folders(request):
            """The listener's own folders for their playlists (kept on the device: Spotify's
            Web API tells nothing of the folders in its app)."""
            if not hub.logged_in(request):
                raise web.HTTPUnauthorized(text=T("Logga in under Profil för att göra det här."))
            return web.json_response(folders.load(FOLDERS))

        @routes.post("/api/spotify/folders")
        async def spotify_folders_change(request):
            if not hub.logged_in(request):
                raise web.HTTPUnauthorized(text=T("Logga in under Profil för att göra det här."))
            try:
                d = folders.change(folders.load(FOLDERS), await hub.body(request))
            except folders.Refused as e:
                raise web.HTTPBadRequest(text={"name": T("Mappen behöver ett namn."), "full": T("Det går inte att lägga till fler mappar där."),
                                               "into itself": T("En mapp kan inte läggas i sig själv.")}.get(str(e), T("Okänd mapp eller spellista.")))
            folders.save(FOLDERS, d)
            hub.persist(FOLDERS)
            return web.json_response(d)

        @routes.get("/api/spotify/tracks")
        async def spotify_tracks(request):
            """Names and sleeves for tracks the queue names only by address."""
            ids = list(dict.fromkeys(i for i in request.query.get("ids", "").split(",") if webapi.ID.fullmatch(i)))[:10]
            if not ids or not sp.web.ready():
                raise web.HTTPBadRequest()
            return web.json_response({"tracks": await sp.web.tracks(ids)})

        @routes.get("/api/spotify/{kind:album|artist|playlist}/{sid}")
        async def spotify_item(request):
            sid = request.match_info["sid"]
            if not webapi.ID.fullmatch(sid):
                raise web.HTTPBadRequest()
            try:
                # (a playlist's contents are the signed-in listener's own: for someone logged in to the panel)
                d = await (sp.web.playlist(sid, items=hub.logged_in(request)) if request.match_info["kind"] == "playlist" else getattr(sp.web, request.match_info["kind"])(sid))
            except webapi.WebApiError as e:
                raise failed(e)
            if d is None:
                raise web.HTTPNotFound()
            return web.json_response(d)

        @routes.get("/api/spotify/played")
        async def spotify_played(request):
            # Open to the network like the rest of what the panel shows a listener (what plays,
            # the queue): the owner switches the keeping off, or clears it, in the settings.
            return web.json_response(sp.played() if hub.settings.get("listen_history", True) else {"albums": [], "tracks": [], "artists": [], "off": True})

        @routes.post("/api/soloist/restart")
        async def soloist_restart(request):
            rc, out, err = await hub.user_systemctl("restart", "soloist.service")
            return hub.ok(rc=rc, error=err.strip())

        @routes.post("/api/soloist/update")
        async def soloist_update(request):
            await hub.run_job("soloist-update", ["/usr/local/sbin/soloist-update"])
            return hub.ok()

        @routes.post("/api/soloist/normalization")
        async def soloist_normalization(request):
            enabled = bool((await hub.body(request)).get("enabled"))
            if not so.prefs_paths():
                raise web.HTTPConflict(text=T("Soloist har inte loggat in än. Anslut från Spotify-appen först."))
            hub.settings["soloist_normalization"] = enabled  # kept for every account, guests too
            await asyncio.get_running_loop().run_in_executor(None, hub.save_settings)
            await sp.apply_normalization(enabled)
            return hub.ok(enabled=enabled)

        @routes.post("/api/soloist/apikey")
        async def soloist_apikey(request):
            return hub.ok(**await sp.set_key(str((await hub.body(request)).get("key", "")).strip()))

        @routes.post("/api/audio/test")
        async def audio_test(request):
            secs = max(5, min(int((await hub.body(request)).get("seconds", 30)), 120))
            if hub.jobs.get("audio-test", {}).get("running"):
                raise web.HTTPConflict(text=T("En verifiering pågår redan"))
            await sp.run_audio_test(secs)
            return hub.ok(seconds=secs)


PLUGIN = Spotify
