import unittest

from dlna import DlnaError
from dlna.contentdir import Container
from dlna.resolve import Match, NotFound, Segment, _match_all, escape_title, pick, split_path


def texts(path):
    return [s.text for s in split_path(path)]


class SplitPathTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(split_path("/NAS/Movies/Dune.mkv/"),
                         [Segment("NAS"), Segment("Movies"), Segment("Dune.mkv")])

    def test_escapes(self):
        self.assertEqual(texts(r"NAS/AC\/DC/a\\b"), ["NAS", "AC/DC", "a\\b"])

    def test_object_id(self):
        self.assertEqual(split_path("NAS/#64$1$2/x")[1], Segment("64$1$2", is_id=True))
        self.assertEqual(split_path(r"NAS/\#hashtag")[1], Segment("#hashtag"))
        self.assertEqual(split_path("NAS/a#b")[1], Segment("a#b"))

    def test_descendant(self):
        segs = split_path("NAS/Media//Season 1/x")
        self.assertEqual([s.descendant for s in segs], [False, False, True, False])
        self.assertEqual(texts("NAS///a"), ["NAS", "a"])
        self.assertTrue(split_path("NAS///a")[1].descendant)
        # leading/trailing slashes don't create descendant steps
        self.assertEqual(split_path("//NAS//"), [Segment("NAS")])

    def test_glob(self):
        seg = split_path("NAS/dune*")[1]
        self.assertEqual(seg.pattern, "dune*")
        self.assertTrue(seg.matches("Dune Part Two"))
        self.assertFalse(seg.matches("The Dune"))
        self.assertIsNone(split_path("NAS/plain")[1].pattern)

    def test_escaped_glob_is_literal(self):
        seg = split_path(r"NAS/what\?*")[1]
        self.assertEqual(seg.text, "what?*")
        self.assertTrue(seg.matches("What? Really"))
        self.assertFalse(seg.matches("Whatever"))
        self.assertIsNone(split_path(r"NAS/\*\[x]")[1].pattern)

    def test_non_glob_set_match_is_exact_casefold(self):
        seg = Segment("season 1")
        self.assertTrue(seg.matches("Season 1"))
        self.assertFalse(seg.matches("Season 10"))

    def test_escape_roundtrip(self):
        for title in ["AC/DC", "#1 hits", "back\\slash", "plain", "a/#/b", "What? [Live] *", "#"]:
            seg = split_path("S/" + escape_title(title))[1]
            self.assertEqual((seg.text, seg.pattern, seg.is_id), (title, None, False))


class MatchAllTests(unittest.TestCase):
    candidates = [Match(("S", t), Container(id=t, parent_id="0", title=t))
                  for t in ["Season 1", "Season 10", "Kittens (1999)", "season 1"]]

    def titles(self, path):
        return [m.entry.title for m in _match_all(self.candidates, split_path("S//" + path)[1])]

    def test_exact_beats_prefix(self):
        self.assertEqual(self.titles("Season 1"), ["Season 1", "season 1"])

    def test_prefix_fallback(self):
        self.assertEqual(self.titles("kittens"), ["Kittens (1999)"])

    def test_glob_has_no_prefix_fallback(self):
        self.assertEqual(self.titles("kittens?"), [])
        self.assertEqual(self.titles("*1?"), ["Season 10"])


class PickTests(unittest.TestCase):
    names = ["Movies", "Audio", "movies (old)", "Series"]

    def p(self, name):
        return pick(self.names, name, key=str)

    def test_exact_then_casefold_then_prefix(self):
        self.assertEqual(self.p("Movies"), "Movies")
        self.assertEqual(self.p("series"), "Series")
        self.assertEqual(self.p("ser"), "Series")
        self.assertEqual(self.p("movies"), "Movies")  # casefold-exact beats prefix ambiguity

    def test_ambiguous_and_missing(self):
        with self.assertRaisesRegex(DlnaError, "ambiguous"):
            self.p("m")
        with self.assertRaises(NotFound):
            self.p("Podcasts")


if __name__ == "__main__":
    unittest.main()
