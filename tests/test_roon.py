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

    def test_a_kept_token_goes_only_to_its_own_server(self):
        sent = []

        class Ws:
            def __init__(self, frames):
                self.frames = frames

            async def send(self, raw):
                sent.append(roonapi.moo_parse(raw))

            def __aiter__(self):
                async def gen():
                    for f in self.frames:
                        yield f
                return gen()

        info = roonapi.moo_encode("COMPLETE", "Success", 0, {"core_id": "core-A"})
        tokens = {"core-A": "tok-A"}
        for claimed, want in (("core-A", "tok-A"), ("core-B", None)):
            sent.clear()
            api = roonapi.Roon({"extension_id": "x"}, lambda: None, lambda cid: tokens.get(cid, ""))
            asyncio.run(api.run(Ws([info.replace(b"core-A", claimed.encode())])))
            reg = next(m["body"] for m in sent if m["name"].endswith("/register"))
            self.assertEqual(reg.get("token"), want, claimed)

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
            asked = []
            api = roonapi.Roon({"extension_id": "org.example.test", "display_name": "Test"}, lambda: changes.append(1),
                               lambda core_id: asked.append(core_id) or "")
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
        self.assertEqual(asked, ["core-1"], "the token is chosen by who the server says it is")
        self.assertIn(("COMPLETE", "Success", None), seen, "the server's ping is answered")
        self.assertIn(("COMPLETE", "InvalidRequest", None), seen, "a service we do not offer is refused")


class BridgeLog(unittest.TestCase):
    """What Roon Bridge's log says of the stream it plays (modules/roon/raat.py)."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("mscos_test_raat", os.path.join(ROOT, "modules", "roon", "raat.py"))
        self.raat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.raat)

    HEAD = "11/12 23:26:25 Trace: [RAAT::Some DAC] "
    SETUP = HEAD + '[lua@0x1] [192.0.2.7:41622]  GOT [82] {"request":"setup","format":{"sample_type":"pcm","sample_rate":96000,"bits_per_sample":24,"channels":2,"sample_subtype":"none"},"wire_codec":"flac"}'

    def test_the_latest_stream(self):
        text = "\n".join([
            self.HEAD + '[lua@0x1] [192.0.2.7:41622]  GOT [70] {"request":"setup","format":{"sample_type":"dsd","sample_rate":2822400,"bits_per_sample":1,"channels":2,"sample_subtype":"native"},"wire_codec":"pcm"}',
            self.HEAD + '[lua@0x1] [192.0.2.7:41622]  GOT [81] {"request":"teardown"}',
            self.SETUP,
            self.HEAD + "[ALSA] [hw:CARD=Audio,DEV=0] using hw pcmformat S32_LE bitspersample 24",
            self.HEAD + "[ALSA] [hw:CARD=Audio,DEV=0] Initialized with 2 periods with 1920 frames/period and 3840 frames/buffer",
            self.HEAD + "sched_setscheduler succeeded",
            self.HEAD + '[lua@0x1] [192.0.2.7:41622]  GOT [84] {"request":"start","min_offset":1,"stream_sample":0}'])
        got = self.raat.parse(text)
        self.assertEqual((got["type"], got["rate"], got["bits"], got["wire"]), ("pcm", 96000, 24, "flac"))
        self.assertEqual((got["hw_format"], got["hw_bits"], got["period"], got["buffer"]), ("S32_LE", 24, 1920, 3840))
        self.assertTrue(got["realtime"] and got["open"])
        self.assertEqual(got["at"], "11/12 23:26:25")

    def test_a_stream_that_ended_and_a_log_without_one(self):
        self.assertFalse(self.raat.parse(self.SETUP + "\n" + self.HEAD + '[lua@0x1] GOT [90] {"request":"end_stream"}')["open"])
        self.assertEqual(self.raat.parse("11/12 22:00:00 Info: nothing of note\n"), {})

    def test_what_the_log_says_is_not_taken_on_trust(self):
        # a log line is text from another program: only numbers and plain words are passed on
        line = self.HEAD + 'GOT [1] {"request":"setup","format":{"sample_type":"<b>pcm</b>","sample_rate":"44100","bits_per_sample":-5,"channels":2},"wire_codec":{"a":1}}'
        got = self.raat.parse(line)
        self.assertEqual((got["type"], got["rate"], got["bits"], got["wire"]), ("", None, None, ""))
        self.assertEqual(self.raat.parse(self.HEAD + 'GOT [1] {"request":"setup", broken')["rate"], None)

    def test_a_log_that_is_not_there(self):
        self.assertEqual(self.raat.Log("/nonexistent/raat.txt").latest(), {})
