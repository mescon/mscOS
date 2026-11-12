"""Board profiles, translations and the volume bridge."""
import asyncio
import json
import os
import subprocess
import time
import unittest

from tests import ROOT

import board
import i18n
from volbridge import VolumeBridge


class BoardProfiles(unittest.TestCase):
    def test_parse(self):
        p = board.parse('# a comment\nAUDIO_CPU="1"\nLEDS=\'*:usr\'\nNET_SPEED=100  # trailing\nEMPTY=""\n')
        self.assertEqual(p, {"AUDIO_CPU": "1", "LEDS": "*:usr", "NET_SPEED": "100", "EMPTY": ""})

    def test_every_profile_uses_known_keys(self):
        d = os.path.join(ROOT, "boards")
        for n in sorted(os.listdir(d)):
            if n.endswith(".conf"):
                with self.subTest(profile=n), open(os.path.join(d, n)) as f:
                    unknown = set(board.parse(f.read())) - set(board.DEFAULTS)
                    self.assertFalse(unknown, f"keys the panel does not know: {unknown}")


class Translations(unittest.TestCase):
    def test_nothing_is_missing(self):
        r = subprocess.run(["python3", os.path.join(ROOT, "tools", "i18n-keys.py"), os.path.join(ROOT, "web", "static"),
                            os.path.join(ROOT, "web", "static", "i18n", "en.json")], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "[]", "texts without a translation: " + r.stdout[:600])

    def test_the_page_names_no_player(self):
        """What is one player's own lives in its module's page.js, not in the panel's page."""
        import re
        static = os.path.join(ROOT, "web", "static")
        for name in ("app.js", "setup.js", "index.html"):
            with open(os.path.join(static, name)) as f:
                s = f.read()
            self.assertIsNone(re.search(r"soloist", s, re.I), name)
            self.assertIsNone(re.search(r"""["'`]spotify["'`]""", s), name)

    def test_placeholders_survive_translation(self):
        import re
        with open(os.path.join(ROOT, "web", "static", "i18n", "en.json")) as f:
            d = json.load(f)
        for sv, en in d.items():
            self.assertEqual(sorted(re.findall(r"\{\w+\}", sv)), sorted(re.findall(r"\{\w+\}", en)), sv)

    def test_backend_texts_follow_the_language(self):
        i18n.setup(os.path.join(ROOT, "web", "static"), lambda: "sv")
        tok = i18n.use("en")
        try:
            self.assertEqual(i18n.T("Okänd modul"), "Unknown module")
            self.assertEqual(i18n.decimal(44.1), "44.1")
        finally:
            i18n._lang.reset(tok)
        tok = i18n.use("sv")
        try:
            self.assertEqual(i18n.T("Okänd modul"), "Okänd modul")
            self.assertEqual(i18n.decimal(44.1), "44,1")
        finally:
            i18n._lang.reset(tok)


class FakeAmp:
    """An amplifier with 0.5 dB steps and a 40 dB ceiling."""
    errors = (OSError,)
    max_db = 40.0

    def __init__(self, db=20.0):
        self.volume_db, self.sets, self.is_ready = db, [], True

    def ready(self):
        return self.is_ready

    def quantize(self, db):
        return round(db * 2) / 2

    async def set_volume(self, db, ceiling=None):
        self.volume_db = min(db, ceiling if ceiling is not None else self.max_db)
        self.sets.append(self.volume_db)

    async def read_volume(self):
        return self.volume_db

    async def refresh(self):
        pass


class FakeSource:
    has_slider = True

    def __init__(self):
        self.vol, self.active, self.moved = 50, True, []

    def slider(self):
        return {"active": self.active, "volume": self.vol}

    async def set_slider(self, pct):
        self.vol = pct
        self.moved.append(pct)


class FakeHub:
    def __init__(self):
        self.amp, self.source, self.settings = FakeAmp(), FakeSource(), {"spotify_vol_to_amp": True}


class VolumeBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = FakeHub()
        self.vb = VolumeBridge(self.hub)

    def test_scale(self):
        self.assertEqual(self.vb.to_db(50), 20.0)
        self.assertEqual(self.vb.to_db(100), 40.0)
        self.assertEqual(self.vb.to_db(250), 40.0)   # never above the ceiling
        self.assertEqual(self.vb.to_pct(20.0), 50)
        self.assertEqual(self.vb.to_pct(60.0), 100)  # set higher with the remote: the slider stays at the top

    async def test_slider_moves_the_amplifier(self):
        self.vb.on_volume_changed(55)
        await asyncio.sleep(0.05)
        self.assertEqual(self.hub.amp.sets, [22.0])

    async def test_a_jump_up_is_capped_and_the_slider_follows(self):
        self.vb.on_volume_changed(100)          # 20 dB -> 40 dB asked for
        await asyncio.sleep(0.05)
        self.assertEqual(self.hub.amp.sets, [26.0])  # at most 6 dB per move
        self.assertEqual(self.hub.source.moved, [65])

    async def test_nothing_moves_when_the_bridge_is_off_or_the_amplifier_is_not_ready(self):
        self.hub.settings["spotify_vol_to_amp"] = False
        self.vb.on_volume_changed(80)
        self.hub.settings["spotify_vol_to_amp"] = True
        self.hub.amp.is_ready = False
        self.vb.on_volume_changed(80)
        await asyncio.sleep(0.05)
        self.assertEqual(self.hub.amp.sets, [])

    async def test_the_app_restoring_its_own_volume_on_selection_is_not_a_move(self):
        self.vb._active_t = time.time()         # the device was just selected
        self.vb.on_volume_changed(100)
        await asyncio.sleep(0.05)
        self.assertEqual(self.hub.amp.sets, [])

    async def test_starting_playback_never_raises_the_amplifier(self):
        """The amplifier at 1 dB late at night; the app has 53 % stored and reports it a few
        seconds after playback starts. The amplifier stays; the slider is put back."""
        self.hub.amp.volume_db = 1.0
        self.vb.settle()                        # playback starts
        self.vb._active_t -= 4.0                # ... and the report comes four seconds later
        self.hub.source.vol = 53
        self.vb.on_volume_changed(53)
        await asyncio.sleep(1.2)
        self.assertEqual(self.hub.amp.sets, [])
        self.assertEqual(self.hub.amp.volume_db, 1.0)
        self.assertEqual(self.hub.source.moved, [2])   # 1 dB of 40

    async def test_a_move_after_things_settled_is_a_move(self):
        self.vb.settle()
        self.vb._active_t -= 8.0
        self.vb.on_volume_changed(55)
        await asyncio.sleep(0.05)
        self.assertEqual(self.hub.amp.sets, [22.0])

    async def test_the_bridge_does_not_answer_its_own_change(self):
        await self.vb._set_spotify(40)
        self.vb.on_volume_changed(40)           # the echo of what we just set
        await asyncio.sleep(0.05)
        self.assertEqual(self.hub.amp.sets, [])

    async def test_the_remote_moves_the_slider(self):
        self.hub.amp.volume_db = 30.0           # changed on the amplifier itself
        await self.vb.sync_from_amp()
        self.assertEqual(self.hub.source.moved, [75])


if __name__ == "__main__":
    unittest.main()
