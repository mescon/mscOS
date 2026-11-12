"""About the music: the text handling (the databases themselves are not asked in the tests)."""
import importlib.util
import os
import unittest

from tests import ROOT

spec = importlib.util.spec_from_file_location("about_lookup", os.path.join(ROOT, "modules", "about", "lookup.py"))
lookup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lookup)


class Texts(unittest.TestCase):
    def test_edition_notes_leave_the_title(self):
        self.assertEqual(lookup.plain("Money - 2011 Remastered Version"), "Money")
        self.assertEqual(lookup.plain("Kind of Blue (Legacy Edition)"), "Kind of Blue")
        self.assertEqual(lookup.plain("Vapen & ammunition"), "Vapen & ammunition")
        self.assertEqual(lookup.plain("Live"), "Live", "nothing left: the title stays")

    def test_the_critics_section_is_found_under_another_heading(self):
        review = "The critics liked it very much indeed, and said it was among the best records of that year."
        intro, secs = lookup.sections(f"Intro.\n\n== Release ==\n{'x' * 90}\n\n=== Critical reception ===\n{review}\n\n== Legacy ==\n{'y' * 90}")
        self.assertEqual(intro, "Intro.")
        self.assertEqual(list(secs), ["Release", "Legacy"])
        self.assertEqual(lookup.reception(secs), review)
        self.assertEqual(lookup.reception({"Track listing": "a" * 200}), "")

    def test_discogs_markup_becomes_plain_text(self):
        s = "Band from [l=Sub Pop]. Members are [a123] and [a456]. Formed by [a=Kurt Cobain (2)] in [b]1987[/b]."
        self.assertEqual(lookup.discogs_text(s), "Band from Sub Pop. Formed by Kurt Cobain in 1987.")

    def test_text_is_cut_at_a_sentence(self):
        s = "One sentence here. " * 30
        out = lookup.clip(s, 100)
        self.assertLessEqual(len(out), 100)
        self.assertTrue(out.endswith("."))

    def test_links_from_musicbrainz(self):
        d = {"relations": [{"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q2306"}},
                           {"type": "discogs", "url": {"resource": "https://www.discogs.com/master/10362"}},
                           {"type": "official homepage", "url": {"resource": "https://example.org/"}}]}
        self.assertEqual(lookup._rels(d), {"wikidata": "Q2306", "discogs": "master/10362"})
