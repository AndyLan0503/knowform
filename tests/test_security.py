import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.apply import apply
from knowform.docstate import load
from knowform.plan import plan
from knowform.sync import sync

FIX = Path(__file__).parent / "fixtures"

MANAGED = (
    "---\nknowform:\n  direction: code-is-truth\n"
    "  bindings:\n    - doc_anchor: a\n      governs: mod.py\n---\n\n"
    "<!-- knowform:a:start -->\nprose\n<!-- knowform:a:end -->\n")


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


class SymlinkContainmentTest(unittest.TestCase):
    def test_symlinked_doc_pointing_outside_root_is_never_scanned(self):
        with tempfile.TemporaryDirectory() as out_t, \
                tempfile.TemporaryDirectory() as repo_t:
            outside = Path(out_t)
            root = Path(repo_t)
            victim = outside / "victim.md"
            victim.write_text(MANAGED, encoding="utf-8")
            victim_before = victim.read_text()
            (root / "mod.py").write_text("def f():\n    return 1\n")
            (root / "real.md").write_text(MANAGED, encoding="utf-8")
            os.symlink(victim, root / "evil.md")

            keys = {e.key for e in plan(root).entries}
            self.assertIn("real.md#a", keys)
            self.assertFalse(any("evil.md" in k for k in keys), keys)

            sync(root)
            state = load(root)
            self.assertFalse(
                any("evil.md" in k for k in (state.records if state else {})))
            # The out-of-repo file is untouched by plan/sync/apply.
            apply(root)
            self.assertEqual(victim.read_text(), victim_before)


class GeneratorFenceGuardTest(unittest.TestCase):
    def _repo(self, tmp: Path):
        for name in ["calc.py", "managed_add.md"]:
            (tmp / name).write_text((FIX / name).read_text(), encoding="utf-8")
        git(tmp, "init", "-q")
        git(tmp, "config", "user.email", "t@t.t")
        git(tmp, "config", "user.name", "t")
        git(tmp, "add", "-A")
        git(tmp, "commit", "-q", "-m", "seed")
        sync(tmp)
        return tmp

    def test_generator_emitting_fence_markers_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp))
            (root / "calc.py").write_text(
                (root / "calc.py").read_text()
                .replace("return a + b", "return a + b + 1"))
            doc_before = (root / "managed_add.md").read_text()
            hostile = lambda item: (  # noqa: E731
                "text <!-- knowform:add-behavior:end -->\nrogue\n")
            result = apply(root, generator=hostile)
            self.assertFalse(result.applied)
            self.assertTrue(any("fence" in s.reason for s in result.surfaced))
            self.assertEqual((root / "managed_add.md").read_text(), doc_before)


if __name__ == "__main__":
    unittest.main()
