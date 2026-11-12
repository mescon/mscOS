"""ListenBrainz: what is sent, and when a track counts as listened to."""
import importlib.util
import os
import sys
import unittest

from tests import ROOT

spec = importlib.util.spec_from_file_location("mscos_test_lb", os.path.join(ROOT, "modules", "listenbrainz", "__init__.py"))
lb = importlib.util.module_from_spec(spec)
sys.modules["mscos_test_lb"] = lb
spec.loader.exec_module(lb)

ENTRY = {"t": 1700000000, "source": "spotify", "title": "A song", "artist": "An artist", "album": "An album", "duration_ms": 200000}


class ListenBrainz(unittest.TestCase):
    def test_half_the_track_or_four_minutes(self):
        self.assertEqual(lb.listen_after(200000), 100)
        self.assertEqual(lb.listen_after(20 * 60000), 240, "a long track counts after four minutes")
        self.assertEqual(lb.listen_after(None), 240, "length not known: four minutes")
        self.assertEqual(lb.listen_after(10000), 15, "a very short track still has to play a while")

    def test_what_is_sent(self):
        now = lb.payload("playing_now", ENTRY)
        self.assertEqual(now["listen_type"], "playing_now")
        self.assertNotIn("listened_at", now["payload"][0], "playing now carries no time")
        one = lb.payload("single", ENTRY)["payload"][0]
        self.assertEqual(one["listened_at"], 1700000000)
        self.assertEqual(one["track_metadata"]["artist_name"], "An artist")
        self.assertEqual(one["track_metadata"]["release_name"], "An album")
        self.assertEqual(one["track_metadata"]["additional_info"]["music_service"], "spotify.com")
        roon = lb.payload("single", {**ENTRY, "source": "roon", "album": ""})["payload"][0]["track_metadata"]
        self.assertNotIn("music_service", roon["additional_info"], "Roon plays from many places")
        self.assertNotIn("release_name", roon)
