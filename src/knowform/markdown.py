"""Fence-aware structural parsing of markdown: headings and blocks.

Shared by the out-of-band binding resolver (heading anchors) and, in principle,
`init` discovery. A `#` inside a fenced code block or the leading YAML
frontmatter is not a heading; blank lines separate blocks; a fenced code block
is a single block.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE = re.compile(r"^\s*(?:```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FRONTMATTER_DELIM = re.compile(r"^---\s*$")


@dataclass(frozen=True)
class Heading:
    level: int          # 1-6 (number of leading `#`)
    text: str           # trimmed heading text
    line: int           # 1-based line of the heading


def _content_start(lines: list[str]) -> int:
    """0-based index of the first line after a leading YAML frontmatter block
    (0 when there is none)."""
    if lines and _FRONTMATTER_DELIM.match(lines[0]):
        for j in range(1, len(lines)):
            if _FRONTMATTER_DELIM.match(lines[j]):
                return j + 1
    return 0


def headings(text: str) -> list[Heading]:
    """ATX headings outside fenced code and leading frontmatter, top-to-bottom."""
    lines = text.split("\n")
    out: list[Heading] = []
    in_fence = False
    for i in range(_content_start(lines), len(lines)):
        line = lines[i]
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            m = _HEADING.match(line)
            if m:
                out.append(Heading(len(m.group(1)), m.group(2).strip(), i + 1))
    return out


def blocks(lines: list[str], start: int, end: int) -> list[tuple[int, int]]:
    """1-based inclusive block spans within [start, end]: blank-line-separated
    runs, a fenced code block counted as a single block."""
    out: list[tuple[int, int]] = []
    i = start
    while i <= end:
        if not lines[i - 1].strip():
            i += 1
            continue
        if _FENCE.match(lines[i - 1]):
            j = i + 1
            while j <= end and not _FENCE.match(lines[j - 1]):
                j += 1
            out.append((i, min(j, end)))
            i = j + 1
            continue
        j = i
        while j <= end and lines[j - 1].strip() and not _FENCE.match(lines[j - 1]):
            j += 1
        out.append((i, j - 1))
        i = j
    return out
