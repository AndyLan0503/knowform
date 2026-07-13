import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.judge import JudgeInput, Verdict, VerdictKind
from knowform.plan import plan

FIX = Path(__file__).parent / "fixtures"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


class RecordingJudge:
    """Injected stub judge that records every frontier item it sees."""

    def __init__(self, verdict=VerdictKind.CODE_DRIFT):
        self.calls: list[JudgeInput] = []
        self.verdict = verdict

    def __call__(self, item: JudgeInput) -> Verdict:
        self.calls.append(item)
        return Verdict(self.verdict, rationale="stub")


class PlanFixture:
    """A throwaway git repo seeded from the fixtures, committed once."""

    def __init__(self, tmp: Path):
        self.root = tmp
        for name in ["calc.py", "managed_add.md", "managed_whole.md",
                     "managed_scaled.md", "unmanaged.md", "no_direction.md"]:
            (self.root / name).write_text(
                (FIX / name).read_text(encoding="utf-8"), encoding="utf-8")
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
            e = _entry(result, "managed_add.md#add-behavior")
            self.assertTrue(e.doc_hash.startswith("sha256:"))
            self.assertTrue(e.code_hash.startswith("sha256:"))


class CodeChangeTest(unittest.TestCase):
    def test_change_reaches_frontier_and_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            fx.edit("calc.py", (FIX / "calc.py").read_text()
                    .replace("return a + b", "return a + b + 1"))
            judge = RecordingJudge(VerdictKind.CODE_DRIFT)
            result = plan(fx.root, base="HEAD", judge=judge)
            e = _entry(result, "managed_add.md#add-behavior")
            self.assertTrue(e.on_frontier)
            self.assertEqual(e.verdict, VerdictKind.CODE_DRIFT.value)
            self.assertGreaterEqual(len(judge.calls), 1)
            item = judge.calls[0]
            self.assertIn("return a + b", item.code_text)
            self.assertTrue(any("def add" in s for s in item.signatures))

    def test_no_judge_yields_needs_judge_zero_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            fx.edit("calc.py", (FIX / "calc.py").read_text()
                    .replace("return a + b", "return a + b + 1"))
            result = plan(fx.root, base="HEAD", judge=None)
            e = _entry(result, "managed_add.md#add-behavior")
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
            fx.edit("calc.py", (FIX / "calc.py").read_text()
                    .replace("return a + b", "return a + b + 1"))
            judge = RecordingJudge()
            result = plan(fx.root, base="HEAD", judge=judge, depth=2)
            e = _entry(result, "managed_scaled.md#scaled-behavior")
            self.assertTrue(e.on_frontier,
                            "changed callee must reach the caller's doc")
            judged_keys = {c.key for c in judge.calls}
            self.assertIn("managed_scaled.md#scaled-behavior", judged_keys)

    def test_changing_caller_does_not_pull_unrelated_callee_doc(self):
        # Change only `scaled_add`'s body. `add` does not depend on scaled_add,
        # so add's symbol-scoped doc must NOT be pulled in (precision).
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            fx.edit("calc.py", (FIX / "calc.py").read_text()
                    .replace("return add(a, b) * factor",
                             "return add(a, b) * factor * 2"))
            judge = RecordingJudge()
            result = plan(fx.root, base="HEAD", judge=judge, depth=2)
            e = _entry(result, "managed_add.md#add-behavior")
            self.assertFalse(
                e.on_frontier,
                "changing a caller must not pull the unrelated callee's doc")
            judged_keys = {c.key for c in judge.calls}
            self.assertNotIn("managed_add.md#add-behavior", judged_keys)


