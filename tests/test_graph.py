import tempfile
import unittest
from pathlib import Path

from knowform.graph import DocNode, build_graph, frontier
from knowform.regions import Region

FIX = Path(__file__).parent / "fixtures"


class GraphTest(unittest.TestCase):
    def test_calls_edge_extracted(self):
        graph = build_graph(FIX, [], {Path("calc.py")})
        # scaled_add and Accumulator.push both CALL add.
        callers = {src for src, dsts in graph.edges.items()
                   if "calc.py::add" in dsts}
        self.assertIn("calc.py::scaled_add", callers)

    def test_dependents_bounded_by_depth(self):
        graph = build_graph(FIX, [], {Path("calc.py")})
        seed = {"calc.py::add"}
        # scaled_add depends on add (calls it) -> is a dependent one hop away.
        self.assertIn("calc.py::scaled_add", graph.dependents(seed, 1))
        self.assertNotIn("calc.py::scaled_add", graph.dependents(seed, 0))

    def test_governs_maps_doc_to_symbol(self):
        add_region = Region(Path("calc.py"), 4, 5)  # def add span
        doc = DocNode(key="d#add", region=add_region)
        graph = build_graph(FIX, [doc], {Path("calc.py")})
        self.assertEqual(graph.governs["d#add"], "calc.py::add")

    def test_frontier_follows_dependents_then_governs(self):
        # A doc binds `scaled_add`; changing `add` (which scaled_add calls)
        # must reach scaled_add's doc, because scaled_add depends on add.
        scaled_region = Region(Path("calc.py"), 8, 10)  # def scaled_add span
        doc = DocNode(key="d#scaled", region=scaled_region)
        graph = build_graph(FIX, [doc], {Path("calc.py")})
        reached = frontier(graph, {"calc.py::add"}, depth=1)
        self.assertIn("d#scaled", reached)

    def test_frontier_does_not_pull_unrelated_callee(self):
        # A doc binds `add`; changing only a *caller* (scaled_add) must NOT
        # pull in the unrelated callee's doc. `add` does not depend on
        # scaled_add, so its doc is not at risk from that change.
        add_region = Region(Path("calc.py"), 4, 5)
        doc = DocNode(key="d#add", region=add_region)
        graph = build_graph(FIX, [doc], {Path("calc.py")})
        reached = frontier(graph, {"calc.py::scaled_add"}, depth=2)
        self.assertNotIn("d#add", reached)

    def test_directly_changed_governed_symbol_on_frontier(self):
        # Changing the governed symbol itself must reach its doc (seed).
        add_region = Region(Path("calc.py"), 4, 5)
        doc = DocNode(key="d#add", region=add_region)
        graph = build_graph(FIX, [doc], {Path("calc.py")})
        reached = frontier(graph, {"calc.py::add"}, depth=0)
        self.assertIn("d#add", reached)

    def test_graph_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "m.py").write_text("def f():\n    return 1\n")
            before = {p.name for p in root.iterdir()}
            build_graph(root, [], {Path("m.py")})
            self.assertEqual({p.name for p in root.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
