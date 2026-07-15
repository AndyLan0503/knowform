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
import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .frontmatter import Direction, parse_frontmatter
from .judge import Matcher, MatchInput, MatchResult
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
    category: str = "stale-ref"     # ambiguous | stale-ref | example
    context: str = "inline"         # inline | fenced

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "category": self.category,
            "context": self.context,
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


def init(root: Path, matcher: "Matcher | None" = None) -> Proposal:
    """Discover candidate bindings. Read-only: resolves regions and hashes
    nothing to disk (the caller `write_proposal`s the artifact separately).

    Deterministic by default (Tiers 0-1). An injected `matcher` adds Tier 2:
    the LLM disambiguates references the deterministic pass left as ambiguous
    `unmatched`. With no matcher, zero tokens are spent."""
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
    proposal.candidates = candidates
    proposal.unmatched = unmatched
    if matcher is not None:
        _llm_resolve(root, proposal, table, matcher)
    proposal.candidates = sorted(proposal.candidates, key=_candidate_key)
    proposal.unmatched = sorted(proposal.unmatched, key=_unmatched_key)
    return proposal


def write_proposal(root: Path, proposal: Proposal) -> Path:
    out = Path(root) / INIT_PROPOSAL
    out.write_text(json.dumps(proposal.to_dict(), indent=2) + "\n",
                   encoding="utf-8")
    return out


def read_proposal(root: Path) -> Proposal | None:
    """Load a (possibly human-edited) `knowform.init.json` back into a Proposal.
    None when the artifact is absent - the materializer refuses rather than
    guessing. Unknown/extra keys are ignored so a hand-reviewed file survives."""
    f = Path(root) / INIT_PROPOSAL
    if not f.exists():
        return None
    data = json.loads(f.read_text(encoding="utf-8"))
    proposal = Proposal()
    for c in data.get("candidates", []):
        proposal.candidates.append(Candidate(
            kind=c["kind"], governs=c["governs"], doc_path=c["doc_path"],
            doc_region=(c["doc_region"][0], c["doc_region"][1]),
            direction=c["direction"], confidence=c["confidence"],
            rationale=c["rationale"], source_tier=c["source_tier"],
            symbol=c.get("symbol"), code_anchor=c.get("code_anchor")))
    for u in data.get("unmatched", []):
        proposal.unmatched.append(Unmatched(
            kind=u["kind"], doc_path=u["doc_path"],
            doc_region=(u["doc_region"][0], u["doc_region"][1]),
            identifier=u["identifier"], match_count=u["match_count"],
            reason=u["reason"], category=u.get("category", "stale-ref"),
            context=u.get("context", "inline")))
    return proposal


def _candidate_key(c: Candidate) -> tuple:
    return (c.kind, c.doc_path, c.doc_region[0], c.doc_region[1], c.governs,
            c.symbol or c.code_anchor or "")


_CATEGORY_RANK = {"ambiguous": 0, "stale-ref": 1, "example": 2}


def _unmatched_key(u: Unmatched) -> tuple:
    # Actionable first (ambiguous, stale-ref), illustrative examples last.
    return (_CATEGORY_RANK.get(u.category, 3), u.doc_path,
            u.doc_region[0], u.doc_region[1], u.identifier)


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
            if node.name.startswith("_"):
                continue  # internal helper/dunder: not load-bearing public doc
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

_FENCE = re.compile(r"^\s*(?:```|~~~)\s*([A-Za-z0-9_+-]*)")

# Fenced blocks in these languages are documentation illustrations, not Python
# API references - a 0-match call inside them is an example, not stale code.
# Untagged and code-language (```python) fences stay reviewable as `stale-ref`.
_EXAMPLE_LANGS = {"markdown", "md", "yaml", "yml", "toml", "json", "bash",
                  "sh", "shell", "console", "text", "txt", "ini", "cfg",
                  "diff", "html", "xml"}
