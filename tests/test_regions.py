import unittest
from pathlib import Path

from knowform.frontmatter import Binding
from knowform.regions import (
    hash_span, normalize, resolve_code_region, resolve_doc_region,
    resolve_governed_files,
)

FIX = Path(__file__).parent / "fixtures"


class NormalizeHashTest(unittest.TestCase):
    def test_normalize_strips_and_trims(self):
        self.assertEqual(normalize("\n\n  a  \nb   \n\n"), "  a\nb")

    def test_normalize_line_endings(self):
        self.assertEqual(normalize("a\r\nb\r\n"), "a\nb")

    def test_hash_is_stable_under_trailing_ws(self):
        self.assertEqual(hash_span("a\nb"), hash_span("a  \nb\n\n"))

    def test_hash_differs_on_content(self):
        self.assertNotEqual(hash_span("a"), hash_span("b"))

    def test_hash_prefix(self):
        self.assertTrue(hash_span("x").startswith("sha256:"))


class DocRegionTest(unittest.TestCase):
    def test_fenced_region(self):
        b = Binding(doc_anchor="add-behavior", governs="calc.py")
        region = resolve_doc_region(FIX, Path("managed_add.md"), b)
        self.assertFalse(region.whole)
        self.assertIn("returns the sum", region.text(FIX))

    def test_absent_fence_degrades_to_whole(self):
        b = Binding(doc_anchor="overview", governs="calc.py")
        region = resolve_doc_region(FIX, Path("managed_whole.md"), b)
        self.assertTrue(region.whole)
        self.assertEqual(region.start, 1)


class CodeRegionTest(unittest.TestCase):
    def test_def_anchor(self):
        r = resolve_code_region(FIX, Path("calc.py"), "def add")
        self.assertFalse(r.whole)
        self.assertIn("return a + b", r.text(FIX))
        self.assertNotIn("scaled_add", r.text(FIX))

    def test_class_anchor(self):
        r = resolve_code_region(FIX, Path("calc.py"), "class Accumulator")
        self.assertIn("def push", r.text(FIX))
        self.assertFalse(r.whole)

    def test_bare_name_anchor(self):
        r = resolve_code_region(FIX, Path("calc.py"), "scaled_add")
        self.assertIn("factor", r.text(FIX))
        self.assertFalse(r.whole)

    def test_unresolved_anchor_degrades_to_whole_file(self):
        r = resolve_code_region(FIX, Path("calc.py"), "def nonexistent")
        self.assertTrue(r.whole)

    def test_non_python_degrades_to_whole_file(self):
        r = resolve_code_region(FIX, Path("managed_add.md"), "def add")
        self.assertTrue(r.whole)

    def test_no_anchor_is_whole_file(self):
        r = resolve_code_region(FIX, Path("calc.py"), None)
        self.assertTrue(r.whole)

    def test_decorator_included_in_region(self):
        r = resolve_code_region(FIX, Path("decorated.py"), "def cached")
        self.assertFalse(r.whole)
        # The region must start at the decorator, not the `def` line, so a
        # decorator-arg change is inside the hashed/gated span.
        self.assertIn("@lru_cache", r.text(FIX))


def _paths(results):
    return [r.path for r in results if r.error is None]


def _errors(results):
    return [r for r in results if r.error is not None]


class GovernsTest(unittest.TestCase):
    def test_glob_expands(self):
        results = resolve_governed_files(FIX, "*.py")
        self.assertIn(Path("calc.py"), _paths(results))

    def test_missing_path_returned_literally(self):
        results = resolve_governed_files(FIX, "nope.py")
        self.assertEqual(_paths(results), [Path("nope.py")])
        self.assertEqual(_errors(results), [])

    def test_absolute_governs_is_error_not_crash(self):
        results = resolve_governed_files(FIX, "/etc/passwd")
        self.assertEqual(_paths(results), [])
        self.assertTrue(_errors(results))

    def test_dotdot_escape_is_error_not_crash(self):
        results = resolve_governed_files(FIX, "../../../etc/passwd")
        self.assertEqual(_paths(results), [])
        self.assertTrue(_errors(results))

    def test_symlink_escaping_root_is_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as outer:
            outer_p = Path(outer)
            root = outer_p / "root"
            root.mkdir()
            (outer_p / "secret.py").write_text("s = 1\n")
            (root / "link.py").symlink_to(outer_p / "secret.py")
            results = resolve_governed_files(root, "link.py")
            # Symlink escaping root must never resolve to a readable path.
            self.assertEqual(_paths(results), [])
            self.assertTrue(_errors(results))


if __name__ == "__main__":
    unittest.main()
