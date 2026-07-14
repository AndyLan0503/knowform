import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.__main__ import main
from knowform.frontmatter import parse_frontmatter
from knowform.init import INIT_PROPOSAL, init, read_proposal, write_proposal
from knowform.manifest import MANIFEST, load as load_manifest
from knowform.materialize import materialize
from knowform.plan import plan
from knowform.sync import sync
from knowform.regions import resolve_doc_region

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


class MaterializeRoundtripTest(unittest.TestCase):
    """The load-bearing test: init -> write proposal -> materialize -> the
    materialized repo resolves through sync/plan with every binding in-sync and
    zero errors. Proves the materializer emits a corpus the pipeline consumes."""

    def _repo(self, tmp: str) -> Path:
        root = Path(tmp)
        _write(root, "lib.py", LIB)
        _write(root, "guide.md", GUIDE)
        return root

    def test_materialize_roundtrips_through_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            write_proposal(root, init(root))
            result = materialize(root, read_proposal(root))
            self.assertIn("guide.md", result.docs_written)
            self.assertTrue(result.manifest_entries)
            _commit(root)

            synced = sync(root)
            self.assertEqual(synced.errors, 0)
            # 2 markdown + 3 docstring bindings.
            self.assertEqual(synced.blessed, 5)

            after = plan(root, base="HEAD")
            self.assertFalse([e for e in after.entries if e.verdict == "error"])
            self.assertTrue(all(e.verdict == "in-sync" for e in after.entries))

    def test_markdown_gets_frontmatter_and_targetable_fences(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            write_proposal(root, init(root))
            materialize(root, read_proposal(root))

            text = (root / "guide.md").read_text()
            managed = parse_frontmatter(text)
            self.assertIsNotNone(managed)
            self.assertEqual(managed.direction.value, "code-is-truth")
            anchors = {(b.doc_anchor, b.governs, b.code_anchor)
                       for b in managed.bindings}
            self.assertIn(("add", "lib.py", "def add"), anchors)
            self.assertIn(("Widget", "lib.py", "class Widget"), anchors)

            # Fences target real prose (not whole-doc, not empty).
            for b in managed.bindings:
                region = resolve_doc_region(root, Path("guide.md"), b)
                self.assertFalse(region.whole)
                self.assertTrue(region.text(root).strip())
            # Original prose survives inside the fences.
            self.assertIn("computes a sum", text)
            self.assertIn("rendered via", text)

    def test_docstrings_land_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            write_proposal(root, init(root))
            materialize(root, read_proposal(root))

            manifest = load_manifest(root)
            self.assertIsNone(manifest.error)
            symbols = {(d.governs, d.symbol) for d in manifest.docstrings}
            self.assertIn(("lib.py", "def add"), symbols)
            self.assertIn(("lib.py", "class Widget"), symbols)
            self.assertIn(("lib.py", "def render"), symbols)
            for d in manifest.docstrings:
                self.assertEqual(d.direction.value, "code-is-truth")


class MaterializeSafetyTest(unittest.TestCase):
    def _repo(self, tmp: str) -> Path:
        root = Path(tmp)
        _write(root, "lib.py", LIB)
        _write(root, "guide.md", GUIDE)
        return root

    def test_idempotent_no_double_wrap_or_double_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            write_proposal(root, init(root))
            materialize(root, read_proposal(root))
            once = (root / "guide.md").read_text()
            manifest_once = (root / MANIFEST).read_text()

            # A second materialize of the same proposal is a no-op on managed
            # docs and dedups manifest entries.
            second = materialize(root, read_proposal(root))
            self.assertEqual((root / "guide.md").read_text(), once)
            self.assertEqual((root / MANIFEST).read_text(), manifest_once)
            self.assertIn("guide.md", second.skipped)

    def test_merges_into_existing_non_knowform_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "guide.md",
                   "---\ntitle: Guide\n---\n" + GUIDE)
            write_proposal(root, init(root))
            materialize(root, read_proposal(root))

            text = (root / "guide.md").read_text()
            self.assertIn("title: Guide", text)   # existing key preserved
            managed = parse_frontmatter(text)
            self.assertIsNotNone(managed)
            self.assertTrue(managed.bindings)

    def test_two_paragraphs_same_symbol_get_unique_anchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "dup.md",
                   "# Dup\n\nFirst `add` mention.\n\nSecond `add` mention.\n")
            write_proposal(root, init(root))
            materialize(root, read_proposal(root))

            managed = parse_frontmatter((root / "dup.md").read_text())
            anchors = [b.doc_anchor for b in managed.bindings]
            self.assertEqual(len(anchors), len(set(anchors)))
            for b in managed.bindings:
                region = resolve_doc_region(root, Path("dup.md"), b)
                self.assertFalse(region.whole)

    def test_materialize_only_writes_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            write_proposal(root, init(root))
            materialize(root, read_proposal(root))
            names = {p.name for p in root.iterdir() if p.is_file()}
            self.assertEqual(names,
                             {"lib.py", "guide.md", INIT_PROPOSAL, MANIFEST})


class MaterializeCliTest(unittest.TestCase):
    def _repo(self, tmp: str) -> Path:
        root = Path(tmp)
        _write(root, "lib.py", LIB)
        _write(root, "guide.md", GUIDE)
        return root

    def test_cli_init_write_materializes_existing_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            self.assertEqual(
                main(["init", "--write", "--root", str(root)]), 0)
            self.assertIsNotNone(
                parse_frontmatter((root / "guide.md").read_text()))
            self.assertTrue((root / MANIFEST).exists())

    def test_cli_init_write_without_proposal_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            rc = main(["init", "--write", "--root", str(root)])
            self.assertNotEqual(rc, 0)
            # Nothing materialized.
            self.assertIsNone(
                parse_frontmatter((root / "guide.md").read_text()))
            self.assertFalse((root / MANIFEST).exists())


if __name__ == "__main__":
    unittest.main()
