"""Resolve doc/code regions to normalized, hashed text spans.

A binding's doc region comes from a markdown heading anchor or a Python
docstring span; its code region from a stdlib-`ast` symbol lookup (or whole
file). Precision over recall: an unresolved code anchor widens to the whole
file, never narrows silently (the design notes).
"""
from __future__ import annotations

import ast
import glob
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .markdown import blocks as _md_blocks, headings as _md_headings


@dataclass(frozen=True)
class Region:
    """A resolved text region: file plus 1-based inclusive line span.

    `whole` marks a file-level degrade (an unresolved code/docstring anchor
    widens to the whole file). `exclude` carves a 1-based inclusive line
    sub-span out of the region (a docstring binding's code region is the symbol
    MINUS its docstring lines).
    """
    path: Path
    start: int
    end: int
    whole: bool = False
    exclude: tuple[int, int] | None = None

    def text(self, root: Path) -> str:
        lines = _read_lines(root / self.path)
        numbers = range(self.start, self.end + 1)
        if self.exclude is not None:
            lo, hi = self.exclude
            numbers = [n for n in numbers if not (lo <= n <= hi)]
        return "\n".join(lines[n - 1] for n in numbers)


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def normalize(span: str) -> str:
    """Normalize a text span before hashing: LF line endings, strip trailing
    per-line whitespace, drop a leading/trailing blank-line run."""
    lines = [ln.rstrip() for ln in span.replace("\r\n", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def hash_span(span: str) -> str:
    digest = hashlib.sha256(normalize(span).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class DocAnchorResult:
    """A resolved out-of-band doc region, or an `error` the plan must surface
    (heading not found / ambiguous / block out of range) - never a silent
    whole-file degrade, since an out-of-band anchor is an explicit human choice."""
    region: Region | None
    error: str | None = None


def _ancestors(hs: list, idx: int) -> list:
    """The nested-parent chain of heading `idx` (outermost first): the closest
    preceding heading of each strictly-smaller level."""
    chain: list = []
    level = hs[idx].level
    for k in range(idx - 1, -1, -1):
        if hs[k].level < level:
            chain.append(hs[k])
            level = hs[k].level
    chain.reverse()
    return chain


def _match_heading_path(hs: list, path: tuple[str, ...]):
    """The unique heading whose full parent-path ends with `path`, or "none" /
    "ambiguous"."""
    matches = []
    for idx, h in enumerate(hs):
        if h.text != path[-1]:
            continue
        full = [a.text for a in _ancestors(hs, idx)] + [h.text]
        if full[-len(path):] == list(path):
            matches.append(h)
    if not matches:
        return "none"
    if len(matches) > 1:
        return "ambiguous"
    return matches[0]


def _trim_blanks(lines: list[str], start: int, end: int) -> tuple[int, int]:
    while start <= end and not lines[start - 1].strip():
        start += 1
    while end >= start and not lines[end - 1].strip():
        end -= 1
    return start, end


def resolve_heading_region(root: Path, doc_path: Path,
                           heading: tuple[str, ...],
                           block: int | None = None) -> DocAnchorResult:
    """Resolve an out-of-band markdown anchor (heading path, optional 1-based
    block) to an exact line span, recomputed each run so edits elsewhere never
    misalign it. Failures surface as errors, never guesses."""
    what = "/".join(heading) + (f"#{block}" if block else "")
    try:
        text = (root / doc_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return DocAnchorResult(None, f"cannot read {doc_path}")
    if not heading:
        return DocAnchorResult(None, f"empty heading path for {doc_path}")

    hs = _md_headings(text)
    match = _match_heading_path(hs, tuple(heading))
    if match == "none":
        return DocAnchorResult(None, f"heading {what!r} not found in {doc_path}")
    if match == "ambiguous":
        return DocAnchorResult(None, f"heading {what!r} is ambiguous in {doc_path}")

    lines = text.split("\n")
    end = len(lines)
    for h in hs:
        if h.line > match.line and h.level <= match.level:
            end = h.line - 1
            break
    start, end = _trim_blanks(lines, match.line + 1, end)
    if start > end:
        return DocAnchorResult(None, f"section {what!r} is empty in {doc_path}")

    if block is not None:
        blks = _md_blocks(lines, start, end)
        if block < 1 or block > len(blks):
            return DocAnchorResult(
                None, f"block {block} out of range under {what!r} in {doc_path}")
        start, end = blks[block - 1]
    return DocAnchorResult(Region(doc_path, start, end))


def derive_heading_anchor(root: Path, doc_path: Path, region: Region):
    """The `(heading, block)` anchor that resolves to `region` - the inverse of
    `resolve_heading_region`, used to materialize a binding out-of-band. Returns
    None when the region is not under any heading (preamble), i.e. not
    heading-addressable."""
    try:
        text = (root / doc_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.split("\n")
    hs = _md_headings(text)
    containing = None
    ci = -1
    for idx, h in enumerate(hs):
        if h.line < region.start:
            containing, ci = h, idx
        else:
            break
    if containing is None:
        return None
    heading = tuple(a.text for a in _ancestors(hs, ci)) + (containing.text,)
    end = len(lines)
    for h in hs:
        if h.line > containing.line and h.level <= containing.level:
            end = h.line - 1
            break
    start, end = _trim_blanks(lines, containing.line + 1, end)
    blks = _md_blocks(lines, start, end)
    if len(blks) <= 1:
        return heading, None
    for i, (bs, be) in enumerate(blks, start=1):
        if bs <= region.start <= be:
            return heading, i
    return heading, None


@dataclass(frozen=True)
class GovernedFile:
    """One resolved governed target: either a repo-relative `path`, or an
    `error` when the `governs` value is malformed or escapes root."""
    path: Path | None
    error: str | None = None


def _contained(root: Path, candidate: Path) -> bool:
    """True iff `candidate` resolves to a path inside `root` (symlinks
    followed). Contains resolution to the repo (precision +
    path-containment)."""
    try:
        resolved = candidate.resolve()
        return resolved.is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def resolve_governed_files(root: Path, governs: str) -> list[GovernedFile]:
    """`governs` is a file or glob relative to root. Returns matching files
    (repo-relative), or the literal path (even if missing) so the plan can
    surface it. An absolute / `..` / symlink-escaping `governs` is contained
    to root: it surfaces as an `error` entry rather than crashing or reading
    outside the repo."""
    root = root.resolve()
    if Path(governs).is_absolute():
        return [GovernedFile(None, error=f"governs escapes root: {governs!r}")]

    matches = sorted(glob.glob(str(root / governs), recursive=True))
    if not matches:
        # Literal (possibly missing) path, still contained to root.
        literal = Path(governs)
        if not _contained(root, root / literal) or literal.is_symlink():
            return [GovernedFile(
                None, error=f"governs escapes root: {governs!r}")]
        return [GovernedFile(literal)]

    out: list[GovernedFile] = []
    for m in matches:
        mp = Path(m)
        if mp.is_symlink() or not _contained(root, mp):
            out.append(GovernedFile(
                None, error=f"governs escapes root: {m!r}"))
            continue
        try:
            out.append(GovernedFile(mp.resolve().relative_to(root)))
        except ValueError:
            out.append(GovernedFile(
                None, error=f"governs escapes root: {m!r}"))
    return out


def resolve_code_region(root: Path, file_path: Path,
                        code_anchor: str | None) -> Region:
    """Resolve a code_anchor to a symbol's line span via `ast`; degrade to the
    whole file for non-Python, missing files, or unresolved anchors."""
    full = root / file_path
    try:
        line_count = len(_read_lines(full)) or 1
    except (OSError, UnicodeDecodeError):
        return Region(file_path, 1, 1, whole=True)

    whole = Region(file_path, 1, line_count, whole=True)
    if not code_anchor or full.suffix != ".py":
        return whole

    node = _find_symbol(full, code_anchor)
    if node is None:
        return whole
    end = getattr(node, "end_lineno", node.lineno)
    return Region(file_path, symbol_start(node), end)


def symbol_start(node: ast.AST) -> int:
    """First line of a def/class *including* its decorators, so a decorator
    change falls inside the hashed/gated span."""
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        return min(d.lineno for d in decorators)
    return node.lineno


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None


def _find_symbol(path: Path, anchor: str):
    tree = _parse(path)
    if tree is None:
        return None
    return _find_symbol_in_tree(tree, anchor)


def _find_symbol_in_tree(tree: ast.Module, anchor: str):
    """Match `def name`, `class name`, or a bare `name` to a top-level (or
    nested) function/class definition."""
    kind, _, name = anchor.strip().partition(" ")
    if not name:
        name = kind
        kind = ""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        if node.name != name:
            continue
        if kind == "def" and isinstance(node, ast.ClassDef):
            continue
        if kind == "class" and not isinstance(node, ast.ClassDef):
            continue
        return node
    return None


def _docstring_span(node: ast.AST | None) -> tuple[int, int] | None:
    """1-based inclusive line span of `node`'s docstring, or None when the
    def/class/module has no docstring first statement."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
        return None
    body = getattr(node, "body", [])
    if not body:
        return None
    first = body[0]
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        return (first.lineno, getattr(first, "end_lineno", first.lineno))
    return None


def resolve_docstring_region(root: Path, file_path: Path,
                             symbol: str) -> Region:
    """The docstring's own line span (the doc side of a docstring binding).
    Degrades to whole-file for non-Python, a missing symbol, or a symbol with
    no docstring - the caller then treats it as unanchorable."""
    full = root / file_path
    try:
        line_count = len(_read_lines(full)) or 1
    except (OSError, UnicodeDecodeError):
        return Region(file_path, 1, 1, whole=True)
    whole = Region(file_path, 1, line_count, whole=True)
    if full.suffix != ".py":
        return whole
    span = _docstring_span(_find_symbol(full, symbol))
    if span is None:
        return whole
    return Region(file_path, span[0], span[1])


def resolve_docstring_code_region(root: Path, file_path: Path,
                                  symbol: str) -> Region:
    """The bound symbol's span MINUS its docstring lines, so the doc tracks
    behavior rather than the prose describing it. With no docstring to carve
    out, this is the plain symbol region."""
    region = resolve_code_region(root, file_path, symbol)
    if region.whole:
        return region
    doc = resolve_docstring_region(root, file_path, symbol)
    if doc.whole:
        return region
    return Region(region.path, region.start, region.end,
                  exclude=(doc.start, doc.end))


def replace_docstring(text: str, symbol: str, new_prose: str) -> str | None:
    """Replace `symbol`'s docstring with `new_prose`, preserving its
    indentation and leaving the rest of the file intact. Returns None when the
    symbol has no docstring to target - apply must never write blind."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    span = _docstring_span(_find_symbol_in_tree(tree, symbol))
    if span is None:
        return None
    lines = text.split("\n")
    start, end = span
    opener = lines[start - 1]
    indent = opener[:len(opener) - len(opener.lstrip())]
    lines[start - 1:end] = _format_docstring(new_prose, indent)
    return "\n".join(lines)


def _format_docstring(prose: str, indent: str) -> list[str]:
    body = [ln.rstrip() for ln in prose.strip().split("\n")]
    if len(body) == 1:
        return [f'{indent}"""{body[0]}"""']
    inner = [f"{indent}{ln}".rstrip() for ln in body]
    return [f'{indent}"""', *inner, f'{indent}"""']
