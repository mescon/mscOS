"""Spotify's Web API client: how its answers become the Music view's rows."""
import importlib.util
import os
import sys
import unittest

import tests
from tests import ROOT

spec = importlib.util.spec_from_file_location("mscos_test_webapi", os.path.join(ROOT, "modules", "spotify", "webapi.py"))
webapi = importlib.util.module_from_spec(spec)
sys.modules["mscos_test_webapi"] = webapi
spec.loader.exec_module(webapi)

IMAGES = [{"url": "https://i.example/640", "width": 640}, {"url": "https://i.example/300", "width": 300}, {"url": "https://i.example/64", "width": 64}]


class Rows(unittest.TestCase):
    def test_the_picture_nearest_the_size(self):
        self.assertEqual(webapi.image(IMAGES), "https://i.example/300")
        self.assertEqual(webapi.image(IMAGES, 64), "https://i.example/64")
        self.assertIsNone(webapi.image([{"url": "http://plain.example/x", "width": 300}]), "https or nothing")
        self.assertIsNone(webapi.image(None))

    def test_rows(self):
        album = {"uri": "spotify:album:" + "a" * 22, "id": "a" * 22, "name": "An album", "artists": [{"name": "A"}, {"name": "B"}],
                 "release_date": "1973-03-01", "album_type": "album", "images": IMAGES}
        self.assertEqual(webapi.album_row(album), {"uri": album["uri"], "id": "a" * 22, "name": "An album", "artist": "A, B",
                                                   "year": "1973", "kind": "album", "cover": "https://i.example/300"})
        track = {"uri": "spotify:track:" + "t" * 22, "name": "A song", "artists": [{"name": "A"}], "duration_ms": 1000, "track_number": 3}
        self.assertEqual(webapi.track_row(track, album)["album"], "An album", "an album's own tracks carry no album: it is given")
        self.assertEqual(webapi.track_row(track, album)["n"], 3)

    def test_a_playlist(self):
        p = {"uri": "spotify:playlist:" + "p" * 22, "id": "p" * 22, "name": "Morning", "owner": {"display_name": "Someone"},
             "items": {"total": 29}, "images": IMAGES, "description": 'Calm <a href="x">music</a> for mornings'}
        self.assertEqual(webapi.playlist_row(p), {"uri": p["uri"], "id": "p" * 22, "name": "Morning", "owner": "Someone", "count": 29,
                                                  "cover": "https://i.example/300", "about": "Calm music for mornings"})
        self.assertIsNone(webapi.playlist_row({"uri": "x"})["count"])

    def test_ids(self):
        self.assertTrue(webapi.ID.fullmatch("4LH4d3cOWNNsVw41Gqt2kv"))
        for bad in ("", "../albums", "4LH4d3cOWNNsVw41Gqt2k", "4LH4d3cOWNNsVw41Gqt2kv/x"):
            self.assertIsNone(webapi.ID.fullmatch(bad), bad)

    def test_no_credentials_no_search(self):
        self.assertFalse(webapi.WebApi(lambda: ("", "")).ready())
        self.assertFalse(webapi.WebApi(lambda: ("id", "")).ready())
        self.assertTrue(webapi.WebApi(lambda: ("id", "secret")).ready())
