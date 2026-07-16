"""`knowform init --write`: materialize an accepted proposal - OUT OF BAND.

Reads a (human-reviewed) `knowform.init.json` and records every binding in
`knowform.bindings.json`. It NEVER writes into the docs themselves, so a
materialized doc stays pristine prose (no frontmatter, no fences) - important
when docs are fed to LLMs/agents as context.

- markdown candidates -> a `markdown` manifest entry anchored by heading path
  (+ block ordinal), re-resolved structurally on every run.
- docstring candidates -> a `docstrings` manifest entry.

A markdown region not addressable by a heading (e.g. preamble before the first
heading, or an anchor that would resolve ambiguously) is reported
`unanchorable`, never silently bound.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .init import Candidate, Proposal
from .manifest import MANIFEST
from .regions import Region, derive_heading_anchor, resolve_heading_region


@dataclass
class MaterializeResult:
    manifest_entries: list[str] = field(default_factory=list)  # added bindings
    unanchorable: list[str] = field(default_factory=list)      # no heading anchor
    skipped: list[str] = field(default_factory=list)           # path escapes


def materialize(root: Path, proposal: Proposal) -> MaterializeResult:
    root = Path(root).resolve()
    result = MaterializeResult()

    md_entries: list[dict] = []
    markdown = sorted((c for c in proposal.candidates if c.kind == "markdown"),
                      key=lambda c: (c.doc_path, c.doc_region[0]))
    for c in markdown:
        target = root / c.doc_path
        if not _within(root, target) or target.is_symlink():
            result.skipped.append(c.doc_path)
            continue
        anchor = _anchor_for(root, c)
        if anchor is None:
            result.unanchorable.append(
                f"{c.doc_path}:{c.doc_region[0]} ({c.code_anchor})")
            continue
        heading, block = anchor
        entry = {"doc": c.doc_path, "heading": list(heading),
                 "governs": c.governs, "code_anchor": c.code_anchor,
                 "direction": c.direction}
        if block is not None:
            entry["block"] = block
        md_entries.append(entry)

    docstrings = [c for c in proposal.candidates if c.kind == "docstring"]
    result.manifest_entries = _write_manifest(root, docstrings, md_entries)
    return result


def _anchor_for(root: Path, c: Candidate):
    """Derive the heading anchor for a candidate and verify it round-trips to
    the same region - else it is not safely addressable out-of-band."""
    region = Region(Path(c.doc_path), c.doc_region[0], c.doc_region[1])
    anchor = derive_heading_anchor(root, Path(c.doc_path), region)
    if anchor is None:
        return None
    heading, block = anchor
    back = resolve_heading_region(root, Path(c.doc_path), heading, block)
    if (back.error is not None or back.region is None
            or (back.region.start, back.region.end) != c.doc_region):
        return None
    return anchor


def _write_manifest(root: Path, docstrings: list[Candidate],
                    md_entries: list[dict]) -> list[str]:
    f = root / MANIFEST
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []  # malformed manifest: leave it for the human to fix
        if not isinstance(data, dict):
            return []
    else:
        data = {"version": 1}

    added: list[str] = []

    ds = data.setdefault("docstrings", [])
    seen_ds = {(d.get("governs"), d.get("symbol")) for d in ds
               if isinstance(d, dict)}
    for c in docstrings:
        if not _within(root, root / c.governs):
            continue
        key = (c.governs, c.symbol)
        if key in seen_ds:
            continue
        seen_ds.add(key)
        ds.append({"governs": c.governs, "symbol": c.symbol,
                   "direction": c.direction})
        added.append(c.symbol)

    md = data.setdefault("markdown", [])
    seen_md = {_md_key(m) for m in md if isinstance(m, dict)}
    for entry in md_entries:
        key = _md_key(entry)
        if key in seen_md:
            continue
        seen_md.add(key)
        md.append(entry)
        added.append(f"{entry['doc']}#{'/'.join(entry['heading'])}")

    if added:
        f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return added


def _md_key(m: dict) -> tuple:
    return (m.get("doc"), tuple(m.get("heading", [])), m.get("block"),
            m.get("governs"), m.get("code_anchor"))


def _within(root: Path, p: Path) -> bool:
    try:
        return p.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
