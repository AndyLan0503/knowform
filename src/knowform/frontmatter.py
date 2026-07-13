"""Parse the narrow `knowform:` frontmatter and anchor fences.

Stdlib-only: a tolerant hand-written parser for this one fixed shape, not a
general YAML engine (see the design notes - stdlib-first). A `.md` with no
`knowform:` frontmatter is unmanaged and ignored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

FRONTMATTER = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


class Direction(str, Enum):
    CODE_IS_TRUTH = "code-is-truth"
    DOC_IS_TRUTH = "doc-is-truth"
    MANUAL = "manual"


@dataclass(frozen=True)
class Binding:
    doc_anchor: str
    governs: str
    code_anchor: str | None = None


@dataclass
class ManagedDoc:
    """A parsed managed doc. `error` set (and `direction` None) on a hard
    error the plan must surface rather than guess around."""
    direction: Direction | None
    bindings: list[Binding] = field(default_factory=list)
    error: str | None = None


def _strip(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    # Drop a trailing inline comment only when `#` follows whitespace, so a
    # `#` inside an unquoted scalar (e.g. `a#b.py`) is preserved.
    comment = re.search(r"\s#", value)
    if comment:
        value = value[:comment.start()]
    return value.strip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_frontmatter(text: str) -> ManagedDoc | None:
    """Return the ManagedDoc, or None if the doc carries no `knowform:` block.

    A managed doc missing `direction` is a hard error (never inferred).
    """
    match = FRONTMATTER.match(text)
    if not match:
        return None
    block = match.group(1)
    lines = [ln.rstrip() for ln in block.splitlines()]

    # Find the `knowform:` top-level key and its indented body.
    body: list[str] = []
    in_knowform = False
    base_indent = 0
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            if in_knowform:
                body.append(line)
            continue
        if not in_knowform:
            if re.match(r"^knowform\s*:", line):
                in_knowform = True
                base_indent = _indent(line)
            continue
        if _indent(line) <= base_indent:
            break
        body.append(line)

    if not in_knowform:
        return None

    direction: Direction | None = None
    bindings: list[Binding] = []
    current: dict[str, str] | None = None
    in_bindings = False

    def flush() -> str | None:
        nonlocal current
        if current is None:
            return None
        anchor = current.get("doc_anchor")
        governs = current.get("governs")
        if not anchor or not governs:
            current = None
            return "binding missing doc_anchor or governs"
        bindings.append(Binding(
            doc_anchor=anchor,
            governs=governs,
            code_anchor=current.get("code_anchor") or None,
        ))
        current = None
        return None

    for line in body:
        if not line.strip():
            continue
        stripped = line.strip()
        key_match = re.match(r"^(-\s*)?([\w]+)\s*:\s*(.*)$", stripped)

        if re.match(r"^bindings\s*:", stripped):
            in_bindings = True
            continue
        if re.match(r"^direction\s*:", stripped):
            err = flush()
            if err:
                return ManagedDoc(direction=None, error=err)
            in_bindings = False
            raw = _strip(stripped.split(":", 1)[1])
            try:
                direction = Direction(raw)
            except ValueError:
                return ManagedDoc(
                    direction=None,
                    error=f"unknown direction: {raw!r}",
                )
            continue

        if in_bindings and key_match:
            is_item = stripped.startswith("-")
            key, value = key_match.group(2), _strip(key_match.group(3))
            if is_item:
                err = flush()
                if err:
                    return ManagedDoc(direction=None, error=err)
                current = {}
            if current is not None:
                current[key] = value

    err = flush()
    if err:
        return ManagedDoc(direction=None, error=err)

    if direction is None:
        return ManagedDoc(
            direction=None,
            error="managed doc missing `direction` in knowform frontmatter",
        )
    return ManagedDoc(direction=direction, bindings=bindings)


def fence_span(text: str, anchor: str) -> tuple[int, int] | None:
    """1-based inclusive [start, end] line span between the anchor's fences.

    Returns None when the fences are absent (caller degrades to whole-doc).
    The span covers the prose BETWEEN the fences, excluding the fence lines.
    """
    start_re = re.compile(
        rf"<!--\s*knowform:{re.escape(anchor)}:start\s*-->")
    end_re = re.compile(
        rf"<!--\s*knowform:{re.escape(anchor)}:end\s*-->")
    start = end = None
    for i, line in enumerate(text.splitlines(), start=1):
        if start is None and start_re.search(line):
            start = i
        elif start is not None and end_re.search(line):
            end = i
            break
    if start is None or end is None:
        return None
    if end - start <= 1:
        return (start + 1, start)  # empty region
    return (start + 1, end - 1)


def replace_fenced_region(text: str, anchor: str, new_inner: str) -> str | None:
    """Replace the lines BETWEEN the anchor's fences with `new_inner`, leaving
    the fence lines and the rest of the doc intact. Returns None when the
    fences are absent - apply must never write a region it cannot target."""
    start_re = re.compile(
        rf"<!--\s*knowform:{re.escape(anchor)}:start\s*-->")
    end_re = re.compile(
        rf"<!--\s*knowform:{re.escape(anchor)}:end\s*-->")
    lines = text.split("\n")
    start = end = None
    for i, line in enumerate(lines):
        if start is None and start_re.search(line):
            start = i
        elif start is not None and end_re.search(line):
            end = i
            break
    if start is None or end is None:
        return None
    lines[start + 1:end] = new_inner.split("\n") if new_inner else []
    return "\n".join(lines)
