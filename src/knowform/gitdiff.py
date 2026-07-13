"""Changed line ranges per file, from `git diff` against a base ref.

Read-only shell-out (`git diff`, `git rev-parse`). Default base is the
working tree vs `HEAD`. Missing repo / unknown ref degrade to "no changes
known" so the caller can still emit hashes (Tier 0 of the cost model).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
DIFF_FILE = re.compile(r"^\+\+\+ (.*)$")


@dataclass
class ChangedSet:
    """Changed line ranges (1-based inclusive) per repo-relative file path.

    `available` is False when git could not produce a diff (not a repo,
    unknown ref); the caller treats an unavailable set as "cannot prove
    unchanged" and still emits hashes.
    """
    available: bool = True
    ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    def overlaps(self, path: Path, start: int, end: int) -> bool:
        if not self.available:
            return False
        for lo, hi in self.ranges.get(str(path), []):
            if start <= hi and lo <= end:
                return True
        return False

    def touched(self, path: Path) -> bool:
        return self.available and bool(self.ranges.get(str(path)))


def _run(root: Path, args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=root,
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout


def head_sha(root: Path) -> str:
    """Current HEAD sha for lockfile provenance, or "uncommitted" when there
    is no repo/commit yet."""
    code, out = _run(root, ["rev-parse", "--verify", "--quiet", "HEAD"])
    return out.strip() if code == 0 and out.strip() else "uncommitted"


def _is_repo(root: Path) -> bool:
    code, _ = _run(root, ["rev-parse", "--is-inside-work-tree"])
    return code == 0


def _ref_exists(root: Path, ref: str) -> bool:
    # `--end-of-options` fences the user-derived ref so a hostile `--base`
    # (e.g. `--output=...`) can never be parsed as a git option.
    code, _ = _run(root, ["rev-parse", "--verify", "--quiet",
                          "--end-of-options", ref])
    return code == 0


def changed_set(root: Path, base: str = "HEAD") -> ChangedSet:
    """Line ranges changed on the NEW side of `git diff <base>`, unioned with
    untracked files (treated as fully changed).

    base=="HEAD" (or any ref) diffs the working tree against that ref, which
    is what an editing agent sees on its own diff. Untracked/intent-to-add
    files never appear in `git diff`, so they are added explicitly - otherwise
    a brand-new governed file reads as in-sync on its first commit.
    """
    if not _is_repo(root) or not _ref_exists(root, base):
        return ChangedSet(available=False)
    # core.quotepath=false keeps non-ascii paths literal; `--` guards the ref.
    code, out = _run(root, ["-c", "core.quotepath=false", "diff",
                            "--unified=0", "--no-color",
                            "--end-of-options", base, "--"])
    if code != 0:
        return ChangedSet(available=False)
    ranges = _parse_diff(out)
    _add_untracked(root, ranges)
    return ChangedSet(ranges={k: v for k, v in ranges.items() if v})


def _add_untracked(root: Path,
                   ranges: dict[str, list[tuple[int, int]]]) -> None:
    """Mark every untracked (and intent-to-add) file as fully changed."""
    code, out = _run(root, ["ls-files", "--others", "--exclude-standard",
                            "-z"])
    if code != 0:
        return
    for rel in out.split("\0"):
        if not rel:
            continue
        try:
            n = len((root / rel).read_text(encoding="utf-8").splitlines()) or 1
        except (OSError, UnicodeDecodeError):
            n = 1
        ranges.setdefault(rel, []).append((1, n))


def _diff_path(raw: str) -> str:
    """Recover the on-disk relative path from a `+++ ` diff header token.

    Strips git's trailing tab (added when the name contains a space) and the
    `b/` prefix, and C-unquotes a double-quoted path (special chars)."""
    raw = raw.rstrip("\t")
    if raw == "/dev/null":
        return raw
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = _c_unquote(raw[1:-1])
    if raw.startswith("b/"):
        raw = raw[2:]
    return raw


def _c_unquote(s: str) -> str:
    """Undo git's C-style backslash escaping of a quoted path."""
    out = bytearray()
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            simple = {"n": 10, "t": 9, "\\": 92, '"': 34}.get(nxt)
            if simple is not None:
                out.append(simple)
                i += 2
                continue
            if nxt.isdigit() and i + 3 < len(s):  # \ooo octal
                out.append(int(s[i + 1:i + 4], 8))
                i += 4
                continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace")


def _parse_diff(out: str) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in out.splitlines():
        fm = DIFF_FILE.match(line)
        if fm:
            current = _diff_path(fm.group(1))
            ranges.setdefault(current, [])
            continue
        hm = HUNK.match(line)
        if hm and current is not None:
            start = int(hm.group(1))
            count = int(hm.group(2)) if hm.group(2) is not None else 1
            if count == 0:
                # Pure deletion: mark the anchor line as touched.
                ranges[current].append((start, start))
            else:
                ranges[current].append((start, start + count - 1))
    return ranges
