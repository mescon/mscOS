"""The panel's HTTP API on a new installation: no modules on, no DAC, no systemd.
One app, one hub: the steps run in order, like a first evening with the device."""
import json
import unittest

from aiohttp.test_utils import TestClient, TestServer

import tests  # noqa: F401 - sets the paths and a state directory of the run's own

import app as panel

JSON = {"Content-Type": "application/json"}


class NewInstallation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(panel.make_app(background=False)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def post(self, path, data=None, headers=None):
        return await self.client.post(path, data=json.dumps(data or {}), headers={**JSON, **(headers or {})})

    async def json(self, path):
        r = await self.client.get(path)
        self.assertEqual(r.status, 200, path)
        return await r.json()

    async def test_first_evening(self):
        # the page and what it needs are served
        for path in ("/", "/app.js", "/app.css", "/i18n/en.json", "/manifest.webmanifest"):
            r = await self.client.get(path)
            self.assertEqual(r.status, 200, path)

        # a module's page code is served only while the module is on, and nothing else from its folder ever
        for path in ("/modules/spotify/page.js", "/modules/spotify/i18n/en.json", "/modules/spotify/__init__.py",
                     "/modules/spotify/module.json", "/modules/made-up/page.js"):
            self.assertEqual((await self.client.get(path)).status, 404, path)

        # every module is offered, none is on
        mods = (await self.json("/api/plugins"))["plugins"]
        self.assertGreaterEqual(len(mods), 10)
        self.assertFalse([m["id"] for m in mods if m["enabled"]])
        self.assertEqual({m["kind"] for m in mods}, {"source", "amp", "device", "notify", "integration"})

        # nothing can be changed before a password exists, and a short one is refused
        self.assertEqual((await self.post("/api/settings", {"output_name": "x"})).status, 401)
        self.assertEqual((await self.post("/api/auth/setup", {"password": "short"})).status, 400)
        self.assertEqual((await self.post("/api/auth/setup", {"password": "a long enough one"})).status, 200)
        self.assertTrue((await self.json("/api/auth"))["logged_in"])

        # names: the DAC, the streamer, and which of them the device is called on the network
        r = await self.post("/api/settings", {"output_name": "Hejsan", "streamer_name": "Svejsan"})
        self.assertEqual(r.status, 200)
        n = await self.json("/api/names")
        self.assertEqual((n["output"], n["streamer"], n["network"]), ("Hejsan", "Svejsan", "Svejsan"))
        await self.post("/api/settings", {"network_name": "output"})
        self.assertEqual((await self.json("/api/names"))["network"], "Hejsan")
        self.assertEqual((await self.post("/api/settings", {"network_name": "custom"})).status, 400)  # no name given
        await self.post("/api/settings", {"network_name": "custom", "network_name_custom": "Vardagsrummet"})
        self.assertEqual((await self.json("/api/names"))["network"], "Vardagsrummet")
        self.assertEqual((await self.post("/api/settings", {"network_name": "nonsense"})).status, 400)
        self.assertEqual((await self.post("/api/settings", {"output_name": "x" * 61})).status, 400)

        # which alerts the user wants
        self.assertEqual((await self.post("/api/settings", {"alerts_off": ["dropouts"]})).status, 200)
        self.assertEqual((await self.post("/api/settings", {"alerts_off": ["made-up"]})).status, 400)
        # a module's own alerts exist only while the module is on
        self.assertEqual((await self.post("/api/settings", {"alerts_off": ["verify"]})).status, 400)
        s = await self.json("/api/settings")
        self.assertEqual(s["alerts_off"], ["dropouts"])
        self.assertFalse(s["alerts_configured"])  # no alert module is on
        self.assertEqual((await self.post("/api/alerts/test")).status, 409)

        # without a source or an amplifier module their endpoints say so
        self.assertEqual((await self.post("/api/player", {"command": "toggle"})).status, 409)
        self.assertEqual((await self.post("/api/amp", {"volume_db": 10})).status, 404)
        self.assertEqual((await self.post("/api/panels/arcam", {"name": "power", "value": True})).status, 404)
        self.assertEqual((await self.post("/api/source", {"source": "made-up"})).status, 400)

        # turning a module on is remembered and asks for a restart of the panel
        self.assertEqual((await self.post("/api/plugins/set", {"id": "made-up", "enabled": True})).status, 404)
        self.assertEqual((await self.post("/api/plugins/set", {"id": "ntfy", "enabled": True})).status, 200)
        p = await self.json("/api/plugins")
        self.assertTrue(p["restart_needed"])
        self.assertEqual([m["id"] for m in p["plugins"] if m["enabled"]], ["ntfy"])
        # a module that needs an amplifier is refused without one
        self.assertEqual((await self.post("/api/plugins/set", {"id": "tvmode", "enabled": True})).status, 409)

        # installing a module asks for an address or a catalogue id it can use
        self.assertEqual((await self.post("/api/plugins/install", {"url": "http://example.org/module.json"})).status, 400)

        # logging out locks it again
        self.assertEqual((await self.post("/api/auth/logout")).status, 200)
        self.assertEqual((await self.post("/api/settings", {"output_name": "x"})).status, 401)
        self.assertEqual((await self.post("/api/auth/login", {"password": "wrong"})).status, 401)
        self.assertEqual((await self.post("/api/auth/login", {"password": "a long enough one"})).status, 200)

    async def test_requests_from_elsewhere_are_refused(self):
        # another site's page: a foreign Origin, or a form post (not JSON)
        r = await self.post("/api/player", {"command": "toggle"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        r = await self.client.post("/api/player", data="command=toggle", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 415)
        # DNS rebinding: a name that is not this device's
        r = await self.client.get("/api/status", headers={"Host": "evil.example"})
        self.assertEqual(r.status, 421)

    async def test_secrets_never_leave(self):
        panel.hub.set_secret("ntfy_url", "https://ntfy.example/a-secret-topic")
        try:
            for path in ("/api/settings", "/api/plugins", "/api/status"):
                self.assertNotIn("a-secret-topic", await (await self.client.get(path)).text(), path)
        finally:
            panel.hub.set_secret("ntfy_url", "")


if __name__ == "__main__":
    unittest.main()
