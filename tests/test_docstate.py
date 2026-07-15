import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.docstate import DocState, Record, classify, load, write
from knowform.judge import VerdictKind
from knowform.manifest import MANIFEST
from knowform.sync import sync

CALC = (
    '"""Tiny module governed by out-of-band bindings."""\n\n\n'
    "def add(a, b):\n    return a + b\n\n\n"
    "def scaled_add(a, b, factor):\n    return add(a, b) * factor\n"
)

# Plain markdown: bindings live only in the manifest, never in the doc.
DOC_ADD = "# Calc\n\n## Add\n\n`add(a, b)` returns the sum of its two arguments.\n"
DOC_WHOLE = "# Calc\n\n## Overview\n\nWhole-file overview prose.\n"
DOC_SCALED = ("# Calc\n\n## Scaled\n\n"
              "`scaled_add(a, b, factor)` returns `add(a, b)` times `factor`.\n")

# Out-of-band bindings mirroring the old inline managed docs.
BINDINGS = {
    "version": 1,
    "markdown": [
        {"doc": "managed_add.md", "heading": ["Add"], "governs": "calc.py",
         "code_anchor": "def add", "direction": "code-is-truth"},
        {"doc": "managed_whole.md", "heading": ["Overview"], "governs": "calc.py",
         "direction": "doc-is-truth"},
        {"doc": "managed_scaled.md", "heading": ["Scaled"], "governs": "calc.py",
         "code_anchor": "def scaled_add", "direction": "code-is-truth"},
    ],
}

# Entry-id of the `add` binding under the new out-of-band key scheme.
ADD_KEY = "managed_add.md#heading:Add::calc.py::def add"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


class Repo:
    """A throwaway git repo with plain docs bound out-of-band via the manifest."""

    def __init__(self, tmp: Path):
        self.root = tmp
        (self.root / "calc.py").write_text(CALC, encoding="utf-8")
        (self.root / "managed_add.md").write_text(DOC_ADD, encoding="utf-8")
        (self.root / "managed_whole.md").write_text(DOC_WHOLE, encoding="utf-8")
        (self.root / "managed_scaled.md").write_text(DOC_SCALED, encoding="utf-8")
        (self.root / MANIFEST).write_text(
            json.dumps(BINDINGS), encoding="utf-8")
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
            rec = state.records[ADD_KEY]
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
                "# Modules\n\n## Mods\n\ncovers mods\n", encoding="utf-8")
            bindings = json.loads(json.dumps(BINDINGS))
            bindings["markdown"].append(
                {"doc": "managed_glob.md", "heading": ["Mods"],
                 "governs": "mod_*.py", "direction": "code-is-truth"})
            (repo.root / MANIFEST).write_text(
                json.dumps(bindings), encoding="utf-8")
            git(repo.root, "add", "-A")
            git(repo.root, "commit", "-q", "-m", "glob")
            sync(repo.root)
            state = load(repo.root)
            glob_keys = [k for k in state.records
                         if k.startswith("managed_glob.md#heading:Mods")]
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
