import unittest
from pathlib import Path

from knowform.frontmatter import (
    Direction, fence_span, parse_frontmatter,
)

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FrontmatterTest(unittest.TestCase):
    def test_valid_frontmatter(self):
        doc = parse_frontmatter(read("managed_add.md"))
        self.assertIsNotNone(doc)
        self.assertEqual(doc.direction, Direction.CODE_IS_TRUTH)
        self.assertEqual(len(doc.bindings), 1)
        b = doc.bindings[0]
        self.assertEqual(b.doc_anchor, "add-behavior")
        self.assertEqual(b.governs, "calc.py")
        self.assertEqual(b.code_anchor, "def add")
        self.assertIsNone(doc.error)

    def test_binding_without_code_anchor(self):
        doc = parse_frontmatter(read("managed_whole.md"))
        self.assertEqual(doc.direction, Direction.DOC_IS_TRUTH)
        self.assertIsNone(doc.bindings[0].code_anchor)

    def test_sidecar_card_parses_ignoring_sidecar_scalars(self):
        # A single file carries rich sidecar metadata (type/title/.../sources
        # scalars) AND a knowform block; the parser reads the knowform block
        # and ignores the sibling sidecar scalars before it.
        doc = parse_frontmatter(read("managed_sidecar.md"))
        self.assertIsNotNone(doc)
        self.assertIsNone(doc.error)
        self.assertEqual(doc.direction, Direction.CODE_IS_TRUTH)
        self.assertEqual(len(doc.bindings), 1)
        b = doc.bindings[0]
        self.assertEqual(b.doc_anchor, "sidecar-behavior")
        self.assertEqual(b.governs, "calc.py")
        self.assertEqual(b.code_anchor, "def add")

    def test_missing_knowform_is_unmanaged(self):
        self.assertIsNone(parse_frontmatter(read("unmanaged.md")))

    def test_no_frontmatter_at_all(self):
        self.assertIsNone(parse_frontmatter("# plain\n\nbody\n"))

    def test_missing_direction_is_hard_error(self):
        doc = parse_frontmatter(read("no_direction.md"))
        self.assertIsNotNone(doc)
        self.assertIsNone(doc.direction)
        self.assertIn("direction", doc.error)

    def test_unknown_direction_is_error(self):
        text = "---\nknowform:\n  direction: sideways\n---\n"
        doc = parse_frontmatter(text)
        self.assertIsNone(doc.direction)
        self.assertIn("sideways", doc.error)

    def test_multiple_bindings(self):
        text = (
            "---\n"
            "knowform:\n"
            "  direction: manual\n"
            "  bindings:\n"
            "    - doc_anchor: a\n"
            "      governs: x.py\n"
            "    - doc_anchor: b\n"
            "      governs: y.py\n"
            "      code_anchor: class Foo\n"
            "---\n"
        )
        doc = parse_frontmatter(text)
        self.assertEqual(doc.direction, Direction.MANUAL)
        self.assertEqual([b.doc_anchor for b in doc.bindings], ["a", "b"])
        self.assertEqual(doc.bindings[1].code_anchor, "class Foo")


class InlineHashTest(unittest.TestCase):
    def test_hash_in_quoted_value_preserved(self):
        text = ('---\nknowform:\n  direction: code-is-truth\n'
                '  bindings:\n    - doc_anchor: a\n'
                '      governs: "a#b.py"\n---\n')
        doc = parse_frontmatter(text)
        self.assertEqual(doc.bindings[0].governs, "a#b.py")

    def test_hash_in_unquoted_value_preserved(self):
        # `#` not preceded by whitespace is part of the value, not a comment.
        text = ('---\nknowform:\n  direction: code-is-truth\n'
                '  bindings:\n    - doc_anchor: a\n'
                '      governs: a#b.py\n---\n')
        doc = parse_frontmatter(text)
        self.assertEqual(doc.bindings[0].governs, "a#b.py")

    def test_trailing_comment_still_stripped(self):
        text = ('---\nknowform:\n  direction: code-is-truth  # a note\n'
                '  bindings:\n    - doc_anchor: a\n'
                '      governs: x.py\n---\n')
        doc = parse_frontmatter(text)
        self.assertEqual(doc.direction, Direction.CODE_IS_TRUTH)


class FenceTest(unittest.TestCase):
    def test_fenced_span_excludes_fences(self):
        text = read("managed_add.md")
        span = fence_span(text, "add-behavior")
        self.assertIsNotNone(span)
        start, end = span
        lines = text.splitlines()
        body = "\n".join(lines[start - 1:end])
        self.assertIn("returns the sum", body)
        self.assertNotIn("start -->", body)
        self.assertNotIn("end -->", body)

    def test_absent_fence_returns_none(self):
        self.assertIsNone(fence_span(read("managed_whole.md"), "overview"))


if __name__ == "__main__":
    unittest.main()
