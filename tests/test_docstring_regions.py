import unittest
from pathlib import Path

from knowform.regions import (
    replace_docstring, resolve_docstring_code_region, resolve_docstring_region,
)

FIX = Path(__file__).parent / "fixtures"
DOC = Path("documented.py")


class DocstringRegionTest(unittest.TestCase):
    def test_single_line_docstring_span(self):
        r = resolve_docstring_region(FIX, DOC, "def add")
        self.assertFalse(r.whole)
        self.assertIn("Return the sum of a and b.", r.text(FIX))
        self.assertNotIn("return a + b", r.text(FIX))

    def test_multi_line_docstring_span(self):
        r = resolve_docstring_region(FIX, DOC, "def greet")
        self.assertFalse(r.whole)
        text = r.text(FIX)
        self.assertIn("Greet someone.", text)
        self.assertIn("multiple lines.", text)
        self.assertNotIn("hello", text)

    def test_class_docstring_span(self):
        r = resolve_docstring_region(FIX, DOC, "class Widget")
        self.assertIn("A small widget.", r.text(FIX))
        self.assertFalse(r.whole)

    def test_missing_docstring_degrades_to_whole(self):
        r = resolve_docstring_region(FIX, DOC, "def undocumented")
        self.assertTrue(r.whole)

    def test_unresolved_symbol_degrades_to_whole(self):
        r = resolve_docstring_region(FIX, DOC, "def nope")
        self.assertTrue(r.whole)

    def test_non_python_degrades_to_whole(self):
        r = resolve_docstring_region(FIX, Path("managed_add.md"), "def add")
        self.assertTrue(r.whole)


class DocstringCodeRegionTest(unittest.TestCase):
    def test_code_region_excludes_docstring(self):
        r = resolve_docstring_code_region(FIX, DOC, "def add")
        self.assertFalse(r.whole)
        text = r.text(FIX)
        self.assertIn("def add", text)
        self.assertIn("return a + b", text)
        # The docstring prose must NOT be part of the behavior region.
        self.assertNotIn("Return the sum of a and b.", text)

    def test_code_region_excludes_multiline_docstring(self):
        r = resolve_docstring_code_region(FIX, DOC, "def greet")
        text = r.text(FIX)
        self.assertIn("return f", text)
        self.assertNotIn("Greet someone.", text)
        self.assertNotIn("multiple lines.", text)

    def test_undocumented_code_region_is_full_symbol(self):
        # No docstring to subtract: behavior region is the whole symbol.
        r = resolve_docstring_code_region(FIX, DOC, "def undocumented")
        self.assertFalse(r.whole)
        self.assertIn("return x * 2", r.text(FIX))

    def test_editing_docstring_does_not_change_code_region_text(self):
        before = resolve_docstring_code_region(FIX, DOC, "def add").text(FIX)
        self.assertNotIn("sum", before)


class ReplaceDocstringTest(unittest.TestCase):
    SRC = ('def add(a, b):\n'
           '    """Old."""\n'
           '    return a + b\n')

    def test_replaces_single_line_preserving_indent(self):
        out = replace_docstring(self.SRC, "def add", "New prose.")
        self.assertIn("New prose.", out)
        self.assertNotIn("Old.", out)
        self.assertIn("    return a + b", out)

    def test_returns_none_when_no_docstring(self):
        src = 'def add(a, b):\n    return a + b\n'
        self.assertIsNone(replace_docstring(src, "def add", "x"))

    def test_multiline_prose_becomes_block_docstring(self):
        out = replace_docstring(self.SRC, "def add", "Line one.\nLine two.")
        self.assertIn("Line one.", out)
        self.assertIn("Line two.", out)
        self.assertIn("    return a + b", out)


if __name__ == "__main__":
    unittest.main()
