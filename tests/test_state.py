import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.judge import JudgeInput, Verdict, VerdictKind
from knowform.plan import plan
from knowform.sync import sync

FIX = Path(__file__).parent / "fixtures"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


class RecordingJudge:
    def __init__(self, verdict=VerdictKind.CODE_DRIFT):
        self.calls: list[JudgeInput] = []
        self.verdict = verdict

    def __call__(self, item: JudgeInput) -> Verdict:
        self.calls.append(item)
        return Verdict(self.verdict, rationale="stub")


class Repo:
    def __init__(self, tmp: Path):
        self.root = tmp
        for name in ["calc.py", "managed_add.md", "managed_whole.md",
                     "managed_scaled.md"]:
            (self.root / name).write_text(
                (FIX / name).read_text(encoding="utf-8"), encoding="utf-8")
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "t@t.t")
        git(self.root, "config", "user.name", "t")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "seed")
        sync(self.root)  # bless current state
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "bless")

    def edit(self, name: str, replace: str, with_: str) -> None:
        p = self.root / name
        p.write_text(p.read_text().replace(replace, with_), encoding="utf-8")


def entry(result, key):
    for e in result.entries:
        if e.key == key:
            return e
    raise AssertionError(f"no {key}: {[e.key for e in result.entries]}")


ADD = "managed_add.md#add-behavior"


class ThreeWayStateTest(unittest.TestCase):
    def test_blessed_unchanged_is_in_sync_zero_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            judge = RecordingJudge()
            result = plan(repo.root, base="HEAD", judge=judge)
            self.assertEqual(judge.calls, [])
            self.assertEqual(entry(result, ADD).verdict,
                             VerdictKind.IN_SYNC.value)

    def test_code_edit_yields_code_drift_without_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.edit("calc.py", "return a + b", "return a + b + 1")
            result = plan(repo.root, base="HEAD", judge=None)
            e = entry(result, ADD)
            self.assertTrue(e.on_frontier)
            self.assertEqual(e.verdict, VerdictKind.CODE_DRIFT.value)

    def test_doc_edit_yields_doc_drift_without_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.edit("managed_add.md", "the sum of its two arguments",
                      "something entirely different now")
            result = plan(repo.root, base="HEAD", judge=None)
            e = entry(result, ADD)
            self.assertTrue(e.on_frontier)
            self.assertEqual(e.verdict, VerdictKind.DOC_DRIFT.value)

    def test_both_edited_yields_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.edit("calc.py", "return a + b", "return a + b + 1")
            repo.edit("managed_add.md", "the sum of its two arguments",
                      "a changed description")
            result = plan(repo.root, base="HEAD", judge=None)
            e = entry(result, ADD)
            self.assertEqual(e.verdict, VerdictKind.CONFLICT.value)

    def test_transitive_dependent_still_reaches_frontier_under_recorded(self):
        # scaled_add calls add; its doc is bound to scaled_add. Editing only
        # add's body must still reach scaled_add's doc via the blast-radius,
        # even with a lockfile - the recorded-authoritative path must not drop
        # transitive drift (interrogator BLOCKER 1).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.edit("calc.py", "return a + b", "return a + b + 1")
            judge = RecordingJudge()
            result = plan(repo.root, base="HEAD", judge=judge, depth=2)
            e = entry(result, "managed_scaled.md#scaled-behavior")
            self.assertTrue(e.on_frontier,
                            "transitive drift must survive recorded state")
            self.assertIn("managed_scaled.md#scaled-behavior",
                          {c.key for c in judge.calls})

    def test_transitive_dependent_quiets_after_resync(self):
        # After re-blessing, the transitive risk is gone: no spurious re-flag
        # of the caller's doc while the (now blessed) code edit is uncommitted.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.edit("calc.py", "return a + b", "return a + b + 1")
            sync(repo.root)  # re-bless the edited world
            judge = RecordingJudge()
            result = plan(repo.root, base="HEAD", judge=judge)
            self.assertEqual(judge.calls, [])
            self.assertEqual(entry(result, "managed_scaled.md#scaled-behavior")
                             .verdict, VerdictKind.IN_SYNC.value)

    def test_resync_after_code_edit_returns_to_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.edit("calc.py", "return a + b", "return a + b + 1")
            git(repo.root, "add", "-A")
            git(repo.root, "commit", "-q", "-m", "change")
            sync(repo.root)  # re-bless
            result = plan(repo.root, base="HEAD", judge=None)
            self.assertEqual(entry(result, ADD).verdict,
                             VerdictKind.IN_SYNC.value)


if __name__ == "__main__":
    unittest.main()
