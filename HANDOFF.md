# Handoff: `init` materializer + LLM tier (init Milestones 3 + 4)

Branch: `freebird/init-discovery` (branched off `freebird/docstring-regions`,
which carries M1; this branch contains M1 + M2 + M2.1 + **M3 + M4**).

## M3 + M4: latest work

Completes the `init` design (`memory/knowform-init-design.md`): after M2
PROPOSES bindings (read-only `knowform.init.json`), M3 MATERIALIZES an accepted
proposal and M4 adds the opt-in LLM tier. The propose -> review -> materialize
gate is preserved: `--write` reads the (human-editable) artifact and never
re-discovers.

### M3: `init --write` materializer

`src/knowform/materialize.py` (new): `materialize(root, proposal) ->
MaterializeResult`. Two sinks mirroring the two discovery sources:

- **markdown candidates** -> the doc gains `knowform:` frontmatter plus anchor
  fences (`<!-- knowform:ANCHOR:start/end -->`) wrapping each governed
  paragraph - the exact shape `plan`/`sync`/`apply` already read. Fences are
  inserted BOTTOM-UP so earlier line spans stay valid; frontmatter is injected
  last. A doc already carrying `knowform:` frontmatter is skipped (idempotent);
  a doc with non-knowform frontmatter (e.g. Jekyll `title:`) gets the
  `knowform:` block merged in, keeping its existing keys. Duplicate anchors
  within a doc (two paragraphs naming the same symbol) get unique suffixes.
- **docstring candidates** -> `knowform.bindings.json` gains a `{governs,
  symbol, direction}` entry, deduped against what is already declared.

`init.py` gained `read_proposal(root) -> Proposal | None` (round-trips the
artifact; unknown keys ignored so a hand-reviewed file survives). CLI:
`init --write` materializes the reviewed proposal, or exits nonzero if none
exists ("run `knowform init` first"). Writes are contained to the repo
(symlink / escape-root guards, matching `apply`'s defense-in-depth).

**The load-bearing test** (`tests/test_materialize.py`,
`MaterializeRoundtripTest`): `init` -> `write_proposal` -> `materialize` ->
`sync` -> `plan` reports every binding `in-sync` with zero errors. This proves
the materializer emits a corpus the existing pipeline consumes end to end.

### M4: LLM Tier-2 via `--anthropic`

Same seam as the existing judge/generator - an OPTIONAL injected `Matcher`
(`src/knowform/judge.py`: `Matcher` Protocol, `MatchInput`/`MatchResult`,
`AnthropicMatcher` lazy-importing `anthropic`). `init(root, matcher=None)`:
with no matcher, zero tokens (unchanged). With one, the LLM disambiguates the
AMBIGUOUS (multi-symbol) `unmatched` references the deterministic tier
explicitly punts on: given the doc region text plus the enumerated candidate
symbols, it picks which ONE the prose describes. A confident, validated pick
becomes a `source_tier=2` markdown candidate; everything else stays unmatched.

Precision holds under the LLM: only one candidate per doc region (guarded), a
symbol outside the presented set is rejected as a hallucination, and a
`doc-is-truth` direction hint is clamped to `manual` (a spec is a human
declaration, never auto-assigned). CLI: `init --anthropic` wires
`AnthropicMatcher`.

Strict TDD throughout: `tests/test_materialize.py` (9 tests) and
`tests/test_init_llm.py` (8 tests) were written and confirmed red before the
code existed. Both use larger-scoped fixtures (temp repos, stub Matcher, real
`sync`/`plan` roundtrip) over narrow unit mocks.

---

## M2.1: precision tightening

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

## Verification (M3 + M4)

- **148 tests pass** (`python -m unittest`), green in the final state (was 131
  before this run: +9 materialize, +8 LLM tier).
- Strict TDD: both new test files were confirmed red (`ModuleNotFoundError:
  knowform.materialize`; missing `MatchInput` in judge) before implementation.
- Manual end-to-end CLI dogfood on a throwaway repo: `init` -> `init --write`
  -> `sync` -> `plan --format summary` reported "0 binding(s) need attention
  (2 total)". The materialized `guide.md` (frontmatter + fences) and
  `knowform.bindings.json` were inspected and correct.
- `AnthropicMatcher`/`AnthropicGenerator`/`AnthropicJudge` all lazy-import
  `anthropic`; the package imports and the whole suite runs with no third-party
  deps and never touches the network.

## Assumptions made (M3 + M4)

- **`--write` reads the artifact, never re-discovers.** It requires an existing
  `knowform.init.json` (the review gate). This is the defensible reading of
  "human accepts, then materialize" - the human may edit/prune the JSON first.
- **One direction per managed doc.** All markdown candidates in a doc share
  `code-is-truth` (init never emits another direction for markdown), so the
  doc-level `direction:` is taken from the first candidate.
- **doc_anchor = bare symbol name** (`add`, `Widget`), suffixed (`add-2`) only
  on collision within a doc. Chosen for readable, stable anchors.
- **M4 scope = disambiguate the ambiguous `unmatched` set.** The LLM resolves
  only references the deterministic tier flagged as multi-symbol (>=2
  candidates), choosing among the enumerated symbols. Broad "prose that names
  no symbol at all" fuzzy scanning was deliberately NOT built - it is unbounded
  and expensive; the bounded disambiguation is the high-value, testable slice
  of "LLM for fuzzy matches / direction hints".

## Residual / left undone

- M4 does not attempt fuzzy binding of prose that contains no resolvable
  backtick/call token, nor does it revisit call-shaped-to-zero unmatched
  entries (no symbol to bind). Both remain in `unmatched` for human review.
- Markdown resolution is still by bare symbol name; a dotted reference
  (`module.func`) resolves on its trailing identifier. Cross-module
  disambiguation without `--anthropic` is left to the human.
- The pre-existing uncommitted changes to `.gitignore` and `pyproject.toml`
  (present before this run and every prior run) are **left untouched,
  unstaged**; they are unrelated to this task.

## Commands to verify and push

```sh
git checkout freebird/init-discovery
source .venv/bin/activate
python -m unittest discover -s tests -q      # expect: 148 passed

# try it end to end on a throwaway repo:
python -m knowform init --root <repo>        # writes knowform.init.json (review it)
python -m knowform init --write --root <repo># materializes frontmatter+fences+manifest
python -m knowform sync --root <repo>        # bless
python -m knowform plan --root <repo> --format summary
# opt-in LLM disambiguation (network; needs ANTHROPIC_API_KEY + `anthropic`):
python -m knowform init --anthropic --root <repo>

# if satisfied (this branch includes M1's commits):
git checkout main
git merge --ff-only freebird/init-discovery
git push origin main                         # (not done by this run)
```
