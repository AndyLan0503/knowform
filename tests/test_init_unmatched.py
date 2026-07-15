import tempfile
import unittest
from pathlib import Path

from knowform.init import init


def _write(root: Path, name: str, text: str) -> None:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


STORE = 'def process(data):\n    """Store."""\n    return data\n'
QUEUE = 'def process(job):\n    """Queue."""\n    return job\n'
LIB = 'def real():\n    """R."""\n    return 1\n'


class UnmatchedTaxonomyTest(unittest.TestCase):
    def _only(self, root: Path, ident: str):
        return next(u for u in init(root).unmatched if u.identifier == ident)

    def test_ambiguous_multi_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "store.py", STORE)
            _write(root, "queue.py", QUEUE)
            _write(root, "d.md", "# D\n\nCall `process()` now.\n")
            u = self._only(root, "process")
            self.assertEqual(u.category, "ambiguous")
            self.assertEqual(u.context, "inline")

    def test_stale_ref_inline_prose(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "d.md", "# D\n\nUse `gone()` to do it.\n")
            u = self._only(root, "gone")
            self.assertEqual(u.category, "stale-ref")
            self.assertEqual(u.context, "inline")

    def test_example_in_non_code_fence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "d.md",
                   "# D\n\n```markdown\ncall `add(a, b)` here\n```\n")
            u = self._only(root, "add")
            self.assertEqual(u.category, "example")
            self.assertEqual(u.context, "fenced")

    def test_python_fence_is_not_an_example(self):
        # A code-language fence may be real usage docs -> stale-ref, not example.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "d.md",
                   "# D\n\n```python\ngone()\n```\n")
            u = self._only(root, "gone")
            self.assertEqual(u.category, "stale-ref")
            self.assertEqual(u.context, "fenced")

    def test_actionable_sorted_before_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "d.md",
                   "# D\n\n```markdown\n`add(a, b)` example\n```\n\n"
                   "Use `gone()` really.\n")
            cats = [u.category for u in init(root).unmatched]
            self.assertIn("stale-ref", cats)
            self.assertIn("example", cats)
            self.assertLess(cats.index("stale-ref"), cats.index("example"))

    def test_category_and_context_serialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "d.md", "# D\n\nUse `gone()`.\n")
            from knowform.init import read_proposal, write_proposal
            write_proposal(root, init(root))
            u = next(u for u in read_proposal(root).unmatched
                     if u.identifier == "gone")
            self.assertEqual(u.category, "stale-ref")
            self.assertEqual(u.context, "inline")


if __name__ == "__main__":
    unittest.main()
