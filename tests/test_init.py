import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from knowform.__main__ import main
from knowform.init import INIT_PROPOSAL, init, write_proposal

REPO_ROOT = Path(__file__).resolve().parents[1]

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
            _write(root, "ops.md", "# Ops\n\nUse `process()` to handle items.\n")
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

    def test_bound_symbol_is_not_reproposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            # `def add` is already bound out-of-band via a manifest markdown
            # binding: init must not re-propose it from any source.
            _write(root, "bound.md", "# Bound\n\n## Add\n\nThe `add` behavior.\n")
            _write(root, "knowform.bindings.json", json.dumps(
                {"version": 1, "markdown": [
                    {"doc": "bound.md", "heading": ["Add"], "governs": "lib.py",
                     "code_anchor": "def add", "direction": "code-is-truth"}]}))
            proposal = init(root)
            # lib.py#add already bound: no docstring candidate for it, and the
            # markdown reference to `add` (in guide.md) is skipped too.
            cands = _candidates(proposal)
            self.assertNotIn(("docstring", "lib.py", "def add"), cands)
            self.assertFalse(any(c.code_anchor == "def add"
                                 for c in proposal.candidates))
            # Other documented symbols still surface.
            self.assertIn(("docstring", "lib.py", "class Widget"), cands)

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


class InitPrecisionTest(unittest.TestCase):
    def test_prose_parenthetical_is_not_a_call(self):
        # "the design (...)" in running prose must not read as design().
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py",
                   'def design(x):\n    """Design."""\n    return x\n')
            _write(root, "notes.md",
                   "# Notes\n\nThe design (no-LLM) step drives discovery "
                   "(fast) here.\n")
            proposal = init(root)
            self.assertFalse(any(c.kind == "markdown"
                                 and c.code_anchor == "def design"
                                 for c in proposal.candidates))
            idents = {u.identifier for u in proposal.unmatched}
            self.assertNotIn("design", idents)
            self.assertNotIn("discovery", idents)

    def test_call_in_backticks_still_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "use.md", "# Use\n\nCall `add(a, b)` to sum.\n")
            proposal = init(root)
            self.assertTrue(any(c.kind == "markdown"
                                and c.code_anchor == "def add"
                                for c in proposal.candidates))

    def test_at_most_one_candidate_per_doc_region(self):
        # A paragraph naming two symbols cannot fence to both.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "dense.md",
                   "# Dense\n\nUse `add` and `render` together here.\n")
            proposal = init(root)
            per_region = Counter((c.doc_path, c.doc_region)
                                 for c in proposal.candidates)
            self.assertTrue(all(v <= 1 for v in per_region.values()))

    def test_fenced_block_does_not_explode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "ex.md",
                   "# Ex\n\n```python\nadd(1, 2)\nWidget().render()\n```\n")
            proposal = init(root)
            per_region = Counter((c.doc_path, c.doc_region)
                                 for c in proposal.candidates)
            self.assertTrue(all(v <= 1 for v in per_region.values()))

    def test_private_symbol_is_not_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py",
                   'def _internal(x):\n    """Internal."""\n    return x\n')
            _write(root, "doc.md", "# Doc\n\nSee `_internal` for details.\n")
            proposal = init(root)
            for c in proposal.candidates:
                anchor = (c.symbol or c.code_anchor or "").split(" ")[-1]
                self.assertFalse(anchor.startswith("_"))


class InitDogfoodTest(unittest.TestCase):
    """Run `init` on knowform's own repo: the fixtures unit tests missed the
    precision bugs this milestone fixes, so assert their invariants here."""

    def setUp(self):
        self.proposal = init(REPO_ROOT)

    def test_no_prose_words_in_unmatched(self):
        idents = {u.identifier for u in self.proposal.unmatched}
        leaked = {"design", "discovery", "deterministically", "manifest",
                  "references", "styling", "truth", "undone", "today"}
        self.assertEqual(idents & leaked, set())

    def test_each_doc_region_maps_to_at_most_one_candidate(self):
        per_region = Counter((c.doc_path, c.doc_region)
                             for c in self.proposal.candidates)
        offenders = {k: v for k, v in per_region.items() if v > 1}
        self.assertEqual(offenders, {})

    def test_no_leading_underscore_candidate(self):
        for c in self.proposal.candidates:
            anchor = (c.symbol or c.code_anchor or "").split(" ")[-1]
            self.assertFalse(anchor.startswith("_"),
                             f"underscore candidate: {c}")


if __name__ == "__main__":
    unittest.main()
