"""Roon Bridge: this device as a Roon endpoint (RAAT). Roon opens the DAC itself, which
is what lets it send DSD and every sample rate the DAC takes. Installed on the device
from Roon Labs by the install script.

Roon Bridge is told nothing about the music, so what plays is asked of the Roon server
itself: the panel announces itself to it as an extension (roonapi.py), the listener approves
it once in Roon (Settings, Extensions), and from then on the panel shows the title, the
artist, the album and the sleeve of this device's zone, and its buttons work."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time

from aiohttp import ClientSession, ClientTimeout, web
import websockets

import collect
from i18n import T
from plugins import SourcePlugin

from . import roonapi

log = logging.getLogger("roon")
API_FILE = os.path.join(collect.STATE_DIR, "roon-api.json")   # the servers' tokens, root only
EXTENSION = {"extension_id": "org.mscos.panel", "display_name": "mscOS", "display_version": "1",
             "publisher": "mscOS", "email": "noreply@mscos.invalid",
             "required_services": [roonapi.TRANSPORT], "optional_services": [roonapi.BROWSE],
             "provided_services": ["com.roonlabs.ping:1"]}
LOOP = {"off": "disabled", "context": "loop", "track": "loop_one"}


class Roon(SourcePlugin):
    id = "roon"
    label = "Roon"
    protocol = "RAAT"
    pipewire = False
    name = {"sv": "Roon Bridge", "en": "Roon Bridge"}
    description = {"sv": "Enheten som Roon-utgång. Alla format DAC:en klarar, även DSD. Kräver en Roon-server i nätverket.",
                   "en": "This device as a Roon output. Every format the DAC takes, DSD too. Needs a Roon server on the network."}
    system_units = ("roonbridge.service",)
    HINT = hint = {"sv": "Välj den här enheten i Roon-appen", "en": "Choose this device in the Roon app"}
    HINT_APPROVE = {"sv": "Godkänn mscOS i Roon under Inställningar, Tillägg, för att se vad som spelas",
                    "en": "Enable mscOS in Roon under Settings, Extensions, to see what is playing"}
    transport = ("toggle", "play", "pause", "next", "prev", "seek", "shuffle", "repeat")
    defaults = {"roon_api": True, "roon_zone": ""}
    terms = {"sv": "Roon Bridge hämtas från Roon Labs till den här enheten.",
             "en": "Roon Bridge is downloaded from Roon Labs to this device."}
    terms_url = "https://help.roonlabs.com/portal/en/kb/articles/linux-install"

    updates = "self"  # Roon Bridge updates itself from the Roon server

    def __init__(self, hub):
        super().__init__(hub)
        self.api: roonapi.Roon | None = None   # the connection to the Roon server, while Roon is the source
        self.server: dict = {}                 # {"host", "port", "id", "name"} of that server
        self.np: dict | None = None
        self._auto = ""                        # the zone found to be this device's
        self._told = None                      # what the panel was last told (to tell it only of changes)
        self.http: ClientSession | None = None

    # ---- what the listener can set
    def option_list(self) -> list[dict]:
        zones = sorted((self.api.zones if self.api else {}).values(), key=lambda z: z.get("display_name", ""))
        return [
            {"key": "roon_api", "type": "toggle", "label": {"sv": "Visa vad som spelas", "en": "Show what is playing"},
             "help": {"sv": "Panelen anmäler sig som tillägg hos Roon-servern. Godkänn mscOS en gång i Roon under Inställningar, Tillägg.",
                      "en": "The panel announces itself to the Roon server as an extension. Enable mscOS once in Roon under Settings, Extensions."}},
            {"key": "roon_zone", "type": "select", "label": {"sv": "Den här enhetens zon i Roon", "en": "This device's zone in Roon"},
             "options": [["", {"sv": "Hitta själv", "en": "Find it"}]] + [[z["zone_id"], {"sv": z.get("display_name", ""), "en": z.get("display_name", "")}] for z in zones],
             "help": {"sv": "Behövs bara med flera zoner, om panelen visar fel zon.", "en": "Only needed with several zones, if the panel shows the wrong one."}},
        ]

    def update_settings(self, b: dict, upd: dict) -> None:
        if "roon_api" in b:
            upd["roon_api"] = bool(b["roon_api"])
        if "roon_zone" in b:
            z = str(b["roon_zone"])
            if z and not re.fullmatch(r"[0-9A-Za-z_-]{8,80}", z):
                raise web.HTTPBadRequest(text=T("Okänd zon"))
            upd["roon_zone"] = z

    async def settings_applied(self, upd: dict) -> None:
        self._auto = ""
        self._changed()

    # ---- which zone is this device
    def _zone(self) -> dict | None:
        zones = self.api.zones if self.api and self.api.registered else {}
        if not zones:
            return None
        want = self.hub.settings.get("roon_zone")
        if want in zones:
            return zones[want]
        fold = lambda t: re.sub(r"[^\w]+", " ", str(t or "").casefold()).strip()
        names = {fold(v) for v in self.hub.names().values() if isinstance(v, str)} - {""}
        for z in zones.values():      # the zone, or one of its outputs, is called what this device is called
            if ({fold(z.get("display_name"))} | {fold(o.get("display_name")) for o in z.get("outputs", [])}) & names:
                return z
        if len(zones) == 1:
            return next(iter(zones.values()))
        # several zones: the one that plays while this device's DAC is being played to
        playing = [z for z in zones.values() if z.get("state") == "playing"]
        if len(playing) == 1 and (self.hub.status.get("dac") or {}).get("state") == "RUNNING":
            self._auto = playing[0]["zone_id"]
        return zones.get(self._auto)

    def now_playing(self) -> dict | None:
        return self.np

    def _now(self) -> dict | None:
        z = self._zone()
        n = (z or {}).get("now_playing")
        if not n:
            return None
        t = n.get("three_line") or {}
        key = str(n.get("image_key") or "")
        sec = n.get("seek_position") if n.get("seek_position") is not None else z.get("seek_position")
        loop = (z.get("settings") or {}).get("loop")
        return {"title": t.get("line1") or "", "artist": t.get("line2") or "", "album": t.get("line3") or "",
                "image": f"/api/roon/image/{key}" if re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key) else None,
                "playing": z.get("state") == "playing",
                "duration_ms": int(n["length"] * 1000) if n.get("length") else None,
                "position_ms": int((sec or 0) * 1000), "position_ts": time.time(),
                "shuffle": bool((z.get("settings") or {}).get("shuffle")),
                "repeat": "track" if loop == "loop_one" else "context" if loop == "loop" else "off"}

    def _changed(self) -> None:
        """The server said something: keep its token, and tell the panel when what plays changed."""
        api = self.api
        if api and api.registered and api.token:
            self._keep(str(api.core.get("core_id") or ""), api.token, self.server.get("host", ""))
        old, self.np = self.np, self._now()
        np = self.np
        keys = ("title", "artist", "album", "image", "playing", "duration_ms", "shuffle", "repeat")
        moved = old and np and abs((np["position_ms"] - old["position_ms"]) - (np["position_ts"] - old["position_ts"]) * 1000 * (1 if np["playing"] else 0)) > 2500
        if (old is None) != (np is None) or moved or any((old or {}).get(k) != (np or {}).get(k) for k in keys):
            self.hub.source_changed(self)
        # found a server that has not let us in yet: that is what the listener has to do next
        waiting = api is not None and api.ws is not None and not api.registered and bool(self.hub.settings.get("roon_api"))
        self.hint = self.HINT_APPROVE if waiting else self.HINT
        told = (waiting, tuple(sorted((api.zones if api else {}).keys())), bool(api and api.registered))
        if told != self._told:    # the hint and the list of zones are part of what the panel shows
            self._told = told
            asyncio.ensure_future(self.hub.broadcast({"type": "settings", "settings": self.hub.settings,
                                                      "features": self.hub.features(), "names": self.hub.names()}))

    # ---- the servers' tokens
    @staticmethod
    def _kept() -> dict:
        try:
            with open(API_FILE) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _token_for(self, host: str):
        """The token a server issued, given only to that server: the same id at the same
        address. A server that has moved, or one that only claims the id, gets none, and the
        listener approves the extension again in Roon."""
        def token(core_id: str) -> str:
            t = (self._kept().get("tokens") or {}).get(core_id)
            return t["token"] if isinstance(t, dict) and t.get("host") == host and isinstance(t.get("token"), str) else ""
        return token

    def _keep(self, core_id: str, token: str, host: str) -> None:
        d = self._kept()
        if not core_id or not host or (d.get("tokens") or {}).get(core_id) == {"token": token, "host": host}:
            return
        if not isinstance(d.get("tokens"), dict):
            d["tokens"] = {}
        d["tokens"][core_id] = {"token": token, "host": host}
        d["core"] = core_id
        try:
            fd = os.open(API_FILE + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(d, f)
            os.replace(API_FILE + ".new", API_FILE)
        except OSError as e:
            log.warning("keeping the Roon server's token: %s", e)

    # ---- the buttons
    async def command(self, cmd: str, **kw) -> None:
        z = self._zone()
        if not z:
            raise RuntimeError(T("Godkänn mscOS i Roon under Inställningar, Tillägg."))
        zid = z["zone_id"]
        try:
            if cmd in ("play", "pause", "next"):
                await self.api.control(zid, cmd)
            elif cmd == "toggle":
                await self.api.control(zid, "playpause")
            elif cmd == "prev":
                await self.api.control(zid, "previous")
            elif cmd == "seek":
                await self.api.seek(zid, max(0, int(kw["position_ms"])) / 1000)
            elif cmd == "shuffle":
                await self.api.settings(zid, shuffle=bool(kw.get("enabled")))
            elif cmd == "repeat":
                await self.api.settings(zid, loop=LOOP.get(kw.get("mode"), "disabled"))
        except (RuntimeError, OSError, websockets.WebSocketException) as e:
            raise RuntimeError(T("Roon tog inte emot kommandot ({why}).", why=e))

    def hello(self) -> dict:
        return {"roon_ready": bool(self.api and self.api.registered)}

    def routes(self, routes) -> None:
        @routes.post("/api/roon/browse")
        async def browse(request):
            """One step in Roon's own lists (its library, a search, a streaming service's shelves),
            and what is there after it. An item that is an action (Play now, Queue) is done in this device's zone."""
            if not (self.api and self.api.registered):
                raise web.HTTPConflict(text=T("Godkänn mscOS i Roon under Inställningar, Tillägg."))
            try:
                b = await request.json()
                hierarchy = b.get("hierarchy")
                where = {}
                if isinstance(b.get("item_key"), str) and re.fullmatch(r"[\w:.\-]{1,200}", b["item_key"]):
                    where["item_key"] = b["item_key"]
                if isinstance(b.get("input"), str) and b["input"].strip():
                    where["input"] = " ".join(b["input"].split())[:120]
                if b.get("pop_all") is True:
                    where["pop_all"] = True
                if isinstance(b.get("pop_levels"), int) and 0 < b["pop_levels"] < 20:
                    where["pop_levels"] = b["pop_levels"]
            except (ValueError, AttributeError):
                raise web.HTTPBadRequest()
            if hierarchy not in ("browse", "search") or ("item_key" in where and "pop_all" in where):
                raise web.HTTPBadRequest()
            zone = self._zone()
            try:
                d = await self.api.browse(hierarchy, zone["zone_id"] if zone else "", **where)
            except (RuntimeError, OSError, websockets.WebSocketException) as e:
                raise web.HTTPBadGateway(text=T("Roon tog inte emot kommandot ({why}).", why=e))
            ok_key = lambda k: isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", k)   # noqa: E731
            lst = d.get("list") or {}
            return web.json_response({
                "action": d["action"], "message": str(d["message"])[:300], "is_error": d["is_error"],
                "list": {"title": str(lst.get("title") or "")[:200], "subtitle": str(lst.get("subtitle") or "")[:200], "level": lst.get("level") or 0,
                         "count": lst.get("count") or 0, "image": f"/api/roon/image/{lst['image_key']}" if ok_key(lst.get("image_key")) else None,
                         "hint": lst.get("hint")},
                "items": [{"title": str(i.get("title") or "")[:300], "subtitle": str(i.get("subtitle") or "")[:300], "hint": i.get("hint"),
                           "item_key": i.get("item_key") if isinstance(i.get("item_key"), str) else None,
                           "image": f"/api/roon/image/{i['image_key']}" if ok_key(i.get("image_key")) else None,
                           "prompt": bool(i.get("input_prompt"))} for i in d.get("items", [])[:200]]})

        @routes.get("/api/roon/image/{key}")
        async def image(request):
            """The sleeve, fetched from the Roon server for the page (the page is not let at the server itself)."""
            key = request.match_info["key"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key) or not self.server or not (self.api and self.api.registered):
                raise web.HTTPNotFound()
            if self.http is None:
                self.http = ClientSession(timeout=ClientTimeout(total=8))
            url = f"http://{self.server['host']}:{self.server['port']}/api/image/{key}"
            try:
                async with self.http.get(url, params={"scale": "fit", "width": 640, "height": 640, "format": "image/jpeg"}) as r:
                    data = await collect.read_capped(r, 4 * 1024 * 1024)   # all of it: a part of a picture is a picture cut off
                    ctype = r.headers.get("Content-Type", "")
                    ctype = ctype.split(";")[0].strip().lower()
                    if r.status != 200 or len(data) > 4 * 1024 * 1024 or ctype not in ("image/jpeg", "image/png", "image/webp"):
                        raise web.HTTPNotFound()
            except (OSError, asyncio.TimeoutError):
                raise web.HTTPBadGateway()
            # whatever the server sent, the browser takes it as that picture and nothing else
            return web.Response(body=data, content_type=ctype, headers={
                "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox"})

    # ---- follow the Roon server while Roon is the source
    def _wanted(self) -> bool:
        # Whichever source is chosen: the listener approves the extension in Roon while it is
        # there to be seen, and Roon is ready the moment it is chosen. (What plays is shown
        # only while Roon is the source: the core takes it from the current source alone.)
        return bool(self.hub.settings.get("roon_api"))

    async def _follow(self):
        while True:
            try:
                if not self._wanted() or not await self.installed():
                    await asyncio.sleep(5)
                    continue
                found = await roonapi.discover()
                if not found:
                    await asyncio.sleep(15)
                    continue
                kept = self._kept()
                self.server = next((c for c in found if c["id"] == kept.get("core")), found[0])
                api = roonapi.Roon(EXTENSION, self._changed, self._token_for(self.server["host"]))
                async with websockets.connect(f"ws://{self.server['host']}:{self.server['port']}/api",
                                              open_timeout=6, ping_interval=None, max_size=8 * 1024 * 1024) as ws:
                    self.api = api
                    log.info("Roon server %s found", self.server.get("name") or self.server["host"])

                    async def leave():   # the option was switched off
                        while self._wanted():
                            await asyncio.sleep(3)
                        await ws.close()
                    watch = asyncio.ensure_future(leave())
                    try:
                        await api.run(ws)
                    finally:
                        watch.cancel()
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError, ValueError):
                pass
            except Exception:  # noqa: BLE001 - never let this loop die
                log.exception("following the Roon server")
            if self.api is not None:
                self.api = None
                with contextlib.suppress(Exception):
                    self._changed()
            await asyncio.sleep(5)

    def tasks(self) -> list:
        return [self._follow()]

    async def version(self) -> str | None:
        import collect
        lines = collect.read("/opt/RoonBridge/VERSION").splitlines()
        return (lines[1].split(" production")[0].strip() if len(lines) > 1 else "") or None

    NAME_FILE = "/etc/mscos/roon-name"
    DROPIN = "/etc/systemd/system/roonbridge.service.d/30-name.conf"

    @staticmethod
    def _put(path: str, text: str) -> bool:
        """Write the file when it differs. True when it was written."""
        import collect
        if collect.read(path) == text:
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".new", "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(path + ".new", path)
        return True

    async def write_name(self) -> bool:
        """Roon heads the device with the machine's host name: roon-start gives Roon Bridge
        the device's name on the network instead. True when the name changed."""
        import collect
        starter = os.path.join(self.module_dir, "roon-start")
        unit = f"[Service]\nExecStart=\nExecStart=/usr/bin/python3 {starter}\n"
        if self._put(self.DROPIN, unit):
            await collect.run(["systemctl", "daemon-reload"], timeout=30)
        return self._put(self.NAME_FILE, self.hub.names()["network"] + "\n")

    async def start(self) -> str:
        try:
            await self.write_name()
        except OSError as e:
            return str(e)
        return await super().start()

    async def names_changed(self) -> None:
        import collect
        if await self.installed() and await self.write_name() and await self.active():
            await collect.run(["systemctl", "restart", *self.system_units], timeout=60)

    async def installed(self) -> bool:
        return os.path.exists("/opt/RoonBridge/start.sh") and os.path.exists("/etc/systemd/system/roonbridge.service")


PLUGIN = Roon
