import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.judge import JudgeInput, Verdict, VerdictKind
from knowform.manifest import MANIFEST
from knowform.plan import plan

# Governed code: `scaled_add` calls `add`, giving a CALLS edge for the
# blast-radius tests. `Accumulator.push` also calls add.
CALC = (
    '"""Tiny module governed by fixture docs."""\n'
    "\n\n"
    "def add(a, b):\n"
    "    return a + b\n"
    "\n\n"
    "def scaled_add(a, b, factor):\n"
    "    # Calls add: creates a CALLS edge for blast-radius tests.\n"
    "    return add(a, b) * factor\n"
    "\n\n"
    "class Accumulator:\n"
    "    def __init__(self):\n"
    "        self.total = 0\n"
    "\n"
    "    def push(self, value):\n"
    "        self.total = add(self.total, value)\n"
    "        return self.total\n"
)

DECORATED = (
    '"""Module with a decorated symbol for region-span tests."""\n'
    "from functools import lru_cache\n"
    "\n\n"
    "@lru_cache(maxsize=1)\n"
    "def cached(n):\n"
    "    return n * 2\n"
)

# Plain-markdown docs: a heading over the governed prose, no fences.
ADD_DOC = (
    "# Calc\n\n"
    "## Add behavior\n\n"
    "`add(a, b)` returns the sum of its two arguments.\n"
)
WHOLE_DOC = (
    "# Overview\n\n"
    "Whole-file binding: no code anchor, so the code region degrades to the\n"
    "whole file.\n"
)
SCALED_DOC = (
    "# Scaled\n\n"
    "## Scaled behavior\n\n"
    "`scaled_add(a, b, factor)` returns `add(a, b)` multiplied by `factor`.\n"
)

# Stable plan keys under the out-of-band heading model.
ADD_KEY = "add.md#heading:Add behavior"
WHOLE_KEY = "whole.md#heading:Overview"
SCALED_KEY = "scaled.md#heading:Scaled behavior"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


def _manifest(root: Path, bindings: list[dict]) -> None:
    (root / MANIFEST).write_text(
        json.dumps({"version": 1, "markdown": bindings}), encoding="utf-8")


class RecordingJudge:
    """Injected stub judge that records every frontier item it sees."""

    def __init__(self, verdict=VerdictKind.CODE_DRIFT):
        self.calls: list[JudgeInput] = []
        self.verdict = verdict

    def __call__(self, item: JudgeInput) -> Verdict:
        self.calls.append(item)
        return Verdict(self.verdict, rationale="stub")


