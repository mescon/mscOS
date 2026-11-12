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

    def test_other_places_to_read(self):
        d = {"relations": [{"type": "allmusic", "url": {"resource": "https://www.allmusic.com/artist/mn0000346336"}},
                           {"type": "official homepage", "url": {"resource": "https://example.org/"}},
                           {"type": "other databases", "url": {"resource": "https://rateyourmusic.com/artist/x"}},
                           {"type": "official homepage", "url": {"resource": "http://plain.example.org/"}},
                           {"type": "lyrics", "url": {"resource": "https://genius.com/artists/X"}},
                           {"type": "social network", "url": {"resource": "https://twitter.example/x"}}]}
        self.assertEqual([l["name"] for l in lookup.out_links(d)], ["AllMusic", "site", "Rate Your Music", "Genius"])

    def test_credits_by_person_and_by_role(self):
        rels = [{"type": "instrument", "attributes": ["electric guitar"], "artist": {"name": "A"}},
                {"type": "vocal", "attributes": ["lead vocals"], "artist": {"name": "A"}},
                {"type": "producer", "attributes": [], "artist": {"name": "B"}},
                {"type": "phonographic copyright", "artist": {"name": "Label"}},
                {"type": "performance", "work": {"relations": [{"type": "composer", "artist": {"name": "C"}}, {"type": "lyricist", "artist": {"name": "C"}}]}}]
        self.assertEqual(lookup.credits_of(rels), {"people": [["A", "electric guitar, lead vocals"]],
                                                   "roles": [["composer", "C"], ["lyricist", "C"], ["producer", "B"]]})

    def test_timed_words(self):
        self.assertEqual(lookup.lrc_lines("[00:17.69] Money\n[00:20.26] \n[01:02.5]Get away\nno time here\n[10:00] end"),
                         [[17690, "Money"], [20260, ""], [62500, "Get away"], [600000, "end"]])

    def test_lastfm_text_without_its_link(self):
        html = 'A band from <b>somewhere</b> &amp; elsewhere. <a href="https://www.last.fm/music/X">Read more on Last.fm</a>. User-contributed text…'
        self.assertEqual(lookup.lastfm_clean(html), "A band from somewhere & elsewhere.")

    def test_words_with_their_times(self):
        text = (
            "version: '1.0'\nmetadata:\n  title: Money\n  duration_ms: 383000\nlines:\n"
            "- text: Money\n  start_ms: 40580\n  end_ms: 41320\n  words:\n  - text: Money\n    start_ms: 40580\n"
            "- text: You're okay\n  start_ms: 46880\n  end_ms: 51660\n  words:\n  - text: 'You''re '\n    start_ms: 46880\n  - text: okay\n    start_ms: 50560\n"
            "plain: |\n  Money\n  - text: not a line\n"
        )
        self.assertEqual(lookup.word_lines(text), [
            {"ms": 40580, "text": "Money", "words": [[40580, "Money"]]},
            {"ms": 46880, "text": "You're okay", "words": [[46880, "You're "], [50560, "okay"]]}])
        self.assertEqual(lookup.word_lines("not that format at all"), [])

