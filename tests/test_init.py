import json
import tempfile
import unittest
from pathlib import Path

from knowform.__main__ import main
from knowform.init import INIT_PROPOSAL, init, write_proposal

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


def _candidates(proposal):
    return {(c.kind, c.governs, c.symbol or c.code_anchor)
            for c in proposal.candidates}


class InitDiscoveryTest(unittest.TestCase):
    def _repo(self, tmp: str) -> Path:
        root = Path(tmp)
        _write(root, "lib.py", LIB)
        _write(root, "guide.md", GUIDE)
        return root

    def test_harvests_documented_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            proposal = init(root)
            cands = _candidates(proposal)
            self.assertIn(("docstring", "lib.py", "def add"), cands)
            self.assertIn(("docstring", "lib.py", "class Widget"), cands)
            self.assertIn(("docstring", "lib.py", "def render"), cands)
            # Undocumented symbol is not a candidate.
            self.assertNotIn(("docstring", "lib.py", "def _helper"), cands)

    def test_docstring_candidate_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            proposal = init(root)
            add = next(c for c in proposal.candidates
                       if c.kind == "docstring" and c.symbol == "def add")
            self.assertEqual(add.governs, "lib.py")
            self.assertEqual(add.doc_path, "lib.py")
            self.assertEqual(add.direction, "code-is-truth")
            self.assertEqual(add.source_tier, 1)
            self.assertEqual(add.doc_region, (5, 5))  # the docstring line

    def test_markdown_reference_resolves_to_one_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            proposal = init(root)
            md = [c for c in proposal.candidates if c.kind == "markdown"]
            add = next(c for c in md if c.code_anchor == "def add")
            self.assertEqual(add.governs, "lib.py")
            self.assertEqual(add.doc_path, "guide.md")
            self.assertEqual(add.direction, "code-is-truth")
            self.assertEqual(add.source_tier, 0)
            self.assertEqual(add.doc_region, (3, 3))  # enclosing paragraph
            # `Widget` in a different paragraph binds too.
            self.assertTrue(any(c.code_anchor == "class Widget" for c in md))

    def test_markdown_reference_is_deduped_within_paragraph(self):
        # `add` and add(1, 2) are the same paragraph + symbol -> one candidate.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            proposal = init(root)
            md_add = [c for c in proposal.candidates
                      if c.kind == "markdown" and c.code_anchor == "def add"]
            self.assertEqual(len(md_add), 1)

    def test_ambiguous_reference_goes_to_unmatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "store.py",
                   'def process(data):\n    """Store."""\n    return data\n')
            _write(root, "queue.py",
                   'def process(job):\n    """Queue."""\n    return job\n')
            _write(root, "ops.md", "# Ops\n\nUse process() to handle items.\n")
            proposal = init(root)
            self.assertFalse(any(c.code_anchor == "def process"
                                 for c in proposal.candidates))
            um = [u for u in proposal.unmatched if u.identifier == "process"]
            self.assertEqual(len(um), 1)
            self.assertEqual(um[0].match_count, 2)
            self.assertEqual(um[0].doc_path, "ops.md")

    def test_unresolved_backtick_word_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "prose.md",
                   "# Prose\n\nThis mentions `nonexistent` styling only.\n")
            proposal = init(root)
            self.assertFalse(any(u.identifier == "nonexistent"
                                 for u in proposal.unmatched))

    def test_managed_doc_and_bound_symbol_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            managed = (
                "---\n"
                "knowform:\n"
                "  direction: code-is-truth\n"
                "  bindings:\n"
                "    - doc_anchor: add\n"
                "      governs: lib.py\n"
                "      code_anchor: def add\n"
                "---\n"
                "# Managed\n\nThe `add` function.\n"
            )
            _write(root, "managed.md", managed)
            proposal = init(root)
            # lib.py#add already bound: no docstring candidate for it, and the
            # markdown reference to `add` (in guide.md) is skipped too.
            cands = _candidates(proposal)
            self.assertNotIn(("docstring", "lib.py", "def add"), cands)
            self.assertFalse(any(c.code_anchor == "def add"
                                 for c in proposal.candidates))
            # Other documented symbols still surface.
            self.assertIn(("docstring", "lib.py", "class Widget"), cands)
            # The managed doc itself is not rescanned for references.
            self.assertFalse(any(c.doc_path == "managed.md"
                                 for c in proposal.candidates))

    def test_existing_manifest_binding_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            _write(root, "knowform.bindings.json", json.dumps(
                {"version": 1, "docstrings": [
                    {"governs": "lib.py", "symbol": "def add"}]}))
            proposal = init(root)
            cands = _candidates(proposal)
            self.assertNotIn(("docstring", "lib.py", "def add"), cands)
            self.assertIn(("docstring", "lib.py", "class Widget"), cands)

    def test_respects_knowformignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            _write(root, "vendored.py",
                   'def secret():\n    """Hidden."""\n    return 1\n')
            _write(root, ".knowformignore", "vendored.py\n")
            proposal = init(root)
            self.assertFalse(any(c.governs == "vendored.py"
                                 for c in proposal.candidates))

    def test_stable_sorted_and_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            a = init(root).to_dict()
            b = init(root).to_dict()
            self.assertEqual(a, b)
            ids = [(c["kind"], c["doc_path"], c["doc_region"])
                   for c in a["candidates"]]
            self.assertEqual(ids, sorted(ids))

    def test_init_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            before = {p.name: p.read_text() for p in root.iterdir()}
            init(root)
            after = {p.name: p.read_text() for p in root.iterdir()
                     if p.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((root / INIT_PROPOSAL).exists())

    def test_write_proposal_creates_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            proposal = init(root)
            out = write_proposal(root, proposal)
            self.assertEqual(out, root / INIT_PROPOSAL)
            data = json.loads(out.read_text())
            self.assertIn("candidates", data)
            self.assertIn("unmatched", data)

    def test_cli_init_writes_proposal_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            lib_before = (root / "lib.py").read_text()
            guide_before = (root / "guide.md").read_text()
            rc = main(["init", "--root", str(root)])
            self.assertEqual(rc, 0)
            self.assertTrue((root / INIT_PROPOSAL).exists())
            # Docs/code untouched: init only writes the proposal artifact.
            self.assertEqual((root / "lib.py").read_text(), lib_before)
            self.assertEqual((root / "guide.md").read_text(), guide_before)


if __name__ == "__main__":
    unittest.main()
