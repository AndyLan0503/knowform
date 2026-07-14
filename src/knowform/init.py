"""`knowform init`: deterministic (no-LLM) discovery of candidate doc↔code
bindings for a repo with no knowform wiring.

Read-only over the repo: it proposes, it never materializes. The only artifact
it writes is `knowform.init.json` - a reviewable proposal a human accepts
before a later milestone writes frontmatter/fences/manifest.

Two discovery sources:
- Tier 1 docstrings: every documented function/class/method is a candidate
  docstring binding (high precision, nearly free).
- Tier 0 markdown refs: backtick/call-shaped identifiers in unmanaged `.md`
  resolved against the repo symbol table; exactly-one-match -> candidate,
  ambiguous -> `unmatched` for a human. Precision over recall: never bind on a
  guess, never auto-assign `doc-is-truth`.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .frontmatter import Direction, parse_frontmatter
from .manifest import load as load_manifest
from .plan import _managed_docs, _pruned_walk, load_ignore
from .regions import (
    _docstring_span, resolve_docstring_region, resolve_governed_files,
)

INIT_PROPOSAL = "knowform.init.json"
VERSION = 1


@dataclass(frozen=True)
class Candidate:
    kind: str                        # "docstring" | "markdown"
    governs: str                     # the .py file hosting the symbol
    doc_path: str
    doc_region: tuple[int, int]      # 1-based inclusive line span
    direction: str
    confidence: str
    rationale: str
    source_tier: int
    symbol: str | None = None        # docstring anchor
    code_anchor: str | None = None   # markdown-resolved anchor

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "governs": self.governs,
            "symbol": self.symbol,
            "code_anchor": self.code_anchor,
            "doc_path": self.doc_path,
            "doc_region": list(self.doc_region),
            "direction": self.direction,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "source_tier": self.source_tier,
        }


@dataclass(frozen=True)
class Unmatched:
    kind: str
    doc_path: str
    doc_region: tuple[int, int]
    identifier: str
    match_count: int
    reason: str

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "doc_path": self.doc_path,
            "doc_region": list(self.doc_region),
            "identifier": self.identifier,
            "match_count": self.match_count,
            "reason": self.reason,
        }


@dataclass
class Proposal:
    candidates: list[Candidate] = field(default_factory=list)
    unmatched: list[Unmatched] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": VERSION,
            "candidates": [c.to_dict() for c in self.candidates],
            "unmatched": [u.to_dict() for u in self.unmatched],
        }


@dataclass(frozen=True)
class _Symbol:
    file: str
    anchor: str                      # "def name" | "class name"


def init(root: Path) -> Proposal:
    """Discover candidate bindings. Read-only: resolves regions and hashes
    nothing to disk (the caller `write_proposal`s the artifact separately)."""
    root = Path(root).resolve()
    ignore = load_ignore(root)
    py_files = _pruned_walk(root, ".py", ignore)
    table = _symbol_table(root, py_files)
    bound = _bound(root, ignore)

    candidates: list[Candidate] = []
    unmatched: list[Unmatched] = []
    candidates += _docstring_candidates(root, py_files, bound)
    md_c, md_u = _markdown_candidates(root, ignore, table, bound)
    candidates += md_c
    unmatched += md_u

    proposal = Proposal()
    proposal.candidates = sorted(candidates, key=_candidate_key)
    proposal.unmatched = sorted(unmatched, key=_unmatched_key)
    return proposal


def write_proposal(root: Path, proposal: Proposal) -> Path:
    out = Path(root) / INIT_PROPOSAL
    out.write_text(json.dumps(proposal.to_dict(), indent=2) + "\n",
                   encoding="utf-8")
    return out


def _candidate_key(c: Candidate) -> tuple:
    return (c.kind, c.doc_path, c.doc_region[0], c.doc_region[1], c.governs,
            c.symbol or c.code_anchor or "")


def _unmatched_key(u: Unmatched) -> tuple:
    return (u.doc_path, u.doc_region[0], u.doc_region[1], u.identifier)


# --- Tier 1: docstrings ------------------------------------------------------

def _docstring_candidates(root: Path, py_files: list[Path],
                          bound: set[tuple[str, str]]) -> list[Candidate]:
    out: list[Candidate] = []
    for rel in py_files:
        tree = _parse(root / rel)
        if tree is None:
            continue
        anchors: dict[str, list[ast.AST]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            if _docstring_span(node) is None:
                continue
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            anchors.setdefault(f"{kind} {node.name}", []).append(node)
        for anchor, nodes in anchors.items():
            if len(nodes) != 1:
                continue  # ambiguous within-file name; never bind on a guess
            if (str(rel), _bare(anchor)) in bound:
                continue
            region = resolve_docstring_region(root, rel, anchor)
            if region.whole:
                continue
            out.append(Candidate(
                kind="docstring", governs=str(rel), doc_path=str(rel),
                doc_region=(region.start, region.end),
                direction=Direction.CODE_IS_TRUTH.value, confidence="high",
                rationale=f"documented symbol `{anchor}`", source_tier=1,
                symbol=anchor))
    return out


# --- Tier 0: markdown explicit references ------------------------------------

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE = re.compile(r"`([^`]+)`")
_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _markdown_candidates(root: Path, ignore: list[str],
                         table: dict[str, list[_Symbol]],
                         bound: set[tuple[str, str]]
                         ) -> tuple[list[Candidate], list[Unmatched]]:
    candidates: list[Candidate] = []
    unmatched: list[Unmatched] = []
    for doc in _pruned_walk(root, ".md", ignore):
        text = (root / doc).read_text(encoding="utf-8")
        if parse_frontmatter(text) is not None:
            continue  # already managed -> not rescanned
        lines = text.split("\n")
        paras = _paragraphs(lines)
        seen: set[tuple[int, int, str]] = set()
        for line_no, name, strong in _references(lines):
            span = _paragraph_for(line_no, paras)
            matches = table.get(name, [])
            if len(matches) == 1:
                sym = matches[0]
                if (sym.file, _bare(sym.anchor)) in bound:
                    continue
                dkey = (span[0], span[1], sym.anchor)
                if dkey in seen:
                    continue
                seen.add(dkey)
                candidates.append(Candidate(
                    kind="markdown", governs=sym.file, doc_path=str(doc),
                    doc_region=span, direction=Direction.CODE_IS_TRUTH.value,
                    confidence="high",
                    rationale=f"`{name}` resolves to {sym.anchor} "
                              f"in {sym.file}",
                    source_tier=0, code_anchor=sym.anchor))
            elif len(matches) > 1 or strong:
                # >1: genuinely ambiguous. 0 + call-shaped: looks like a call
                # to an unknown symbol - worth review, never a silent bind.
                ukey = (span[0], span[1], name)
                if ukey in seen:
                    continue
                seen.add(ukey)
                reason = ("ambiguous: multiple symbols" if len(matches) > 1
                          else "call-shaped reference resolves to no symbol")
                unmatched.append(Unmatched(
                    kind="markdown", doc_path=str(doc), doc_region=span,
                    identifier=name, match_count=len(matches), reason=reason))
    return candidates, unmatched


def _references(lines: list[str]) -> list[tuple[int, str, bool]]:
    """(1-based line, identifier, strong) references. `strong` marks a
    call-shaped token `name(...)`; weak marks a plain backtick identifier.
    Inside fenced code blocks only call-shaped tokens are harvested, to keep
    prose/variable noise out of the proposal."""
    out: list[tuple[int, str, bool]] = []
    in_fence = False
    for i, line in enumerate(lines, start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            for m in _CALL.finditer(line):
                out.append((i, m.group(1).split(".")[-1], True))
            continue
        for m in _CALL.finditer(line):
            out.append((i, m.group(1).split(".")[-1], True))
        for span in _INLINE.findall(line):
            tok = span.strip()
            base = tok.split("(")[0].split(".")[-1]
            if _IDENT.match(base):
                out.append((i, base, False))
    return out


def _paragraphs(lines: list[str]) -> list[tuple[int, int]]:
    """Contiguous non-blank line runs, 1-based inclusive. A fenced code block
    is one paragraph spanning its opening to closing fence."""
    out: list[tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        if _FENCE.match(lines[i]):
            j = i + 1
            while j < n and not _FENCE.match(lines[j]):
                j += 1
            out.append((i + 1, min(j, n - 1) + 1))
            i = j + 1
            continue
        j = i
        while j < n and lines[j].strip() and not _FENCE.match(lines[j]):
            j += 1
        out.append((i + 1, j))
        i = j
    return out


def _paragraph_for(line_no: int, paras: list[tuple[int, int]]
                   ) -> tuple[int, int]:
    for start, end in paras:
        if start <= line_no <= end:
            return (start, end)
    return (line_no, line_no)


# --- shared ------------------------------------------------------------------

def _symbol_table(root: Path, py_files: list[Path]
                  ) -> dict[str, list[_Symbol]]:
    table: dict[str, list[_Symbol]] = {}
    for rel in py_files:
        tree = _parse(root / rel)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "def"
            elif isinstance(node, ast.ClassDef):
                kind = "class"
            else:
                continue
            table.setdefault(node.name, []).append(
                _Symbol(str(rel), f"{kind} {node.name}"))
    return table


def _bound(root: Path, ignore: list[str]) -> set[tuple[str, str]]:
    """(governed-file, bare-symbol-name) pairs already bound by an existing
    manifest or frontmatter binding - skipped so `init` never re-proposes."""
    out: set[tuple[str, str]] = set()
    manifest = load_manifest(root)
    if manifest is not None and not manifest.error:
        for db in manifest.docstrings:
            for gov in resolve_governed_files(root, db.governs):
                if gov.path is not None:
                    out.add((str(gov.path), _bare(db.symbol)))
    for doc in _managed_docs(root, ignore):
        managed = parse_frontmatter((root / doc).read_text(encoding="utf-8"))
        if managed is None or managed.error:
            continue
        for b in managed.bindings:
            if not b.code_anchor:
                continue
            for gov in resolve_governed_files(root, b.governs):
                if gov.path is not None:
                    out.add((str(gov.path), _bare(b.code_anchor)))
    return out


def _bare(anchor: str) -> str:
    kind, _, name = anchor.strip().partition(" ")
    return name or kind


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None
