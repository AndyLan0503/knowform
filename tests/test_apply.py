import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.apply import apply
from knowform.judge import JudgeInput, VerdictKind
from knowform.plan import plan
from knowform.sync import sync
from knowform import __main__ as cli

FIX = Path(__file__).parent / "fixtures"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


class StubGenerator:
    """Injected prose generator; returns a fixed string, records its inputs."""

    def __init__(self, text="`add(a, b)` returns a+b+1.\n"):
        self.text = text
        self.calls: list[JudgeInput] = []

    def __call__(self, item: JudgeInput) -> str:
        self.calls.append(item)
        return self.text


DOC_IS_TRUTH = (
    "---\nknowform:\n  direction: doc-is-truth\n"
    "  bindings:\n    - doc_anchor: spec\n"
    "      governs: calc.py\n      code_anchor: \"def add\"\n---\n\n"
    "<!-- knowform:spec:start -->\n"
    "`add` MUST return the sum.\n"
    "<!-- knowform:spec:end -->\n")


class Repo:
    def __init__(self, tmp: Path, extra: dict[str, str] | None = None):
        self.root = tmp
        for name in ["calc.py", "managed_add.md"]:
            (self.root / name).write_text(
                (FIX / name).read_text(encoding="utf-8"), encoding="utf-8")
        for name, text in (extra or {}).items():
            (self.root / name).write_text(text, encoding="utf-8")
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
            code_before = repo.read("calc.py")
            repo.edit("calc.py", "return a + b", "return a + b + 1")
            code_after_edit = repo.read("calc.py")
            gen = StubGenerator()
            result = apply(repo.root, generator=gen)
            self.assertIn("managed_add.md#add-behavior::calc.py::def add",
                          result.applied)
            self.assertTrue(result.ok)
            # Doc region rewritten to the generator output.
            self.assertIn("returns a+b+1", repo.read("managed_add.md"))
            # Code is the user's edit, byte-for-byte - apply never wrote it.
            self.assertEqual(repo.read("calc.py"), code_after_edit)
            self.assertNotEqual(repo.read("calc.py"), code_before)
            self.assertEqual(len(gen.calls), 1)

    def test_after_apply_binding_reads_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.edit("calc.py", "return a + b", "return a + b + 1")
            apply(repo.root, generator=StubGenerator())
            result = plan(repo.root, base="HEAD", judge=None)
            e = _entry(result, "managed_add.md#add-behavior")
            self.assertEqual(e.verdict, VerdictKind.IN_SYNC.value)

    def test_no_generator_surfaces_not_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            doc_before = repo.read("managed_add.md")
            repo.edit("calc.py", "return a + b", "return a + b + 1")
            result = apply(repo.root, generator=None)
            self.assertFalse(result.ok)
            self.assertFalse(result.applied)
            self.assertTrue(result.surfaced)
            self.assertEqual(repo.read("managed_add.md"), doc_before)


class ApplyUnsafeDirectionTest(unittest.TestCase):
    def test_doc_is_truth_drift_refused_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp), extra={"spec.md": DOC_IS_TRUTH})
            code_before = repo.read("calc.py")
            doc_before = repo.read("spec.md")
            repo.edit("calc.py", "return a + b", "return a + b + 1")
            after_edit = repo.read("calc.py")
            gen = StubGenerator()
            result = apply(repo.root, generator=gen)
            self.assertFalse(result.ok)
            refused = {r.entry_id for r in result.refused}
            self.assertIn("spec.md#spec::calc.py::def add", refused)
            # Neither code nor the prescriptive doc was written.
            self.assertEqual(repo.read("calc.py"), after_edit)
            self.assertEqual(repo.read("spec.md"), doc_before)
            self.assertNotEqual(repo.read("calc.py"), code_before)
            # A doc-is-truth binding must never reach the generator.
            self.assertFalse(
                any(c.key == "spec.md#spec" for c in gen.calls))

    def test_conflict_refused_no_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp))
            repo.edit("calc.py", "return a + b", "return a + b + 1")
            repo.edit("managed_add.md", "the sum of its two arguments",
                      "changed prose")
            doc_before = repo.read("managed_add.md")
            result = apply(repo.root, generator=StubGenerator())
            self.assertFalse(result.ok)
            self.assertTrue(any(r.entry_id.startswith("managed_add.md")
                                for r in result.refused))
            self.assertEqual(repo.read("managed_add.md"), doc_before)

    def test_cli_apply_exits_nonzero_on_unsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp), extra={"spec.md": DOC_IS_TRUTH})
            repo.edit("calc.py", "return a + b", "return a + b + 1")
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = cli.main(["apply", "--root", str(repo.root)])
            self.assertEqual(rc, 1)


class ApplyNoStateTest(unittest.TestCase):
    def test_apply_without_lockfile_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "calc.py").write_text((FIX / "calc.py").read_text())
            (root / "managed_add.md").write_text(
                (FIX / "managed_add.md").read_text())
            before = root.joinpath("calc.py").read_text()
            result = apply(root, generator=StubGenerator())
            self.assertFalse(result.ok)
            self.assertTrue(result.refused)
            self.assertEqual(root.joinpath("calc.py").read_text(), before)


def _entry(result, key):
    for e in result.entries:
        if e.key == key:
            return e
    raise AssertionError(f"no {key}")


if __name__ == "__main__":
    unittest.main()
