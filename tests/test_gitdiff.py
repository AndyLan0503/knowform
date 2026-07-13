import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.gitdiff import changed_set


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True)


def init_repo(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")


class SpacedFilenameTest(unittest.TestCase):
    def test_change_to_spaced_filename_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            name = "a b.py"
            (root / name).write_text("x = 1\n")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "seed")
            (root / name).write_text("x = 2\n")
            cs = changed_set(root, "HEAD")
            self.assertTrue(cs.available)
            self.assertTrue(cs.overlaps(Path(name), 1, 1),
                            f"spaced-name change missing; ranges={cs.ranges}")


class UntrackedFileTest(unittest.TestCase):
    def test_untracked_file_counts_as_fully_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "seed.py").write_text("x = 1\n")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "seed")
            (root / "new.py").write_text("def f():\n    return 1\n")
            cs = changed_set(root, "HEAD")
            self.assertTrue(cs.available)
            self.assertTrue(cs.overlaps(Path("new.py"), 1, 2),
                            f"untracked file not in changed set; {cs.ranges}")


class BaseRefGuardTest(unittest.TestCase):
    def test_hostile_base_ref_cannot_write_and_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "seed.py").write_text("x = 1\n")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "seed")
            sentinel = root / "pwned"
            cs = changed_set(root, f"--output={sentinel}")
            self.assertFalse(cs.available)
            self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
