"""TV mode: the amplifier between this device and an AV receiver (or TV) input.

  tv      wake the amplifier, select the TV input (tv_input) and unmute; pause music
          playing here, which would otherwise play into an input nobody hears
  stereo  wake it and select this device's input, as when music starts here
  tv_off  standby, but only if it is on the TV input: music playing stays on

POST /api/mode {"mode": ...}. The TV, the receiver and the player are another system's job
(for example a Home Assistant script); this module does the amplifier's part. The mode is
also reported (tv / stereo / off / other) from what the amplifier is on, so it follows the
remote as well. Needs an amplifier module with inputs.
"""
from __future__ import annotations

from aiohttp import web

from i18n import T
from plugins import Plugin


class TvMode(Plugin):
    id = "tvmode"
    kind = "integration"
    requires = ("amp",)
    name = {"sv": "TV-läge", "en": "TV mode"}
    description = {"sv": "Förstärkaren växlar mellan den här enheten och AV-receiverns ingång.",
                   "en": "The amplifier switches between this device and the AV receiver's input."}
    defaults = {"tv_input": "RCA3"}

    def __init__(self, hub):
        super().__init__(hub)
        if "ml_tv_input" in hub.settings and "tv_input" not in hub.settings:  # older name
            hub.settings["tv_input"] = hub.settings["ml_tv_input"]

    def _amp(self):
        a = self.hub.amp
        return a if a is not None and hasattr(a, "select") else None

    def update_settings(self, b: dict, upd: dict) -> None:
        if "tv_input" in b:
            a = self._amp()
            if a is None or b["tv_input"] not in getattr(a, "inputs", ()):
                raise web.HTTPBadRequest(text=T("Okänd ingång"))
            upd["tv_input"] = b["tv_input"]

    def mode(self) -> str | None:
        a = self._amp()
        cur = a.current_input() if a else None
        if cur is None:
            return None
        if cur == "standby":
            return "off"
        if cur == self.hub.settings.get("tv_input"):
            return "tv"
        return "stereo" if cur == a.play_input else "other"

    def summary(self) -> dict:
        return {"mode": self.mode()}

    def option_list(self) -> list[dict]:
        a = self._amp()
        st = a.amp_state() if a else {}
        return [{"key": "tv_input", "type": "select", "label": {"sv": "Förstärkarens ingång för TV och AV-receiver", "en": "The amplifier's input for the TV and AV receiver"},
                 "options": [[i["id"], {"sv": i["label"], "en": i["label"]}] for i in st.get("inputs", [])]}]

    def panel(self) -> dict | None:
        m = self.mode()
        if self._amp() is None:
            return None
        names = {"tv": {"sv": "TV", "en": "TV"}, "stereo": {"sv": "Musik", "en": "Music"}, "off": {"sv": "Av", "en": "Off"},
                 "other": {"sv": "Annan ingång", "en": "Another input"}}
        return {"title": {"sv": "TV-läge", "en": "TV mode"}, "summary": names.get(m, {"sv": "", "en": ""}), "controls": [
            {"type": "button", "name": "mode", "value": "tv", "pressed": m == "tv", "label": {"sv": "TV", "en": "TV"}},
            {"type": "button", "name": "mode", "value": "stereo", "pressed": m == "stereo", "label": {"sv": "Musik", "en": "Music"}},
            {"type": "button", "name": "mode", "value": "tv_off", "label": {"sv": "Stäng av efter TV", "en": "Off after TV"}}]}

    async def panel_action(self, name: str, value) -> None:
        if name == "refresh":
            return
        if name != "mode" or value not in ("tv", "stereo", "tv_off"):
            raise RuntimeError(T("Okänt reglage"))
        a = self._amp()
        if a is None or not a.host:
            raise RuntimeError(T("Förstärkaren är inte hittad"))
        try:
            await self._switch(a, value)
        except a.errors as e:
            raise RuntimeError(T("Förstärkaren: {error}", error=e))
        await a.refresh()
        self.hub.panel_changed(self)

    async def _switch(self, a, mode: str) -> dict:
        hub, tv, did = self.hub, self.hub.settings.get("tv_input", "RCA3"), {}
        if mode == "tv":
            if await hub.pause_if_playing():
                did["paused_soloist"] = True
            did |= await a.select(tv, unmute=True)
        elif mode == "stereo":
            did |= await a.select(a.play_input, unmute=True)
            a.mark_selected()  # the next music start need not do it again
        else:
            did |= await a.standby_if_on(tv)
        return did

    def features(self) -> dict:
        return {"tvmode": True}

    def routes(self, routes) -> None:
        @routes.post("/api/mode")
        async def living_room_mode(request):
            hub = self.hub
            b = await hub.body(request)
            mode = b.get("mode")
            if mode not in ("tv", "stereo", "tv_off"):
                raise web.HTTPBadRequest(text=T("mode ska vara tv, stereo eller tv_off"))
            a = self._amp()
            if a is None or not a.host:
                raise web.HTTPServiceUnavailable(text=T("Förstärkaren är inte hittad"))
            try:
                did = await self._switch(a, mode)
            except a.errors as e:
                raise web.HTTPBadGateway(text=T("Förstärkaren: {error}", error=e))
            await a.refresh()
            return hub.ok(mode=mode, did=did, **a.summary())


PLUGIN = TvMode
