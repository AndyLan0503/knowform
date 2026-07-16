import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.__main__ import main
from knowform.init import INIT_PROPOSAL, init, read_proposal, write_proposal
from knowform.manifest import MANIFEST, load as load_manifest
from knowform.materialize import materialize
from knowform.plan import plan
from knowform.sync import sync

LIB = (
    '"""Module docstring."""\n'
    '\n'
    '\n'
    'def add(a, b):\n'
    '    """Return the sum of a and b."""\n'
    '    return a + b\n'
    '\n'
    '\n'
    'def _helper(x):\n'
    '    return x\n'
    '\n'
    '\n'
    'class Widget:\n'
    '    """A widget."""\n'
    '\n'
    '    def render(self):\n'
    '        """Render the widget."""\n'
    '        return "widget"\n'
)

GUIDE = (
    '# Guide\n'
    '\n'
    'The `add` function computes a sum. Call add(1, 2) to use it.\n'
    '\n'
    'Widgets are rendered via `Widget`.\n'
)


def _write(root: Path, name: str, text: str) -> None:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


def _commit(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")


class MaterializeOutOfBandTest(unittest.TestCase):
    def _repo(self, tmp: str) -> Path:
        root = Path(tmp)
        _write(root, "lib.py", LIB)
        _write(root, "guide.md", GUIDE)
        return root

    def test_docs_are_never_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            before = (root / "guide.md").read_text()
            write_proposal(root, init(root))
            materialize(root, read_proposal(root))
            self.assertEqual((root / "guide.md").read_text(), before)

    def test_bindings_land_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            write_proposal(root, init(root))
            materialize(root, read_proposal(root))

            m = load_manifest(root)
            self.assertIsNone(m.error)
            md = {(x.doc, x.code_anchor) for x in m.markdown}
            self.assertIn(("guide.md", "def add"), md)
            self.assertIn(("guide.md", "class Widget"), md)
            for x in m.markdown:
                self.assertEqual(x.heading, ("Guide",))  # heading-anchored
            syms = {(d.governs, d.symbol) for d in m.docstrings}
            self.assertIn(("lib.py", "def add"), syms)
            self.assertIn(("lib.py", "class Widget"), syms)
            self.assertIn(("lib.py", "def render"), syms)

    def test_roundtrips_through_plan_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            write_proposal(root, init(root))
            materialize(root, read_proposal(root))
            _commit(root)

            synced = sync(root)
            self.assertEqual(synced.errors, 0)
            self.assertEqual(synced.blessed, 5)  # 2 markdown + 3 docstring

            after = plan(root, base="HEAD")
            self.assertFalse([e for e in after.entries if e.verdict == "error"])
            self.assertTrue(all(e.verdict == "in-sync" for e in after.entries))

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            write_proposal(root, init(root))
            materialize(root, read_proposal(root))
            manifest_once = (root / MANIFEST).read_text()
            doc_once = (root / "guide.md").read_text()

            materialize(root, read_proposal(root))
            self.assertEqual((root / MANIFEST).read_text(), manifest_once)
            self.assertEqual((root / "guide.md").read_text(), doc_once)

    def test_only_writes_manifest_and_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            write_proposal(root, init(root))
            materialize(root, read_proposal(root))
            names = {p.name for p in root.iterdir() if p.is_file()}
            self.assertEqual(names,
                             {"lib.py", "guide.md", INIT_PROPOSAL, MANIFEST})

    def test_preamble_reference_is_unanchorable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "pre.md",
                   "Uses `add` before any heading.\n\n# Later\n\nother text\n")
            write_proposal(root, init(root))
            result = materialize(root, read_proposal(root))
            self.assertTrue(any("pre.md" in u for u in result.unanchorable))
            m = load_manifest(root)
            self.assertTrue(all(x.doc != "pre.md" for x in m.markdown))


class MaterializeCliTest(unittest.TestCase):
    def test_write_flag_materializes_and_leaves_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "guide.md", GUIDE)
            before = (root / "guide.md").read_text()
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            self.assertEqual(main(["init", "--write", "--root", str(root)]), 0)
            self.assertTrue((root / MANIFEST).exists())
            self.assertEqual((root / "guide.md").read_text(), before)

    def test_write_without_proposal_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            self.assertEqual(main(["init", "--write", "--root", str(root)]), 1)


if __name__ == "__main__":
    unittest.main()
