import tempfile
import unittest
from pathlib import Path

from knowform.init import init

# Plain docs: `f` resolves to the top-level `mod.py` symbol; `g` resolves to a
# symbol defined only under the fixture tree.
REAL_DOC = "# Guide\n\n## Behavior\n\nThe `f` function returns 1.\n"
BAD_DOC = "# Fixture\n\n## Behavior\n\nThe `g` fixture returns 2.\n"


class IgnoreFileTest(unittest.TestCase):
    def _repo(self, tmp: Path):
        (tmp / "mod.py").write_text("def f():\n    return 1\n")
        (tmp / "real.md").write_text(REAL_DOC)
        fixtures = tmp / "pkg" / "tests" / "fixtures"
        fixtures.mkdir(parents=True)
        (fixtures / "fix.py").write_text("def g():\n    return 2\n")
        (fixtures / "bad.md").write_text(BAD_DOC)
        return tmp

    def _docs(self, root: Path) -> set[str]:
        return {c.doc_path for c in init(root).candidates}

    def test_knowformignore_excludes_matching_docs(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._repo(Path(t))
            (root / ".knowformignore").write_text("pkg/tests\n")
            docs = self._docs(root)
            self.assertIn("real.md", docs)
            self.assertFalse(any("bad.md" in d for d in docs), docs)

    def test_without_ignore_fixtures_are_scanned(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._repo(Path(t))
            docs = self._docs(root)
            self.assertTrue(any("bad.md" in d for d in docs), docs)

    def test_ignore_excludes_ignored_py_from_discovery(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._repo(Path(t))
            (root / ".knowformignore").write_text(
                "# comment\npkg/tests\n")
            governs = {c.governs for c in init(root).candidates}
            self.assertFalse(
                any("pkg/tests" in g for g in governs), governs)


if __name__ == "__main__":
    unittest.main()
