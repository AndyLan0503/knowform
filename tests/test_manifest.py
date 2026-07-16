import json
import tempfile
import unittest
from pathlib import Path

from knowform.manifest import MANIFEST, Direction, load


class ManifestLoadTest(unittest.TestCase):
    def _write(self, root: Path, data) -> None:
        (root / MANIFEST).write_text(
            data if isinstance(data, str) else json.dumps(data),
            encoding="utf-8")

    def test_absent_manifest_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load(Path(tmp)))

    def test_parses_docstring_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, {"version": 1, "docstrings": [
                {"governs": "calc.py", "symbol": "def add",
                 "direction": "code-is-truth"}]})
            m = load(root)
            self.assertIsNone(m.error)
            self.assertEqual(len(m.docstrings), 1)
            b = m.docstrings[0]
            self.assertEqual(b.governs, "calc.py")
            self.assertEqual(b.symbol, "def add")
            self.assertEqual(b.direction, Direction.CODE_IS_TRUTH)

    def test_direction_defaults_to_code_is_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, {"docstrings": [
                {"governs": "calc.py", "symbol": "def add"}]})
            self.assertEqual(load(root).docstrings[0].direction,
                             Direction.CODE_IS_TRUTH)

    def test_missing_symbol_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, {"docstrings": [{"governs": "calc.py"}]})
            self.assertIsNotNone(load(root).error)

    def test_unknown_direction_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, {"docstrings": [
                {"governs": "calc.py", "symbol": "def add",
                 "direction": "sideways"}]})
            self.assertIsNotNone(load(root).error)

    def test_malformed_json_is_error_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "{not json")
            self.assertIsNotNone(load(root).error)

    def test_parses_markdown_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, {"markdown": [
                {"doc": "README.md", "heading": ["Usage", "Add"],
                 "governs": "calc.py", "code_anchor": "def add",
                 "direction": "code-is-truth", "block": 2}]})
            m = load(root)
            self.assertIsNone(m.error)
            self.assertEqual(len(m.markdown), 1)
            b = m.markdown[0]
            self.assertEqual(b.doc, "README.md")
            self.assertEqual(b.heading, ("Usage", "Add"))
            self.assertEqual(b.governs, "calc.py")
            self.assertEqual(b.code_anchor, "def add")
            self.assertEqual(b.direction, Direction.CODE_IS_TRUTH)
            self.assertEqual(b.block, 2)

    def test_markdown_missing_governs_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, {"markdown": [
                {"doc": "README.md", "heading": ["Usage"]}]})
            self.assertIsNotNone(load(root).error)


if __name__ == "__main__":
    unittest.main()
