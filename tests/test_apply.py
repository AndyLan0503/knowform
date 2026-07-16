import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.apply import apply
from knowform.judge import JudgeInput, VerdictKind
from knowform.manifest import MANIFEST
from knowform.plan import plan
from knowform.sync import sync
from knowform import __main__ as cli

LIB = 'def add(a, b):\n    return a + b\n'
GUIDE = "# Calc\n\n## Behavior\n\n`add(a, b)` returns the sum of its two arguments.\n"
SPEC = "# Spec\n\n## Contract\n\n`add` MUST return the sum.\n"

# Out-of-band manifest keys mirror plan.py's entry_id construction:
#   {doc}#heading:{heading-path}::{governs}::{code_anchor}
CODE_TRUTH_ID = "guide.md#heading:Behavior::lib.py::def add"
CODE_TRUTH_KEY = "guide.md#heading:Behavior"
DOC_TRUTH_ID = "spec.md#heading:Contract::lib.py::def add"
DOC_TRUTH_KEY = "spec.md#heading:Contract"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


class StubGenerator:
    """Injected prose generator; returns a fixed string, records its inputs."""

    def __init__(self, text="`add(a, b)` returns a+b+1."):
        self.text = text
        self.calls: list[JudgeInput] = []

    def __call__(self, item: JudgeInput) -> str:
        self.calls.append(item)
        return self.text


class Repo:
    """Plain-markdown corpus with out-of-band bindings, git-committed and
    synced so `apply` has recorded state to drift against."""

    def __init__(self, tmp: Path, doc_is_truth: bool = False):
        self.root = tmp
        _write(self.root, "lib.py", LIB)
        _write(self.root, "guide.md", GUIDE)
        markdown = [{
            "doc": "guide.md", "heading": ["Behavior"], "governs": "lib.py",
            "code_anchor": "def add", "direction": "code-is-truth",
        }]
        if doc_is_truth:
            _write(self.root, "spec.md", SPEC)
            markdown.append({
                "doc": "spec.md", "heading": ["Contract"], "governs": "lib.py",
                "code_anchor": "def add", "direction": "doc-is-truth",
            })
        (self.root / MANIFEST).write_text(
            json.dumps({"version": 1, "markdown": markdown}), encoding="utf-8")
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "t@t.t")
        git(self.root, "config", "user.name", "t")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "seed")
        sync(self.root)

    def edit(self, name, replace, with_):
        p = self.root / name
        p.write_text(p.read_text().replace(replace, with_), encoding="utf-8")

    def read(self, name):
        return (self.root / name).read_text()


class ApplySafeTest(unittest.TestCase):
    def test_code_drift_regenerates_doc_leaves_code_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            code_before = repo.read("lib.py")
            repo.edit("lib.py", "return a + b", "return a + b + 1")
            code_after_edit = repo.read("lib.py")
            gen = StubGenerator()
            result = apply(repo.root, generator=gen)
            self.assertIn(CODE_TRUTH_ID, result.applied)
            self.assertTrue(result.ok)
            # Doc region rewritten to the generator output.
            self.assertIn("returns a+b+1", repo.read("guide.md"))
            # Code is the user's edit, byte-for-byte - apply never wrote it.
            self.assertEqual(repo.read("lib.py"), code_after_edit)
            self.assertNotEqual(repo.read("lib.py"), code_before)
            self.assertEqual(len(gen.calls), 1)

    def test_after_apply_binding_reads_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.edit("lib.py", "return a + b", "return a + b + 1")
            apply(repo.root, generator=StubGenerator())
            result = plan(repo.root, base="HEAD", judge=None)
            e = _entry(result, CODE_TRUTH_KEY)
            self.assertEqual(e.verdict, VerdictKind.IN_SYNC.value)

    def test_no_generator_surfaces_not_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            doc_before = repo.read("guide.md")
            repo.edit("lib.py", "return a + b", "return a + b + 1")
            result = apply(repo.root, generator=None)
            self.assertFalse(result.ok)
            self.assertFalse(result.applied)
            self.assertTrue(result.surfaced)
            self.assertEqual(repo.read("guide.md"), doc_before)


class ApplyUnsafeDirectionTest(unittest.TestCase):
    def test_doc_is_truth_drift_refused_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp), doc_is_truth=True)
            code_before = repo.read("lib.py")
            doc_before = repo.read("spec.md")
            repo.edit("lib.py", "return a + b", "return a + b + 1")
            after_edit = repo.read("lib.py")
            gen = StubGenerator()
            result = apply(repo.root, generator=gen)
            self.assertFalse(result.ok)
            refused = {r.entry_id for r in result.refused}
            self.assertIn(DOC_TRUTH_ID, refused)
            # Neither code nor the prescriptive doc was written.
            self.assertEqual(repo.read("lib.py"), after_edit)
            self.assertEqual(repo.read("spec.md"), doc_before)
            self.assertNotEqual(repo.read("lib.py"), code_before)
            # A doc-is-truth binding must never reach the generator.
            self.assertFalse(
                any(c.key == DOC_TRUTH_KEY for c in gen.calls))

    def test_conflict_refused_no_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.edit("lib.py", "return a + b", "return a + b + 1")
            repo.edit("guide.md", "the sum of its two arguments",
                      "changed prose")
            doc_before = repo.read("guide.md")
            result = apply(repo.root, generator=StubGenerator())
            self.assertFalse(result.ok)
            self.assertTrue(any(r.entry_id.startswith("guide.md")
                                for r in result.refused))
            self.assertEqual(repo.read("guide.md"), doc_before)

    def test_cli_apply_exits_nonzero_on_unsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp), doc_is_truth=True)
            repo.edit("lib.py", "return a + b", "return a + b + 1")
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = cli.main(["apply", "--root", str(repo.root)])
            self.assertEqual(rc, 1)


class ApplyNoStateTest(unittest.TestCase):
    def test_apply_without_lockfile_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "guide.md", GUIDE)
            (root / MANIFEST).write_text(json.dumps({
                "version": 1,
                "markdown": [{
                    "doc": "guide.md", "heading": ["Behavior"],
                    "governs": "lib.py", "code_anchor": "def add",
                    "direction": "code-is-truth",
                }]}), encoding="utf-8")
            before = root.joinpath("lib.py").read_text()
            result = apply(root, generator=StubGenerator())
            self.assertFalse(result.ok)
            self.assertTrue(result.refused)
            self.assertEqual(root.joinpath("lib.py").read_text(), before)


def _entry(result, key):
    for e in result.entries:
        if e.key == key:
            return e
    raise AssertionError(f"no {key}")


if __name__ == "__main__":
    unittest.main()
