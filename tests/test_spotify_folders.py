"""The listener's own folders for their Spotify playlists: a tree that holds together."""
import importlib.util
import os
import sys
import tempfile
import unittest

from tests import ROOT

spec = importlib.util.spec_from_file_location("mscos_test_folders", os.path.join(ROOT, "modules", "spotify", "folders.py"))
folders = importlib.util.module_from_spec(spec)
sys.modules["mscos_test_folders"] = folders
spec.loader.exec_module(folders)

P1, P2 = "1" * 22, "2" * 22


class Folders(unittest.TestCase):
    def tree(self):
        d = folders.change(folders.empty(), {"op": "add", "name": "  Jazz   and such "})
        a = d["folders"][0]["id"]
        d = folders.change(d, {"op": "add", "name": "Trios", "parent": a})
        return d, a, d["folders"][1]["id"]

    def test_add_and_place(self):
        d, a, b = self.tree()
        self.assertEqual(d["folders"][0]["name"], "Jazz and such")
        self.assertEqual(d["folders"][1]["parent"], a)
        d = folders.change(d, {"op": "place", "playlist": P1, "folder": b})
        self.assertEqual(d["place"], {P1: b})
        self.assertEqual(folders.change(d, {"op": "place", "playlist": P1, "folder": None})["place"], {})

    def test_what_was_in_a_removed_folder_moves_up(self):
        d, a, b = self.tree()
        d = folders.change(folders.change(d, {"op": "place", "playlist": P1, "folder": b}), {"op": "place", "playlist": P2, "folder": a})
        up = folders.change(d, {"op": "remove", "id": b})
        self.assertEqual(up["place"], {P1: a, P2: a})
        top = folders.change(d, {"op": "remove", "id": a})
        self.assertIsNone(top["folders"][0]["parent"])
        self.assertEqual(top["place"], {P1: b}, "from a folder at the top, a playlist goes back to none")

    def test_not_into_itself(self):
        d, a, b = self.tree()
        for op in ({"op": "move_folder", "id": a, "parent": b}, {"op": "move_folder", "id": a, "parent": a}):
            with self.assertRaises(folders.Refused):
                folders.change(d, op)
        self.assertIsNone(folders.change(d, {"op": "move_folder", "id": b, "parent": None})["folders"][1]["parent"])

    def test_refused(self):
        d, a, _ = self.tree()
        for op in ({"op": "add", "name": "  "}, {"op": "add", "name": "x", "parent": "nope"}, {"op": "rename", "id": "f0000000000", "name": "x"},
                   {"op": "place", "playlist": "../x", "folder": a}, {"op": "place", "playlist": P1, "folder": "nope"}, {"op": "else"}, {}):
            with self.assertRaises(folders.Refused, msg=op):
                folders.change(d, op)
        deep = folders.empty()
        parent = None
        for _ in range(folders.MAX_DEPTH):
            deep = folders.change(deep, {"op": "add", "name": "x", "parent": parent})
            parent = deep["folders"][-1]["id"]
        with self.assertRaises(folders.Refused):
            folders.change(deep, {"op": "add", "name": "too deep", "parent": parent})

    def test_a_file_that_does_not_hold_together(self):
        d = folders.clean({"folders": [{"id": "f" + "a" * 10, "name": "A", "parent": "f" + "b" * 10}, {"id": "f" + "b" * 10, "name": "B", "parent": "f" + "a" * 10},
                                       {"id": "bad", "name": "x"}, {"id": "f" + "c" * 10, "name": ""}, "junk"],
                           "place": {P1: "f" + "a" * 10, P2: "gone", "bad id": "f" + "a" * 10}})
        self.assertEqual(len(d["folders"]), 2)
        self.assertTrue(any(f["parent"] is None for f in d["folders"]), "a ring is opened")
        self.assertEqual(d["place"], {P1: "f" + "a" * 10})
        self.assertEqual(folders.clean("junk"), folders.empty())

    def test_kept_root_only(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "f.json")
            d, _, _ = self.tree()
            folders.save(p, d)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            self.assertEqual(folders.load(p), d)
            self.assertEqual(folders.load(os.path.join(t, "none.json")), folders.empty())
