import json
import stat
import tempfile
import unittest
from pathlib import Path

from knowform import judge
from knowform.__main__ import main
from knowform.init import INIT_PROPOSAL, init
from knowform.judge import (
    ClaudeCliMatcher,
    ClaudeCliUnavailable,
    MatchInput,
)


def _write(root: Path, name: str, text: str) -> None:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


STORE = 'def process(data):\n    """Store."""\n    return data\n'
QUEUE = 'def process(job):\n    """Queue."""\n    return job\n'
OPS = "# Ops\n\nUse `process()` to persist records to the store.\n"


def _envelope(obj) -> str:
    """Mimic `claude -p --output-format json`: an envelope whose `result`
    holds the assistant's text."""
    inner = obj if isinstance(obj, str) else json.dumps(obj)
    return json.dumps({"type": "result", "subtype": "success",
                       "result": inner})


class FakeRunner:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, cmd, timeout):
        self.calls.append((cmd, timeout))
        return self.stdout


ITEM = MatchInput(
    doc_path="ops.md",
    region_text="Use `process()` to persist records to the store.",
    identifier="process",
    candidates=[("def process", "store.py"), ("def process", "queue.py")],
)


class ClaudeCliMatcherParseTest(unittest.TestCase):
    def test_parses_envelope_into_match_result(self):
        runner = FakeRunner(_envelope({
            "matched": True, "code_anchor": "def process",
            "governs": "store.py", "direction": "code-is-truth",
            "confidence": 0.91, "rationale": "about the store"}))
        m = ClaudeCliMatcher(binary="/fake/claude", runner=runner)
        r = m(ITEM)
        self.assertTrue(r.matched)
        self.assertEqual(r.code_anchor, "def process")
        self.assertEqual(r.governs, "store.py")
        self.assertEqual(r.direction, "code-is-truth")
        self.assertEqual(r.confidence, 0.91)
        self.assertEqual(r.rationale, "about the store")

    def test_command_is_headless_json_with_prompt(self):
        runner = FakeRunner(_envelope({"matched": False}))
        ClaudeCliMatcher(binary="/fake/claude", runner=runner)(ITEM)
        cmd, _timeout = runner.calls[0]
        self.assertEqual(cmd[0], "/fake/claude")
        self.assertIn("-p", cmd)
        self.assertIn("--output-format", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        prompt = cmd[cmd.index("-p") + 1]
        self.assertIn("process", prompt)
        self.assertIn("store.py", prompt)
        self.assertIn("queue.py", prompt)

    def test_model_flag_forwarded_when_set(self):
        runner = FakeRunner(_envelope({"matched": False}))
        ClaudeCliMatcher(binary="/fake/claude", model="claude-x",
                         runner=runner)(ITEM)
        cmd, _ = runner.calls[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-x")

    def test_no_model_flag_by_default(self):
        runner = FakeRunner(_envelope({"matched": False}))
        ClaudeCliMatcher(binary="/fake/claude", runner=runner)(ITEM)
        cmd, _ = runner.calls[0]
        self.assertNotIn("--model", cmd)

    def test_fenced_json_in_result_is_extracted(self):
        fenced = "```json\n" + json.dumps({
            "matched": True, "code_anchor": "def process",
            "governs": "store.py"}) + "\n```"
        runner = FakeRunner(_envelope(fenced))
        r = ClaudeCliMatcher(binary="/fake/claude", runner=runner)(ITEM)
        self.assertTrue(r.matched)
        self.assertEqual(r.governs, "store.py")

    def test_prose_around_json_is_tolerated(self):
        text = ("Sure, here is my answer:\n"
                + json.dumps({"matched": True, "code_anchor": "def process",
                              "governs": "queue.py"})
                + "\nHope that helps.")
        runner = FakeRunner(_envelope(text))
        r = ClaudeCliMatcher(binary="/fake/claude", runner=runner)(ITEM)
        self.assertEqual(r.governs, "queue.py")

    def test_not_matched(self):
        runner = FakeRunner(_envelope({"matched": False}))
        r = ClaudeCliMatcher(binary="/fake/claude", runner=runner)(ITEM)
        self.assertFalse(r.matched)


class ClaudeCliDiscoveryTest(unittest.TestCase):
    def test_uses_which_when_on_path(self):
        self._patch(which="/usr/bin/claude", candidates=[])
        self.assertEqual(judge._find_claude_binary(), "/usr/bin/claude")

    def test_falls_back_to_known_install_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "claude"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            self._patch(which=None, candidates=[exe])
            self.assertEqual(judge._find_claude_binary(), str(exe))

    def test_returns_none_when_absent(self):
        self._patch(which=None, candidates=[Path("/no/such/claude")])
        self.assertIsNone(judge._find_claude_binary())

    def test_constructing_without_binary_raises(self):
        self._patch(which=None, candidates=[])
        with self.assertRaises(ClaudeCliUnavailable):
            ClaudeCliMatcher()

    def _patch(self, which, candidates):
        orig_which = judge.shutil.which
        orig_cands = judge._candidate_binary_paths
        judge.shutil.which = lambda _name: which
        judge._candidate_binary_paths = lambda: list(candidates)
        self.addCleanup(setattr, judge.shutil, "which", orig_which)
        self.addCleanup(setattr, judge, "_candidate_binary_paths", orig_cands)


class ClaudeCliInitIntegrationTest(unittest.TestCase):
    """Plugs into the same Tier-2 seam as any Matcher: an ambiguous ref binds."""

    def test_disambiguates_via_cli_into_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "store.py", STORE)
            _write(root, "queue.py", QUEUE)
            _write(root, "ops.md", OPS)
            runner = FakeRunner(_envelope({
                "matched": True, "code_anchor": "def process",
                "governs": "store.py", "confidence": 0.9}))
            matcher = ClaudeCliMatcher(binary="/fake/claude", runner=runner)
            proposal = init(root, matcher=matcher)

            self.assertEqual(len(runner.calls), 1)
            got = [c for c in proposal.candidates
                   if c.code_anchor == "def process"]
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].governs, "store.py")
            self.assertEqual(got[0].source_tier, 2)
            self.assertFalse(any(u.identifier == "process"
                                 for u in proposal.unmatched))


class ClaudeCliFlagWiringTest(unittest.TestCase):
    def test_llm_flag_wires_cli_matcher_without_calling_it(self):
        # No ambiguous refs -> matcher constructed, never invoked (no subprocess).
        orig = judge._find_claude_binary
        judge._find_claude_binary = lambda: "/fake/claude"
        self.addCleanup(setattr, judge, "_find_claude_binary", orig)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py",
                   'def add(a, b):\n    """Sum."""\n    return a + b\n')
            _write(root, "use.md", "# Use\n\nCall `add` to sum.\n")
            rc = main(["init", "--llm", "--root", str(root)])
            self.assertEqual(rc, 0)
            self.assertTrue((root / INIT_PROPOSAL).exists())

    def test_llm_flag_reports_when_binary_missing(self):
        orig = judge._find_claude_binary
        judge._find_claude_binary = lambda: None
        self.addCleanup(setattr, judge, "_find_claude_binary", orig)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "store.py", STORE)
            _write(root, "queue.py", QUEUE)
            _write(root, "ops.md", OPS)
            rc = main(["init", "--llm", "--root", str(root)])
            self.assertEqual(rc, 1)
            self.assertFalse((root / INIT_PROPOSAL).exists())

    def test_llm_and_anthropic_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            main(["init", "--llm", "--anthropic", "--root", "."])


if __name__ == "__main__":
    unittest.main()
