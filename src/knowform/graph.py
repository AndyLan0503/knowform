"""Disposable structural graph for Tier-1 blast-radius scoping.

Rebuilt in memory each run, never persisted - the graph is disposable.
Nodes: DocRegion, CodeRegion. Edges: GOVERNS (doc→code),
IMPORTS/CALLS (code→code, dependent→dependency, from `ast`). From CodeRegions
overlapping the git diff, walk those edges *in reverse* to the bounded set of
dependents (callers/importers), then follow GOVERNS to the DocRegions
governing that set - the frontier that would reach the judge.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from .regions import Region, symbol_start


@dataclass(frozen=True)
class DocNode:
    key: str                # <doc-path>#<anchor> (doc-anchor identity)
    region: Region
    node_id: str = ""       # unique per (binding, governed-file); defaults to key

    def __post_init__(self) -> None:
        if not self.node_id:
            object.__setattr__(self, "node_id", self.key)


@dataclass(frozen=True)
class CodeNode:
    """A Python symbol node, keyed by module-relative path + qualified name."""
    path: str               # repo-relative file
    symbol: str             # function/class name, or "" for module scope
    lineno: int
    end_lineno: int

    @property
    def key(self) -> str:
        return f"{self.path}::{self.symbol}" if self.symbol else self.path


@dataclass
class Graph:
    docs: list[DocNode] = field(default_factory=list)
    code: dict[str, CodeNode] = field(default_factory=dict)
    governs: dict[str, str] = field(default_factory=dict)  # doc node_id -> code key
    edges: dict[str, set[str]] = field(default_factory=dict)  # dep -> deps
    rev_edges: dict[str, set[str]] = field(default_factory=dict)  # dep -> dependents

    def add_edge(self, src: str, dst: str) -> None:
        """Record a dependent→dependency edge (src depends on dst)."""
        if src != dst:
            self.edges.setdefault(src, set()).add(dst)
            self.rev_edges.setdefault(dst, set()).add(src)

    def dependents(self, seeds: set[str], depth: int) -> set[str]:
        """Code keys that depend on the seeds within `depth` hops.

        Walks IMPORTS/CALLS edges in reverse: from a changed symbol to its
        callers/importers, transitively. The seeds themselves are included so a
        directly-changed governed symbol is always in the result.
        """
        seen = set(seeds)
        frontier = set(seeds)
        for _ in range(max(0, depth)):
            nxt: set[str] = set()
            for node in frontier:
                nxt |= self.rev_edges.get(node, set()) - seen
            if not nxt:
                break
            seen |= nxt
            frontier = nxt
        return seen


def _symbol_index(tree: ast.Module) -> list[tuple[str, int, int]]:
    """Top-level def/class symbols as (name, start, end)."""
    out: list[tuple[str, int, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            out.append((node.name,
                        symbol_start(node),
                        getattr(node, "end_lineno", node.lineno)))
    return out


def build_graph(root: Path, doc_nodes: list[DocNode],
                py_files: set[Path]) -> Graph:
    """Assemble the graph over the given docs and the Python files in play.

    IMPORTS: a module referencing a name defined in another indexed module.
    CALLS: a symbol whose body calls another indexed symbol by name. Both are
    coarse name matches - deliberately over-inclusive (precision over recall).
    """
    graph = Graph(docs=list(doc_nodes))

    parsed: dict[str, ast.Module] = {}
    symbols_by_name: dict[str, list[CodeNode]] = {}
    for rel in sorted(py_files, key=str):
        full = root / rel
        if full.suffix != ".py":
            continue
        try:
            tree = ast.parse(full.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        parsed[str(rel)] = tree
        # module-scope node
        module_node = CodeNode(str(rel), "", 1,
                               len(full.read_text(encoding="utf-8")
                                   .splitlines()) or 1)
        graph.code[module_node.key] = module_node
        for name, start, end in _symbol_index(tree):
            node = CodeNode(str(rel), name, start, end)
            graph.code[node.key] = node
            symbols_by_name.setdefault(name, []).append(node)

    # IMPORTS / CALLS edges by name reference.
    for rel, tree in parsed.items():
        module_key = rel
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    target = alias.name.split(".")[-1]
                    for tgt in symbols_by_name.get(target, []):
                        graph.add_edge(module_key, tgt.key)
                if isinstance(node, ast.ImportFrom) and node.module:
                    mod = node.module.split(".")[-1] + ".py"
                    for path in parsed:
                        if Path(path).name == mod:
                            graph.add_edge(module_key, path)
            if isinstance(node, ast.Call):
                name = _call_name(node.func)
                if name:
                    for tgt in symbols_by_name.get(name, []):
                        src = _enclosing_symbol_key(rel, tree, node.lineno)
                        graph.add_edge(src, tgt.key)

    for doc in doc_nodes:
        code_key = _governed_code_key(doc, graph)
        if code_key:
            graph.governs[doc.node_id] = code_key
    return graph


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _enclosing_symbol_key(rel: str, tree: ast.Module, lineno: int) -> str:
    best: tuple[int, int, str] | None = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end:
                best = (node.lineno, end, node.name)
    return f"{rel}::{best[2]}" if best else rel


def _governed_code_key(doc: DocNode, graph: Graph) -> str | None:
    """Best code node for a doc region: the exact symbol if the region maps to
    one, else the module."""
    region = doc.region
    path = str(region.path)
    if region.whole and path in graph.code:
        return path
    for key, node in graph.code.items():
        if node.path == path and node.symbol and \
                node.lineno == region.start and node.end_lineno == region.end:
            return key
    return path if path in graph.code else None


def frontier(graph: Graph, changed_code_keys: set[str],
             depth: int) -> set[str]:
    """Doc keys at risk: docs governing {changed code ∪ its transitive
    dependents within `depth`}. A directly-changed governed region is included
    (its own key is a seed)."""
    at_risk = graph.dependents(changed_code_keys, depth)
    return {doc.node_id for doc in graph.docs
            if graph.governs.get(doc.node_id) in at_risk}
