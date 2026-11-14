"""Spotify's Web API client: how its answers become the Music view's rows."""
import importlib.util
import os
import sys
import unittest

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
        album = {"uri": "spotify:album:" + "a" * 22, "id": "a" * 22, "name": "An album", "artists": [{"name": "A", "id": "x" * 22}, {"name": "B"}],
                 "release_date": "1973-03-01", "album_type": "album", "images": IMAGES}
        self.assertEqual(webapi.album_row(album), {"uri": album["uri"], "id": "a" * 22, "name": "An album", "artist": "A, B",
                                                   "artists": [{"id": "x" * 22, "name": "A"}, {"id": None, "name": "B"}],
                                                   "year": "1973", "kind": "album", "cover": "https://i.example/300"})
        track = {"uri": "spotify:track:" + "t" * 22, "name": "A song", "artists": [{"name": "A"}], "duration_ms": 1000, "track_number": 3}
        self.assertEqual(webapi.track_row(track, album)["album"], "An album", "an album's own tracks carry no album: it is given")
        self.assertEqual(webapi.track_row(track, album)["n"], 3)

    def test_a_playlist(self):
        p = {"uri": "spotify:playlist:" + "p" * 22, "id": "p" * 22, "name": "Morning", "owner": {"display_name": "Someone", "id": "someone"},
             "items": {"total": 29}, "images": IMAGES, "description": 'Calm <a href="x">music</a> for mornings'}
        self.assertEqual(webapi.playlist_row(p), {"uri": p["uri"], "id": "p" * 22, "name": "Morning", "owner": "Someone", "owner_id": "someone", "snap": "", "count": 29,
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

    def test_signed_in_needs_no_secret(self):
        kept = []
        w = webapi.WebApi(lambda: ("id", ""), refresh=lambda: kept[-1] if kept else "", keep_refresh=kept.append)
        self.assertFalse(w.ready())
        kept.append("a refresh token")
        self.assertTrue(w.ready() and w.signed_in())
        w.sign_out()
        self.assertFalse(w.signed_in(), "signing out forgets the token")

    def test_held_back(self):
        import asyncio
        import time

        async def go():
            w = webapi.WebApi(lambda: ("id", "secret"))
            for _ in range(webapi.BURST):
                await w._my_turn()
            self.assertEqual(w.asked()["half_minute"], webapi.BURST, "a burst goes through at once")
            with self.assertRaises(asyncio.TimeoutError, msg="the next one waits for its turn"):
                await asyncio.wait_for(w._my_turn(), 0.2)
            w2 = webapi.WebApi(lambda: ("id", "secret"))
            w2.wait_until = time.time() + 60
            with self.assertRaises(webapi.WebApiError, msg="while Spotify has asked for a pause, nothing is asked"):
                await w2._my_turn()
            self.assertEqual(w2.asked()["half_minute"], 0)
        asyncio.run(go())
        self.assertLessEqual(30 / webapi.EVERY * 10, 120, "over five minutes: well under what brought a pause")

    def test_the_same_question_is_asked_once(self):
        import asyncio

        async def go():
            w = webapi.WebApi(lambda: ("id", "secret"))
            w.http = object()
            asked = []

            async def fake(path, key, keep, params):
                asked.append(path)
                await asyncio.sleep(0.05)
                return {"n": len(asked)}
            w._get = fake
            got = await asyncio.gather(w.get("me/playlists", limit=50), w.get("me/playlists", limit=50), w.get("me/albums", limit=50))
            self.assertEqual(asked.count("me/playlists"), 1)
            self.assertEqual(got[0], got[1])
        asyncio.run(go())

    def test_a_pause_is_kept(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as t:
            f = os.path.join(t, "wait")
            w = webapi.WebApi(lambda: ("id", "secret"), wait_file=f)
            w._pause("3000")
            again = webapi.WebApi(lambda: ("id", "secret"), wait_file=f)
            self.assertGreater(again.asked()["wait"], 2900, "a restart does not forget it")

    def test_the_answer_brought_back_by_hand(self):
        parts = webapi.answer_parts
        self.assertEqual(parts(" http://127.0.0.1:8888/callback?code=abc&state=xyz \n"), ("abc", "xyz", ""))
        self.assertEqual(parts("code=abc&state=xyz"), ("abc", "xyz", ""), "what follows the question mark is enough")
        self.assertEqual(parts("http://127.0.0.1:8888/callback?error=access_denied&state=xyz"), ("", "xyz", "access_denied"))
        self.assertEqual(parts("something else"), ("", "", ""))
        self.assertEqual(parts(None), ("", "", ""))
        self.assertEqual(parts("http://[not-an-address?code=x"), ("", "", ""))

    def test_leave_to_save(self):
        kept = {"refresh": "r", "scope": "user-library-read user-read-playback-state"}
        w = webapi.WebApi(lambda: ("id", ""), refresh=lambda: kept["refresh"], granted=lambda: kept["scope"], keep_granted=lambda s: kept.update(scope=s))
        self.assertFalse(w.can_save(), "an older sign-in reads only")
        kept["scope"] += " user-library-modify"
        self.assertTrue(w.can_save())
        for ok in ("spotify:track:" + "a" * 22, "spotify:album:" + "b" * 22):
            self.assertTrue(webapi.SAVES.fullmatch(ok))
        for bad in ("spotify:artist:" + "a" * 22, "spotify:playlist:" + "a" * 22, "spotify:track:short", "spotify:track:" + "a" * 22 + ",x", ""):
            self.assertIsNone(webapi.SAVES.fullmatch(bad), bad)

    def test_the_sign_in_address(self):
        from urllib.parse import parse_qs, urlsplit
        w = webapi.WebApi(lambda: ("my id", ""))
        u = urlsplit(w.sign_in_address("https://panel.example/api/spotify/callback"))
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.assertEqual((u.scheme, u.netloc, u.path), ("https", "accounts.spotify.com", "/authorize"))
        self.assertEqual((q["client_id"], q["response_type"], q["code_challenge_method"]), ("my id", "code", "S256"))
        self.assertEqual(q["redirect_uri"], "https://panel.example/api/spotify/callback")
        self.assertIn("user-library-modify", q["scope"].split(), "leave to like and save")
        self.assertNotIn("user-modify-playback-state", q["scope"], "the music is not steered this way")
        for more in ("user-follow-modify", "playlist-modify-public", "playlist-modify-private"):
            self.assertNotIn(more, q["scope"], "no leave beyond the library")
        self.assertNotIn(w.pending[1], u.query, "the verifier stays on the device")
        with self.assertRaises(webapi.WebApiError):
            webapi.WebApi(lambda: ("", "")).sign_in_address("https://panel.example/x")

    def test_a_code_with_another_state_is_not_taken(self):
        import asyncio
        w = webapi.WebApi(lambda: ("id", ""))
        w.sign_in_address("https://panel.example/api/spotify/callback")
        with self.assertRaises(webapi.WebApiError):
            asyncio.run(w.finish("a code", "not the state"))
        self.assertIsNotNone(w.pending, "someone else's guess does not end the sign-in that was begun")
        with self.assertRaises(webapi.WebApiError):
            asyncio.run(w.finish("", w.pending[0]))
        self.assertIsNone(w.pending, "the right state is good once")
