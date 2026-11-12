"""Roon: the extension client against a stand-in for a Roon server (its messages, as
node-roon-api speaks them)."""
import asyncio
import importlib.util
import os
import sys
import unittest

import websockets

from tests import ROOT

spec = importlib.util.spec_from_file_location("mscos_test_roonapi", os.path.join(ROOT, "modules", "roon", "roonapi.py"))
roonapi = importlib.util.module_from_spec(spec)
sys.modules["mscos_test_roonapi"] = roonapi
spec.loader.exec_module(roonapi)

ZONE = {"zone_id": "1601aabbccddeeff00112233", "display_name": "Living room", "state": "playing",
        "outputs": [{"output_id": "1701aa", "display_name": "Living room"}], "settings": {"loop": "disabled", "shuffle": False},
        "now_playing": {"seek_position": 12, "length": 200, "image_key": "abcdef0123456789",
                        "three_line": {"line1": "A song", "line2": "An artist", "line3": "An album"}}}


class Messages(unittest.TestCase):
    def test_a_request_and_its_answer(self):
        raw = roonapi.moo_encode("REQUEST", "com.roonlabs.transport:2/control", 7, {"control": "pause"})
        self.assertTrue(raw.startswith(b"MOO/1 REQUEST com.roonlabs.transport:2/control\nRequest-Id: 7\nContent-Length: 20\nContent-Type: application/json\n\n"))
        self.assertEqual(roonapi.moo_parse(raw), {"verb": "REQUEST", "name": "com.roonlabs.transport:2/control", "request_id": "7", "body": {"control": "pause"}})
        self.assertEqual(roonapi.moo_parse(b"MOO/1 COMPLETE Success\nRequest-Id: 3\n\n"), {"verb": "COMPLETE", "name": "Success", "request_id": "3", "body": None})

    def test_what_is_not_a_message(self):
        for junk in (b"", b"hello", b"MOO/1 COMPLETE Success\n\n", b"HTTP/1.1 200 OK\nRequest-Id: 1\n\n",
                     b"MOO/1 COMPLETE Success\nRequest-Id: 1\nContent-Length: 5\nContent-Type: application/json\n\n{bad}"):
            self.assertIsNone(roonapi.moo_parse(junk), junk)

    def test_discovery_messages(self):
        q = roonapi.sood_query("tid-1")
        self.assertEqual(roonapi.sood_parse(q), {"_type": "Q", "query_service_id": roonapi.ROON_SERVICE, "_tid": "tid-1"})
        for junk in (b"", b"SOOD", b"SOOD\x01Q", b"SOOD\x02R\x05ab", b"NOPE\x02R\x01a\x00\x01b"):
            self.assertIsNone(roonapi.sood_parse(junk), junk)


class Extension(unittest.IsolatedAsyncioTestCase):
    async def test_approved_then_told_what_plays_and_obeyed(self):
        seen, approve = [], asyncio.Event()

        async def server(ws):
            async def say(verb, name, rid, body=None):
                await ws.send(roonapi.moo_encode(verb, name, rid, body))
            async for frame in ws:
                m = roonapi.moo_parse(frame)
                seen.append((m["verb"], m["name"], m["body"]))
                if m["verb"] != "REQUEST":
                    continue
                if m["name"].endswith("/info"):
                    await say("COMPLETE", "Success", m["request_id"], {"core_id": "core-1", "display_name": "Server"})
                elif m["name"].endswith("/register"):
                    async def later(rid=m["request_id"]):   # the listener enables the extension in Roon
                        await approve.wait()
                        await say("CONTINUE", "Registered", rid, {"core_id": "core-1", "token": "tok-1", "http_port": 9330})
                    asyncio.ensure_future(later())
                elif m["name"].endswith("/subscribe_zones"):
                    await say("CONTINUE", "Subscribed", m["request_id"], {"zones": [ZONE]})
                    await say("CONTINUE", "Changed", m["request_id"], {"zones_seek_changed": [{"zone_id": ZONE["zone_id"], "seek_position": 13}]})
                    await say("REQUEST", "com.roonlabs.ping:1/ping", 900)     # the server checks that we are there
                    await say("REQUEST", "com.example.unknown:1/thing", 901)
                elif m["name"].endswith("/control"):
                    await say("COMPLETE", "Success" if m["body"]["control"] == "pause" else "InvalidRequest", m["request_id"])

        changes = []
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            api = roonapi.Roon({"extension_id": "org.example.test", "display_name": "Test"}, lambda: changes.append(1))
            async with websockets.connect(f"ws://127.0.0.1:{port}/api") as ws:
                run = asyncio.ensure_future(api.run(ws))
                await asyncio.sleep(0.3)
                self.assertFalse(api.registered, "not let in before the listener approves")
                self.assertEqual(api.zones, {})
                approve.set()
                for _ in range(50):
                    if api.zones and api.zones[ZONE["zone_id"]]["now_playing"]["seek_position"] == 13:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(api.registered)
                self.assertEqual(api.token, "tok-1")
                z = api.zones[ZONE["zone_id"]]
                self.assertEqual(z["now_playing"]["three_line"]["line1"], "A song")
                self.assertEqual(z["now_playing"]["seek_position"], 13, "the position follows the server")
                await api.control(ZONE["zone_id"], "pause")
                with self.assertRaises(RuntimeError):
                    await api.control(ZONE["zone_id"], "fly")
                await asyncio.sleep(0.1)
                run.cancel()
        self.assertTrue(changes)
        reg = next(b for v, n, b in seen if n.endswith("/register"))
        self.assertEqual(reg["extension_id"], "org.example.test")
        self.assertNotIn("token", reg, "no token the first time")
        self.assertIn(("COMPLETE", "Success", None), seen, "the server's ping is answered")
        self.assertIn(("COMPLETE", "InvalidRequest", None), seen, "a service we do not offer is refused")
