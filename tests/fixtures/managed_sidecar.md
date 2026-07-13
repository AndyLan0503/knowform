---
type: mechanism
title: A card with rich sidecar metadata that is also knowform-governed
description: Exercises arbitrary sidecar scalars coexisting with a knowform block in one frontmatter.
tags: [sidecar, knowform]
timestamp: 2026-07-13
id: managed-sidecar
related: [managed-add]
confidence: high
sources: ["calc.py"]
knowform:
  direction: code-is-truth
  bindings:
    - doc_anchor: sidecar-behavior
      governs: calc.py
      code_anchor: "def add"
---

# Sidecar + knowform

<!-- knowform:sidecar-behavior:start -->
`add(a, b)` returns the sum, and this card carries rich sidecar metadata too.
<!-- knowform:sidecar-behavior:end -->
