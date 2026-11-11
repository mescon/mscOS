"""Mark Levinson Nº585 / Nº585.5 over IP (TCP 7701): volume, inputs, standby, mute.

The streamer is on one of the amplifier's inputs (ml_play_input, USB by default). When
music starts here the module wakes the amplifier and selects that input; it learns what the
display calls each input ("SOtM", "Arcam"), finds the amplifier on the LAN if no address is
given, and can put it in standby after a quiet spell, but only if it woke it itself.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time

from aiohttp import web

import collect
from i18n import N_, T
from plugins import AmpPlugin
from .driver import INPUTS, Levinson, LevinsonError, discover

log = logging.getLogger("levinson")


def _bypass(text: str | None) -> bool:
    """The display shows "Line" instead of a level when the input is in processor bypass
    (fixed gain, e.g. "Arcam      Line"): an AV receiver sets the volume then."""
    parts = re.split(r"\s{2,}", text or "", maxsplit=1)
    return len(parts) > 1 and parts[1].strip().lower() == "line"


class LevinsonAmp(AmpPlugin):
    id = "levinson"
    name = {"sv": "Mark Levinson Nº585/585.5", "en": "Mark Levinson Nº585/585.5"}
    description = {"sv": "Volym, ingångar, standby och mute över nätverket (TCP 7701).",
                   "en": "Volume, inputs, standby and mute over the network (TCP 7701)."}
    defaults = {
        "ml_host": "",            # "" = find it on the LAN
        "ml_play_input": "USB",   # the input this device is connected to
        "ml_auto_on": True,       # wake it and select that input when music starts
        "ml_auto_off_min": 0,     # standby after N quiet minutes, only if we woke it (0 = never)
        "ml_max_db": 40.0,        # the panel's ceiling
    }

    model = "Mark Levinson Nº585.5"  # default name of the output (System -> Names changes it)

    def option_list(self) -> list[dict]:
        lab = {v: k for k, v in self.labels().items()}
        return [
            {"key": "ml_play_input", "type": "select", "label": {"sv": "Ingången den här enheten sitter på", "en": "The input this device is connected to"},
             "options": [[i, {"sv": f"{lab[i]} ({i})" if i in lab else i, "en": f"{lab[i]} ({i})" if i in lab else i}] for i in INPUTS]},
            {"key": "ml_auto_on", "type": "toggle", "label": {"sv": "Slå på och välj den ingången när musik börjar spela", "en": "Turn on and select that input when music starts"}},
            self.BRIDGE_OPTION,
            {"key": "ml_auto_off_min", "type": "select", "label": {"sv": "Standby när inget spelas", "en": "Standby when nothing plays"},
             "options": [[0, {"sv": "Aldrig", "en": "Never"}], [15, {"sv": "Efter 15 min", "en": "After 15 min"}],
                         [30, {"sv": "Efter 30 min", "en": "After 30 min"}], [60, {"sv": "Efter 1 h", "en": "After 1 h"}]],
             "help": {"sv": "Gäller bara när panelen själv har slagit på förstärkaren, så att en skivspelare eller annan källa inte stängs av mitt i.",
                      "en": "Only when the panel itself turned the amplifier on, so a turntable or another source is not cut off."}},
            {"key": "ml_max_db", "type": "number", "min": 20, "max": 80, "step": 0.5, "unit": "dB",
             "label": {"sv": "Högsta volym från panelen", "en": "Highest volume from the panel"}},
            {"key": "ml_host", "type": "text", "label": {"sv": "Adress", "en": "Address"},
             "placeholder": {"sv": "Hittas automatiskt", "en": "Found automatically"},
             "help": {"sv": "På förstärkaren: Setup, Advanced, Connect = Ethernet, och standbyläget Normal. I läget Green stängs nätverket av i standby, och då kan panelen inte väcka den.",
                      "en": "On the amplifier: Setup, Advanced, Connect = Ethernet, and standby mode Normal. In Green mode the network is off in standby, and the panel cannot wake it."}},
        ]

    def status(self) -> dict | None:
        m, host = self.state or {}, self.ml.host
        if not host:
            return {"state": "warn", "text": {"sv": "Inte hittad än. Panelen söker var tionde minut.", "en": "Not found yet. The panel searches every ten minutes."}}
        if not m.get("reachable"):
            return {"state": "warn", "text": {"sv": f"Ingen kontakt på {host}", "en": f"No contact at {host}"}}
        auto = "" if self.hub.settings.get("ml_host") else {"sv": ", hittad automatiskt", "en": ", found automatically"}
        return {"state": "ok", "text": {"sv": f"Ansluten på {host}{auto and auto['sv']}", "en": f"Connected at {host}{auto and auto['en']}"}}

    def log(self) -> list[str] | None:
        return [f"{time.strftime('%H:%M:%S', time.localtime(t))}  {'→' if d == 'tx' else '←'} {txt}" for t, d, txt in (self.state or {}).get("log") or []]

    def amp_state(self) -> dict:
        m, host = self.state or {}, self.ml.host
        lab = m.get("labels") or {}
        note = None
        if m.get("reachable") is None:
            note = {"sv": "Söker förstärkaren i nätverket…", "en": "Looking for the amplifier on the network…"}
        elif not host:
            note = {"sv": "Hittar inte förstärkaren i nätverket. Anslut nätverkskabeln och välj Connect = Ethernet i dess Setup-meny.",
                    "en": "Cannot find the amplifier on the network. Connect the network cable and choose Connect = Ethernet in its Setup menu."}
        elif not m.get("reachable"):
            note = {"sv": f"Ingen kontakt med förstärkaren på {host}. Står den i standbyläget Green är nätverket avstängt.",
                    "en": f"No contact with the amplifier at {host}. In standby mode Green its network is off."}
        return {"reachable": m.get("reachable") if host or m.get("reachable") is None else False,
                "power": m.get("power"), "muted": bool(m.get("muted")), "volume_db": m.get("volume_db"),
                "max_db": self.max_db, "input": m.get("input"), "note": note,
                "inputs": [{"id": i, "label": lab.get(i) or i} for i in INPUTS]}

    def __init__(self, hub):
        super().__init__(hub)
        s = hub.settings
        self.ml = Levinson(s.get("ml_host") or s.get("ml_host_found") or None)
        self.state: dict = {"reachable": None}
        self.found = ""
        self._woke_t = 0.0      # when we last woke it (auto standby only undoes our own wake)
        self._seen_t = time.time()
        self._play_t = 0.0      # last automatic input selection
        self._select_lock = asyncio.Lock()
        self._learn_gen = 0

    # ---- settings
    @property
    def play_input(self) -> str:
        v = self.hub.settings.get("ml_play_input", "USB")
        return v if v in INPUTS else "USB"

    @property
    def max_db(self) -> float:
        return float(self.hub.settings.get("ml_max_db", 40.0))

    def labels(self) -> dict:
        """display name -> input id, learned ("SOtM" -> "USB")."""
        return self.hub.settings.get("ml_labels") or {}

    def label_of(self, inp: str) -> str | None:
        return next((k for k, v in self.labels().items() if v == inp), None)

    def update_settings(self, b: dict, upd: dict) -> None:
        upd.update({k: bool(b[k]) for k in ("ml_auto_on",) if k in b})
        if "ml_auto_off_min" in b:
            upd["ml_auto_off_min"] = self.hub.num(b["ml_auto_off_min"], 0, 240, N_("Standby efter"), integer=True)
        if "ml_max_db" in b:
            upd["ml_max_db"] = self.hub.num(b["ml_max_db"], 20, 80, N_("Högsta volym"))
        if "ml_play_input" in b:
            if b["ml_play_input"] not in INPUTS:
                raise web.HTTPBadRequest(text=T("Okänd ingång"))
            upd["ml_play_input"] = b["ml_play_input"]
        if "ml_host" in b:
            h = str(b["ml_host"]).strip()
            if h:
                try:
                    ipaddress.IPv4Address(h)
                except ValueError:
                    raise web.HTTPBadRequest(text=T("Ange en IP-adress, till exempel 192.168.1.150, eller lämna tomt."))
            upd["ml_host"] = h

    async def settings_applied(self, upd: dict) -> None:
        if "ml_host" in upd:
            await self.ml.close()
            self.ml.host = upd["ml_host"] or self.found or None
            self._seen_t = time.time()

    # ---- AmpPlugin
    def ready(self) -> bool:
        m = self.state or {}
        return bool(self.ml.host and m.get("reachable") and m.get("power") is not False
                    and m.get("input") == self.play_input)

    @property
    def volume_db(self) -> float | None:
        return self.ml.volume_db

    async def read_volume(self) -> float | None:
        return await self.ml.read_volume()

    async def set_volume(self, db: float, ceiling: float | None = None) -> None:
        await self.ml.set_volume(db, self.max_db if ceiling is None else ceiling)

    def quantize(self, db: float) -> float:
        """Whole-dB steps up to 24 dB and 0.1 dB above (it rounds what it is sent): ask for
        what it will take, whole dB below 24, half dB above."""
        return float(round(db)) if db < 24 else round(db * 2) / 2

    def bypass(self, text: str | None = None) -> bool:
        return _bypass(text if text is not None else (self.state or {}).get("display"))

    # ---- playback started here: wake it, select our input
    async def on_play_start(self, force: bool = False) -> None:
        """force: an explicit request to play here (from an automation), so the guard against
        fighting a manual input change does not apply."""
        if not self.hub.settings.get("ml_auto_on") or not self.ml.host:
            return
        guard = 10 if force else 90  # don't fight a manual input change
        if time.time() - self._play_t < guard:
            return
        self._play_t = time.time()
        try:
            await self.select(self.play_input, unmute=force)
        except LevinsonError as e:
            log.warning("585.5 auto-on failed: %s", e)
        await self.refresh()

    async def select(self, name: str, unmute: bool = False) -> dict:
        """Wake the amplifier if needed and put it on an input, without re-selecting the input
        it is already on (that makes it relock: audible). Raises LevinsonError."""
        async with self._select_lock:
            # Read the display now: the remote may have changed power or input since the last poll.
            d = await self.ml.read_display()
            if d:
                self.ml.power = d["on"]
            woke = self.ml.power is not True
            if woke:
                await self.ml.set_power(True)
                self._woke_t = time.time()
                await asyncio.sleep(6)  # leaving standby takes a few seconds; inputs are ignored meanwhile
            label = self.label_of(name)
            already = not woke and d is not None and label is not None and d["label"] == label
            if not already:
                await self.ml.set_input(name)
                asyncio.ensure_future(self.learn_label(name))
                if label:  # let the display catch up, so the answer shows the new input
                    for _ in range(10):
                        await asyncio.sleep(0.5)
                        d2 = await self.ml.read_display()
                        if d2 and d2["label"] == label and d2.get("level"):
                            break
            log.info("585.5: %s, %s", "woken" if woke else "already on",
                     f"already on {name}" if already else f"input {name}")
            if unmute:  # an explicit "play here": mute set with the remote should not leave it silent
                await self.ml.set_mute(False)
            return {"woke": woke, "switched": not already, "unmuted": unmute}

    def mark_selected(self):
        """Someone else selected our input just now: the next play start need not."""
        self._play_t = time.time()

    async def learn_label(self, name: str):
        """After selecting an input, remember what the display calls it (e.g. USB = "SOtM")."""
        self._learn_gen = gen = self._learn_gen + 1
        for _ in range(6):
            await asyncio.sleep(1.5)
            if gen != self._learn_gen:
                return  # another input was selected meanwhile; that one learns its own name
            d = await self.ml.read_display()
            if d and d["label"]:
                labels = dict(self.labels())
                if d["label"].lower() == "unused":
                    break  # the factory name of every unused input says nothing
                if labels.get(d["label"]) not in (None, name):
                    continue  # still showing the previous input (slow, or this input is disabled)
                if labels.get(d["label"]) != name:
                    labels = {k: v for k, v in labels.items() if v != name}
                    labels[d["label"]] = name
                    self.hub.settings["ml_labels"] = labels
                    await asyncio.get_running_loop().run_in_executor(None, self.hub.save_settings)
                break
        await self.refresh()

    async def refresh(self) -> None:
        self.state = await self.ml.status() if self.ml.host else {"reachable": False, "host": None, "searching": True}
        lab = self.state.get("label")
        if lab:  # the display is the truth, also after the remote was used
            self.ml.input = self.state["input"] = self.labels().get(lab)
        self.state["labels"] = {v: k for k, v in self.labels().items()}
        self.state["found"] = self.found
        await self.hub.broadcast({"type": "amp", "amp": self.amp_state()})

    # ---- background: find it, poll it, auto standby
    def tasks(self) -> list:
        return [self.loop()]

    async def loop(self):
        hub = self.hub
        last_scan = 0.0
        while True:
            try:
                if not self.ml.host and time.time() - last_scan > 600:
                    last_scan = time.time()
                    net, own = await collect.local_network()
                    others = {p.host for p in hub.plugins.values() if getattr(p, "host", None)}
                    ip = await discover(net, skip=own | others) if net else None
                    if ip:
                        log.info("585.5 found at %s", ip)
                        self.found = self.ml.host = ip
                        self._seen_t = time.time()
                        hub.settings["ml_host_found"] = ip
                        await asyncio.get_running_loop().run_in_executor(None, hub.save_settings)
                await self.refresh()
                # an automatically found address that stops answering may have moved (DHCP): search again
                if self.state.get("reachable"):
                    self._seen_t = time.time()
                elif (self.ml.host and not hub.settings.get("ml_host")
                      and time.time() - self._seen_t > 900):
                    await self.ml.close()
                    self.ml.host = self.found = ""
                    self._seen_t = time.time()
                    last_scan = 0.0
                mins = int(hub.settings.get("ml_auto_off_min") or 0)
                if (mins and self._woke_t and not hub.playing_here
                        and time.time() - max(hub.idle_since, self._woke_t) > mins * 60):
                    await self.ml.set_power(False)
                    self._woke_t = 0.0
                    log.info("585.5: standby after %d idle minutes", mins)
            except Exception:
                log.exception("585.5 loop")
            await asyncio.sleep(10 if hub.clients else 60)

    # ---- panel and API
    def features(self) -> dict:
        return {"amp": self.id}

    def summary(self) -> dict:
        m = self.state or {}
        reach = bool(m.get("reachable"))
        bypass = reach and self.bypass()
        inp = m.get("input") if reach else None
        labels = m.get("labels") or {}
        return {"amp": {
            "reachable": reach,
            "power": ("on" if m.get("power") else "standby" if m.get("power") is False else "unknown") if reach else "unknown",
            "input": (labels.get(inp) or m.get("label") or inp) if reach else None,  # what the display calls it, e.g. "SOtM"
            "input_id": inp,  # the panel's name: USB, XLR, ...
            "input_no": INPUTS.index(inp) + 1 if inp in INPUTS else None,  # as on the remote, 1..11
            "volume_db": m.get("volume_db") if reach and not bypass else None,
            "bypass": bypass if reach else None,  # fixed gain: the AV receiver sets the volume
            "mute": m.get("muted") if reach else None,
            "max_db": self.max_db,
        }}

    def routes(self, routes) -> None:
        @routes.post("/api/levinson")  # the name before amplifiers shared /api/amp; automations use it
        async def levinson_action(request):
            await self.amp_action(await self.hub.body(request))
            return self.hub.ok(levinson=self.state, amp=self.amp_state(), **self.summary())

    async def amp_action(self, b: dict) -> None:
        hub = self.hub
        if True:
            try:
                if "power" in b:
                    await self.ml.set_power(bool(b["power"]))
                    if b["power"]:
                        await asyncio.sleep(0.5)
                if "input" in b:
                    await self.ml.set_input(str(b["input"]))
                    asyncio.ensure_future(self.learn_label(str(b["input"])))
                if "volume_db" in b or "volume_step_db" in b:
                    # read fresh, and by input too: right after a switch the display shows no level yet
                    d = await self.ml.read_display()
                    tv = hub.settings.get("tv_input")
                    tv_label = self.label_of(tv) if tv else None
                    if self.bypass(d["text"] if d else "") or (d and tv_label and d["label"] == tv_label):
                        raise web.HTTPConflict(text=T("Ingången står i bypass. Volymen ställs på AV-receivern."))
                if "volume_db" in b:
                    await self.ml.set_volume(hub.num(b["volume_db"], 0, 80, N_("Volym")), self.max_db)
                if "volume_step_db" in b:
                    step = hub.num(b["volume_step_db"], -20, 20, N_("Volymsteg"))
                    mx = self.max_db
                    cur = await self.ml.read_volume()  # fresh: the remote may have changed it
                    if cur is None:
                        raise LevinsonError(T("volymen gick inte att läsa"))
                    # never raise past the ceiling, and a level set higher with the remote only goes down
                    if not (step > 0 and cur >= mx):
                        await self.ml.set_volume(max(0.0, min(mx, cur + step)) if step > 0 else max(0.0, cur + step), max(mx, cur))
                if "mute" in b:
                    await self.ml.set_mute(bool(b["mute"]))
                if b.get("mute_toggle"):
                    await self.ml.toggle_mute()
            except LevinsonError as e:
                raise web.HTTPBadGateway(text=T("Förstärkaren: {error}", error=e))
            await self.refresh()
            if "volume_db" in b or "volume_step_db" in b:
                asyncio.ensure_future(hub.vol.sync_from_amp())  # the Spotify slider follows

    # ---- for scenes (TV mode)
    async def standby_if_on(self, inp: str) -> dict:
        d = await self.ml.read_display()
        label = self.label_of(inp)
        if d and d["on"] and label and d["label"] == label:
            await self.ml.set_power(False)
            return {"standby": True}
        return {"standby": False,
                "reason": T("förstärkaren är inte på den ingången") if d and d["on"] else T("redan i standby")}

    def current_input(self) -> str | None:
        m = self.state or {}
        if not m.get("reachable") or m.get("power") is None:
            return None
        return "standby" if m.get("power") is False else m.get("input")

    errors = (LevinsonError,)
    inputs = INPUTS

    @property
    def host(self):
        return self.ml.host


PLUGIN = LevinsonAmp
