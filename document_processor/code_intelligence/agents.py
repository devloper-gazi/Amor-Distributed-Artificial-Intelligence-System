"""
Five specialist agents for the Code Intelligence pipeline.

Every agent is an async callable wrapper around the injected ``llm_call``
function. Inputs come from the engine's ``AgentContext`` dataclass;
outputs are typed dataclasses the engine consumes downstream. None of
these classes import anthropic, openai, or any other vendor SDK — the
``llm_call`` is the sole bridge to a model and is injected by the
engine, exactly as ThinkingEngine does.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import prompts as P

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Shared types
# ─────────────────────────────────────────────────────────────────────────────


# Same shape as ThinkingEngine's LLMCall: prompt, system, max_tokens → str.
LLMCall = Callable[[str, str | None, int], Awaitable[str]]


@dataclass
class AgentContext:
    """
    Bundle of every input an agent might want. Plumbed end-to-end by
    the engine; agents only read the fields they care about.
    """

    user_prompt: str
    code_context: str | None = None
    triage: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    code: str | None = None
    tests: str | None = None
    language: str = "python"
    execution_feedback: str | None = None
    static_feedback: str | None = None
    test_failure: str | None = None
    # Cycle D — pytest / node:test / go test / cargo test outcome
    # forwarded to the reviewer so the verdict reflects whether the
    # implementation actually satisfies the test contract.
    test_execution_feedback: str | None = None
    debug_iteration: int = 0


@dataclass
class AgentOutput:
    """Generic agent return shape."""

    raw: str = ""  # Untouched LLM output, for debugging
    data: dict[str, Any] = field(default_factory=dict)
    code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Output parsing helpers
# ─────────────────────────────────────────────────────────────────────────────


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)
_CODE_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+\-#]*)\s*\n([\s\S]*?)```", re.MULTILINE)


def _extract_json(raw: str) -> dict[str, Any]:
    """
    Extract a JSON object from a model reply. Mirrors the helper in
    thinking/engine.py. Falls back through fenced JSON → widest braces →
    trailing-comma cleanup before giving up.
    """
    if not raw:
        raise ValueError("empty model output")

    stripped = raw.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    fence_match = _JSON_FENCE_RE.search(stripped)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    first = stripped.find("{")
    last = stripped.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidate = stripped[first : last + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            cleaned = re.sub(r",(\s*[}\]])", r"\1", candidate)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as exc:
                raise ValueError(f"could not parse model JSON: {exc}") from exc

    raise ValueError("no JSON object found in model output")


def _sniff_language_from_content(code: str, fallback: str = "") -> str:
    """Cycle D — full polyglot content sniffer.  Overrides the fence
    label when the code body is unmistakably a different language.
    Common cases the sniffer catches:

      * An LLM asked to "build a snake game website" returns
        ``<!DOCTYPE html>`` inside a ``js`` fence (or no fence at all)
        and the sandbox runs ``node main.js`` → "Unexpected token '<'".
      * A C++ deliverable shipped in an unfenced block — the sniffer
        sees ``#include <...>`` + ``int main()`` and routes to ``cpp``.
      * A Rust deliverable starting with ``fn main() {`` is routed to
        ``rust`` even when the LLM used a generic ``rs`` fence label
        the parser drops.

    Returns ``fallback`` when no strong signal is found — the caller
    keeps whatever the fence label / planner / triage said.
    """
    if not code:
        return fallback

    head_raw = "\n".join(code.lstrip().splitlines()[:8])
    head = head_raw.lower()
    head_lstripped = head.lstrip()

    # ── Markup ─────────────────────────────────────────────────
    if "<!doctype html" in head or head_lstripped.startswith("<html"):
        return "html"
    if head_lstripped.startswith("<?xml"):
        return "html"  # closest runner; html.parser tolerates xml-ish
    if head_lstripped.startswith("<?php"):
        return "php"
    # Java FIRST (before CSS) because ``public class Main {`` matches
    # the generic ``a-z...{`` CSS regex, and the Java pattern is
    # strictly more specific (requires ``public class``/``void main``
    # keywords AND ``import java`` or ``public class`` lexeme).
    if re.search(
        r"\bpublic\s+(?:static\s+)?(?:class|void\s+main)\b", head,
    ) and ("import java" in head or "public class" in head):
        return "java"
    # Solid CSS markers
    if (
        head_lstripped.startswith(("@import", "@media", "@keyframes"))
        or re.match(r"^[a-z\.\#\*][\w\.\-\#\:\,\s>+~]*\s*\{", head)
    ):
        return "css"

    # ── Shebang / shell scripts ───────────────────────────────
    if head.startswith("#!") and "python" in head:
        return "python"
    if head.startswith("#!/usr/bin/env node"):
        return "javascript"
    if head.startswith("#!/usr/bin/env ruby") or head.startswith("#!/usr/bin/ruby"):
        return "ruby"
    if head.startswith("#!/usr/bin/env bash") or head.startswith(("#!/bin/bash", "#!/bin/sh")):
        return "bash"
    if head.startswith("#!/usr/bin/env perl"):
        return "other"

    # ── Compiled languages / strong syntactic markers ─────────
    # Rust: fn main + fn foo() -> Type
    if re.search(r"\bfn\s+main\s*\(", head) or "fn " in head and "->" in head:
        return "rust"
    # Go: package main + import "fmt" / func main()
    if re.search(r"^\s*package\s+main\b", head, re.M) or re.search(
        r"\bfunc\s+main\s*\(", head,
    ):
        return "go"
    # Kotlin: fun main(args: Array<String>) | val/var with type
    if re.search(r"\bfun\s+main\s*\(", head):
        return "kotlin"
    # (Java handled earlier — before CSS — because ``public class Foo
    # {`` would otherwise match the generic CSS selector regex.)
    # C++: #include <iostream> / std:: / class X { … } with C++ flavor
    if re.search(r"#include\s*<[^>]+>", head) and (
        "std::" in head_raw or "using namespace std" in head
    ):
        return "cpp"
    # Plain C: #include <stdio.h> / int main(void)
    if re.search(r"#include\s*<(?:stdio|stdlib|string)\.h>", head):
        return "c"
    # C# (script mode):  using System; / Console.WriteLine
    if "using system" in head and "console.writeline" in head_raw.lower():
        return "csharp"

    # ── Dynamic languages ─────────────────────────────────────
    # Ruby: def name | puts | require_relative
    if re.search(r"^\s*(?:def|class|module|require|puts)\s", head, re.M):
        # Disambiguate from Python: Ruby uses ``end`` keyword + no colons
        if "end\n" in head_raw.lower() or "puts " in head:
            return "ruby"
    # PHP: <?php already handled; bare PHP without tag rare
    if "echo " in head and "$" in head and ";" in head and "<?php" not in head:
        # weak signal — skip unless very strong
        pass

    # SQL (heuristic — common DDL/DML keywords at line start)
    if re.search(
        r"^\s*(?:CREATE\s+TABLE|INSERT\s+INTO|SELECT\s+|UPDATE\s+|DROP\s+TABLE)",
        head_raw,
        re.I | re.M,
    ):
        return "sql"

    return fallback


_CPP_INCLUDE_RE = re.compile(r"^\s*#\s*include\s*([<\"][^>\"]+[>\"])", re.MULTILINE)
_CPP_STD_USAGE_RE = re.compile(r"\bstd::([A-Za-z_][A-Za-z0-9_]*)")


def _validate_cpp_includes(code: str) -> tuple[str, list[str]]:
    """Cycle D Fix #1 — pre-validate C++ #include lines against std::*
    usage and inject any missing canonical headers BEFORE the sandbox
    sees the code.  Catches the "uses std::function but forgot
    #include <functional>" gotcha that costs a debug iteration.

    Returns ``(patched_code, headers_added)``.  The patched code has
    the missing headers prepended to the existing #include block (or
    to the very top if no includes exist).  ``headers_added`` is the
    list of headers added — empty when the code is already valid.

    Pure deterministic, no LLM round-trip.  Heuristic — uses regex on
    ``std::*`` symbols, not a real parser; misses using-declarations
    + template aliases, but catches the 90% of missing-header bugs.
    """
    if not code or "std::" not in code:
        return code, []

    # Late import to avoid circular dependency at module load.
    try:
        from . import prompts as _P  # noqa: PLC0415
        symbol_to_header = _P.CPP_STD_SYMBOL_TO_HEADER
    except Exception:
        return code, []

    existing_headers = {
        m.group(1) for m in _CPP_INCLUDE_RE.finditer(code)
    }
    used_symbols = {m.group(1) for m in _CPP_STD_USAGE_RE.finditer(code)}

    needed_headers: list[str] = []
    for sym in sorted(used_symbols):
        header = symbol_to_header.get(sym)
        if header and header not in existing_headers and header not in needed_headers:
            needed_headers.append(header)

    if not needed_headers:
        return code, []

    # Find insertion point: after the last existing #include, or at
    # the very top if there are none.  Preserve any leading comment
    # block / pragma.
    last_include_match = None
    for m in _CPP_INCLUDE_RE.finditer(code):
        last_include_match = m
    inject_lines = "\n".join(f"#include {h}" for h in needed_headers)
    if last_include_match:
        end = last_include_match.end()
        # Walk to the end of the include's line.
        nl = code.find("\n", end)
        if nl == -1:
            nl = len(code)
        patched = code[:nl] + "\n" + inject_lines + code[nl:]
    else:
        patched = inject_lines + "\n\n" + code
    return patched, needed_headers


# Pattern: `{"key", funcName}` — map literal entry referencing a bare
# identifier.  We only flag when the identifier is defined LATER in
# the file (forward-reference).  Not a perfect C++ parser; matches
# the actual bug class hit by the user's output (function pointers
# stored in unordered_map literal initializers).
_CPP_MAP_FN_REF_RE = re.compile(
    r"""\{\s*                       # opening brace
        "[^"]+"\s*,\s*               # string key
        ([A-Za-z_][A-Za-z0-9_]*)     # function-name identifier
        \s*\}""",
    re.VERBOSE,
)
_CPP_FN_DEF_RE = re.compile(
    r"^\s*(?:static\s+|inline\s+|constexpr\s+|extern\s+)*"
    r"(?:[A-Za-z_][\w:<>,\s\*\&]*?)\s+"  # return type (greedy enough)
    r"([A-Za-z_][A-Za-z0-9_]*)"           # function name
    r"\s*\(",                              # opening paren
    re.MULTILINE,
)


def _detect_cpp_forward_ref(code: str) -> list[str]:
    """Detect map-literal entries that reference functions defined
    LATER in the file.  Returns the list of names that need a
    forward declaration.  Used to pre-warn the coder via the
    debugger feedback channel without requiring an extra LLM call."""
    if not code or "{" not in code or "std::" not in code:
        return []

    refs = list(_CPP_MAP_FN_REF_RE.finditer(code))
    if not refs:
        return []

    fn_def_positions: dict[str, int] = {}
    for m in _CPP_FN_DEF_RE.finditer(code):
        name = m.group(1)
        fn_def_positions.setdefault(name, m.start())

    forward_refs: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        name = ref.group(1)
        if name in seen:
            continue
        ref_pos = ref.start()
        def_pos = fn_def_positions.get(name)
        if def_pos is not None and def_pos > ref_pos:
            forward_refs.append(name)
            seen.add(name)
    return forward_refs


def _inject_cpp_forward_decls(code: str, names: list[str]) -> str:
    """Inject minimal `auto name(...);` forward declarations above
    the first function definition so the map literal compiles.
    The actual signature is reconstructed from the function's
    definition line.  Conservative: if anything looks weird,
    return the code unchanged."""
    if not code or not names:
        return code
    # Build name → full first-line signature
    decls: list[str] = []
    for name in names:
        # Find the function definition line and clone its signature
        pat = re.compile(
            r"^([^\n]*\b" + re.escape(name) + r"\s*\([^)]*\))[^;]*\{",
            re.MULTILINE,
        )
        m = pat.search(code)
        if not m:
            continue
        sig = m.group(1).rstrip()
        decls.append(f"{sig};")
    if not decls:
        return code
    block = "\n".join(decls)
    # Insert just before the first function definition.  Find earliest
    # opening brace that follows a function-shaped header.
    earliest = None
    for m in _CPP_FN_DEF_RE.finditer(code):
        earliest = m
        break
    if earliest is None:
        # Fallback: prepend after #include block
        last_include = None
        for m in _CPP_INCLUDE_RE.finditer(code):
            last_include = m
        if last_include is None:
            return block + "\n\n" + code
        nl = code.find("\n", last_include.end())
        if nl == -1:
            nl = len(code)
        return code[:nl] + "\n\n" + block + code[nl:]
    pos = earliest.start()
    return code[:pos] + "// Forward declarations (auto-injected)\n" + block + "\n\n" + code[pos:]


def _extract_code_and_meta(raw: str) -> dict[str, Any]:
    """
    Pull a code fence and a JSON metadata fence out of an LLM reply.

    Tolerant of:
      • Models that omit the JSON fence — `metadata` ends up empty.
      • Models that swap fence order — code first, metadata second is
        the spec, but we accept either.
      • Models that emit only one fence (just code) — metadata empty.
      • Models that mis-label the fence — the language sniffer
        overrides the fence label when the body content is
        unmistakably a different language.
    """
    code = ""
    language = ""
    metadata: dict[str, Any] = {}

    fences = list(_CODE_FENCE_RE.finditer(raw))
    json_blocks: list[dict[str, Any]] = []
    code_blocks: list[dict[str, Any]] = []
    for m in fences:
        lang = (m.group(1) or "").strip().lower()
        body = m.group(2).rstrip()
        if lang in ("json", "json5"):
            try:
                json_blocks.append(json.loads(body))
            except json.JSONDecodeError:
                # Try cleaning trailing commas.
                cleaned = re.sub(r",(\s*[}\]])", r"\1", body)
                try:
                    json_blocks.append(json.loads(cleaned))
                except json.JSONDecodeError:
                    pass
        else:
            code_blocks.append({"lang": lang, "body": body})

    if code_blocks:
        # Prefer the longest code block as the "main" implementation.
        primary = max(code_blocks, key=lambda b: len(b["body"]))
        code = primary["body"]
        language = primary["lang"]
    if json_blocks:
        metadata = json_blocks[0]

    # Phase 16.5 Commit L — content-based override.  The fence label
    # is wrong frequently enough (especially for HTML with embedded
    # <script>) that we let the body win when the signal is strong.
    sniffed = _sniff_language_from_content(code, fallback=language)
    if sniffed and sniffed != language:
        language = sniffed

    return {
        "code": code,
        "language": language,
        "metadata": metadata,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Base agent
# ─────────────────────────────────────────────────────────────────────────────


class _BaseAgent:
    """Common plumbing for every specialist."""

    role: str = "base"
    system_prompt: str = ""

    def __init__(self, llm_call: LLMCall, max_tokens: int = 2000):
        self.llm_call = llm_call
        self.max_tokens = max_tokens

    async def _call(self, prompt: str) -> str:
        """Single LLM call with this agent's persona + token budget."""
        raw = await self.llm_call(
            prompt,
            self.system_prompt,
            self.max_tokens,
        )
        return raw or ""

    async def _call_with_system(
        self, prompt: str, system_prompt: str,
    ) -> str:
        """Phase 17 Commit T — same as ``_call`` but lets the
        DebuggerAgent swap in the diff-mode persona without
        permanently mutating ``self.system_prompt``."""
        raw = await self.llm_call(
            prompt, system_prompt, self.max_tokens,
        )
        return raw or ""


