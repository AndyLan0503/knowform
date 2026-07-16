import tempfile
import unittest
from pathlib import Path

from knowform.init import init
from knowform.judge import MatchInput, MatchResult


def _write(root: Path, name: str, text: str) -> None:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


CONFIG = 'def parse_config(path):\n    """Parse config."""\n    return path\n'


class StubMatcher:
    def __init__(self, result):
        self.result = result
        self.calls: list[MatchInput] = []

    def __call__(self, item: MatchInput) -> MatchResult:
        self.calls.append(item)
        return self.result(item) if callable(self.result) else self.result


class FuzzyResolveTest(unittest.TestCase):
    def test_fuzzy_resolves_stale_ref_via_shortlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "config.py", CONFIG)
            _write(root, "d.md", "# D\n\nUse `parse_cfg()` to load settings.\n")

            base = init(root)  # deterministic: parse_cfg -> 0 exact -> stale-ref
            self.assertTrue(any(u.identifier == "parse_cfg"
                                and u.category == "stale-ref"
                                for u in base.unmatched))

            stub = StubMatcher(MatchResult(
                matched=True, code_anchor="def parse_config",
                governs="config.py"))
            proposal = init(root, matcher=stub)

            self.assertEqual(len(stub.calls), 1)
            # the near-named real symbol was presented as a candidate
            self.assertIn(("def parse_config", "config.py"),
                          set(stub.calls[0].candidates))
            got = [c for c in proposal.candidates
                   if c.code_anchor == "def parse_config"]
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].source_tier, 2)
            self.assertFalse(any(u.identifier == "parse_cfg"
                                 for u in proposal.unmatched))

    def test_examples_are_never_sent_to_matcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "config.py", CONFIG)
            _write(root, "d.md",
                   "# D\n\n```markdown\ncall `add(a, b)` here\n```\n")
            stub = StubMatcher(MatchResult(
                matched=True, code_anchor="def parse_config",
                governs="config.py"))
            proposal = init(root, matcher=stub)
            self.assertEqual(stub.calls, [])
            self.assertTrue(any(u.category == "example"
                                for u in proposal.unmatched))

    def test_no_plausible_symbol_stays_unmatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "config.py", CONFIG)
            _write(root, "d.md", "# D\n\nUse `zzz()` somehow.\n")
            stub = StubMatcher(MatchResult(
                matched=True, code_anchor="def parse_config",
                governs="config.py"))
            proposal = init(root, matcher=stub)
            self.assertEqual(stub.calls, [])  # nothing close -> no LLM call
            self.assertTrue(any(u.identifier == "zzz"
                                for u in proposal.unmatched))

    # Hallucination rejection (a matcher response outside the presented set)
    # is covered by test_init_llm.test_hallucinated_symbol_is_rejected - the
    # same `_accept_match` guard, so it is not re-tested per option-source here.


if __name__ == "__main__":
    unittest.main()
