"""`knowform apply`: fix drift in the SAFE direction only.

The one auto-write case is a `code-is-truth` binding whose code drifted
(`code-drift`): its descriptive doc region is regenerated from the code via an
injected `Generator`. Everything else is surfaced, never written:

- `doc-is-truth` drift is the UNSAFE direction (the fix would touch code); it
  is refused and `apply` exits nonzero. Code is never written by this module.
- `conflict` (both sides moved) is refused - a human resolves it.
- `doc-drift` under `code-is-truth` is surfaced for `sync` to bless.
- `manual`, whole-doc (unanchorable) regions, and unrecorded/unsynced
  bindings are surfaced, not touched.

apply requires recorded state; without a `knowform.lock` it refuses (run `sync`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import docstate
from .docstate import DocState, Record, classify
from .gitdiff import head_sha
from .judge import Generator, VerdictKind, build_frontier
from .manifest import Direction, DocstringBinding
from .plan import resolve_bindings
from .regions import (
    hash_span, replace_docstring, resolve_docstring_region,
    resolve_heading_region,
)


@dataclass
class Refusal:
    entry_id: str
    reason: str


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)      # regenerated docs
    refused: list[Refusal] = field(default_factory=list)  # unsafe / conflict
    surfaced: list[Refusal] = field(default_factory=list)  # left for a human
    ok: bool = True   # False iff any drift was left unresolved

    def finalize(self) -> "ApplyResult":
        self.ok = not self.refused and not self.surfaced
        return self


def apply(root: Path, generator: Generator | None = None) -> ApplyResult:
    root = Path(root).resolve()
    recorded = docstate.load(root)
    if recorded is None:
        return ApplyResult(refused=[Refusal(
            "*", "no knowform.lock lockfile; run `knowform sync` first")], ok=False)

    res = resolve_bindings(root)
    result = ApplyResult()
    updated = dict(recorded.records)
    blessed_at = head_sha(root)
    wrote = False

    for r in res.resolved:
        rec = recorded.get(r.entry_id)
        if rec is None:
            result.surfaced.append(Refusal(
                r.entry_id, "unrecorded binding; run `knowform sync` first"))
            continue
        structural = classify(rec.doc_hash != r.doc_hash,
                              rec.code_hash != r.code_hash)
        if structural is VerdictKind.IN_SYNC:
            continue

        # Unsafe direction: the fix would land in code. Never written here.
        if r.direction is Direction.DOC_IS_TRUTH:
            result.refused.append(Refusal(
                r.entry_id,
                "doc-is-truth drift: code must conform; never auto-applied"))
            continue
        if r.direction is Direction.MANUAL:
            result.surfaced.append(Refusal(
                r.entry_id, "manual: tracked only, never auto-applied"))
            continue

        # code-is-truth below.
        if structural is VerdictKind.CONFLICT:
            result.refused.append(Refusal(
                r.entry_id, "conflict: both sides changed; resolve by hand"))
            continue
        if structural is VerdictKind.DOC_DRIFT:
            result.surfaced.append(Refusal(
                r.entry_id, "doc-drift: confirm the edit via `knowform sync`"))
            continue

        # structural is CODE_DRIFT + code-is-truth: the safe regen target.
        if r.doc_region.whole:
            result.surfaced.append(Refusal(
                r.entry_id, "code-drift but no doc region to target safely"))
            continue
        if generator is None:
            result.surfaced.append(Refusal(
                r.entry_id, "code-drift: no generator configured"))
            continue

        if isinstance(r.binding, DocstringBinding):
            ok, why = _regenerate_docstring(
                root, r, generator, updated, blessed_at)
        else:
            ok, why = _regenerate_markdown(
                root, r, generator, updated, blessed_at)
        if ok:
            result.applied.append(r.entry_id)
            wrote = True
        else:
            result.surfaced.append(Refusal(r.entry_id, why))

    if wrote:
        docstate.write(root, DocState(version=recorded.version,
                                      records=updated))
    return result.finalize()


def _regenerate_markdown(root: Path, r, generator: Generator,
                         updated: dict[str, Record],
                         blessed_at: str) -> tuple[bool, str]:
    """Replace the doc's heading-anchored region (its exact line span) with
    regenerated prose and re-bless. No inline markers: the region is re-resolved
    by heading after the write. Refuses if the generator emits a heading, which
    could break the doc structure the anchor depends on."""
    doc_file = root / r.doc_region.path
    # Defense in depth: the walk already excludes symlinks, but re-check right
    # before the write so a swapped path can never escape root (security F1).
    if doc_file.is_symlink() or not _within(root, doc_file):
        return False, "doc path escapes the repository; refused"
    item = build_frontier(root, r.key, r.direction, r.binding,
                          r.doc_region, r.code_region)
    new_inner = generator(item)
    if _introduces_heading(new_inner):
        return False, "generator emitted a markdown heading; refused"
    lines = doc_file.read_text(encoding="utf-8").split("\n")
    lines[r.doc_region.start - 1:r.doc_region.end] = new_inner.split("\n")
    doc_file.write_text("\n".join(lines), encoding="utf-8")
    back = resolve_heading_region(root, r.doc_region.path,
                                  r.binding.heading, r.binding.block)
    if back.error is not None or back.region is None:
        return False, "anchor could not be re-resolved after write; skipped"
    updated[r.entry_id] = Record(
        direction=r.direction.value, governs=r.governs,
        doc_hash=hash_span(back.region.text(root)), code_hash=r.code_hash,
        last_verdict=VerdictKind.IN_SYNC.value, blessed_at=blessed_at)
    return True, ""


def _regenerate_docstring(root: Path, r, generator: Generator,
                          updated: dict[str, Record],
                          blessed_at: str) -> tuple[bool, str]:
    """Rewrite a docstring in place from its bound behavior and re-bless the
    record. The code region excludes the docstring, so its hash is unchanged by
    the rewrite; only the doc hash moves."""
    py_file = root / r.doc_region.path
    if py_file.is_symlink() or not _within(root, py_file):
        return False, "code path escapes the repository; refused"
    item = build_frontier(root, r.key, r.direction, r.binding,
                          r.doc_region, r.code_region)
    prose = generator(item)
    if '"""' in prose:
        return False, "generator emitted a triple quote; refused"
    new_text = replace_docstring(
        py_file.read_text(encoding="utf-8"), r.binding.symbol, prose)
    if new_text is None:
        return False, "docstring vanished before write; skipped"
    py_file.write_text(new_text, encoding="utf-8")
    new_region = resolve_docstring_region(root, r.doc_region.path,
                                          r.binding.symbol)
    updated[r.entry_id] = Record(
        direction=r.direction.value, governs=r.governs,
        doc_hash=hash_span(new_region.text(root)), code_hash=r.code_hash,
        last_verdict=VerdictKind.IN_SYNC.value, blessed_at=blessed_at)
    return True, ""


def _within(root: Path, p: Path) -> bool:
    try:
        return p.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


_HEADING_LINE = re.compile(r"^\s*#{1,6}\s")


def _introduces_heading(text: str) -> bool:
    return any(_HEADING_LINE.match(ln) for ln in text.split("\n"))
