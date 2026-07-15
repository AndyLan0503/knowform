"""The LLM-judge frontier seam (Tier 3 of the cost model).

`plan` takes an OPTIONAL injected judge. With none, frontier bindings are
`needs-judge` and zero tokens are spent. `build_frontier` assembles a tight
`JudgeInput` (the claim + bound symbol text/signatures, not whole files).
`AnthropicJudge` lazy-imports its client so the package imports with no
third-party deps and tests never touch the network.
"""
from __future__ import annotations

import ast
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

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


@dataclass
class MatchInput:
    """The `init` Tier-2 neighborhood: one ambiguous doc reference plus the
    enumerated code symbols it could name - never whole files."""
    doc_path: str
    region_text: str
    identifier: str
    candidates: list[tuple[str, str]]   # (code_anchor, governs) options


@dataclass
class MatchResult:
    matched: bool
    code_anchor: str | None = None
    governs: str | None = None
    direction: str = "code-is-truth"
    confidence: float | None = None
    rationale: str = ""


class Matcher(Protocol):
    """The `init` fuzzy-match seam. Given a reference the deterministic tiers
    could not bind and its candidate symbols, pick which one (if any) the prose
    describes. With none injected, `init` spends zero tokens."""
    def __call__(self, item: MatchInput) -> MatchResult: ...


_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "matched": {"type": "boolean"},
        "code_anchor": {"type": "string"},
        "governs": {"type": "string"},
        "direction": {"type": "string",
                      "enum": [d.value for d in Direction]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["matched"],
}

_MATCH_PROMPT = (
    "A documentation passage references `{identifier}`. From the candidate code "
    "symbols below, decide which ONE the passage describes, or that none clearly "
    "does. The reference may name a candidate exactly or approximately (a "
    "rename) - choose only from the listed candidates, never invent a symbol. "
    "Prefer code-is-truth; use manual when unsure of direction; never "
    "doc-is-truth.\n\n"
    "DOC ({doc_path}):\n{region}\n\n"
    "CANDIDATE SYMBOLS:\n{candidates}"
)


@dataclass
class AnthropicMatcher:
    """Concrete Tier-2 matcher. Lazy-imports `anthropic` so the package imports
    without the dep and tests never reach it."""
    model: str = "claude-opus-4-8"

    def __call__(self, item: MatchInput) -> MatchResult:
        import anthropic  # lazy: never imported at module top level

        client = anthropic.Anthropic()
        options = "\n".join(f"- {anchor} in {governs}"
                            for anchor, governs in item.candidates)
        message = client.messages.create(
            model=self.model,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            output_config={"format": {
                "type": "json_schema",
                "schema": _MATCH_SCHEMA,
            }},
            messages=[{
                "role": "user",
                "content": _MATCH_PROMPT.format(
                    identifier=item.identifier,
                    doc_path=item.doc_path,
                    region=item.region_text,
                    candidates=options,
                ),
            }],
        )
        return _parse_match(message)


def _parse_match(message: object) -> MatchResult:
    import json

    text = ""
    for block in getattr(message, "content", []):
        text += getattr(block, "text", "")
    data = json.loads(text)
    return MatchResult(
        matched=bool(data.get("matched")),
        code_anchor=data.get("code_anchor"),
        governs=data.get("governs"),
        direction=data.get("direction", "code-is-truth"),
        confidence=data.get("confidence"),
        rationale=data.get("rationale", ""),
    )


class ClaudeCliUnavailable(RuntimeError):
    """Raised when the local `claude` binary cannot be located."""


def _candidate_binary_paths() -> list[Path]:
    """Where Claude Code installs `claude` when it is not on a bare PATH
    (e.g. invoked from a subprocess or a GUI-launched shell)."""
    home = Path.home()
    return [
        home / ".claude" / "local" / "claude",
        home / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ]


def _find_claude_binary() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for c in _candidate_binary_paths():
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def _default_claude_runner(cmd: list[str], timeout: float) -> str:
    import subprocess  # lazy: never reached under test

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ClaudeCliUnavailable(
            f"`claude` exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


_CLI_MATCH_INSTRUCTION = (
    "\n\nReply with ONLY a JSON object (no prose, no code fence) with keys: "
    '"matched" (boolean), "code_anchor" (string), "governs" (string), '
    '"direction" ("code-is-truth" or "manual"), "confidence" (0..1 number), '
    '"rationale" (string). Set "matched" false when no candidate clearly fits.'
)


@dataclass
class ClaudeCliMatcher:
    """Tier-2 matcher that rides the local Claude Code CLI, so it authenticates
    with whatever the user is logged in with (a subscription included) - no API
    key. Shells out to `claude -p ... --output-format json`. The `runner` seam
    keeps tests off the network; `binary` is auto-detected when omitted."""
    binary: str | None = None
    model: str | None = None
    timeout: float = 120.0
    runner: Callable[[list[str], float], str] | None = None

    def __post_init__(self) -> None:
        if self.binary is None:
            self.binary = _find_claude_binary()
        if self.binary is None:
            raise ClaudeCliUnavailable(
                "the `claude` CLI was not found; install Claude Code and log "
                "in, or use --anthropic with ANTHROPIC_API_KEY")
        if self.runner is None:
            self.runner = _default_claude_runner

    def __call__(self, item: MatchInput) -> MatchResult:
        options = "\n".join(f"- {anchor} in {governs}"
                            for anchor, governs in item.candidates)
        prompt = _MATCH_PROMPT.format(
            identifier=item.identifier,
            doc_path=item.doc_path,
            region=item.region_text,
            candidates=options,
        ) + _CLI_MATCH_INSTRUCTION
        cmd = [self.binary, "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        stdout = self.runner(cmd, self.timeout)
        return _parse_cli_match(stdout)


def _parse_cli_match(stdout: str) -> MatchResult:
    import json

    text = stdout
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        env = None
    if isinstance(env, dict) and "matched" not in env and "result" in env:
        r = env["result"]
        text = r if isinstance(r, str) else json.dumps(r)
    data = _extract_json_object(text)
    return MatchResult(
        matched=bool(data.get("matched")),
        code_anchor=data.get("code_anchor"),
        governs=data.get("governs"),
        direction=data.get("direction", "code-is-truth"),
        confidence=data.get("confidence"),
        rationale=data.get("rationale", ""),
    )


def _extract_json_object(text: str) -> dict:
    """The first balanced `{...}` in `text`, tolerating code fences and prose
    the model may wrap around it."""
    import json

    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in claude output: {text[:200]!r}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError(f"unbalanced JSON in claude output: {text[:200]!r}")


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
