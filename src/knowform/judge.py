"""The LLM-judge frontier seam (Tier 3 of the cost model).

`plan` takes an OPTIONAL injected judge. With none, frontier bindings are
`needs-judge` and zero tokens are spent. `build_frontier` assembles a tight
`JudgeInput` (the claim + bound symbol text/signatures, not whole files).
`AnthropicJudge` lazy-imports its client so the package imports with no
third-party deps and tests never touch the network.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from .frontmatter import Binding, Direction
from .regions import Region


class VerdictKind(str, Enum):
    IN_SYNC = "in-sync"
    DOC_DRIFT = "doc-drift"
    CODE_DRIFT = "code-drift"
    CONFLICT = "conflict"
    NEEDS_JUDGE = "needs-judge"


@dataclass
class JudgeInput:
    """The tight frontier neighborhood handed to the judge - never whole
    files."""
    key: str
    direction: Direction
    doc_claim: str
    code_text: str
    signatures: list[str]
    doc_path: str
    code_path: str


@dataclass
class Verdict:
    verdict: VerdictKind
    rationale: str = ""
    confidence: float | None = None


class Judge(Protocol):
    def __call__(self, item: JudgeInput) -> Verdict: ...


def _signatures(root: Path, region: Region) -> list[str]:
    """Resolved symbol signatures within the code region, via `ast`."""
    full = root / region.path
    if full.suffix != ".py":
        return []
    try:
        tree = ast.parse(full.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            if region.start <= node.lineno <= region.end:
                out.append(_signature(node))
    return out


def _signature(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        return f"class {node.name}"
    args = ast.unparse(node.args) if hasattr(ast, "unparse") else ""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({args})"


def build_frontier(root: Path, key: str, direction: Direction,
                   binding: Binding, doc_region: Region,
                   code_region: Region) -> JudgeInput:
    return JudgeInput(
        key=key,
        direction=direction,
        doc_claim=doc_region.text(root),
        code_text=code_region.text(root),
        signatures=_signatures(root, code_region),
        doc_path=str(doc_region.path),
        code_path=str(code_region.path),
    )


_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string",
                    "enum": [v.value for v in VerdictKind]},
        "rationale": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["verdict", "rationale"],
}

_PROMPT = (
    "You judge whether a documentation claim still matches the code it "
    "governs. Direction {direction}: for code-is-truth the doc must describe "
    "the code; for doc-is-truth the code must satisfy the doc; manual is "
    "tracked only. Report in-sync, doc-drift, code-drift, or conflict.\n\n"
    "DOC CLAIM ({doc_path}):\n{doc}\n\n"
    "CODE ({code_path}), signatures {sigs}:\n{code}"
)


@dataclass
class AnthropicJudge:
    """Concrete judge. Lazy-imports `anthropic` inside `__call__` so the
    package imports without the dep and tests never reach it."""
    model: str = "claude-opus-4-8"

    def __call__(self, item: JudgeInput) -> Verdict:
        import anthropic  # lazy: never imported at module top level

        client = anthropic.Anthropic()
        message = client.messages.create(
            model=self.model,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            output_config={"format": {
                "type": "json_schema",
                "schema": _SCHEMA,
            }},
            messages=[{
                "role": "user",
                "content": _PROMPT.format(
                    direction=item.direction.value,
                    doc_path=item.doc_path,
                    doc=item.doc_claim,
                    code_path=item.code_path,
                    sigs=", ".join(item.signatures) or "n/a",
                    code=item.code_text,
                ),
            }],
        )
        return _parse_verdict(message)


def _parse_verdict(message: object) -> Verdict:
    import json

    text = ""
    for block in getattr(message, "content", []):
        text += getattr(block, "text", "")
    data = json.loads(text)
    return Verdict(
        verdict=VerdictKind(data["verdict"]),
        rationale=data.get("rationale", ""),
        confidence=data.get("confidence"),
    )


class Generator(Protocol):
    """The safe-direction prose seam. Returns fresh
    descriptive prose for a `code-is-truth` doc region, from the bound code."""
    def __call__(self, item: JudgeInput) -> str: ...


_GEN_PROMPT = (
    "Rewrite the documentation region so it accurately describes the code it "
    "governs. Return ONLY the replacement prose - no fences, no preamble, no "
    "code. Keep it as short as the current region.\n\n"
    "CURRENT DOC ({doc_path}):\n{doc}\n\n"
    "CODE ({code_path}), signatures {sigs}:\n{code}"
)


@dataclass
class AnthropicGenerator:
    """Concrete safe-direction generator. Lazy-imports `anthropic` so the
    package imports without the dep and tests never reach it."""
    model: str = "claude-opus-4-8"

    def __call__(self, item: JudgeInput) -> str:
        import anthropic  # lazy: never imported at module top level

        client = anthropic.Anthropic()
        message = client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": _GEN_PROMPT.format(
                    doc_path=item.doc_path,
                    doc=item.doc_claim,
                    code_path=item.code_path,
                    sigs=", ".join(item.signatures) or "n/a",
                    code=item.code_text,
                ),
            }],
        )
        text = ""
        for block in getattr(message, "content", []):
            text += getattr(block, "text", "")
        return text.strip()
