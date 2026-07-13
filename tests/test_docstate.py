import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.docstate import DocState, Record, classify, load, write
from knowform.judge import VerdictKind
from knowform.sync import sync

FIX = Path(__file__).parent / "fixtures"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


class Repo:
    """A throwaway git repo seeded from the calc fixtures."""

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

    def edit(self, name: str, replace: str, with_: str) -> None:
        p = self.root / name
        p.write_text(p.read_text().replace(replace, with_), encoding="utf-8")

    def head(self) -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root,
            capture_output=True, text=True).stdout.strip()


IS = VerdictKind.IN_SYNC.value
CD = VerdictKind.CODE_DRIFT.value
DD = VerdictKind.DOC_DRIFT.value
CF = VerdictKind.CONFLICT.value


class ClassifyTest(unittest.TestCase):
    def test_three_way_truth_table(self):
        self.assertEqual(classify(False, False), VerdictKind.IN_SYNC)
        self.assertEqual(classify(False, True), VerdictKind.CODE_DRIFT)
        self.assertEqual(classify(True, False), VerdictKind.DOC_DRIFT)
        self.assertEqual(classify(True, True), VerdictKind.CONFLICT)


class ReadWriteTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = DocState(records={
                "a.md#x::a.py": Record(
                    direction="code-is-truth", governs="a.py",
                    doc_hash="sha256:aa", code_hash="sha256:bb",
                    last_verdict=IS, blessed_at="deadbeef"),
            })
            write(root, state)
            self.assertTrue((root / "knowform.lock").exists())
            back = load(root)
            self.assertEqual(back, state)

    def test_load_absent_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load(Path(tmp)))

    def test_lockfile_is_human_diffable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, DocState(records={
                "a.md#x::a.py": Record("code-is-truth", "a.py",
                                       "sha256:aa", "sha256:bb", IS, "sha"),
            }))
            raw = (root / "knowform.lock").read_text()
            data = json.loads(raw)
            self.assertEqual(data["version"], 1)
            self.assertIn("a.md#x::a.py", data["bindings"])
            self.assertTrue(raw.endswith("\n"))
            self.assertIn("\n  ", raw)  # indented, not a single line


class SyncTest(unittest.TestCase):
    def test_sync_blesses_current_hashes_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            result = sync(repo.root)
            state = load(repo.root)
            self.assertIsNotNone(state)
            rec = state.records["managed_add.md#add-behavior::calc.py::def add"]
            self.assertEqual(rec.last_verdict, IS)
            self.assertTrue(rec.doc_hash.startswith("sha256:"))
            self.assertTrue(rec.code_hash.startswith("sha256:"))
            self.assertEqual(rec.blessed_at, repo.head())
            self.assertEqual(rec.direction, "code-is-truth")
            self.assertGreater(result.blessed, 0)

    def test_sync_glob_yields_distinct_entry_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            (repo.root / "mod_a.py").write_text("def a():\n    return 1\n")
            (repo.root / "mod_b.py").write_text("def b():\n    return 2\n")
            (repo.root / "managed_glob.md").write_text(
                "---\nknowform:\n  direction: code-is-truth\n"
                "  bindings:\n    - doc_anchor: mods\n"
                "      governs: mod_*.py\n---\n\n"
                "<!-- knowform:mods:start -->\ncovers mods\n"
                "<!-- knowform:mods:end -->\n")
            git(repo.root, "add", "-A")
            git(repo.root, "commit", "-q", "-m", "glob")
            sync(repo.root)
            state = load(repo.root)
            glob_keys = [k for k in state.records
                         if k.startswith("managed_glob.md#mods")]
            self.assertEqual(len(glob_keys), 2, glob_keys)
            self.assertEqual(
                {state.records[k].governs for k in glob_keys},
                {"mod_a.py", "mod_b.py"})

    def test_sync_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            sync(repo.root)
            first = (repo.root / "knowform.lock").read_text()
            sync(repo.root)
            self.assertEqual(first, (repo.root / "knowform.lock").read_text())


if __name__ == "__main__":
    unittest.main()
