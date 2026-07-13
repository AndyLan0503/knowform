"""`knowform sync`: re-bless the recorded state.

Recomputes every binding's normalized doc/code hash and writes them to the
`knowform.lock` lockfile as `in-sync`, stamped with the current HEAD sha. This
is the human's "I accept the current world as truth" action; after it, an
unchanged corpus reads `in-sync` at zero tokens.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .docstate import DocState, Record, write
from .gitdiff import head_sha
from .judge import VerdictKind
from .plan import resolve_bindings


@dataclass
class SyncResult:
    blessed: int
    errors: int


def sync(root: Path) -> SyncResult:
    root = Path(root).resolve()
    res = resolve_bindings(root)
    blessed_at = head_sha(root)
    records: dict[str, Record] = {}
    for r in res.resolved:
        records[r.entry_id] = Record(
            direction=r.direction.value,
            governs=r.governs,
            doc_hash=r.doc_hash,
            code_hash=r.code_hash,
            last_verdict=VerdictKind.IN_SYNC.value,
            blessed_at=blessed_at,
        )
    write(root, DocState(records=records))
    return SyncResult(blessed=len(records), errors=len(res.errors))
