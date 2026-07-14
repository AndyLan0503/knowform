import tempfile
import unittest
from pathlib import Path

from knowform.__main__ import main
from knowform.init import INIT_PROPOSAL, init
from knowform.judge import MatchInput, MatchResult


def _write(root: Path, name: str, text: str) -> None:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


STORE = 'def process(data):\n    """Store."""\n    return data\n'
QUEUE = 'def process(job):\n    """Queue."""\n    return job\n'
OPS = "# Ops\n\nUse `process()` to persist records to the store.\n"


class StubMatcher:
    """Records calls; returns a scripted MatchResult (or a callable)."""

    def __init__(self, result):
        self.result = result
        self.calls: list[MatchInput] = []

    def __call__(self, item: MatchInput) -> MatchResult:
        self.calls.append(item)
        return self.result(item) if callable(self.result) else self.result


class InitLlmTierTest(unittest.TestCase):
    def _ambiguous_repo(self, tmp: str) -> Path:
        root = Path(tmp)
        _write(root, "store.py", STORE)
        _write(root, "queue.py", QUEUE)
        _write(root, "ops.md", OPS)
        return root

    def test_no_matcher_spends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._ambiguous_repo(tmp)
            proposal = init(root)  # deterministic only
            self.assertFalse(any(c.code_anchor == "def process"
                                 for c in proposal.candidates))
            self.assertTrue(any(u.identifier == "process"
                                for u in proposal.unmatched))

    def test_matcher_disambiguates_into_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._ambiguous_repo(tmp)
            stub = StubMatcher(MatchResult(
                matched=True, code_anchor="def process", governs="store.py",
                confidence=0.9, rationale="prose describes the store"))
            proposal = init(root, matcher=stub)

            # The stub saw the enumerated candidate symbols, not raw files.
            self.assertEqual(len(stub.calls), 1)
            self.assertIn(("def process", "store.py"),
                          set(stub.calls[0].candidates))
            self.assertIn(("def process", "queue.py"),
                          set(stub.calls[0].candidates))

            got = [c for c in proposal.candidates
                   if c.code_anchor == "def process"]
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].governs, "store.py")
            self.assertEqual(got[0].source_tier, 2)
            self.assertEqual(got[0].direction, "code-is-truth")
            self.assertEqual(got[0].doc_path, "ops.md")
            # Resolved -> no longer unmatched.
            self.assertFalse(any(u.identifier == "process"
                                 for u in proposal.unmatched))

    def test_no_match_leaves_it_unmatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._ambiguous_repo(tmp)
            stub = StubMatcher(MatchResult(matched=False))
            proposal = init(root, matcher=stub)
            self.assertFalse(any(c.code_anchor == "def process"
                                 for c in proposal.candidates))
            self.assertTrue(any(u.identifier == "process"
                                for u in proposal.unmatched))

    def test_hallucinated_symbol_is_rejected(self):
        # LLM names a governs/anchor not in the presented set -> never bound.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._ambiguous_repo(tmp)
            stub = StubMatcher(MatchResult(
                matched=True, code_anchor="def process", governs="ghost.py"))
            proposal = init(root, matcher=stub)
            self.assertFalse(any(c.source_tier == 2
                                 for c in proposal.candidates))
            self.assertTrue(any(u.identifier == "process"
                                for u in proposal.unmatched))

    def test_doc_is_truth_hint_is_clamped_to_manual(self):
        # Never auto-assign doc-is-truth (that needs a human declaring a spec).
        with tempfile.TemporaryDirectory() as tmp:
            root = self._ambiguous_repo(tmp)
            stub = StubMatcher(MatchResult(
                matched=True, code_anchor="def process", governs="store.py",
                direction="doc-is-truth"))
            proposal = init(root, matcher=stub)
            got = next(c for c in proposal.candidates
                       if c.code_anchor == "def process")
            self.assertEqual(got.direction, "manual")

    def test_result_is_deterministically_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._ambiguous_repo(tmp)
            stub = StubMatcher(MatchResult(
                matched=True, code_anchor="def process", governs="store.py"))
            a = init(root, matcher=StubMatcher(stub.result)).to_dict()
            b = init(root, matcher=StubMatcher(stub.result)).to_dict()
            self.assertEqual(a, b)


class InitLlmMaterializeTest(unittest.TestCase):
    """A Tier-2 candidate must flow through --write like any other: the once
    ambiguous doc becomes managed and resolves in-sync."""

    def test_llm_candidate_materializes_and_resolves(self):
        from knowform.init import read_proposal, write_proposal
        from knowform.materialize import materialize
        from knowform.frontmatter import parse_frontmatter
        from knowform.plan import plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "store.py", STORE)
            _write(root, "queue.py", QUEUE)
            _write(root, "ops.md", OPS)
            stub = StubMatcher(MatchResult(
                matched=True, code_anchor="def process", governs="store.py"))
            write_proposal(root, init(root, matcher=stub))
            materialize(root, read_proposal(root))

            managed = parse_frontmatter((root / "ops.md").read_text())
            self.assertIsNotNone(managed)
            self.assertTrue(any(b.code_anchor == "def process"
                                for b in managed.bindings))
            result = plan(root, base="HEAD")
            self.assertFalse(
                [e for e in result.entries if e.verdict == "error"])


class InitLlmCliTest(unittest.TestCase):
    def test_anthropic_flag_wires_matcher_without_calling_it(self):
        # No ambiguous refs -> the matcher is constructed but never invoked,
        # so no network. Proves the flag path resolves cleanly.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py",
                   'def add(a, b):\n    """Sum."""\n    return a + b\n')
            _write(root, "use.md", "# Use\n\nCall `add` to sum.\n")
            rc = main(["init", "--anthropic", "--root", str(root)])
            self.assertEqual(rc, 0)
            self.assertTrue((root / INIT_PROPOSAL).exists())


if __name__ == "__main__":
    unittest.main()
