"""`knowform.lock` lockfile: recorded (blessed) hashes + verdict per binding.

JSON reader/writer for the lockfile at repo root, plus the 3-way `classify`
of recorded-vs-actual change into a verdict. Key = `entry_id`
(`<doc-path>#<doc_anchor>::<governed-file>`) so glob-expanded rows never
collide (see the design notes). Human-diffable,
machine-written by `sync`/`apply`; never hand-edited in practice.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .judge import VerdictKind

LOCKFILE = "knowform.lock"
VERSION = 1


@dataclass
class Record:
    direction: str
    governs: str
    doc_hash: str | None
    code_hash: str | None
    last_verdict: str
    blessed_at: str

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "governs": self.governs,
            "doc_hash": self.doc_hash,
            "code_hash": self.code_hash,
            "last_verdict": self.last_verdict,
            "blessed_at": self.blessed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Record":
        return cls(
            direction=data["direction"],
            governs=data["governs"],
            doc_hash=data.get("doc_hash"),
            code_hash=data.get("code_hash"),
            last_verdict=data.get("last_verdict", VerdictKind.NEEDS_JUDGE.value),
            blessed_at=data.get("blessed_at", ""),
        )


@dataclass
class DocState:
    version: int = VERSION
    records: dict[str, Record] = field(default_factory=dict)  # entry_id -> Record

    def get(self, entry_id: str) -> Record | None:
        return self.records.get(entry_id)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "bindings": {k: self.records[k].to_dict()
                         for k in sorted(self.records)},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DocState":
        return cls(
            version=data.get("version", VERSION),
            records={k: Record.from_dict(v)
                     for k, v in data.get("bindings", {}).items()},
        )


def classify(doc_changed: bool, code_changed: bool) -> VerdictKind:
    """The 3-way state from recorded vs actual (the design notes)."""
    if doc_changed and code_changed:
        return VerdictKind.CONFLICT
    if code_changed:
        return VerdictKind.CODE_DRIFT
    if doc_changed:
        return VerdictKind.DOC_DRIFT
    return VerdictKind.IN_SYNC


def path(root: Path) -> Path:
    return Path(root) / LOCKFILE


def load(root: Path) -> DocState | None:
    """Return the recorded state, or None when no lockfile exists (the plan
    then falls back to the git-diff Tier-0 gate)."""
    lock = path(root)
    if not lock.exists():
        return None
    return DocState.from_dict(json.loads(lock.read_text(encoding="utf-8")))


def write(root: Path, state: DocState) -> None:
    path(root).write_text(
        json.dumps(state.to_dict(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8")