_INLINE = re.compile(r"`([^`]+)`")
_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\(")  # tight: no space before (
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
        by_region: dict[tuple[int, int],
                        list[tuple[str, bool, str | None]]] = {}
        for line_no, name, strong, fence_lang in _references(lines):
            by_region.setdefault(_paragraph_for(line_no, paras), []).append(
                (name, strong, fence_lang))
        for span, refs in sorted(by_region.items()):
            resolved: dict[str, tuple[_Symbol, str]] = {}
            flagged: list[tuple[str, int, str | None]] = []
            seen_flag: set[str] = set()
            for name, strong, fence_lang in refs:
                matches = table.get(name, [])
                if len(matches) == 1:
                    sym = matches[0]
                    if (sym.file, _bare(sym.anchor)) in bound:
                        continue
                    resolved.setdefault(sym.anchor, (sym, name))
                elif (len(matches) > 1 or strong) and name not in seen_flag:
                    # >1: genuinely ambiguous. 0 + call-shaped: looks like a
                    # call to an unknown symbol - worth review, never bound.
                    seen_flag.add(name)
                    flagged.append((name, len(matches), fence_lang))
            # Invariant: a doc region maps to AT MOST ONE candidate. A region
            # naming exactly one symbol binds it; a multi-symbol region (dense
            # prose or a code fence) binds nothing rather than exploding.
            if len(resolved) == 1:
                anchor, (sym, name) = next(iter(resolved.items()))
                candidates.append(Candidate(
                    kind="markdown", governs=sym.file, doc_path=str(doc),
                    doc_region=span, direction=Direction.CODE_IS_TRUTH.value,
                    confidence="high",
                    rationale=f"`{name}` resolves to {sym.anchor} "
                              f"in {sym.file}",
                    source_tier=0, code_anchor=sym.anchor))
            else:
                for name, count, fence_lang in flagged:
                    category, context = _classify_unmatched(count, fence_lang)
                    unmatched.append(Unmatched(
                        kind="markdown", doc_path=str(doc), doc_region=span,
                        identifier=name, match_count=count,
                        reason=_UNMATCHED_REASON[category],
                        category=category, context=context))
    return candidates, unmatched


_UNMATCHED_REASON = {
    "ambiguous": "ambiguous: names multiple symbols",
    "stale-ref": "references a symbol not found in the code",
    "example": "illustrative reference inside a non-code fenced block",
}


def _classify_unmatched(count: int, fence_lang: str | None) -> tuple[str, str]:
    """Bucket a flagged reference. `ambiguous` (>1 symbol) and `stale-ref`
    (0 symbols, possibly drift) are actionable; a 0-match call inside a non-code
    fenced block is an `example` (documentation illustration, not a binding)."""
    context = "fenced" if fence_lang is not None else "inline"
    if count > 1:
        return "ambiguous", context
    if fence_lang is not None and fence_lang in _EXAMPLE_LANGS:
        return "example", context
    return "stale-ref", context


def _references(lines: list[str]) -> list[tuple[int, str, bool, str | None]]:
    """(1-based line, identifier, strong, fence_lang) references. `strong` marks
    a call-shaped `name(` token; `fence_lang` is the lowercased language tag when
    the reference sits in a fenced block, else None (inline prose backtick). Only
    inline-code backticks and fenced code blocks contribute; plain running prose
    contributes nothing, so a prose parenthetical like "the design (no-LLM)" is
    never read as a call. Leading-underscore names are dropped as internal."""
    out: list[tuple[int, str, bool, str | None]] = []
    fence_lang: str | None = None   # None = outside any fence
    for i, line in enumerate(lines, start=1):
        m = _FENCE.match(line)
        if m:
            fence_lang = ((m.group(1) or "").lower()
                          if fence_lang is None else None)
            continue
        if fence_lang is not None:
            for c in _CALL.finditer(line):
                base = _tail(c.group(1))
                if not base.startswith("_"):
                    out.append((i, base, True, fence_lang))
            continue
        for span in _INLINE.findall(line):
            tok = span.strip()
            strong = bool(_CALL.match(tok))
            base = _tail(tok.split("(")[0])
            if _IDENT.match(base) and not base.startswith("_"):
                out.append((i, base, strong, None))
    return out


def _tail(name: str) -> str:
    return name.split(".")[-1]


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


# --- Tier 2: LLM disambiguation ----------------------------------------------

def _llm_resolve(root: Path, proposal: Proposal,
                 table: dict[str, list[_Symbol]], matcher: Matcher) -> None:
    """Ask the matcher to bind the `unmatched` references it can. An `ambiguous`
    reference is disambiguated among the symbols sharing its name; a `stale-ref`
    is fuzzy-matched against a shortlist of similarly-named symbols (a likely
    rename); `example` references are illustrations and never bound. A confident,
    non-hallucinated pick becomes a Tier-2 candidate; everything else stays
    unmatched. Precision holds: one candidate per region, never doc-is-truth,
    never a symbol outside the presented set."""
    taken = {(c.doc_path, c.doc_region) for c in proposal.candidates}
    kept: list[Unmatched] = []
    for u in proposal.unmatched:
        if u.category == "ambiguous":
            options = table.get(u.identifier, [])
        elif u.category == "stale-ref":
            options = _shortlist(u.identifier, table)
        else:  # example: an illustration, not a binding
            options = []
        if not options or (u.doc_path, u.doc_region) in taken:
            kept.append(u)
            continue
        result = matcher(MatchInput(
            doc_path=u.doc_path,
            region_text=_region_text(root, u.doc_path, u.doc_region),
            identifier=u.identifier,
            candidates=[(s.anchor, s.file) for s in options]))
        chosen = _accept_match(result, options)
        if chosen is None:
            kept.append(u)
            continue
        sym, direction = chosen
        taken.add((u.doc_path, u.doc_region))
        verb = "disambiguated" if u.category == "ambiguous" else "fuzzy-matched"
        proposal.candidates.append(Candidate(
            kind="markdown", governs=sym.file, doc_path=u.doc_path,
            doc_region=u.doc_region, direction=direction,
            confidence="medium" if u.category == "ambiguous" else "low",
            rationale=result.rationale or
            f"LLM {verb} `{u.identifier}` -> {sym.anchor}",
            source_tier=2, code_anchor=sym.anchor))
    proposal.unmatched = kept


def _shortlist(identifier: str, table: dict[str, list[_Symbol]],
               limit: int = 5, cutoff: float = 0.6) -> list[_Symbol]:
    """Plausible real symbols for a 0-match reference: the closest symbol names
    by string similarity. Bounded, so the matcher only ever picks from a small
    real set (never invents). Empty when nothing is close enough - a genuinely
    removed symbol then stays a `stale-ref` for a human rather than being
    force-bound."""
    names = difflib.get_close_matches(identifier, list(table.keys()),
                                      n=limit, cutoff=cutoff)
    out: list[_Symbol] = []
    for name in names:
        out.extend(table.get(name, []))
    return out


def _accept_match(result: MatchResult, options: list[_Symbol]
                  ) -> tuple[_Symbol, str] | None:
    """Validate an LLM match against the presented options. Returns the chosen
    symbol and a clamped direction, or None to reject (unmatched, no match, or
    a hallucinated symbol)."""
    if not result.matched:
        return None
    for sym in options:
        if sym.anchor == result.code_anchor and sym.file == result.governs:
            # Never auto-assign doc-is-truth: a spec is a human declaration.
            direction = (Direction.MANUAL.value
                         if result.direction != Direction.CODE_IS_TRUTH.value
                         else Direction.CODE_IS_TRUTH.value)
            return sym, direction
    return None


def _region_text(root: Path, doc_path: str, span: tuple[int, int]) -> str:
    lines = (root / doc_path).read_text(encoding="utf-8").split("\n")
    return "\n".join(lines[span[0] - 1:span[1]])


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
