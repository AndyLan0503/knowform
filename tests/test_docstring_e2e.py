import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from knowform.apply import apply
from knowform.judge import JudgeInput, VerdictKind
from knowform.plan import plan
from knowform.sync import sync

DOCUMENTED = (
    '"""Module."""\n'
    '\n'
    '\n'
    'def add(a, b):\n'
    '    """Return the sum of a and b."""\n'
    '    return a + b\n'
)

KEY = "documented.py#docstring:def add"


def _manifest() -> str:
    return json.dumps({"version": 1, "docstrings": [
        {"governs": "documented.py", "symbol": "def add",
         "direction": "code-is-truth"}]})


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


class StubGenerator:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[JudgeInput] = []

    def __call__(self, item: JudgeInput) -> str:
        self.calls.append(item)
        return self.text


def _repo(tmp: str) -> Path:
    root = Path(tmp)
    (root / "documented.py").write_text(DOCUMENTED, encoding="utf-8")
    (root / "knowform.bindings.json").write_text(_manifest(), encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    return root


def _entry(result, key):
    for e in result.entries:
        if e.key == key:
            return e
    raise AssertionError(f"no {key}; got {[e.key for e in result.entries]}")


def _edit(root: Path, name: str, old: str, new: str) -> None:
    p = root / name
    p.write_text(p.read_text().replace(old, new), encoding="utf-8")


class DocstringManifestFlowTest(unittest.TestCase):
    def test_manifest_binding_resolves_and_syncs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp)
            result = sync(root)
            self.assertGreaterEqual(result.blessed, 1)
            plan_result = plan(root, base="HEAD")
            self.assertEqual(_entry(plan_result, KEY).verdict,
                             VerdictKind.IN_SYNC.value)


class DocstringDriftEndToEndTest(unittest.TestCase):
    def test_code_drift_regenerates_docstring(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp)
            sync(root)
            # Behavior changes; the docstring is left stale.
            _edit(root, "documented.py", "return a + b", "return a + b + 1")

            result = plan(root, base="HEAD")
            self.assertEqual(_entry(result, KEY).verdict,
                             VerdictKind.CODE_DRIFT.value)

            gen = StubGenerator("Return a plus b plus one.")
            res = apply(root, generator=gen)
            self.assertIn(KEY, res.applied)
            self.assertTrue(res.ok)

            src = (root / "documented.py").read_text()
            self.assertIn("Return a plus b plus one.", src)  # docstring rewritten
            self.assertIn("return a + b + 1", src)           # code untouched
            self.assertEqual(len(gen.calls), 1)
            # The generator saw the changed behavior, not the stale docstring.
            self.assertIn("return a + b + 1", gen.calls[0].code_text)
            self.assertNotIn("Return the sum", gen.calls[0].code_text)

            after = plan(root, base="HEAD")
            self.assertEqual(_entry(after, KEY).verdict,
                             VerdictKind.IN_SYNC.value)

    def test_generator_emitting_triple_quote_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp)
            sync(root)
            _edit(root, "documented.py", "return a + b", "return a + b + 1")
            before = (root / "documented.py").read_text()
            res = apply(root, generator=StubGenerator('bad """ prose'))
            self.assertFalse(res.ok)
            self.assertFalse(res.applied)
            # A corrupting docstring is never written.
            self.assertEqual((root / "documented.py").read_text(), before)

    def test_docstring_only_edit_is_doc_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp)
            sync(root)
            _edit(root, "documented.py",
                  "Return the sum of a and b.", "Adds two numbers.")
            result = plan(root, base="HEAD")
            self.assertEqual(_entry(result, KEY).verdict,
                             VerdictKind.DOC_DRIFT.value)

    def test_code_drift_without_generator_is_surfaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp)
            sync(root)
            before = (root / "documented.py").read_text()
            _edit(root, "documented.py", "return a + b", "return a + b + 1")
            res = apply(root, generator=None)
            self.assertFalse(res.ok)
            self.assertTrue(any(s.entry_id == KEY for s in res.surfaced))
            # Left stale, never touched without a generator.
            self.assertIn("Return the sum of a and b.",
                          (root / "documented.py").read_text())
            self.assertEqual((root / "documented.py").read_text().count(
                "return a + b + 1"), before.count("return a + b + 1") + 1)


if __name__ == "__main__":
    unittest.main()
