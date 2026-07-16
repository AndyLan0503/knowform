import tempfile
import unittest
from pathlib import Path

from knowform.regions import resolve_heading_region

DOC = (
    "# Model\n"                 # 1
    "\n"                        # 2
    "Intro under Model.\n"      # 3
    "\n"                        # 4
    "## Behavior\n"             # 5
    "\n"                        # 6
    "The add function.\n"       # 7
    "\n"                        # 8
    "It sums two numbers.\n"    # 9
    "\n"                        # 10
    "## Other\n"                # 11
    "\n"                        # 12
    "Unrelated.\n"              # 13
    "\n"                        # 14
    "# Appendix\n"              # 15
    "\n"                        # 16
    "## Behavior\n"             # 17
    "\n"                        # 18
    "Duplicate.\n"              # 19
)


class HeadingResolverTest(unittest.TestCase):
    def _doc(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / "g.md").write_text(DOC, encoding="utf-8")
        return root

    def _text(self, root: Path, heading, block=None) -> str:
        r = resolve_heading_region(root, Path("g.md"), heading, block)
        self.assertIsNone(r.error, r.error)
        return r.region.text(root)

    def test_whole_section_stops_at_next_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._doc(tmp)
            text = self._text(root, ("Model", "Behavior"))
            self.assertIn("The add function.", text)
            self.assertIn("It sums two numbers.", text)
            self.assertNotIn("Unrelated", text)       # stopped at ## Other
            self.assertNotIn("Intro under Model", text)

    def test_block_ordinal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._doc(tmp)
            self.assertEqual(self._text(root, ("Model", "Behavior"), 1).strip(),
                             "The add function.")
            self.assertEqual(self._text(root, ("Model", "Behavior"), 2).strip(),
                             "It sums two numbers.")

    def test_nested_path_disambiguates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._doc(tmp)
            self.assertIn("Duplicate",
                          self._text(root, ("Appendix", "Behavior")))
            self.assertIn("add function",
                          self._text(root, ("Model", "Behavior")))

    def test_bare_ambiguous_heading_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._doc(tmp)
            r = resolve_heading_region(root, Path("g.md"), ("Behavior",))
            self.assertIsNone(r.region)
            self.assertIn("ambiguous", r.error)

    def test_missing_heading_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._doc(tmp)
            r = resolve_heading_region(root, Path("g.md"), ("Nope",))
            self.assertIsNone(r.region)
            self.assertIn("not found", r.error)

    def test_block_out_of_range_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._doc(tmp)
            r = resolve_heading_region(root, Path("g.md"),
                                       ("Model", "Behavior"), 9)
            self.assertIsNone(r.region)
            self.assertIn("out of range", r.error)

    def test_fence_hash_is_not_a_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "g.md").write_text(
                "## Real\n\ntext\n\n```python\n# not a heading\n```\n",
                encoding="utf-8")
            self.assertIn("text", self._text(root, ("Real",)))


if __name__ == "__main__":
    unittest.main()
