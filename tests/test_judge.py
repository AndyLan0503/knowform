import sys
import unittest
from pathlib import Path

from knowform.frontmatter import Binding, Direction
from knowform.judge import AnthropicJudge, build_frontier
from knowform.regions import resolve_code_region, resolve_doc_region

FIX = Path(__file__).parent / "fixtures"


class BuildFrontierTest(unittest.TestCase):
    def test_assembles_tight_neighborhood(self):
        b = Binding(doc_anchor="add-behavior", governs="calc.py",
                    code_anchor="def add")
        doc = resolve_doc_region(FIX, Path("managed_add.md"), b)
        code = resolve_code_region(FIX, Path("calc.py"), "def add")
        item = build_frontier(FIX, "managed_add.md#add-behavior",
                              Direction.CODE_IS_TRUTH, b, doc, code)
        self.assertEqual(item.direction, Direction.CODE_IS_TRUTH)
        self.assertIn("returns the sum", item.doc_claim)
        self.assertIn("return a + b", item.code_text)
        self.assertNotIn("scaled_add", item.code_text)  # tight, not whole file
        self.assertIn("def add(a, b)", item.signatures)

    def test_signatures_include_class(self):
        code = resolve_code_region(FIX, Path("calc.py"), "class Accumulator")
        b = Binding(doc_anchor="x", governs="calc.py")
        doc = resolve_doc_region(FIX, Path("managed_whole.md"), b)
        item = build_frontier(FIX, "k", Direction.MANUAL, b, doc, code)
        self.assertTrue(any("class Accumulator" in s for s in item.signatures))


class AnthropicJudgeTest(unittest.TestCase):
    def test_no_top_level_anthropic_import(self):
        # Constructing the adapter must not import anthropic; the import is
        # lazy inside __call__ so the subpackage stays dep-free.
        AnthropicJudge()
        self.assertNotIn("anthropic", sys.modules)

    def test_default_model(self):
        self.assertEqual(AnthropicJudge().model, "claude-opus-4-8")


if __name__ == "__main__":
    unittest.main()
