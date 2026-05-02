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
    """Phase 16.5 Commit L — override the fence label when the code
    body is unmistakably a different language.  Common case: an LLM
    asked to "build a snake game website" returns ``<!DOCTYPE html>``
    inside a ``js`` fence (or no fence at all) and the sandbox runs
    it through ``node main.js`` → ``Unexpected token '<'`` crash.
    Sniff the first few non-blank lines and force the right runner.
    """
    if not code:
        return fallback
    head = "\n".join(code.lstrip().splitlines()[:5]).lower()
    if "<!doctype html" in head or head.lstrip().startswith("<html"):
        return "html"
    if head.lstrip().startswith("<?xml"):
        return "html"  # closest runner; html.parser tolerates xml-ish
    # Solid CSS markers
    if (
        head.lstrip().startswith(("@import", "@media", "@keyframes"))
        or re.match(r"^[a-z\.\#\*][\w\.\-\#\:\,\s>+~]*\s*\{", head)
    ):
        return "css"
    # Python shebang / common idioms
    if head.startswith("#!") and "python" in head:
        return "python"
    if head.startswith("#!/usr/bin/env node"):
        return "javascript"
    return fallback


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
        try:
            data = _extract_json(raw)
        except ValueError as exc:
            logger.warning("planner_json_parse_failed: %s", exc)
            return AgentOutput(raw=raw, error=str(exc))

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
                {
                    "python",
                    "javascript",
                    "typescript",
                    "go",
                    "rust",
                    "cpp",
                    "java",
                    "bash",
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
        return AgentOutput(raw=raw, data=normalized)


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
        return AgentOutput(
            raw=raw,
            code=parsed["code"],
            data={
                "language": (
                    parsed["language"]
                    or str(meta.get("language") or "")
                    or plan.get("language", "python")
                ),
                "filename": str(meta.get("filename") or "")[:120] or None,
                "dependencies": [str(d)[:80] for d in (meta.get("dependencies") or [])][:20],
                "changes": str(meta.get("changes") or "")[:400],
            },
            metadata=meta,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3 — Tester
# ─────────────────────────────────────────────────────────────────────────────


class TesterAgent(_BaseAgent):
    role = "tester"
    system_prompt = P.TESTER_SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> AgentOutput:
        if not ctx.code:
            return AgentOutput(error="No implementation to test")
        prompt = P.tester_prompt(
            ctx.user_prompt,
            code=ctx.code,
            plan=ctx.plan or {},
        )
        raw = await self._call(prompt)
        parsed = _extract_code_and_meta(raw)
        if not parsed["code"]:
            return AgentOutput(
                raw=raw,
                error="Tester produced no test code",
            )
        meta = parsed["metadata"] or {}
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
        )
        raw = await self._call(prompt)
        try:
            data = _extract_json(raw)
        except ValueError as exc:
            logger.warning("critic_json_parse_failed: %s", exc)
            return AgentOutput(raw=raw, error=str(exc))

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
        "language": _enum(
            data.get("language"),
            {"python", "javascript", "typescript", "go", "rust", "cpp", "java", "bash", "other"},
            "python",
        ),
        "complexity": _enum(
            data.get("complexity"),
            {"trivial", "simple", "moderate", "complex", "expert"},
            "moderate",
        ),
        "needs_execution": bool(data.get("needs_execution", True)),
        "needs_tests": bool(data.get("needs_tests", True)),
        "estimated_phases": [str(p)[:30] for p in (data.get("estimated_phases") or [])][:9],
    }


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
