import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.apply import apply
from knowform.manifest import MANIFEST
from knowform.sync import sync

LIB = 'def add(a, b):\n    return a + b\n'
GUIDE = "# Calc\n\n## Behavior\n\n`add(a, b)` returns the sum.\n"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


def _manifest(root: Path, markdown: list[dict]) -> None:
    (root / MANIFEST).write_text(
        json.dumps({"version": 1, "markdown": markdown}), encoding="utf-8")


def _commit_and_sync(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    sync(root)


class SymlinkContainmentTest(unittest.TestCase):
    def test_symlinked_doc_pointing_outside_root_is_never_written(self):
        with tempfile.TemporaryDirectory() as out_t, \
                tempfile.TemporaryDirectory() as repo_t:
            outside = Path(out_t)
            root = Path(repo_t)
            victim = outside / "victim.md"
            victim.write_text(GUIDE, encoding="utf-8")
            victim_before = victim.read_text()

            _write(root, "lib.py", LIB)
            # A manifest binding whose `doc` is a symlink escaping root: even
            # with drift, apply must never write through it to the victim.
            os.symlink(victim, root / "evil.md")
            _manifest(root, [{
                "doc": "evil.md", "heading": ["Behavior"], "governs": "lib.py",
                "code_anchor": "def add", "direction": "code-is-truth",
            }])
            _commit_and_sync(root)

            # Drift the code so the code-is-truth binding wants regeneration.
            (root / "lib.py").write_text(
                LIB.replace("return a + b", "return a + b + 1"),
                encoding="utf-8")
            result = apply(root, generator=lambda item: "regenerated prose")

            self.assertFalse(result.applied)
            self.assertFalse(result.ok)
            self.assertTrue(any("escapes" in r.reason
                                for r in result.surfaced + result.refused))
            # The out-of-repo file is untouched by apply.
            self.assertEqual(victim.read_text(), victim_before)

    def test_governs_escaping_root_surfaces_as_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "lib.py", LIB)
            _write(root, "guide.md", GUIDE)
            _manifest(root, [{
                "doc": "guide.md", "heading": ["Behavior"],
                "governs": "/etc/passwd", "code_anchor": None,
                "direction": "code-is-truth",
            }])
            git(root, "init", "-q")
            git(root, "config", "user.email", "t@t.t")
            git(root, "config", "user.name", "t")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "seed")

            from knowform.plan import plan
            keys = [e for e in plan(root).entries if e.verdict == "error"]
            self.assertTrue(any("escapes root" in (e.error or "")
                                for e in keys))


class GeneratorHeadingGuardTest(unittest.TestCase):
    def _repo(self, tmp: Path) -> Path:
        _write(tmp, "lib.py", LIB)
        _write(tmp, "guide.md", GUIDE)
        _manifest(tmp, [{
            "doc": "guide.md", "heading": ["Behavior"], "governs": "lib.py",
            "code_anchor": "def add", "direction": "code-is-truth",
        }])
        _commit_and_sync(tmp)
        return tmp

    def test_generator_emitting_a_heading_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp))
            (root / "lib.py").write_text(
                LIB.replace("return a + b", "return a + b + 1"),
                encoding="utf-8")
            doc_before = (root / "guide.md").read_text()
            # Prose carrying a markdown heading would break the anchor the
            # binding resolves by; apply must refuse it and write nothing.
            hostile = lambda item: "## Injected\nrogue prose\n"  # noqa: E731
            result = apply(root, generator=hostile)
            self.assertFalse(result.applied)
            self.assertTrue(any("heading" in s.reason
                                for s in result.surfaced))
            self.assertEqual((root / "guide.md").read_text(), doc_before)


if __name__ == "__main__":
    unittest.main()
