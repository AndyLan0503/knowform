"""Read-only drift `plan`.

Tier 0 (git-diff hash-gate) -> Tier 1 (structural blast-radius) -> optional
judge on the frontier. Writes nothing: no lockfile, sync or apply side
effects. With no judge, frontier bindings are `needs-judge` and zero tokens
are spent.
"""
from __future__ import annotations

import fnmatch
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import docstate
from .docstate import classify
from .gitdiff import ChangedSet, changed_set
from .graph import DocNode, build_graph, frontier
from .judge import Judge, VerdictKind, build_frontier
from .manifest import MANIFEST, Direction, load as load_manifest
from .regions import (
    Region, hash_span, resolve_code_region,
    resolve_docstring_code_region, resolve_docstring_region,
    resolve_governed_files, resolve_heading_region,
)


@dataclass
class PlanEntry:
    entry_id: str                # unique per (binding, governed-file)
    key: str                     # <doc-path>#<doc_anchor>
    direction: str | None
    governs: str
    doc_hash: str | None
    code_hash: str | None
    verdict: str
    on_frontier: bool
    rationale: str = ""
    error: str | None = None


@dataclass
class Plan:
    base: str
    diff_available: bool
    judged: bool
    entries: list[PlanEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "base": self.base,
            "diff_available": self.diff_available,
            "judged": self.judged,
            "entries": [asdict(e) for e in self.entries],
        }


_VENDORED = {"node_modules", "vendor", "__pycache__", "site-packages",
             "dist", "build", "target", "venv"}

IGNORE_FILE = ".knowformignore"


def load_ignore(root: Path) -> list[str]:
    """Repo-root `.knowformignore`: one pattern per line (`#` comments/blanks
    skipped) for paths that are not a governed corpus - test fixtures, vendored
    docs. A pattern matches a repo-relative POSIX path as a path prefix (`docs`
    ignores `docs/` and everything under it) OR as an fnmatch glob where `*`
    spans `/` (`*.md` ignores every markdown doc - deliberately broad). A
    leading `/` is stripped (repo-root relative)."""
    f = root / IGNORE_FILE
    if not f.exists():
        return []
    out: list[str] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line.lstrip("/").rstrip("/"))
    return out


def _ignored(rel: Path, patterns: list[str]) -> bool:
    s = rel.as_posix()
    return any(s == p or s.startswith(p + "/") or fnmatch.fnmatch(s, p)
               for p in patterns)


def _pruned_walk(root: Path, suffix: str,
                 ignore: list[str] | None = None) -> list[Path]:
    """rglob for `suffix`, skipping hidden (leading `.`), vendored, and
    `.knowformignore`d dirs/files."""
    ignore = ignore or []
    out: list[Path] = []
    stack = [root]
    while stack:
        for entry in sorted(stack.pop().iterdir()):
            # Never follow a symlink: a symlinked dir or `.md`/`.py` can point
            # outside the repo, and the doc side is the one `apply` writes.
            # Containment is by construction - the walk only descends real
            # dirs under root (security review, Finding 1).
            if entry.is_symlink():
                continue
            rel = entry.relative_to(root)
            if entry.is_dir():
                if (entry.name.startswith(".") or entry.name in _VENDORED
                        or _ignored(rel, ignore)):
                    continue
                stack.append(entry)
            elif entry.suffix == suffix and not _ignored(rel, ignore):
                out.append(rel)
    return sorted(out)


@dataclass
class _Resolved:
    entry_id: str
    key: str
    direction: Direction
    governs: str
    doc_region: Region
    code_region: Region
    binding: object
    doc_hash: str
    code_hash: str


@dataclass
class Resolution:
    """Every managed binding resolved to hashed doc/code regions, plus the
    error entries and graph inputs. Shared by `plan`, `sync` and `apply`."""
    resolved: list[_Resolved] = field(default_factory=list)
    errors: list[PlanEntry] = field(default_factory=list)
    doc_nodes: list[DocNode] = field(default_factory=list)
    py_files: set[Path] = field(default_factory=set)
    ignore: list[str] = field(default_factory=list)


