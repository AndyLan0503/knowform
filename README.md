# knowform

**Terraform for your docs.** Documentation silently drifts from the code it
describes. `knowform` treats that drift the way Terraform treats
infrastructure drift: it maintains a recorded state, shows you a `plan` of what
diverged, and converges the safe direction on `apply`.

Stdlib-only, zero runtime dependencies. Python-implemented today (the
structural layer uses `ast`), but corpus-agnostic in concept.

## The problem

You write `add(a, b) returns the sum` in a doc. Someone changes `add` to add
`+ 1`. The doc is now a lie, and nothing tells you. Tests guard code against
code; nothing guards prose against code. Docs rot, silently, until a reader
trusts a claim that stopped being true three commits ago.

## The model: three states

Like Terraform, `knowform` reasons over three states per binding:

| State        | Where it lives                    |
|--------------|-----------------------------------|
| **desired**  | the doc / spec (the prose claim)  |
| **recorded** | `knowform.lock` (last blessed)    |
| **actual**   | the code (resolved via `ast`)     |

- **`plan`** compares recorded vs actual and reports drift. Read-only, writes
  nothing.
- **`apply`** converges the **safe direction only**: it regenerates
  descriptive prose from the code. It **never rewrites code to match prose** —
  that direction is surfaced for a human to resolve.
- **`sync`** re-blesses the current world as truth (records intentional
  divergence), returning bindings to `in-sync`.

### Truth direction is declared, per binding

Every binding declares who is authoritative:

- `code-is-truth` — the doc describes the code. If the code drifts, `apply`
  can regenerate the prose. **Safe direction.**
- `doc-is-truth` — the code must satisfy the doc (a spec). If they diverge,
  the fix belongs in code; `knowform` refuses to auto-apply and surfaces it.
- `manual` — tracked only, never auto-applied.

## Cost model: cheapest-first, zero in steady state

Most runs spend **zero tokens**. Work is gated in tiers, cheapest first, so
the LLM judge only ever sees the survivors:

1. **Hash gate** (free) — normalized SHA-256 of each bound region vs the
   recorded hash. Unchanged regions stop here. Most runs stop here.
2. **Structural blast-radius** (free) — an in-memory `ast` graph
   (IMPORTS / CALLS, walked in reverse) finds which docs a code change could
   plausibly reach. Everything off the frontier is `in-sync`.
3. **LLM judge** (opt-in, paid) — only bindings that survive both gates reach
   the judge, and only if you wire one. With no judge, survivors are reported
   `needs-judge` and no tokens are spent.

The steady state — nothing changed — costs nothing.

## Quickstart

```bash
pipx install knowform      # or: pip install knowform
knowform plan              # report drift (read-only)
knowform sync              # bless the current world into knowform.lock
knowform apply             # regenerate prose in the safe direction
```

Wire the optional LLM judge/generator with the extra:

```bash
pipx install "knowform[judge]"
knowform apply --anthropic
```

## Managed docs

A doc opts in with a `knowform:` frontmatter block and marks the region it
governs with anchor fences:

```markdown
---
knowform:
  direction: code-is-truth
  bindings:
    - doc_anchor: add-behavior
      governs: calc.py
      code_anchor: "def add"
---

# Calc

<!-- knowform:add-behavior:start -->
`add(a, b)` returns the sum of its two arguments.
<!-- knowform:add-behavior:end -->

Prose outside the fences is never touched.
```

- `governs` is a file or glob, contained to the repo (paths escaping root
  surface as errors, never crash).
- `code_anchor` narrows to a symbol via `ast` (`def add`, `class Foo`, or a
  bare name); without one the whole file is the region.
- A `.md` with no `knowform:` block is unmanaged and ignored.

Rich sidecar frontmatter (titles, tags, ids, whatever your docs system needs)
can coexist with the `knowform:` block in the same frontmatter — the parser
reads its own block and ignores the rest.

## Ignoring paths

Drop a `.knowformignore` at the repo root (one path prefix or glob per line,
`#` comments) to exclude non-corpus docs like test fixtures or vendored trees.

## CI and pre-commit

**GitHub Action** (composite):

```yaml
- uses: andylan/knowform@v1
  with:
    args: plan
```

**pre-commit**:

```yaml
repos:
  - repo: https://github.com/andylan/knowform
    rev: v0.1.0
    hooks:
      - id: knowform
```

## License

MIT © 2026 Andy Lan
