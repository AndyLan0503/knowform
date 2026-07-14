# Handoff: `init` deterministic binding discovery (init Milestones 2 + 2.1)

Branch: `freebird/init-discovery` (branched off `freebird/docstring-regions`,
which carries M1; this branch contains M1 + M2 + M2.1).

## M2.1: precision tightening (latest work)

Dogfooding `init` on knowform's own `src/` tree surfaced three precision bugs
the fixture unit tests missed. All three are now fixed in
`src/knowform/init.py`, surgically (no rebuild):

1. **Prose parentheticals falsely read as calls.** The call regex allowed
   whitespace before `(`, so prose like "the design (no-LLM) step" matched as
   a call to design. Fix: the call regex is now tight `name(` (no space), and plain
   running prose contributes NO references at all - only inline-code backticks
   and fenced code blocks are harvested. Net: "the design (no-LLM)"
   contributes nothing; `add(a, b)` in backticks or a real fence still
   resolves.
2. **Fenced/dense-prose cartesian explosion.** One doc region (e.g. a code
   fence or a multi-symbol list item) emitted one candidate per symbol
   mentioned - HANDOFF.md (15-39) alone produced 8. Fix: a doc region now maps
   to AT MOST ONE candidate. References are grouped by enclosing region; a
   region naming exactly one symbol binds it, a multi-symbol region binds
   nothing (rather than exploding). Deterministic and documented in-code.
3. **Private/dunder symbols proposed.** `_bound`, `_regenerate`, `__init__`
   etc. were proposed as docstring/markdown candidates. Fix: leading-underscore
   names are dropped - excluded from docstring anchors and from harvested
   markdown references.

Effect on the self-scan: unmatched fell from 34 (flooded with prose words like
`design`, `discovery`, `manifest`) to 5 genuine code tokens; zero regions map
to >1 candidate; zero underscore candidates.

Acceptance is enforced by a `InitDogfoodTest` that runs `init` on the real repo
root and asserts the three invariants (no prose word in unmatched, no region
with >1 candidate, no leading-underscore candidate), plus focused
`InitPrecisionTest` unit cases. Strict TDD: the 8 new tests were confirmed red
before the fix. Full suite: **131 passed**. The `init` API, artifact schema,
and CLI are unchanged; the precision-over-recall intent is preserved (tighter,
not weaker).

---

## M2 (unchanged, for context)

## What changed and why

Milestone 2 of the `init` design (see `memory/knowform-init-design.md`): the
`knowform init` command that SOLVES BINDING DISCOVERY. It scans a repo with no
knowform wiring and proposes candidate doc↔code bindings deterministically (no
LLM). It is **read-only over the repo** - it never mutates docs, frontmatter,
fences, or the manifest. The only artifact it writes is the reviewable proposal
`knowform.init.json` (materialization is Milestone 3).

1. **`src/knowform/init.py`** (new): `init(root) -> Proposal`, plus
   `write_proposal(root, proposal)` and `Candidate` / `Unmatched` / `Proposal`
   dataclasses. Two discovery sources, both built on M1 + existing infra:
   - **Tier 1 docstrings**: walk every `.py` (reusing `plan._pruned_walk`, so
     `.knowformignore` / vendored / hidden dirs are respected), harvest every
     function/class/method that HAS a docstring via `ast`. Each becomes a
     `docstring` candidate: `governs` = the `.py`, `symbol` = `def name` /
     `class name`, `doc_region` = the docstring span (via the M1
     `resolve_docstring_region`), `direction = code-is-truth`, tier 1.
   - **Tier 0 markdown refs**: scan unmanaged `.md` (no `knowform:`
     frontmatter) for backtick identifiers, call-shaped `name(...)` tokens, and
     fenced-block calls. Resolve each against a repo symbol table built from
     `ast`. Exactly-one-match -> `markdown` candidate (`code_anchor` = the
     resolved `def`/`class`, `doc_region` = the enclosing markdown paragraph,
     `code-is-truth`, tier 0).
   - **Precision over recall**: ambiguous references (>1 symbol) and call-shaped
     references that resolve to no symbol go to a separate `unmatched` list for
     a human, never silently bound. Plain backtick words that resolve to
     nothing are dropped as prose styling (not treated as failed bindings).
   - **Skip already-bound**: `_bound()` collects `(governed-file, bare-symbol)`
     pairs from the existing manifest (`knowform.bindings.json`) and every
     managed doc's frontmatter bindings; those are never re-proposed (as a
     docstring candidate or a markdown candidate).
   - Direction is always `code-is-truth`; `doc-is-truth` is NEVER auto-assigned.
   - Output is stable-sorted and human-diffable.

