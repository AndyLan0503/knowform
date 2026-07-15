"""`knowform.bindings.json`: a central binding source for regions with no
frontmatter host. Python docstrings today - a `.py` cannot carry `knowform:`
frontmatter, so a docstring binding is declared here.

JSON (not TOML) to keep `requires-python >=3.10` and stay consistent with
`knowform.lock` and the init proposal artifact. Docstring bindings default to
`code-is-truth` (the doc describes the code); direction is never inferred to
`doc-is-truth`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .frontmatter import Direction

MANIFEST = "knowform.bindings.json"


@dataclass(frozen=True)
class DocstringBinding:
    governs: str            # the `.py` file (or glob) hosting the symbol
    symbol: str             # code anchor: `def name`, `class name`, or `name`
    direction: Direction


@dataclass(frozen=True)
class MarkdownBinding:
    """An out-of-band markdown binding: a doc region addressed by heading path
    (+ optional 1-based block) governs a code symbol - no inline anchors."""
    doc: str
    heading: tuple[str, ...]
    governs: str
    code_anchor: str | None
    direction: Direction
    block: int | None = None


@dataclass
class Manifest:
    docstrings: list[DocstringBinding] = field(default_factory=list)
    markdown: list[MarkdownBinding] = field(default_factory=list)
    error: str | None = None


def path(root: Path) -> Path:
    return Path(root) / MANIFEST


def load(root: Path) -> Manifest | None:
    """Return the parsed manifest, None when absent, or a Manifest carrying
    `error` on malformed input (surfaced by the plan, never guessed around)."""
    f = path(root)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return Manifest(error=f"malformed {MANIFEST}: {e}")
    if not isinstance(data, dict):
        return Manifest(error=f"malformed {MANIFEST}: top level must be an object")

    out: list[DocstringBinding] = []
    for raw in data.get("docstrings", []):
        governs = raw.get("governs")
        symbol = raw.get("symbol")
        if not governs or not symbol:
            return Manifest(
                error="docstring binding missing governs or symbol")
        raw_dir = raw.get("direction", Direction.CODE_IS_TRUTH.value)
        try:
            direction = Direction(raw_dir)
        except ValueError:
            return Manifest(error=f"unknown direction: {raw_dir!r}")
        out.append(DocstringBinding(governs, symbol, direction))

    md: list[MarkdownBinding] = []
    for raw in data.get("markdown", []):
        doc = raw.get("doc")
        heading = raw.get("heading")
        governs = raw.get("governs")
        if not doc or not heading or not governs:
            return Manifest(
                error="markdown binding missing doc, heading, or governs")
        if (not isinstance(heading, list)
                or not all(isinstance(h, str) for h in heading)):
            return Manifest(
                error="markdown binding `heading` must be a list of strings")
        block = raw.get("block")
        if block is not None and not isinstance(block, int):
            return Manifest(error="markdown binding `block` must be an integer")
        raw_dir = raw.get("direction", Direction.CODE_IS_TRUTH.value)
        try:
            direction = Direction(raw_dir)
        except ValueError:
            return Manifest(error=f"unknown direction: {raw_dir!r}")
        md.append(MarkdownBinding(
            doc=doc, heading=tuple(heading), governs=governs,
            code_anchor=raw.get("code_anchor"), direction=direction,
            block=block))
    return Manifest(docstrings=out, markdown=md)