# ─────────────────────────────────────────────────────────────────────────────
# 1 — Planner
# ─────────────────────────────────────────────────────────────────────────────


class PlannerAgent(_BaseAgent):
    role = "planner"
    system_prompt = P.PLANNER_SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> AgentOutput:
        prompt = P.planner_prompt(
            ctx.user_prompt,
            code_context=ctx.code_context,
            triage=ctx.triage,
        )
        raw = await self._call(prompt)
        # Cycle D Fix #6 — planner resilience.  The user observed two
        # different planner failures back-to-back ("could not parse
        # model JSON" and "empty model output") that wedged the
        # entire Build pipeline at the plan phase.  Add a single
        # retry with a sharper system prompt + a deterministic
        # minimal-fallback so the pipeline can always proceed past
        # plan, even when the model is misbehaving.
        try:
            data = _extract_json(raw)
        except ValueError as exc:
            logger.warning(
                "planner_json_parse_failed (attempt 1): %s — retrying with stricter prompt",
                exc,
            )
            retry_prompt = (
                prompt
                + "\n\nIMPORTANT: Your previous output was not valid JSON. "
                  "Return ONE JSON object only. No prose, no Markdown fences, "
                  "no comments. Start with `{` and end with `}`."
            )
            try:
                raw_retry = await self._call(retry_prompt)
                data = _extract_json(raw_retry)
                raw = raw_retry  # use retry output for downstream
                logger.info("planner_retry_succeeded")
            except Exception as retry_exc:  # noqa: BLE001
                logger.warning(
                    "planner_retry_failed: %s — using minimal-fallback plan",
                    retry_exc,
                )
                data = self._minimal_fallback_plan(ctx)
                # Mark explicitly so downstream phases / the UI can
                # surface the degradation.
                data["_resilience_fallback"] = True

        # Clamp lists / strings to keep one rogue plan from blowing up
        # the UI or downstream prompts.
        plan_steps_raw = data.get("plan") if isinstance(data.get("plan"), list) else []
        plan_steps_raw = plan_steps_raw or []  # narrow Optional → list for pyright
        plan_steps: list[dict[str, Any]] = []
        for i, step in enumerate(plan_steps_raw[:20], start=1):
            if not isinstance(step, dict):
                continue
            plan_steps.append(
                {
                    "step": int(step.get("step", i)),
                    "action": str(step.get("action", ""))[:200],
                    "agent": _enum(
                        step.get("agent"),
                        {"coder", "tester", "debugger", "critic", "planner"},
                        "coder",
                    ),
                    "description": str(step.get("description", ""))[:600],
                    "depends_on": [
                        int(d)
                        for d in (step.get("depends_on") or [])
                        if isinstance(d, (int, str)) and str(d).isdigit()
                    ][:5],
                }
            )

        normalized: dict[str, Any] = {
            "task_type": _enum(
                data.get("task_type"),
                {
                    "generation",
                    "debugging",
                    "review",
                    "refactoring",
                    "explanation",
                    "architecture",
                    "optimization",
                    "testing",
                },
                "generation",
            ),
            "language": _enum(
                data.get("language"),
                # Phase 17 Cycle B Commit V — added "html" and "css" so a
                # frontend ask ("snake game website") doesn't silently
                # collapse to python and then trip the pip→Flask path.
                # The sandbox's html/css runners (sandbox.py:117-164) have
                # always existed; the planner enum was the gate.
                {
                    "python",
                    "javascript",
                    "typescript",
                    "go",
                    "rust",
                    "cpp",
                    "c",
                    "java",
                    "kotlin",
                    "csharp",
                    "ruby",
                    "php",
                    "bash",
                    "html",
                    "css",
                    "sql",
                    "other",
                },
                "python",
            ),
            "framework": str(data.get("framework") or "")[:80] or None,
            "complexity": _enum(
                data.get("complexity"),
                {"trivial", "simple", "moderate", "complex", "expert"},
                "moderate",
            ),
            "title": str(data.get("title") or "Code task")[:100],
            "plan": plan_steps,
            "context_needed": [str(c)[:200] for c in (data.get("context_needed") or [])][:10],
            "risks": [str(r)[:300] for r in (data.get("risks") or [])][:10],
            "test_strategy": _enum(
                data.get("test_strategy"),
                {"unit", "integration", "e2e", "none"},
                "unit",
            ),
            "deliverable_type": _enum(
                data.get("deliverable_type"),
                {
                    "code_file",
                    "code_snippet",
                    "explanation",
                    "diff",
                    "test_suite",
                    "architecture_doc",
                },
                "code_snippet",
            ),
            # Phase 17 Commit M — strict spec block flows downstream
            # to the Coder prompt and to the engine's
            # ``_phase_execute`` (for ``dependencies``).  Keys
            # default to empty lists so older planners that don't
            # emit a spec block degrade cleanly.
            "spec": _normalise_spec(data.get("spec")),
        }
        # Surface the resilience flag so the engine can emit a
        # "planner_fallback" event (UI shows a subtle banner).
        if data.get("_resilience_fallback"):
            normalized["_resilience_fallback"] = True
        return AgentOutput(raw=raw, data=normalized)

    @staticmethod
    def _minimal_fallback_plan(ctx: AgentContext) -> dict[str, Any]:
        """Cycle D Fix #6 — deterministic minimal plan used when the
        planner LLM emits malformed/empty JSON twice.  Crafted so the
        downstream phases (implement / execute / debug / review) can
        still produce a useful deliverable instead of failing the
        entire pipeline at the plan phase.

        Strategy:
          • Inherit ``triage.language`` if available, otherwise
            "python" — matches the legacy default.
          • Single-step plan: "implement, then test".  The coder /
            critic phases use ``user_prompt`` directly so a one-line
            plan is sufficient.
          • Empty spec block — the engine handles that path safely.
        """
        triage = ctx.triage or {}
        language = triage.get("language") or "python"
        return {
            "task_type": triage.get("task_type") or "generation",
            "language": language,
            "framework": None,
            "complexity": triage.get("complexity") or "moderate",
            "title": (ctx.user_prompt or "Code task")[:80],
            "plan": [
                {
                    "step": 1,
                    "action": "implement",
                    "agent": "coder",
                    "description": (
                        "Implement the user's request in "
                        + str(language)
                        + ".  No detailed plan available — refer to "
                        "the user prompt directly."
                    ),
                    "depends_on": [],
                },
            ],
            "context_needed": [],
            "risks": [],
            "test_strategy": "unit",
            "deliverable_type": "code_snippet",
            "spec": {},
            "_resilience_fallback": True,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 2 — Coder
# ─────────────────────────────────────────────────────────────────────────────


class CoderAgent(_BaseAgent):
    role = "coder"
    system_prompt = P.CODER_SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> AgentOutput:
        plan = ctx.plan or {}
        prompt = P.coder_prompt(
            ctx.user_prompt,
            plan=plan,
            code_context=ctx.code_context,
        )
        raw = await self._call(prompt)
        parsed = _extract_code_and_meta(raw)
        if not parsed["code"]:
            return AgentOutput(
                raw=raw,
                error="Coder produced no code fence",
            )
        meta = parsed["metadata"] or {}
        resolved_language = (
            parsed["language"]
            or str(meta.get("language") or "")
            or plan.get("language", "python")
        )

        # Cycle D Fix #1 — C++-specific pre-validation.  Inject any
        # missing canonical std::* headers + minimal forward decls
        # for map-literal-of-functions patterns.  Pure deterministic
        # post-process; no extra LLM call.  Catches the two compile-
        # error classes that drove the user's 2-iteration debug loop.
        code_out = parsed["code"]
        coder_warnings: list[str] = []
        if resolved_language == "cpp":
            patched, headers_added = _validate_cpp_includes(code_out)
            if headers_added:
                code_out = patched
                coder_warnings.append(
                    "auto-added missing std headers: "
                    + ", ".join(headers_added)
                )
            forward_refs = _detect_cpp_forward_ref(code_out)
            if forward_refs:
                code_out = _inject_cpp_forward_decls(code_out, forward_refs)
                coder_warnings.append(
                    "auto-added forward declarations for: "
                    + ", ".join(forward_refs)
                )

        return AgentOutput(
            raw=raw,
            code=code_out,
            data={
                "language": resolved_language,
                "filename": str(meta.get("filename") or "")[:120] or None,
                "dependencies": [str(d)[:80] for d in (meta.get("dependencies") or [])][:20],
                "changes": str(meta.get("changes") or "")[:400],
                "coder_auto_fixes": coder_warnings,
            },
            metadata=meta,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3 — Tester
# ─────────────────────────────────────────────────────────────────────────────


class TesterAgent(_BaseAgent):
    # Tell pytest this isn't a test class — its `T*` prefix triggers
    # collection otherwise.  Cosmetic fix; the `__init__` constructor
    # would prevent actual collection anyway.
    __test__ = False

    role = "tester"
    system_prompt = P.TESTER_SYSTEM_PROMPT

    def __init__(
        self,
        llm_call: LLMCall | None = None,
        max_tokens: int = 2000,
        *,
        # Cycle F Sprint 2 — property_mode: when True (and language is
        # Python), the tester prompt is augmented to require Hypothesis
        # @given invariants in addition to example-based tests.  The
        # engine sets this from settings.code_property_tests_enabled
        # (default True).  Off for non-Python languages automatically
        # since the property_block in tester_prompt() is Python-only.
        property_mode: bool = False,
    ) -> None:
        super().__init__(llm_call=llm_call, max_tokens=max_tokens)
        self.property_mode = property_mode

    async def run(self, ctx: AgentContext) -> AgentOutput:
        if not ctx.code:
            return AgentOutput(error="No implementation to test")
        prompt = P.tester_prompt(
            ctx.user_prompt,
            code=ctx.code,
            plan=ctx.plan or {},
            property_mode=self.property_mode,
        )
        raw = await self._call(prompt)
        parsed = _extract_code_and_meta(raw)
        if not parsed["code"]:
            return AgentOutput(
                raw=raw,
                error="Tester produced no test code",
            )
        meta = parsed["metadata"] or {}
        # Cycle F Sprint 2 — surface whether the tester actually wrote
        # any @given properties.  Cheap heuristic: scan for the
        # decorator string.  Helps the operator + reflexion loop see
        # whether property_mode took effect.
        property_tests_present = bool(parsed["code"]) and (
            "@given(" in parsed["code"] or "@given\n" in parsed["code"]
        )
        return AgentOutput(
            raw=raw,
            code=parsed["code"],
            data={
                "language": (parsed["language"] or str(meta.get("language") or "") or ctx.language),
                "framework": str(meta.get("framework") or "")[:80],
                "test_count": _clamp_int(
                    meta.get("test_count"),
                    0,
                    10_000,
                    0,
                ),
                "coverage_estimate": str(meta.get("coverage_estimate") or "")[:80],
                "critical_cases": [str(c)[:200] for c in (meta.get("critical_cases") or [])][:10],
                "property_mode": self.property_mode,
                "property_tests_present": property_tests_present,
            },
            metadata=meta,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4 — Debugger
# ─────────────────────────────────────────────────────────────────────────────


class DebuggerAgent(_BaseAgent):
    role = "debugger"
    system_prompt = P.DEBUGGER_SYSTEM_PROMPT

    def __init__(
        self,
        llm_call: LLMCall | None = None,
        max_tokens: int = 1500,
        *,
        # Phase 17 Commit T — diff mode emits search/replace
        # blocks instead of the whole file.  3-5x token savings
        # on a typical 500-LOC project + fewer regressions in
        # untouched lines.  Default-on (config-flag overridden in
        # ``run`` so per-instance overrides win).
        output_mode: str = "diff",
    ) -> None:
        super().__init__(llm_call=llm_call, max_tokens=max_tokens)
        self.output_mode = output_mode

    async def run(self, ctx: AgentContext) -> AgentOutput:
        if not ctx.code:
            return AgentOutput(error="No code to debug")

        # Resolve mode at run-time so settings can flip behaviour
        # without re-instantiating agents.  Per-instance override
        # ``self.output_mode != "diff"`` always wins.
        mode = self.output_mode
        if mode == "diff":
            try:
                from ..config.settings import settings  # noqa: PLC0415
                if not getattr(
                    settings, "code_debug_diff_mode_enabled", True,
                ):
                    mode = "whole_file"
            except Exception:
                pass

        if mode == "diff":
            return await self._run_diff_mode(ctx)
        return await self._run_whole_file(ctx)

    async def _run_diff_mode(self, ctx: AgentContext) -> AgentOutput:
        """Phase 17 Commit T — emit minimal SEARCH/REPLACE diff;
        fall back to whole-file rewrite when the patch doesn't
        apply cleanly so the debug loop never wedges."""
        from .diff_apply import apply_search_replace_diff  # noqa: PLC0415

        prompt = P.debugger_prompt(
            ctx.user_prompt,
            code=ctx.code,
            execution_feedback=ctx.execution_feedback or "(no execution data)",
            static_feedback=ctx.static_feedback or "(no static analysis)",
            test_failure=ctx.test_failure,
            iteration=ctx.debug_iteration,
            language=ctx.language,
        )
        raw = await self._call_with_system(prompt, P.DEBUGGER_DIFF_SYSTEM_PROMPT)
        result = apply_search_replace_diff(ctx.code, raw)
        # Metadata is in a separate JSON fence regardless of which
        # mode the debugger ran in.
        try:
            meta = _extract_json(raw)
        except Exception:
            meta = {}
        if not result.ok:
            # Diff didn't apply → re-prompt with the fallback
            # whole-file system prompt.  Logged for diagnostics.
            try:
                from . import diagnostics as _diag  # noqa: PLC0415
                _diag.record_failure(
                    "debugger.diff_apply_failed",
                    result.error,
                    blocks_applied=result.blocks_applied,
                    blocks_total=result.blocks_total,
                )
            except Exception:
                pass
            logger.info(
                "debugger_diff_apply_failed err=%s — falling back",
                result.error,
            )
            return await self._run_whole_file(ctx)
        return AgentOutput(
            raw=raw,
            code=result.patched,
            data={
                "language": str(meta.get("language") or "") or ctx.language,
                "root_cause": str(meta.get("root_cause") or "")[:600],
                "fix_description": str(meta.get("fix_description") or "")[:600],
                "lines_changed": _clamp_int(
                    meta.get("lines_changed"), 0, 100_000,
                    result.blocks_applied,
                ),
                "confidence": _enum(
                    meta.get("confidence"),
                    {"high", "medium", "low"},
                    "medium",
                ),
                "diff_mode": True,
                "diff_blocks_applied": result.blocks_applied,
                "diff_blocks_total": result.blocks_total,
            },
            metadata=meta,
        )

    async def _run_whole_file(self, ctx: AgentContext) -> AgentOutput:
        """Original whole-file rewrite mode — kept as the fallback
        path for when diff-mode can't produce a clean patch."""
        prompt = P.debugger_prompt(
            ctx.user_prompt,
            code=ctx.code,
            execution_feedback=ctx.execution_feedback or "(no execution data)",
            static_feedback=ctx.static_feedback or "(no static analysis)",
            test_failure=ctx.test_failure,
            iteration=ctx.debug_iteration,
            language=ctx.language,
        )
        raw = await self._call(prompt)
        parsed = _extract_code_and_meta(raw)
        if not parsed["code"]:
            return AgentOutput(
                raw=raw,
                error="Debugger produced no fixed code",
            )
        meta = parsed["metadata"] or {}
        return AgentOutput(
            raw=raw,
            code=parsed["code"],
            data={
                "language": (parsed["language"] or str(meta.get("language") or "") or ctx.language),
                "root_cause": str(meta.get("root_cause") or "")[:600],
                "fix_description": str(meta.get("fix_description") or "")[:600],
                "lines_changed": _clamp_int(
                    meta.get("lines_changed"),
                    0,
                    100_000,
                    0,
                ),
                "confidence": _enum(
                    meta.get("confidence"),
                    {"high", "medium", "low"},
                    "medium",
                ),
                "diff_mode": False,
            },
            metadata=meta,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5 — Critic
# ─────────────────────────────────────────────────────────────────────────────


class CriticAgent(_BaseAgent):
    role = "critic"
    system_prompt = P.CRITIC_SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> AgentOutput:
        if not ctx.code:
            return AgentOutput(error="No code to review")
        prompt = P.critic_prompt(
            ctx.user_prompt,
            code=ctx.code,
            plan=ctx.plan or {},
            execution_feedback=ctx.execution_feedback,
            static_feedback=ctx.static_feedback,
            language=ctx.language,
            test_execution_feedback=ctx.test_execution_feedback,
        )
        raw = await self._call(prompt)
        # Cycle D Fix #6 — critic resilience.  Same pattern as
        # PlannerAgent: retry on parse failure with a sharper prompt;
        # if both attempts fail, return a neutral fallback review so
        # the pipeline reaches "done" instead of erroring out at the
        # final phase (the user's iter where critic emitted empty
        # output but the code itself was perfectly fine).
        try:
            data = _extract_json(raw)
        except ValueError as exc:
            logger.warning(
                "critic_json_parse_failed (attempt 1): %s — retrying", exc,
            )
            retry_prompt = (
                prompt
                + "\n\nIMPORTANT: Return ONE valid JSON object only. "
                  "No prose, no Markdown fences. Start with `{` and "
                  "end with `}`."
            )
            try:
                raw_retry = await self._call(retry_prompt)
                data = _extract_json(raw_retry)
                raw = raw_retry
                logger.info("critic_retry_succeeded")
            except Exception as retry_exc:  # noqa: BLE001
                logger.warning(
                    "critic_retry_failed: %s — using fallback review",
                    retry_exc,
                )
                # Neutral fallback: pipeline proceeds, user sees a
                # clearly-marked "automated review skipped" notice.
                return AgentOutput(raw=raw, data={
                    "verdict": "approved_with_minor",
                    "score": 70,
                    "strengths": [
                        "Code passed the execution / debug phases.",
                    ],
                    "issues": [{
                        "severity": "minor",
                        "description": (
                            "Automated code review unavailable for this "
                            "run (model produced unparseable output)."
                        ),
                        "suggestion": (
                            "Re-run the request to get a full review, "
                            "or inspect the code manually."
                        ),
                    }],
                    "security_concerns": [],
                    "performance_concerns": [],
                    "final_comment": (
                        "Automated review unavailable; the code was "
                        "produced successfully and passed earlier "
                        "pipeline phases."
                    ),
                    "_resilience_fallback": True,
                })

        verdict_raw = str(data.get("verdict") or "").lower()
        # Accept a couple of common spellings.
        verdict = {
            "approved": "approved",
            "approved_with_minor": "approved_with_minor",
            "approved_with_minor_comments": "approved_with_minor",
            "minor_comments": "approved_with_minor",
            "needs_revision": "needs_revision",
            "revise": "needs_revision",
            "rejected": "rejected",
        }.get(verdict_raw, "needs_revision")

        issues_raw = data.get("issues") if isinstance(data.get("issues"), list) else []
        issues_raw = issues_raw or []  # narrow Optional → list for pyright
        issues: list[dict[str, Any]] = []
        for it in issues_raw[:20]:
            if not isinstance(it, dict):
                continue
            issues.append(
                {
                    "severity": _enum(
                        it.get("severity"),
                        {"critical", "major", "minor", "nit"},
                        "minor",
                    ),
                    "description": str(it.get("description") or "")[:500],
                    "suggestion": str(it.get("suggestion") or "")[:500],
                }
            )

        normalized: dict[str, Any] = {
            "verdict": verdict,
            "score": _clamp_int(data.get("score"), 0, 100, 70),
            "strengths": [str(s)[:300] for s in (data.get("strengths") or [])][:10],
            "issues": issues,
            "security_concerns": [str(s)[:300] for s in (data.get("security_concerns") or [])][:10],
            "performance_concerns": [
                str(s)[:300] for s in (data.get("performance_concerns") or [])
            ][:10],
            "final_comment": str(data.get("final_comment") or "")[:1500],
        }

        # Cycle D Fix #4 — verdict-severity coherence guard.  The user's
        # output showed the reviewer emitting `approved_with_minor` while
        # the issue list contained a `major` item — a contradictory
        # verdict.  Auto-correct to `needs_revision` so the verdict is
        # always consistent with the worst-listed severity.
        severities = {i.get("severity", "minor") for i in normalized["issues"]}
        if {"critical", "blocker"} & severities:
            if normalized["verdict"] != "rejected":
                logger.warning(
                    "critic_verdict_severity_mismatch: critical/blocker "
                    "issue forces needs_revision (was %s)",
                    normalized["verdict"],
                )
                normalized["verdict"] = "needs_revision"
                normalized["verdict_auto_corrected"] = True
        elif "major" in severities and normalized["verdict"] == "approved_with_minor":
            logger.warning(
                "critic_verdict_severity_mismatch: downgraded "
                "approved_with_minor to needs_revision (major issue present)"
            )
            normalized["verdict"] = "needs_revision"
            normalized["verdict_auto_corrected"] = True

        # Carry the resilience flag (set by retry-success path's data)
        if data.get("_resilience_fallback"):
            normalized["_resilience_fallback"] = True

        return AgentOutput(raw=raw, data=normalized)


# ─────────────────────────────────────────────────────────────────────────────
# Triage helper (not a full agent — used by the routes layer)
# ─────────────────────────────────────────────────────────────────────────────


async def run_triage(
    llm_call: LLMCall,
    user_prompt: str,
    code_context: str | None = None,
    max_tokens: int = 600,
) -> dict[str, Any]:
    """
    Fast classification call. Returns a normalised dict with safe
    defaults if the model returns nonsense.
    """
    raw = await llm_call(
        P.triage_prompt(user_prompt, code_context),
        P.TRIAGE_SYSTEM_PROMPT,
        max_tokens,
    )
    try:
        data = _extract_json(raw or "")
    except ValueError:
        data = {}
    language = _enum(
        data.get("language"),
        # Cycle B Commit V — html/css are first-class deliverable
        # languages, mirrored from the planner enum above.
        {
            "python",
            "javascript",
            "typescript",
            "go",
            "rust",
            "cpp",
            "java",
            "bash",
            "html",
            "css",
            "other",
        },
        "python",
    )

    # Cycle B Commit V — heuristic correction.  The triage LLM frequently
    # confuses "snake game website" for python because it sees "game"
    # and falls back to its training prior.  When the user prompt clearly
    # asks for a website / HTML / CSS / canvas / browser deliverable
    # AND triage said python, override to html.  This is a one-way
    # nudge — we never override an explicitly chosen non-python
    # language.
    language = _heuristic_language_override(user_prompt, language)

    # Cycle D — domain detection.  Rule-based; runs OUTSIDE the LLM
    # call so it's deterministic + free.  Falls through to ``None``
    # for unrecognised prompts (pipeline degrades to baseline).
    from .domain_templates import _detect_domain  # noqa: PLC0415
    domain_detection = _detect_domain(user_prompt)

    # When the domain has a strong language preference (e.g. game →
    # html canvas), upgrade the language IF the LLM defaulted to a
    # weaker choice (typically python+pygame which doesn't run in a
    # headless sandbox).  We only override when:
    #   * domain has preferred_languages, AND
    #   * the current language isn't already in that preference list,
    #     AND
    #   * the current language is the python-default (the
    #     mis-classification we're trying to fix)
    if domain_detection and domain_detection.get("preferred_languages"):
        prefs = domain_detection["preferred_languages"]
        if language == "python" and language not in prefs:
            language = prefs[0]

    return {
        "task_type": _enum(
            data.get("task_type"),
            {
                "generation",
                "debugging",
                "review",
                "refactoring",
                "explanation",
                "architecture",
                "optimization",
                "testing",
            },
            "generation",
        ),
        "language": language,
        "complexity": _enum(
            data.get("complexity"),
            {"trivial", "simple", "moderate", "complex", "expert"},
            "moderate",
        ),
        "needs_execution": bool(data.get("needs_execution", True)),
        "needs_tests": bool(data.get("needs_tests", True)),
        "estimated_phases": [str(p)[:30] for p in (data.get("estimated_phases") or [])][:9],
        # Cycle D — domain enrichment carried through the pipeline.
        # Planner sees these in ``ctx.triage`` and uses the
        # ``must_have_features`` list to lock its plan.
        "domain": domain_detection,
    }


# Cycle B Commit V — keyword-based fallback the engine uses to short-
# circuit a python misroute when the prompt is unmistakably a frontend /
# web ask.  Kept conservative on purpose: only fires when the prompt
# directly mentions web/HTML/CSS/browser primitives AND the language
# guess defaulted to python.  A python-with-html-doc prompt won't be
# flipped because the prompt usually says "python" first.
_FRONTEND_KEYWORDS = (
    "html",
    "css",
    "<!doctype",
    "stylesheet",
    "browser",
    "web page",
    "webpage",
    "website",
    "static site",
    "landing page",
    "single page app",
    "<canvas",
    "jsfiddle",
    "codepen",
    "tailwind",
    "p5.js",
    "phaser",
    "three.js",
    "html/css",
    "html and css",
    "no python",
    "no backend",
    "no flask",
    "frontend",
    "front-end",
    "front end",
)
# Browser-native game scaffolds that the LLM keeps misrouting to
# python+pygame.  When triage said python and the prompt names one of
# these classic games WITHOUT also naming a python game framework,
# default to an html/canvas deliverable — which actually runs in the
# user's browser without a sandbox install step.
_BROWSER_GAME_KEYWORDS = (
    "snake game",
    "tetris",
    "pong",
    "breakout",
    "tic tac toe",
    "minesweeper",
    "2048",
    "flappy",
    "asteroids",
    "platformer",
)
_PYTHON_HARDCODED_HINTS = (
    "python",
    "fastapi",
    "flask",
    "django",
    "pytest",
    "pip install",
    "conda",
    "numpy",
    "pandas",
    "pygame",
    "pyglet",
    "tkinter",
    "kivy",
    "arcade",
    "panda3d",
)


def _heuristic_language_override(prompt: str, current: str) -> str:
    """Cycle D — two-pass language override:

    Pass 1 (HIGHEST PRIORITY): explicit user language mention.  If the
    user wrote ``in python`` / ``with rust`` / ``using kotlin`` /
    ``a java program`` / ``write me ruby code``, force that language
    regardless of what triage said.  This fixes the "user said rust,
    triage said python because the prompt also mentioned 'safe'"
    failure mode we saw on long prompts.

    Pass 2 (FALLBACK): the legacy frontend keyword override — only
    fires when the current language is python AND the prompt
    unambiguously asks for HTML/CSS without naming a python framework.
    """
    p = (prompt or "").lower()

    # Pass 1 — explicit language match.  Patterns ordered by specificity
    # so multi-word matches (``in c++``, ``in c#``) win over single-char
    # noise (``in c``).  Each entry: (regex, canonical enum value).
    explicit_patterns: list[tuple[str, str]] = [
        # Compound / specific FIRST so they win over generic 'c'.
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)c\+\+", "cpp"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)c\#", "csharp"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)c[\-\s]?sharp", "csharp"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)(?:typescript|ts)\b", "typescript"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)javascript\b", "javascript"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)python\b", "python"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)(?:golang|go)\b", "go"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)rust\b", "rust"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)java\b(?!script)", "java"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)kotlin\b", "kotlin"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)ruby\b", "ruby"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)php\b", "php"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)(?:bash|shell)\b", "bash"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)sql\b", "sql"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)html(?:\s*(?:and|\+|/)\s*css)?", "html"),
        (r"\bplain\s+c\b", "c"),
        (r"\b(?:(?:in|with|using)\s+|write\s+(?:me\s+)?)c\b(?!\+\+|\#)", "c"),
        # Article + language ("a python script", "a rust crate", "an
        # SQL query") — accepts both ``a`` and ``an`` so vowel-initial
        # languages match too.
        (r"\b(?:a|an)\s+python\b", "python"),
        (r"\b(?:a|an)\s+rust\b", "rust"),
        (r"\b(?:a|an)\s+(?:golang|go)\b", "go"),
        (r"\b(?:a|an)\s+java\b(?!script)", "java"),
        (r"\b(?:a|an)\s+kotlin\b", "kotlin"),
        (r"\b(?:a|an)\s+ruby\b", "ruby"),
        (r"\b(?:a|an)\s+php\b", "php"),
        (r"\b(?:a|an)\s+typescript\b", "typescript"),
        (r"\b(?:a|an)\s+javascript\b", "javascript"),
        (r"\b(?:a|an)\s+c\+\+", "cpp"),
        (r"\b(?:a|an)\s+c\#", "csharp"),
        (r"\b(?:a|an)\s+sql\b", "sql"),
        (r"\b(?:a|an)\s+bash\b", "bash"),
    ]
    for pattern, lang in explicit_patterns:
        if re.search(pattern, p):
            return lang

    # Pass 2 — legacy frontend override (python-default safety net).
    if current != "python":
        return current
    if any(h in p for h in _PYTHON_HARDCODED_HINTS):
        return current
    if any(kw in p for kw in _FRONTEND_KEYWORDS):
        return "html"
    if any(kw in p for kw in _BROWSER_GAME_KEYWORDS):
        return "html"
    return current


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _enum(value: Any, allowed: set, default: str) -> str:
    v = str(value or "").lower()
    return v if v in allowed else default


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


# Phase 17 Commit M — spec-block normaliser.  The planner prompt
# now requires a ``spec`` block (invariants / signatures /
# preconditions / postconditions / error_cases / dependencies).
# Older planners that don't emit the block degrade to empty lists
# so downstream agents (Coder, engine ``_phase_execute``) keep
# working.  Each list is capped so a runaway model doesn't blow
# up the prompt or the sandbox install command-line.
def _normalise_spec(value: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {
        "invariants": [],
        "signatures": [],
        "preconditions": [],
        "postconditions": [],
        "error_cases": [],
        "dependencies": [],
    }
    if not isinstance(value, dict):
        return out
    caps = {
        "invariants": (10, 300),
        "signatures": (15, 400),
        "preconditions": (10, 300),
        "postconditions": (10, 300),
        "error_cases": (10, 300),
        "dependencies": (20, 80),
    }
    for key, (max_count, max_len) in caps.items():
        items = value.get(key)
        if not isinstance(items, list):
            continue
        cleaned: list[str] = []
        for item in items[:max_count]:
            if not isinstance(item, str):
                continue
            s = item.strip()
            if not s:
                continue
            cleaned.append(s[:max_len])
        out[key] = cleaned
    return out
