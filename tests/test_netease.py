"""Lyrics from NetEase: which song is this recording, and its words without the credits."""
import importlib.util
import os
import sys
import unittest

from tests import ROOT

sys.path.insert(0, os.path.join(ROOT, "web", "backend"))
spec = importlib.util.spec_from_file_location("mscos_test_netease", os.path.join(ROOT, "modules", "netease", "__init__.py"))
netease = importlib.util.module_from_spec(spec)
sys.modules["mscos_test_netease"] = netease
spec.loader.exec_module(netease)

SONGS = [{"id": 1, "name": "Yesterday (Remastered)", "artists": [{"name": "The Beatles"}], "duration": 123576},
         {"id": 2, "name": "Yesterday", "artists": [{"name": "The Beatles"}, {"name": "Someone"}], "duration": 149506},
         {"id": 3, "name": "Yesterday", "artists": [{"name": "A Cover Band"}], "duration": 123000},
         {"id": 4, "name": "Yesterday Once More", "artists": [{"name": "The Beatles"}], "duration": 123576}, "junk", {"id": "x"}]


class NetEase(unittest.TestCase):
    def test_the_same_recording(self):
        self.assertEqual(netease.pick(SONGS, "The Beatles", "Yesterday - Remastered 2009", 125000), 1)
        self.assertEqual(netease.pick(SONGS, "the beatles", "Yesterday", 150000), 2, "the nearest in length")
        self.assertIsNone(netease.pick(SONGS, "The Beatles", "Yesterday", 180000), "no recording of that length: the times would not fit")
        self.assertIsNone(netease.pick(SONGS, "Nobody", "Yesterday", 123000))
        self.assertIsNone(netease.pick(None, "The Beatles", "Yesterday", 123000))

    def test_words_without_the_credits(self):
        lrc = "[00:00.000] 作词 : Paul McCartney\n[00:01.000] 作曲 : Paul McCartney\n[00:05.470]Yesterday, all my troubles\n[00:13.23][01:02.5]Now it looks\nno stamp\n[ar:x]"
        self.assertEqual(netease.lines(lrc.replace("\\\n", "\n")), [[5470, "Yesterday, all my troubles"], [13230, "Now it looks"], [62500, "Now it looks"]])
        self.assertEqual(netease.lines(""), [])
        self.assertEqual(netease.lines("[00:10.00]Dear John: a letter"), [[10000, "Dear John: a letter"]], "a colon in the song's own words is not a credit")

    def test_titles(self):
        self.assertEqual(netease.bare("Help! (Remastered 2009)"), "Help!")
        self.assertEqual(netease.bare("Let It Be"), "Let It Be")
        self.assertEqual(netease.fold("Beyoncé!"), netease.fold("beyonce"))
