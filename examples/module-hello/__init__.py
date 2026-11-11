"""An example module for mscOS, installed beside it (not shipped with it).

Copy this directory to <state>/plugins/hello on the device (by default
/var/lib/mscos/plugins/hello, owned by root), restart the panel and turn the module on under
Modules. It shows the three things a module can give the panel without any page of its own:
options, a status line, and a card with controls in the Listen view.
"""
from plugins import Plugin


class Hello(Plugin):
    id = "hello"            # the directory's name
    kind = "integration"    # source | amp | device | notify | integration
    name = {"sv": "Exempelmodul", "en": "Example module"}
    description = {"sv": "Visar hur en egen modul ser ut.", "en": "Shows what a module of your own looks like."}
    defaults = {"hello_name": "", "hello_count": 0}
    options = (
        {"key": "hello_name", "type": "text", "label": {"sv": "Vem ska hälsas", "en": "Whom to greet"}},
    )

    def update_settings(self, body: dict, upd: dict) -> None:
        if "hello_name" in body:
            upd["hello_name"] = str(body["hello_name"])[:40]

    def status(self) -> dict | None:
        n = self.hub.settings.get("hello_count", 0)
        return {"state": "ok", "text": {"sv": f"Tryckt {n} gånger", "en": f"Pressed {n} times"}}

    def panel(self) -> dict | None:
        who = self.hub.settings.get("hello_name") or "mscOS"
        return {"title": {"sv": "Exempel", "en": "Example"},
                "summary": {"sv": f"Hej {who}", "en": f"Hello {who}"},
                "controls": [{"type": "button", "name": "press", "value": 1, "label": {"sv": "Tryck", "en": "Press"}}]}

    async def panel_action(self, name: str, value) -> None:
        if name == "press":
            self.hub.settings["hello_count"] = self.hub.settings.get("hello_count", 0) + 1
            self.hub.save_settings()
            self.hub.panel_changed(self)


PLUGIN = Hello