def resolve_bindings(root: Path) -> Resolution:
    """Resolve every `knowform.bindings.json` binding to hashed doc/code regions
    - pure region resolution and hashing, no git/judge/lockfile signal
    (read-only). Bindings are declared out-of-band; docs carry no markup."""
    root = Path(root).resolve()
    out = Resolution(ignore=load_ignore(root))
    _resolve_manifest(root, out)
    return out


def _resolve_manifest(root: Path, out: Resolution) -> None:
    """Resolve every `knowform.bindings.json` binding to hashed regions.
    Docstring bindings: doc region = the docstring span, code region = the
    symbol MINUS its docstring (behavior). Markdown bindings: doc region = the
    heading-anchored span, code region = the governed symbol."""
    manifest = load_manifest(root)
    if manifest is None:
        return
    if manifest.error:
        out.errors.append(PlanEntry(
            entry_id=f"{MANIFEST}#<error>", key=f"{MANIFEST}#<error>",
            direction=None, governs="", doc_hash=None, code_hash=None,
            verdict="error", on_frontier=False, error=manifest.error))
        return
    for db in manifest.docstrings:
        for gov in resolve_governed_files(root, db.governs):
            if gov.error is not None:
                key = f"{db.governs}#docstring:{db.symbol}"
                out.errors.append(PlanEntry(
                    entry_id=key, key=key, direction=db.direction.value,
                    governs=db.governs, doc_hash=None, code_hash=None,
                    verdict="error", on_frontier=False, error=gov.error))
                continue
            gov_file = gov.path
            doc_region = resolve_docstring_region(root, gov_file, db.symbol)
            code_region = resolve_docstring_code_region(
                root, gov_file, db.symbol)
            key = f"{gov_file}#docstring:{db.symbol}"
            out.resolved.append(_Resolved(
                entry_id=key, key=key, direction=db.direction,
                governs=str(gov_file), doc_region=doc_region,
                code_region=code_region, binding=db,
                doc_hash=_safe_hash(root, doc_region),
                code_hash=_safe_hash(root, code_region)))
            out.doc_nodes.append(DocNode(
                key=key, region=code_region, node_id=key))
            if (root / gov_file).suffix == ".py":
                out.py_files.add(gov_file)

    for mb in manifest.markdown:
        what = "/".join(mb.heading) + (f"#{mb.block}" if mb.block else "")
        key = f"{mb.doc}#heading:{what}"
        anchor = resolve_heading_region(root, Path(mb.doc), mb.heading, mb.block)
        if anchor.error is not None:
            out.errors.append(PlanEntry(
                entry_id=key, key=key, direction=mb.direction.value,
                governs=mb.governs, doc_hash=None, code_hash=None,
                verdict="error", on_frontier=False, error=anchor.error))
            continue
        doc_region = anchor.region
        for gov in resolve_governed_files(root, mb.governs):
            if gov.error is not None:
                out.errors.append(PlanEntry(
                    entry_id=key, key=key, direction=mb.direction.value,
                    governs=mb.governs, doc_hash=None, code_hash=None,
                    verdict="error", on_frontier=False, error=gov.error))
                continue
            gov_file = gov.path
            code_region = resolve_code_region(root, gov_file, mb.code_anchor)
            entry_id = f"{key}::{gov_file}"
            if mb.code_anchor:
                entry_id += f"::{mb.code_anchor}"
            out.resolved.append(_Resolved(
                entry_id=entry_id, key=key, direction=mb.direction,
                governs=str(gov_file), doc_region=doc_region,
                code_region=code_region, binding=mb,
                doc_hash=_safe_hash(root, doc_region),
                code_hash=_safe_hash(root, code_region)))
            out.doc_nodes.append(DocNode(
                key=key, region=code_region, node_id=entry_id))
            if (root / gov_file).suffix == ".py":
                out.py_files.add(gov_file)


