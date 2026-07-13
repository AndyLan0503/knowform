"""Doc↔code drift reconciler - the `plan` detector.

Self-contained, stdlib-only. Reads managed `.md` docs carrying `knowform:`
frontmatter, resolves their doc/code regions, hashes normalized spans, gates
on `git diff` (Tier 0) and structural blast-radius (Tier 1), and reports a
read-only drift plan. The LLM judge is an injected seam; with none, frontier
bindings are `needs-judge` and no tokens are spent.
"""
from __future__ import annotations

from .plan import Plan, PlanEntry, plan

__all__ = ["Plan", "PlanEntry", "plan"]
