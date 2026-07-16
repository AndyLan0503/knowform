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
```

Onboard an existing repo, then run the steady-state loop - the same order as
Terraform's `init → plan → apply`:

```bash
knowform init              # discover doc↔code bindings -> knowform.init.json
knowform init --write      # materialize the reviewed proposal
knowform plan              # report drift (read-only)
knowform sync              # bless the current world into knowform.lock
knowform apply             # regenerate prose in the safe direction
```

Wire the optional LLM judge/generator with the extra:

```bash
pipx install "knowform[judge]"
knowform apply --anthropic
```

## Adopting a repo: `init`

`knowform init` is where you start. It discovers candidate doc↔code bindings
for an unwired repo and proposes them for review — the `plan → apply`
philosophy applied to onboarding: **propose, never enforce.**

```bash
knowform init                # scan the repo -> knowform.init.json
$EDITOR knowform.init.json   # keep the good bindings, drop the wrong ones
knowform init --write        # record bindings in knowform.bindings.json (docs untouched)
knowform sync                # bless the newly managed world
```

`init` is **read-only** over your repo — the only file it writes is the
reviewable `knowform.init.json`. Nothing is materialized until you run
`--write`.

Discovery is deterministic and precision-first:

- **Docstrings** — every documented function/class/method becomes a candidate
  (`code-is-truth`; the docstring is the governed region).
- **Markdown references** — backtick and call-shaped tokens in unmanaged `.md`
  that resolve to exactly one symbol become candidates.
- **Ambiguous or unresolved** references never bind silently; they land in an
  `unmatched` list for you to resolve by hand.

Direction is always proposed as `code-is-truth`; `doc-is-truth` is never
auto-assigned (declaring a spec is a human decision).

### Optional: LLM disambiguation

References that are ambiguous (a name shared by several symbols) can be resolved
by a model. This is **opt-in** — the default run spends zero tokens.

```bash
knowform init --llm          # use your local Claude Code login (no API key)
knowform init --anthropic    # use the Anthropic API instead
```

- `--llm` shells out to your installed `claude` CLI, so it authenticates with
  whatever you are already logged in with — a Claude subscription included. It
  auto-detects the binary and, if it is missing, tells you rather than silently
  falling back. No install beyond Claude Code itself.
- `--anthropic` calls the Anthropic API directly; it needs `ANTHROPIC_API_KEY`
  and the judge extra (`pipx install "knowform[judge]"`). Best for CI, where no
  interactive login exists.

Either backend only picks among the enumerated candidates — it can never invent
a symbol, and its direction hints are clamped to `manual`.

## Bindings

Bindings live **out-of-band** in `knowform.bindings.json` at the repo root, so
your docs and code stay free of any knowform markup — no frontmatter, no fences.
This matters when docs are fed to LLMs or agents as context: zero binding tokens
in the prose. There are two kinds:

```json
{
  "version": 1,
  "docstrings": [
    { "governs": "calc.py", "symbol": "def add", "direction": "code-is-truth" }
  ],
  "markdown": [
    { "doc": "guide.md", "heading": ["Calc", "Behavior"], "block": 1,
      "governs": "calc.py", "code_anchor": "def add",
      "direction": "code-is-truth" }
  ]
}
```

- **docstring bindings** track a Python symbol's docstring against its own code.
- **markdown bindings** anchor a doc region by its **heading path** (exact
  heading text, top-down) plus an optional 1-based `block` (a paragraph within
  the section; omit it to bind the whole section). The region is re-resolved
  from the heading every run, so edits elsewhere never misalign it — and the doc
  stays plain prose.
- `governs` is a file or glob, contained to the repo (paths escaping root
  surface as errors, never crash); `code_anchor` narrows to a symbol via `ast`
  (`def add`, `class Foo`, or a bare name).
- A heading that can't be resolved (missing / ambiguous / block out of range)
  surfaces as an error, never a silent guess. `doc-is-truth` is never inferred.

You rarely write this by hand — `knowform init` proposes it and `init --write`
records it.

## Ignoring paths

Drop a `.knowformignore` at the repo root (one path prefix or glob per line,
`#` comments) to exclude non-corpus docs like test fixtures or vendored trees.

## CI and pre-commit

**GitHub Action** (composite):

```yaml
- uses: AndyLan0503/knowform@v0
  with:
    args: plan
```

**pre-commit**:

```yaml
repos:
  - repo: https://github.com/AndyLan0503/knowform
    rev: v0
    hooks:
      - id: knowform
```

Both pin the moving major tag (`v0` today), which each release re-points to the
latest compatible version - so these snippets only change at a major bump, never
on routine releases.

## License

MIT © 2026 Andy Lan