def plan(root: Path, base: str = "HEAD", judge: Judge | None = None,
         depth: int = 2) -> Plan:
    root = Path(root).resolve()
    changed = changed_set(root, base)
    recorded = docstate.load(root)
    res = resolve_bindings(root)
    entries: list[PlanEntry] = list(res.errors)
    resolved = res.resolved
    doc_nodes = res.doc_nodes
    py_files = res.py_files

    graph = build_graph(root, doc_nodes, py_files | _all_py(root, res.ignore))
    changed_code = _changed_code_keys(graph, changed)
    frontier_keys = frontier(graph, changed_code, depth)
    # Recorded bindings seed their blast-radius from recorded-vs-actual code
    # drift, not git - so a re-blessed edit stops seeding once blessed, while
    # a genuinely-drifted callee still reaches its callers' docs.
    recorded_frontier = frontier(
        graph, _recorded_drift_keys(recorded, resolved, graph), depth)

    for r in resolved:
        # Recorded state, when present, is the authoritative drift signal:
        # `Drift = recorded vs actual`. It supersedes
        # the git-diff Tier-0 gate, so a re-blessed binding reads in-sync
        # even while its code edit is still uncommitted. Unrecorded bindings
        # (no lockfile, or new since the last `sync`) fall back to git-diff +
        # structural blast-radius, widening to the frontier they cannot clear.
        record = recorded.get(r.entry_id) if recorded else None
        if record is not None:
            structural = classify(record.doc_hash != r.doc_hash,
                                  record.code_hash != r.code_hash)
            if structural is not VerdictKind.IN_SYNC:
                # Direct drift on the bound region.
                entries.append(_judged(root, r, judge) if judge is not None
                               else _entry(r, structural, on_frontier=True))
            elif r.entry_id in recorded_frontier:
                # Own region unchanged, but a transitive dependency drifted.
                entries.append(_judged(root, r, judge) if judge is not None
                               else _entry(r, VerdictKind.NEEDS_JUDGE,
                                           on_frontier=True))
            else:
                entries.append(_entry(r, VerdictKind.IN_SYNC,
                                      on_frontier=False))
            continue

        doc_changed = changed.overlaps(
            r.doc_region.path, r.doc_region.start, r.doc_region.end)
        code_changed = changed.overlaps(
            r.code_region.path, r.code_region.start, r.code_region.end)
        # No diff signal (not a repo / unknown ref) cannot prove unchanged, so
        # every binding is on the frontier (widen, never narrow).
        on_frontier = (not changed.available or r.entry_id in frontier_keys
                       or code_changed or doc_changed)

        if not on_frontier:
            entries.append(_entry(r, VerdictKind.IN_SYNC, on_frontier=False))
        elif judge is not None:
            entries.append(_judged(root, r, judge))
        else:
            entries.append(_entry(r, VerdictKind.NEEDS_JUDGE, on_frontier=True))

    entries.sort(key=lambda e: e.entry_id)
    return Plan(base=base, diff_available=changed.available,
                judged=judge is not None, entries=entries)


def _judged(root: Path, r: _Resolved, judge: Judge) -> PlanEntry:
    item = build_frontier(root, r.key, r.direction, r.binding,
                          r.doc_region, r.code_region)
    verdict = judge(item)
    return _entry(r, verdict.verdict, on_frontier=True,
                  rationale=verdict.rationale)


def _entry(r: _Resolved, verdict: VerdictKind, on_frontier: bool,
           rationale: str = "") -> PlanEntry:
    return PlanEntry(
        entry_id=r.entry_id, key=r.key, direction=r.direction.value,
        governs=r.governs, doc_hash=r.doc_hash, code_hash=r.code_hash,
        verdict=verdict.value, on_frontier=on_frontier, rationale=rationale)


def _safe_hash(root: Path, region: Region) -> str | None:
    try:
        return hash_span(region.text(root))
    except (OSError, UnicodeDecodeError):
        return None


def _all_py(root: Path, ignore: list[str]) -> set[Path]:
    return set(_pruned_walk(root, ".py", ignore))


def _recorded_drift_keys(recorded, resolved: list[_Resolved], graph) -> set[str]:
    """Graph code keys whose recorded code_hash no longer matches actual - the
    blast-radius seeds when a lockfile exists (hash-based, git-independent)."""
    if not recorded:
        return set()
    keys: set[str] = set()
    for r in resolved:
        rec = recorded.get(r.entry_id)
        if rec is not None and rec.code_hash != r.code_hash:
            code_key = graph.governs.get(r.entry_id)
            if code_key:
                keys.add(code_key)
    return keys


def _changed_code_keys(graph, changed: ChangedSet) -> set[str]:
    keys: set[str] = set()
    for key, node in graph.code.items():
        if changed.overlaps(Path(node.path), node.lineno, node.end_lineno):
            keys.add(key)
    return keys
