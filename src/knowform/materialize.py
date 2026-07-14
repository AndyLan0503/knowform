"""`knowform init --write`: materialize an accepted proposal.

Reads a (human-reviewed) `knowform.init.json` and writes the bindings it
declares - never re-discovers, never guesses. Two sinks, mirroring the two
discovery sources:

- markdown candidates -> the doc gains `knowform:` frontmatter plus anchor
  fences wrapping each governed paragraph (the same shape `plan`/`apply`
  read). A doc already carrying `knowform:` frontmatter is left untouched.
- docstring candidates -> `knowform.bindings.json` gains a docstring entry,
  deduped against whatever is already declared there.

Read-only over code: the only writes are managed `.md` docs and the manifest.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .frontmatter import FRONTMATTER, parse_frontmatter
from .init import Candidate, Proposal, _bare
from .manifest import MANIFEST


@dataclass
class MaterializeResult:
    docs_written: list[str] = field(default_factory=list)   # newly-managed .md
    manifest_entries: list[str] = field(default_factory=list)  # symbols added
    skipped: list[str] = field(default_factory=list)        # already managed


def materialize(root: Path, proposal: Proposal) -> MaterializeResult:
    root = Path(root).resolve()
    result = MaterializeResult()

    by_doc: dict[str, list[Candidate]] = {}
    for c in proposal.candidates:
        if c.kind == "markdown":
            by_doc.setdefault(c.doc_path, []).append(c)
    for doc_path, cands in sorted(by_doc.items()):
        target = root / doc_path
        if not _within(root, target) or target.is_symlink():
            result.skipped.append(doc_path)
            continue
        anchors = _materialize_markdown(root, Path(doc_path), cands)
        (result.docs_written if anchors is not None
         else result.skipped).append(doc_path)

    docstrings = [c for c in proposal.candidates if c.kind == "docstring"]
    result.manifest_entries = _materialize_manifest(root, docstrings)
    return result


# --- markdown: frontmatter + fences ------------------------------------------

def _materialize_markdown(root: Path, doc_path: Path,
                          cands: list[Candidate]) -> list[str] | None:
    """Wrap each candidate's paragraph in anchor fences and add a `knowform:`
    frontmatter binding. Returns the assigned anchors, or None (skip) when the
    doc is already managed. Fences insert bottom-up so earlier line spans stay
    valid; frontmatter is injected last so the region line numbers it never
    references stay irrelevant."""
    full = root / doc_path
    text = full.read_text(encoding="utf-8")
    if parse_frontmatter(text) is not None:
        return None  # already managed -> never re-wrap

    used: set[str] = set()
    assigned: list[tuple[Candidate, str]] = []
    for c in sorted(cands, key=lambda c: c.doc_region[0]):
        anchor = _unique(_bare(c.code_anchor or c.symbol or "region"), used)
        used.add(anchor)
        assigned.append((c, anchor))

    lines = text.split("\n")
    for c, anchor in sorted(assigned, key=lambda ca: ca[0].doc_region[0],
                            reverse=True):
        start, end = c.doc_region
        lines.insert(end, f"<!-- knowform:{anchor}:end -->")
        lines.insert(start - 1, f"<!-- knowform:{anchor}:start -->")
    text = "\n".join(lines)

    direction = assigned[0][0].direction
    block = _frontmatter_block(direction, assigned)
    full.write_text(_inject_frontmatter(text, block), encoding="utf-8")
    return [a for _, a in assigned]


def _frontmatter_block(direction: str,
                       assigned: list[tuple[Candidate, str]]) -> list[str]:
    lines = ["knowform:", f"  direction: {direction}", "  bindings:"]
    for c, anchor in assigned:
        lines.append(f"    - doc_anchor: {anchor}")
        lines.append(f"      governs: {c.governs}")
        lines.append(f"      code_anchor: {c.code_anchor}")
    return lines


def _inject_frontmatter(text: str, block: list[str]) -> str:
    """Insert the `knowform:` block into existing frontmatter, or prepend a
    fresh block when the doc has none - so a doc already carrying (non-knowform)
    frontmatter keeps its keys."""
    if FRONTMATTER.match(text):
        lines = text.split("\n")
        lines[1:1] = block  # right after the opening `---`
        return "\n".join(lines)
    return "---\n" + "\n".join(block) + "\n---\n" + text


def _unique(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    i = 2
    while f"{base}-{i}" in used:
        i += 1
    return f"{base}-{i}"


# --- docstrings: manifest ----------------------------------------------------

def _materialize_manifest(root: Path,
                          docstrings: list[Candidate]) -> list[str]:
    f = root / MANIFEST
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []  # malformed manifest: leave it for the human to fix
        if not isinstance(data, dict):
            return []
    else:
        data = {"version": 1, "docstrings": []}

    entries = data.setdefault("docstrings", [])
    seen = {(d.get("governs"), d.get("symbol")) for d in entries
            if isinstance(d, dict)}
    added: list[str] = []
    for c in docstrings:
        if not _within(root, root / c.governs):
            continue
        key = (c.governs, c.symbol)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"governs": c.governs, "symbol": c.symbol,
                        "direction": c.direction})
        added.append(c.symbol)
    if added:
        f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return added


def _within(root: Path, p: Path) -> bool:
    try:
        return p.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
