import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.manifest import MANIFEST
from knowform.plan import plan
from knowform.sync import sync

LIB = 'def add(a, b):\n    """Sum."""\n    return a + b\n'
GUIDE = "# Guide\n\n## Behavior\n\nThe add function returns a sum.\n"


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


def _commit(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")


def _manifest(root: Path, **binding) -> None:
    (root / MANIFEST).write_text(
        json.dumps({"version": 1, "markdown": [binding]}), encoding="utf-8")


class HeadingBindingPlanTest(unittest.TestCase):
    def _repo(self, tmp: str, heading=("Behavior",)) -> Path:
        root = Path(tmp)
        _write(root, "lib.py", LIB)
        _write(root, "guide.md", GUIDE)
        _manifest(root, doc="guide.md", heading=list(heading),
                  governs="lib.py", code_anchor="def add",
                  direction="code-is-truth")
        return root

    def test_out_of_band_binding_syncs_and_leaves_doc_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            _commit(root)
            before = (root / "guide.md").read_text()

            s = sync(root)
            self.assertEqual(s.errors, 0)
            self.assertGreaterEqual(s.blessed, 1)

            p = plan(root, base="HEAD")
            self.assertFalse([e for e in p.entries if e.verdict == "error"])
            self.assertTrue(all(e.verdict == "in-sync" for e in p.entries))
            # knowform never wrote into the doc - the point of out-of-band.
            self.assertEqual((root / "guide.md").read_text(), before)

    def test_editing_the_governed_section_shows_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            _commit(root)
            sync(root)
            edited = (root / "guide.md").read_text().replace(
                "returns a sum", "does something entirely different")
            (root / "guide.md").write_text(edited, encoding="utf-8")

            p = plan(root, base="HEAD")
            entry = next(e for e in p.entries if "heading:Behavior" in e.key)
            self.assertNotEqual(entry.verdict, "in-sync")

    def test_editing_outside_the_section_does_not_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            _commit(root)
            sync(root)
            # add prose under a different heading; the anchor re-resolves.
            text = (root / "guide.md").read_text() + "\n## Notes\n\nExtra.\n"
            (root / "guide.md").write_text(text, encoding="utf-8")

            p = plan(root, base="HEAD")
            entry = next(e for e in p.entries if "heading:Behavior" in e.key)
            self.assertEqual(entry.verdict, "in-sync")

    def test_missing_heading_is_surfaced_as_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp, heading=("Nonexistent",))
            _commit(root)
            p = plan(root, base="HEAD")
            self.assertTrue(any(e.verdict == "error" for e in p.entries))


if __name__ == "__main__":
    unittest.main()
