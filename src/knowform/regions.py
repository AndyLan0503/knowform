"""Resolve doc/code regions to normalized, hashed text spans.

A binding's doc region comes from anchor fences (or whole doc); its code
region from a stdlib-`ast` symbol lookup (or whole file). Precision over
recall: an unresolved code anchor widens to the whole file, never narrows
silently (the design notes).
"""
from __future__ import annotations

import ast
import glob
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .frontmatter import Binding, fence_span


@dataclass(frozen=True)
class Region:
    """A resolved text region: file plus 1-based inclusive line span.

    `whole` marks a file-level degrade (fences absent / anchor unresolved).
    """
    path: Path
    start: int
    end: int
    whole: bool = False

    def text(self, root: Path) -> str:
        lines = _read_lines(root / self.path)
        return "\n".join(lines[self.start - 1:self.end])


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


def resolve_doc_region(root: Path, doc_path: Path, binding: Binding) -> Region:
    text = (root / doc_path).read_text(encoding="utf-8")
    span = fence_span(text, binding.doc_anchor)
    line_count = len(text.splitlines()) or 1
    if span is None:
        return Region(doc_path, 1, line_count, whole=True)
    start, end = span
    return Region(doc_path, start, max(start, end))


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
    """Match `def name`, `class name`, or a bare `name` to a top-level (or
    nested) function/class definition."""
    tree = _parse(path)
    if tree is None:
        return None
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