2. **`src/knowform/__main__.py`**: wired a `knowform init` subcommand via
   argparse alongside `plan`/`sync`/`apply` (read-only; `--root`; writes only
   `knowform.init.json`).

## Verification

- `uv run --with pytest pytest -q` -> **123 passed** (110 from M1 baseline +
  13 new). All green in the final state.
- Strict TDD: `tests/test_init.py` was written first and confirmed red
  (`ModuleNotFoundError: knowform.init`) before implementation.
- Manual end-to-end run on a throwaway repo confirmed: docstring + markdown
  candidates proposed, a `missing_fn()` call surfaced as `unmatched`, and the
  repo's `.py`/`.md` left byte-for-byte unchanged (only `knowform.init.json`
  written).
- New tests (`tests/test_init.py`, larger-scoped integration over a fixture
  repo built in a temp dir): docstring harvest + candidate shape, markdown
  single-resolution + dedup within a paragraph, ambiguous -> unmatched,
  unresolved backtick dropped, managed-doc + bound-symbol skip, existing
  manifest binding skip, `.knowformignore` respected, stable-sorted /
  deterministic output, `init` is read-only, `write_proposal` artifact, and the
  `knowform init` CLI path leaving docs/code untouched.

## Assumptions made

- **Proposal schema** (`knowform.init.json`): `{version, candidates,
  unmatched}`. Each candidate: `kind` (docstring|markdown), `governs`,
  `symbol` (docstring) / `code_anchor` (markdown) - the inapplicable one is
  `null` so the shape is uniform and diffable - `doc_path`, `doc_region`
  `[start, end]`, `direction`, `confidence`, `rationale`, `source_tier`. Each
  unmatched: `kind`, `doc_path`, `doc_region`, `identifier`, `match_count`,
  `reason`.
- **Unmatched policy**: only references with a real signal reach `unmatched` -
  `>1` resolutions (genuinely ambiguous) and call-shaped `name(...)` that
  resolves to `0` (looks like a call to an unknown symbol, worth review). A
  plain backtick word resolving to `0` is prose styling and is dropped, to keep
  the proposal reviewable rather than flooding it with every non-code word.
  This is the defensible reading of "resolves to 0 or >1 -> unmatched" under
  the precision-over-recall rule.
- **Within-file name collisions**: if a `.py` has >1 documented symbol sharing
  the same `def name` / `class name` anchor, none is proposed (the anchor
  cannot uniquely target under the existing anchor grammar). Precision-
  preserving.
- **Symbol table** spans all `def`/`class`/method names across the walked
  `.py` files (via `ast.walk`); a markdown identifier is confident only when it
  maps to exactly one such symbol repo-wide.
- Module-level docstrings are out of scope (a symbol anchor is required, same
  as M1).

## Residual / left undone (by design for M2)

- No `init --write` materializer (Milestone 3) and no LLM/`--anthropic` fuzzy
  tier (Milestone 4). `init` here is deterministic and read-only.
- Markdown resolution is by bare symbol name; a dotted reference
  (`module.func`) resolves on its trailing identifier (`func`). Cross-module
  disambiguation is left to the human via the `unmatched` list / review.
- The pre-existing uncommitted changes to `.gitignore` and `pyproject.toml`
  (present before both this run and the M1 run) are **left untouched,
  unstaged**; they are unrelated to this task.

## Commands to verify and push

```sh
git checkout freebird/init-discovery
uv run --with pytest pytest -q          # expect: 131 passed

# try it:
python -m knowform init --root <some-repo>   # writes knowform.init.json only

# if satisfied (this branch includes M1's commits):
git checkout main
git merge --ff-only freebird/init-discovery
git push origin main                    # (not done by this run)
```
