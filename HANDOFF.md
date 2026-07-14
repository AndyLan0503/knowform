# Handoff: docstrings as first-class managed regions (init Milestone 1)

Branch: `freebird/docstring-regions`

## What changed and why

Milestone 1 of the `init` design (see `memory/knowform-init-design.md`): make
Python docstrings first-class managed regions so a hand-written binding runs
through the existing `plan → sync → apply` pipeline. This de-risks `init`
before any discovery is built. `init` discovery itself was **not** built.

1. **ast resolvers** (`src/knowform/regions.py`):
   - `resolve_docstring_region(root, file, symbol)` - the docstring's own line
     span (the doc side of a docstring binding). Degrades to whole-file for
     non-Python, a missing symbol, or a symbol with no docstring.
   - `resolve_docstring_code_region(root, file, symbol)` - the symbol's span
     **minus** its docstring lines, so the doc tracks behavior, not the prose
     describing it. Implemented via a new `Region.exclude` sub-span field.
   - `replace_docstring(text, symbol, prose)` - rewrites a docstring in place
     preserving indentation (used by `apply`); returns None when there is no
     docstring to target.

2. **JSON manifest** (`src/knowform/manifest.py`, new): `knowform.bindings.json`
   at repo root is a second binding source alongside per-doc markdown
   frontmatter. JSON (decided) keeps `requires-python >=3.10` and matches
   `knowform.lock`. Docstring bindings default to `code-is-truth`. Wired into
   `resolve_bindings` in `plan.py` via `_resolve_manifest`, so bindings flow
   through `plan`, `sync`, and `apply` unchanged. Malformed manifests and
   escaping `governs` surface as `error` plan entries, never crash.

3. **apply** (`src/knowform/apply.py`): a `code-is-truth` docstring binding
   whose behavior drifted (`code-drift`) is regenerated in place from the bound
   code. The code region excludes the docstring, so its hash is unchanged by
   the rewrite; only the doc hash moves. A generator emitting `"""` is refused
   (mirrors the existing fence-marker guard).

## Verification

- `uv run --with pytest pytest -q` → **110 passed** (86 baseline + 24 new).
- Strict TDD: the 24 new tests were written first and confirmed red (import
  errors / failures) before implementation.
- New tests:
  - `tests/test_docstring_regions.py` - the ast resolvers + `replace_docstring`.
  - `tests/test_manifest.py` - manifest parsing, defaults, error cases.
  - `tests/test_docstring_e2e.py` - larger-scoped integration: a hand-written
    manifest entry drives docstring drift end-to-end through
    `plan → sync → apply` (code-drift regenerates the docstring in place;
    docstring-only edit reads `doc-drift`; no-generator / triple-quote paths
    are surfaced, never written).
  - `tests/fixtures/documented.py` - fixture with documented symbols.

## Assumptions made

- **Manifest schema**: `{"version": 1, "docstrings": [{"governs", "symbol",
  "direction"}]}`. `governs` is a file or glob (reuses `resolve_governed_files`
  for containment); `symbol` is a `def name` / `class name` / bare `name`
  anchor (same grammar as markdown `code_anchor`); `direction` defaults to
  `code-is-truth`.
- **Binding key**: `"<file>#docstring:<symbol>"` (e.g.
  `documented.py#docstring:def add`), unique per (file, symbol).
- **apply writes docstrings**: reading "the doc tracks behavior" + the default
  `code-is-truth` to mean `apply` should regenerate a drifted docstring in
  place (the natural analog of markdown fenced-region regeneration), so
  "first-class" includes the write path, not only detection.
- Methods bind by bare/`def` name via `ast.walk`; their graph blast-radius maps
  to module scope (only top-level symbols are graph-indexed today) - coarser
  but precision-preserving. Module-level docstrings are out of scope (a symbol
  anchor is required).

## Residual / left undone (by design for M1)

- No `init` discovery, no `init --write` materializer, no LLM tier (Milestones
  2-4).
- The pre-existing uncommitted changes to `.gitignore` and `pyproject.toml`
  (repo URL / ignore tweaks) were present before this run and are **left
  untouched, unstaged**; they are unrelated to this task.

## Commands to verify and push

```sh
git checkout freebird/docstring-regions
uv run --with pytest pytest -q          # expect: 110 passed

# if satisfied:
git checkout main
git merge --ff-only freebird/docstring-regions
git push origin main                    # (not done by this run)
```
