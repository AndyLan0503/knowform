"""CLI: `python3 -m knowform {init|plan|sync|apply}`.

- `init`  - propose doc↔code bindings for an unwired repo (read-only; writes
            only the `knowform.init.json` proposal artifact).
- `plan`  - report doc↔code drift (read-only; no judge wired, zero tokens).
- `sync`  - re-bless recorded hashes into `knowform.lock`.
- `apply` - regenerate descriptive prose in the safe direction only; never
            writes code, refuses + exits nonzero on the unsafe direction.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .apply import apply
from .init import INIT_PROPOSAL, init, read_proposal, write_proposal
from .materialize import materialize
from .plan import plan
from .sync import sync


def _summary(result) -> str:
    lines = [f"base {result.base} | diff {'yes' if result.diff_available else 'no'}"
             f" | judged {'yes' if result.judged else 'no'}"]
    for e in result.entries:
        if e.verdict == "in-sync":
            continue
        mark = e.error or e.verdict
        lines.append(f"  {mark:<12} {e.entry_id}")
    total = sum(1 for e in result.entries if e.verdict != "in-sync")
    lines.append(f"{total} binding(s) need attention "
                 f"({len(result.entries)} total)")
    return "\n".join(lines)


def _cmd_plan(args) -> int:
    result = plan(Path(args.root), base=args.base, depth=args.depth)
    if args.format == "summary":
        print(_summary(result))
    else:
        json.dump(result.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


def _cmd_sync(args) -> int:
    result = sync(Path(args.root))
    print(f"blessed {result.blessed} binding(s), {result.errors} error(s)")
    return 0


def _cmd_apply(args) -> int:
    generator = None
    if args.anthropic:
        from .judge import AnthropicGenerator
        generator = AnthropicGenerator()
    result = apply(Path(args.root), generator=generator)
    for a in result.applied:
        print(f"applied   {a}")
    for r in result.refused:
        print(f"refused   {r.entry_id}: {r.reason}", file=sys.stderr)
    for s in result.surfaced:
        print(f"surfaced  {s.entry_id}: {s.reason}", file=sys.stderr)
    return 0 if result.ok else 1


def _cmd_init(args) -> int:
    root = Path(args.root)
    if args.write:
        proposal = read_proposal(root)
        if proposal is None:
            print(f"no {INIT_PROPOSAL}; run `knowform init` first",
                  file=sys.stderr)
            return 1
        result = materialize(root, proposal)
        print(f"materialized {len(result.docs_written)} doc(s), "
              f"{len(result.manifest_entries)} manifest entry(ies)"
              + (f", {len(result.skipped)} skipped" if result.skipped else ""))
        return 0
    proposal = init(root)
    out = write_proposal(root, proposal)
    print(f"proposed {len(proposal.candidates)} binding(s), "
          f"{len(proposal.unmatched)} unmatched -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowform")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="report doc↔code drift (read-only)")
    p.add_argument("--root", default=".", help="repo root to scan")
    p.add_argument("--base", default="HEAD", help="git base ref for the diff")
    p.add_argument("--depth", type=int, default=2,
                   help="IMPORTS/CALLS blast-radius depth (1-2)")
    p.add_argument("--format", choices=["json", "summary"], default="json")
    p.set_defaults(func=_cmd_plan)

    s = sub.add_parser("sync", help="re-bless recorded hashes into knowform.lock")
    s.add_argument("--root", default=".", help="repo root to scan")
    s.set_defaults(func=_cmd_sync)

    a = sub.add_parser("apply", help="regenerate prose, safe direction only")
    a.add_argument("--root", default=".", help="repo root to scan")
    a.add_argument("--anthropic", action="store_true",
                   help="wire the live Anthropic generator (opt-in; network)")
    a.set_defaults(func=_cmd_apply)

    i = sub.add_parser(
        "init", help="propose doc↔code bindings (read-only; writes only "
                     "knowform.init.json)")
    i.add_argument("--root", default=".", help="repo root to scan")
    i.add_argument("--write", action="store_true",
                   help="materialize the reviewed knowform.init.json "
                        "(frontmatter+fences and manifest entries)")
    i.set_defaults(func=_cmd_init)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