class PlanFixture:
    """A throwaway git repo seeded with plain docs + an out-of-band manifest,
    committed once."""

    def __init__(self, tmp: Path):
        self.root = tmp
        _write(self.root, "calc.py", CALC)
        _write(self.root, "add.md", ADD_DOC)
        _write(self.root, "whole.md", WHOLE_DOC)
        _write(self.root, "scaled.md", SCALED_DOC)
        _manifest(self.root, [
            {"doc": "add.md", "heading": ["Add behavior"],
             "governs": "calc.py", "code_anchor": "def add",
             "direction": "code-is-truth"},
            {"doc": "whole.md", "heading": ["Overview"],
             "governs": "calc.py", "direction": "doc-is-truth"},
            {"doc": "scaled.md", "heading": ["Scaled behavior"],
             "governs": "calc.py", "code_anchor": "def scaled_add",
             "direction": "code-is-truth"},
        ])
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "t@t.t")
        git(self.root, "config", "user.name", "t")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "seed")

    def edit(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")


class UnchangedCorpusTest(unittest.TestCase):
    def test_zero_tokens_and_all_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            judge = RecordingJudge()
            result = plan(fx.root, base="HEAD", judge=judge)
            self.assertTrue(result.diff_available)
            self.assertEqual(judge.calls, [],
                             "unchanged corpus must not invoke the judge")
            managed = [e for e in result.entries if e.verdict != "error"]
            self.assertTrue(managed)
            for e in managed:
                self.assertEqual(e.verdict, VerdictKind.IN_SYNC.value, e.key)
                self.assertFalse(e.on_frontier, e.key)

    def test_hashes_emitted_even_when_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            result = plan(fx.root, base="HEAD")
            e = _entry(result, ADD_KEY)
            self.assertTrue(e.doc_hash.startswith("sha256:"))
            self.assertTrue(e.code_hash.startswith("sha256:"))


class CodeChangeTest(unittest.TestCase):
    def test_change_reaches_frontier_and_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            fx.edit("calc.py", CALC.replace("return a + b", "return a + b + 1"))
            judge = RecordingJudge(VerdictKind.CODE_DRIFT)
            result = plan(fx.root, base="HEAD", judge=judge)
            e = _entry(result, ADD_KEY)
            self.assertTrue(e.on_frontier)
            self.assertEqual(e.verdict, VerdictKind.CODE_DRIFT.value)
            self.assertGreaterEqual(len(judge.calls), 1)
            item = next(c for c in judge.calls if c.key == ADD_KEY)
            self.assertIn("return a + b", item.code_text)
            self.assertTrue(any("def add" in s for s in item.signatures))

    def test_no_judge_yields_needs_judge_zero_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            fx.edit("calc.py", CALC.replace("return a + b", "return a + b + 1"))
            result = plan(fx.root, base="HEAD", judge=None)
            e = _entry(result, ADD_KEY)
            self.assertTrue(e.on_frontier)
            self.assertEqual(e.verdict, VerdictKind.NEEDS_JUDGE.value)
            self.assertFalse(result.judged)


class BlastRadiusTest(unittest.TestCase):
    def test_changing_callee_reaches_callers_doc(self):
        # Bind a doc to `scaled_add`; change only `add`'s body. Because
        # scaled_add depends on add, add's change is at risk to scaled_add's
        # doc (walk dependents, then GOVERNS). The correct direction.
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            fx.edit("calc.py", CALC.replace("return a + b", "return a + b + 1"))
            judge = RecordingJudge()
            result = plan(fx.root, base="HEAD", judge=judge, depth=2)
            e = _entry(result, SCALED_KEY)
            self.assertTrue(e.on_frontier,
                            "changed callee must reach the caller's doc")
            judged_keys = {c.key for c in judge.calls}
            self.assertIn(SCALED_KEY, judged_keys)

    def test_changing_caller_does_not_pull_unrelated_callee_doc(self):
        # Change only `scaled_add`'s body. `add` does not depend on scaled_add,
        # so add's symbol-scoped doc must NOT be pulled in (precision).
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            fx.edit("calc.py", CALC.replace(
                "return add(a, b) * factor",
                "return add(a, b) * factor * 2"))
            judge = RecordingJudge()
            result = plan(fx.root, base="HEAD", judge=judge, depth=2)
            e = _entry(result, ADD_KEY)
            self.assertFalse(
                e.on_frontier,
                "changing a caller must not pull the unrelated callee's doc")
            judged_keys = {c.key for c in judge.calls}
            self.assertNotIn(ADD_KEY, judged_keys)


class GlobCollisionTest(unittest.TestCase):
    def test_glob_over_two_files_yields_two_distinct_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            _write(fx.root, "mod_a.py", "def a():\n    return 1\n")
            _write(fx.root, "mod_b.py", "def b():\n    return 2\n")
            _write(fx.root, "mods.md",
                   "# Modules\n\n## Mods\n\nCovers every mod_*.py.\n")
            _manifest(fx.root, [
                {"doc": "mods.md", "heading": ["Mods"],
                 "governs": "mod_*.py", "direction": "code-is-truth"},
            ])
            git(fx.root, "add", "-A")
            git(fx.root, "commit", "-q", "-m", "glob")
            result = plan(fx.root, base="HEAD")
            key = "mods.md#heading:Mods"
            glob_entries = [e for e in result.entries if e.key == key]
            self.assertEqual(len(glob_entries), 2, glob_entries)
            # Distinct identities and distinct governed files, no collision.
            self.assertEqual(len({e.entry_id for e in glob_entries}), 2)
            self.assertEqual({e.governs for e in glob_entries},
                             {"mod_a.py", "mod_b.py"})
            # Both share the same doc anchor key.
            self.assertEqual({e.key for e in glob_entries}, {key})


class UntrackedFileTest(unittest.TestCase):
    def test_new_file_and_binding_not_silently_in_sync(self):
        # A brand-new (untracked) code file plus its doc binding must reach the
        # frontier on the first commit, not read as in-sync.
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            _write(fx.root, "brand_new.py",
                   "def brand_new():\n    return 42\n")
            _write(fx.root, "new.md",
                   "# Brand New\n\n## New\n\n`brand_new()` returns 42.\n")
            _manifest(fx.root, [
                {"doc": "new.md", "heading": ["New"],
                 "governs": "brand_new.py", "code_anchor": "def brand_new",
                 "direction": "code-is-truth"},
            ])
            judge = RecordingJudge()
            result = plan(fx.root, base="HEAD", judge=judge)
            e = _entry(result, "new.md#heading:New")
            self.assertTrue(e.on_frontier,
                            "new governed file must not read as in-sync")


class DecoratorGateTest(unittest.TestCase):
    def test_decorator_arg_change_is_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            _write(fx.root, "decorated.py", DECORATED)
            _write(fx.root, "decorated.md",
                   "# Cached\n\n## Cached behavior\n\n"
                   "`cached(n)` memoizes a single result via "
                   "`@lru_cache(maxsize=1)`.\n")
            _manifest(fx.root, [
                {"doc": "decorated.md", "heading": ["Cached behavior"],
                 "governs": "decorated.py", "code_anchor": "def cached",
                 "direction": "code-is-truth"},
            ])
            git(fx.root, "add", "-A")
            git(fx.root, "commit", "-q", "-m", "decorated")
            fx.edit("decorated.py",
                    DECORATED.replace("maxsize=1", "maxsize=99"))
            judge = RecordingJudge()
            result = plan(fx.root, base="HEAD", judge=judge)
            key = "decorated.md#heading:Cached behavior"
            e = _entry(result, key)
            self.assertTrue(e.on_frontier,
                            "decorator-arg change must gate the symbol")
            self.assertIn(key, {c.key for c in judge.calls})


class GovernsContainmentTest(unittest.TestCase):
    def test_escaping_governs_degrades_to_error_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            _write(fx.root, "escape.md",
                   "# Escaping\n\n## Escape\n\nNames a path outside the repo.\n")
            _manifest(fx.root, [
                {"doc": "escape.md", "heading": ["Escape"],
                 "governs": "../../../etc/passwd",
                 "direction": "code-is-truth"},
            ])
            git(fx.root, "add", "-A")
            git(fx.root, "commit", "-q", "-m", "escape")
            result = plan(fx.root, base="HEAD", judge=RecordingJudge())
            escape = [e for e in result.entries if "escape.md" in e.key]
            self.assertTrue(escape)
            self.assertTrue(all(e.verdict == "error" for e in escape))


class DirectionExplicitTest(unittest.TestCase):
    def test_invalid_direction_surfaced_as_error(self):
        # Direction is never inferred: an unknown direction is a hard manifest
        # error surfaced by the plan, with no direction on the entry.
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            _manifest(fx.root, [
                {"doc": "add.md", "heading": ["Add behavior"],
                 "governs": "calc.py", "code_anchor": "def add",
                 "direction": "sideways"},
            ])
            errors = [e for e in plan(fx.root).entries
                      if e.verdict == "error"]
            self.assertTrue(errors)
            for e in errors:
                self.assertIsNone(e.direction)


class PruneVendoredTest(unittest.TestCase):
    def test_hidden_and_vendored_dirs_are_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            for d in [".venv", "node_modules"]:
                sub = fx.root / d
                sub.mkdir()
                (sub / "vendored.py").write_text("x = 1\n")
                (sub / "vendored.md").write_text("# V\n\n## V\n\nv\n")
            result = plan(fx.root, base="HEAD", judge=RecordingJudge())
            self.assertFalse(any("node_modules" in e.key or ".venv" in e.key
                                 for e in result.entries),
                             [e.key for e in result.entries])


class ReadOnlyTest(unittest.TestCase):
    def test_plan_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            before = _snapshot(fx.root)
            plan(fx.root, base="HEAD", judge=RecordingJudge())
            self.assertEqual(before, _snapshot(fx.root))
            self.assertFalse((fx.root / "knowform.lock").exists())


class NoGitTest(unittest.TestCase):
    def test_non_repo_degrades_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "calc.py", CALC)
            _write(root, "add.md", ADD_DOC)
            _manifest(root, [
                {"doc": "add.md", "heading": ["Add behavior"],
                 "governs": "calc.py", "code_anchor": "def add",
                 "direction": "code-is-truth"},
            ])
            judge = RecordingJudge()
            result = plan(root, base="HEAD", judge=judge)
            self.assertFalse(result.diff_available)
            # No diff signal -> cannot prove unchanged; frontier is judged.
            e = _entry(result, ADD_KEY)
            self.assertTrue(e.on_frontier)


def _entry(result, key):
    for e in result.entries:
        if e.key == key:
            return e
    raise AssertionError(f"no entry {key}; got {[e.key for e in result.entries]}")


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.stat().st_mtime_ns
            for p in root.rglob("*") if p.is_file()}


if __name__ == "__main__":
    unittest.main()
