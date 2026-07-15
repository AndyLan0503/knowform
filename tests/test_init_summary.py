import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from knowform.__main__ import _init_summary, main
from knowform.init import Proposal, Unmatched


def _u(category: str) -> Unmatched:
    return Unmatched(kind="markdown", doc_path="d.md", doc_region=(1, 1),
                     identifier="x", match_count=0, reason="r",
                     category=category)


class InitSummaryTest(unittest.TestCase):
    def test_groups_and_guides_by_category(self):
        p = Proposal()
        p.unmatched = [_u("ambiguous"), _u("stale-ref"), _u("stale-ref"),
                       _u("example")]
        text = _init_summary(p, "knowform.init.json")
        self.assertIn("4 unmatched need review", text)
        self.assertIn("1 ambiguous", text)
        self.assertIn("2 stale-ref", text)
        self.assertIn("1 example", text)
        # each category carries a concrete next step
        self.assertIn("pick one", text)
        self.assertIn("fix the doc or the code", text)
        self.assertIn(".knowformignore", text)
        # actionable categories printed before noise
        self.assertLess(text.index("ambiguous"), text.index("example"))

    def test_no_unmatched_is_a_single_line(self):
        text = _init_summary(Proposal(), "knowform.init.json")
        self.assertEqual(text,
                         "proposed 0 binding(s) -> knowform.init.json")

    def test_cli_prints_actionable_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lib.py").write_text(
                'def real():\n    """R."""\n    return 1\n')
            (root / "d.md").write_text("# D\n\nUse `gone()` here.\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["init", "--root", str(root)])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("unmatched need review", out)
            self.assertIn("stale-ref", out)
            self.assertIn("fix the doc or the code", out)


if __name__ == "__main__":
    unittest.main()