class GlobCollisionTest(unittest.TestCase):
    def test_glob_over_two_files_yields_two_distinct_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            fx.root.joinpath("mod_a.py").write_text(
                "def a():\n    return 1\n", encoding="utf-8")
            fx.root.joinpath("mod_b.py").write_text(
                "def b():\n    return 2\n", encoding="utf-8")
            fx.root.joinpath("managed_glob.md").write_text(
                "---\nknowform:\n  direction: code-is-truth\n"
                "  bindings:\n    - doc_anchor: mods\n"
                "      governs: mod_*.py\n---\n\n"
                "<!-- knowform:mods:start -->\n"
                "Covers every mod_*.py.\n"
                "<!-- knowform:mods:end -->\n", encoding="utf-8")
            git(fx.root, "add", "-A")
            git(fx.root, "commit", "-q", "-m", "glob")
            result = plan(fx.root, base="HEAD")
            glob_entries = [e for e in result.entries
                            if e.key.startswith("managed_glob.md#mods")]
            self.assertEqual(len(glob_entries), 2, glob_entries)
            # Distinct identities and distinct governed files, no collision.
            self.assertEqual(len({e.entry_id for e in glob_entries}), 2)
            self.assertEqual({e.governs for e in glob_entries},
                             {"mod_a.py", "mod_b.py"})
            # Both share the same doc anchor key.
            self.assertEqual({e.key for e in glob_entries},
                             {"managed_glob.md#mods"})


class UntrackedFileTest(unittest.TestCase):
    def test_new_file_and_binding_not_silently_in_sync(self):
        # A brand-new (untracked) code file plus its doc binding must reach the
        # frontier on the first commit, not read as in-sync.
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            fx.root.joinpath("brand_new.py").write_text(
                "def brand_new():\n    return 42\n", encoding="utf-8")
            fx.root.joinpath("managed_new.md").write_text(
                "---\nknowform:\n  direction: code-is-truth\n"
                "  bindings:\n    - doc_anchor: new\n"
                "      governs: brand_new.py\n---\n\n"
                "<!-- knowform:new:start -->\n"
                "`brand_new()` returns 42.\n"
                "<!-- knowform:new:end -->\n", encoding="utf-8")
            judge = RecordingJudge()
            result = plan(fx.root, base="HEAD", judge=judge)
            e = _entry(result, "managed_new.md#new")
            self.assertTrue(e.on_frontier,
                            "new governed file must not read as in-sync")


class DecoratorGateTest(unittest.TestCase):
    def test_decorator_arg_change_is_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            for name in ["decorated.py", "managed_decorated.md"]:
                fx.root.joinpath(name).write_text(
                    (FIX / name).read_text(), encoding="utf-8")
            git(fx.root, "add", "-A")
            git(fx.root, "commit", "-q", "-m", "decorated")
            fx.edit("decorated.py", (FIX / "decorated.py").read_text()
                    .replace("maxsize=1", "maxsize=99"))
            judge = RecordingJudge()
            result = plan(fx.root, base="HEAD", judge=judge)
            e = _entry(result, "managed_decorated.md#cached-behavior")
            self.assertTrue(e.on_frontier,
                            "decorator-arg change must gate the symbol")
            self.assertIn("managed_decorated.md#cached-behavior",
                          {c.key for c in judge.calls})


class GovernsContainmentTest(unittest.TestCase):
    def test_escaping_governs_degrades_to_error_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            fx.root.joinpath("managed_escape.md").write_text(
                (FIX / "managed_escape.md").read_text(), encoding="utf-8")
            git(fx.root, "add", "-A")
            git(fx.root, "commit", "-q", "-m", "escape")
            result = plan(fx.root, base="HEAD", judge=RecordingJudge())
            escape = [e for e in result.entries
                      if "managed_escape.md" in e.key]
            self.assertTrue(escape)
            self.assertTrue(all(e.verdict == "error" for e in escape))


class DirectionExplicitTest(unittest.TestCase):
    def test_missing_direction_surfaced_as_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = PlanFixture(Path(tmp))
            errors = [e for e in plan(fx.root).entries
                      if e.verdict == "error"]
            self.assertTrue(
                any("no_direction.md" in e.key for e in errors))
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
                (sub / "vendored.md").write_text(
                    "---\nknowform:\n  direction: code-is-truth\n"
                    "  bindings:\n    - doc_anchor: v\n"
                    "      governs: vendored.py\n---\nv\n")
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
            (root / "calc.py").write_text(
                (FIX / "calc.py").read_text(), encoding="utf-8")
            (root / "managed_add.md").write_text(
                (FIX / "managed_add.md").read_text(), encoding="utf-8")
            judge = RecordingJudge()
            result = plan(root, base="HEAD", judge=judge)
            self.assertFalse(result.diff_available)
            # No diff signal -> cannot prove unchanged; frontier is judged.
            e = _entry(result, "managed_add.md#add-behavior")
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
