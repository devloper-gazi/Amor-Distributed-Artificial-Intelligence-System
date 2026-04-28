"""
Code Intelligence pipeline engine.

Mirrors the structure of ``thinking/engine.py`` but with a 9-phase
pipeline tuned for software-engineering tasks:

    triage → model_prep → plan → implement → execute →
    analyze → test → debug (loop) → review

The engine is **LLM-agnostic**: the caller injects ``llm_call``, exactly
as ThinkingEngine does, so the same pipeline works against any local
Ollama tag, a mock, or (in principle) a remote model — but in practice
the routes only ever wire it to local Ollama, per the zero-API
constraint.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .agents import (
    AgentContext,
    CoderAgent,
    CriticAgent,
    DebuggerAgent,
    LLMCall,
    PlannerAgent,
    TesterAgent,
    run_triage,
)
from .hooks import NoopHooks, PhaseHooks
from .sandbox import ExecutionResult, ExecutionSandbox
from .static_analysis import StaticAnalysisHarness, StaticAnalysisResult

logger = logging.getLogger(__name__)


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
PhaseStatus = Literal[
    "pending",
    "in_progress",
    "completed",
    "failed",
    "skipped",
]


# ─────────────────────────────────────────────────────────────────────────────
# Phase definitions + budgets
# ─────────────────────────────────────────────────────────────────────────────


CODE_PHASES: list[tuple] = [
    ("triage", "Triaging request"),
    ("model_prep", "Preparing models"),
    ("plan", "Planning approach"),
    ("implement", "Implementing"),
    ("execute", "Executing code"),
    ("analyze", "Analyzing quality"),
    ("test", "Running tests"),
    ("debug", "Debugging & fixing"),
    ("review", "Final review"),
]


# Mirrors ThinkingEngine.PHASE_PROGRESS — per-phase end progress %.
PHASE_PROGRESS: dict[str, int] = {
    "triage": 10,
    "model_prep": 15,
    "plan": 25,
    "implement": 50,
    "execute": 60,
    "analyze": 68,
    "test": 78,
    "debug": 88,
    "review": 98,
}


# Effort tier → per-phase token budget. Mirrors thinking/engine.py.
_CODE_EFFORT_BUDGETS: dict[str, dict[str, int]] = {
    "basic": {"plan": 800, "implement": 2000, "test": 600, "debug": 1000, "review": 600},
    "medium": {"plan": 1200, "implement": 3500, "test": 1000, "debug": 1800, "review": 1000},
    "deep": {"plan": 1500, "implement": 5000, "test": 1500, "debug": 2500, "review": 1500},
    "expert": {"plan": 2000, "implement": 7000, "test": 2000, "debug": 3500, "review": 2000},
    "ultra": {"plan": 2500, "implement": 9000, "test": 3000, "debug": 5000, "review": 3000},
}


# Effort tier → max debug→fix→reexecute iterations.
_DEFAULT_DEBUG_ITERATIONS: dict[str, int] = {
    "basic": 1,
    "medium": 3,
    "deep": 3,
    "expert": 5,
    "ultra": 5,
}


# Legacy aliases — same set as ThinkingEngine.
_EFFORT_ALIAS: dict[str, str] = {
    "quick": "basic",
    "fast": "basic",
    "standard": "medium",
    "balanced": "medium",
    "thorough": "deep",
    "comprehensive": "expert",
    "exhaustive": "ultra",
}


def _canonical_effort(effort: str) -> str:
    if not effort:
        return "medium"
    key = str(effort).strip().lower()
    key = _EFFORT_ALIAS.get(key, key)
    return key if key in _CODE_EFFORT_BUDGETS else "medium"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class CodePhase:
    name: str
    label: str
    status: PhaseStatus = "pending"
    detail: dict[str, Any] = field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────


class CodeIntelligenceEngine:
    """Orchestrates a single Code Intelligence session end-to-end."""

    PHASE_PROGRESS = PHASE_PROGRESS  # exposed for routes

    def __init__(
        self,
        *,
        prompt: str,
        code_context: str | None,
        language: str | None,
        effort: str,
        provider: str,
        llm_call: LLMCall,
        sandbox: ExecutionSandbox | None = None,
        static_harness: StaticAnalysisHarness | None = None,
        enable_execution: bool = True,
        enable_static_analysis: bool = True,
        enable_testing: bool = True,
        max_debug_iterations: int | None = None,
        on_event: EventCallback | None = None,
        # Optional pre-flight helpers (the routes layer plugs these in
        # when it wants to surface model-download progress to the UI).
        prepare_models: Callable[[], Awaitable[dict[str, str]]] | None = None,
        # Charter §6 Mandate 3 — phase-boundary hooks. Defaults to
        # NoopHooks; routes wire ChainedHooks(TelemetryHooks(), ...)
        # when they want span emission per phase.
        hooks: PhaseHooks | None = None,
        # v4 — per-phase agent-role hint. Routes wire this to the
        # local-AI ContextVar `set_active_role` so multi-model strategy
        # (per_role / ensemble) can pick a different tag for each
        # phase without changing the engine's LLM-agnostic shape.
        # Called once at the START of every agent phase with the role
        # name (e.g. "planner", "coder", "tester", "debugger", "critic").
        role_setter: Callable[[str | None], Any] | None = None,
    ) -> None:
        self.prompt = prompt
        self.code_context = code_context or None
        self.language_hint = (language or "").lower() or None
        self.effort = _canonical_effort(effort)
        self.provider = provider
        self.llm_call = llm_call
        self._budgets = _CODE_EFFORT_BUDGETS[self.effort]
        self.max_debug_iterations = (
            max_debug_iterations
            if max_debug_iterations is not None
            else _DEFAULT_DEBUG_ITERATIONS[self.effort]
        )
        self.enable_execution = enable_execution
        self.enable_static_analysis = enable_static_analysis
        self.enable_testing = enable_testing

        self.sandbox = sandbox
        self.static_harness = static_harness or StaticAnalysisHarness()
        self._prepare_models = prepare_models
        self._on_event = on_event or _noop_event
        # PhaseHooks default = NoopHooks (zero overhead, never raises).
        self._hooks: PhaseHooks = hooks or NoopHooks()
        # v4 — role_setter is a no-op fallback so engine code stays
        # branch-free at every phase boundary.
        self._role_setter = role_setter or (lambda _r: None)

        self.phases: list[CodePhase] = [
            CodePhase(name=name, label=label) for name, label in CODE_PHASES
        ]
        self._phase_index = {p.name: p for p in self.phases}

        # Accumulated state — surfaced in snapshot()
        self.triage: dict[str, Any] = {}
        self.models_used: dict[str, str] = {}
        self.plan: dict[str, Any] = {}
        self.code: str | None = None
        self.tests: str | None = None
        self.execution_results: list[dict[str, Any]] = []
        self.static_analysis: StaticAnalysisResult | None = None
        self.review: dict[str, Any] = {}
        self.deliverable_markdown: str = ""
        self.debug_iterations_used: int = 0
        self.detected_language: str = self.language_hint or "python"
        self.title: str = "Code task"

    # ── helpers ───────────────────────────────────────────────────────────

    async def _emit(self, event: dict[str, Any]) -> None:
        try:
            await self._on_event(event)
        except Exception:
            logger.exception("code.on_event callback raised")

    async def _run_phase(
        self,
        name: str,
        runner: Callable[[], Awaitable[dict[str, Any] | None]],
    ) -> dict[str, Any] | None:
        phase = self._phase_index[name]
        phase.status = "in_progress"
        phase.started_at = _now()
        await self._emit({"type": "phase_start", "phase": name, "label": phase.label})
        # Charter §6 Mandate 3 — fire phase-boundary hook.
        await self._hooks.before_phase(name, self.snapshot())
        try:
            result = await runner()
            phase.status = "completed"
            phase.completed_at = _now()
            phase.detail = result or {}
            await self._emit(
                {
                    "type": "phase_complete",
                    "phase": name,
                    "label": phase.label,
                    "detail": phase.detail,
                }
            )
            await self._hooks.after_phase(name, self.snapshot(), result)
            return result
        except Exception as exc:
            phase.status = "failed"
            phase.completed_at = _now()
            phase.detail = {"error": str(exc)}
            logger.exception("code.phase_failed phase=%s", name)
            # Hook still fires on failure so telemetry captures the timing.
            try:
                await self._hooks.after_phase(name, self.snapshot(), None)
            except Exception:
                logger.debug("after_phase hook raised on failure path")
            await self._emit(
                {
                    "type": "phase_failed",
                    "phase": name,
                    "label": phase.label,
                    "error": str(exc),
                }
            )
            return None

    def _skip(self, name: str, reason: str) -> None:
        self._phase_index[name].status = "skipped"
        self._phase_index[name].detail = {"reason": reason}

    # ── phases ────────────────────────────────────────────────────────────

    async def _phase_triage(self) -> dict[str, Any]:
        triage = await run_triage(
            self.llm_call,
            self.prompt,
            self.code_context,
            max_tokens=600,
        )
        # Honour the explicit language hint over what the model guessed.
        if self.language_hint:
            triage["language"] = self.language_hint
        self.triage = triage
        self.detected_language = triage.get("language") or self.detected_language
        return triage

    async def _phase_model_prep(self) -> dict[str, Any]:
        if self._prepare_models is None:
            return {"models_used": {}, "skipped": True}
        models_used = await self._prepare_models()
        self.models_used = dict(models_used or {})
        return {"models_used": self.models_used}

    async def _phase_plan(self) -> dict[str, Any]:
        self._role_setter("planner")
        agent = PlannerAgent(self.llm_call, max_tokens=self._budgets["plan"])
        out = await agent.run(
            AgentContext(
                user_prompt=self.prompt,
                code_context=self.code_context,
                triage=self.triage,
            )
        )
        if out.error or not out.data:
            raise RuntimeError(out.error or "Planner returned empty plan")
        self.plan = out.data
        # Pin language + title from the plan if present.
        if out.data.get("language"):
            self.detected_language = out.data["language"]
        if out.data.get("title"):
            self.title = out.data["title"]
        return out.data

    async def _phase_implement(self) -> dict[str, Any]:
        self._role_setter("coder")
        agent = CoderAgent(self.llm_call, max_tokens=self._budgets["implement"])
        out = await agent.run(
            AgentContext(
                user_prompt=self.prompt,
                code_context=self.code_context,
                triage=self.triage,
                plan=self.plan,
                language=self.detected_language,
            )
        )
        if out.error or not out.code:
            raise RuntimeError(out.error or "Coder produced no code")
        self.code = out.code
        if out.data.get("language"):
            self.detected_language = out.data["language"]
        await self._emit(
            {
                "type": "code_ready",
                "language": self.detected_language,
                "code": self.code,
                "metadata": out.data,
            }
        )
        return {
            "language": self.detected_language,
            "loc": len(self.code.splitlines()),
            "metadata": out.data,
        }

    async def _phase_execute(self) -> dict[str, Any]:
        if not self.enable_execution or not self.sandbox or not self.code:
            self._skip("execute", "execution disabled or no code")
            return {"skipped": True}
        await self._emit(
            {
                "type": "execution_start",
                "language": self.detected_language,
            }
        )
        result = await self.sandbox.execute(
            code=self.code,
            language=self.detected_language,
        )
        self.execution_results.append(result.to_dict())
        await self._emit(
            {
                "type": "execution_result",
                "result": result.to_dict(),
                "iteration": 0,
            }
        )
        return result.to_dict()

    async def _phase_analyze(self) -> dict[str, Any]:
        if not self.enable_static_analysis or not self.code:
            self._skip("analyze", "static analysis disabled or no code")
            return {"skipped": True}
        sa = await self.static_harness.analyze(self.code, self.detected_language)
        self.static_analysis = sa
        payload = sa.to_dict()
        await self._emit(
            {
                "type": "static_analysis_result",
                "result": payload,
            }
        )
        return payload

    async def _phase_test(self) -> dict[str, Any]:
        if not self.enable_testing or not self.code:
            self._skip("test", "testing disabled or no code")
            return {"skipped": True}
        # Skip tests for explanation/architecture-only deliverables.
        if self.plan.get("deliverable_type") in {
            "explanation",
            "architecture_doc",
        }:
            self._skip("test", "non-code deliverable")
            return {"skipped": True}
        self._role_setter("tester")
        agent = TesterAgent(self.llm_call, max_tokens=self._budgets["test"])
        out = await agent.run(
            AgentContext(
                user_prompt=self.prompt,
                plan=self.plan,
                code=self.code,
                language=self.detected_language,
            )
        )
        if out.error or not out.code:
            # Tester failure shouldn't kill the pipeline.
            return {
                "skipped": True,
                "reason": out.error or "tester returned empty",
            }
        self.tests = out.code
        await self._emit(
            {
                "type": "test_ready",
                "code": self.tests,
                "metadata": out.data,
            }
        )
        return out.data

    async def _phase_debug(self) -> dict[str, Any]:
        """
        Debug → re-execute loop. Only runs when:
          • Execution was enabled AND
          • The most recent execution result failed (or timed out).
        Up to ``self.max_debug_iterations`` loop iterations.
        """
        if not self.enable_execution or not self.sandbox or not self.code:
            self._skip("debug", "execution disabled or no code")
            return {"skipped": True}
        if self.max_debug_iterations <= 0:
            self._skip("debug", "max_debug_iterations=0")
            return {"skipped": True}
        # If the most recent execution succeeded, nothing to debug.
        if self.execution_results and self.execution_results[-1].get(
            "success",
            False,
        ):
            self._skip("debug", "execution already passing")
            return {"skipped": True, "reason": "no failure"}

        self._role_setter("debugger")
        debugger = DebuggerAgent(self.llm_call, max_tokens=self._budgets["debug"])

        last_result: dict[str, Any] = {}
        iteration = 0
        while iteration < self.max_debug_iterations:
            iteration += 1
            self.debug_iterations_used = iteration

            await self._emit(
                {
                    "type": "debug_iteration_start",
                    "iteration": iteration,
                    "max": self.max_debug_iterations,
                }
            )

            exec_feedback = (
                ExecutionResult(
                    **{
                        k: v
                        for k, v in self.execution_results[-1].items()
                        if k
                        in {
                            "exit_code",
                            "stdout",
                            "stderr",
                            "timed_out",
                            "error",
                            "duration_ms",
                            "language",
                        }
                    }
                ).to_feedback_str()
                if self.execution_results
                else "(no execution data)"
            )
            static_feedback = (
                self.static_analysis.to_feedback_str()
                if self.static_analysis
                else "(no static analysis)"
            )

            out = await debugger.run(
                AgentContext(
                    user_prompt=self.prompt,
                    code_context=self.code_context,
                    plan=self.plan,
                    code=self.code,
                    language=self.detected_language,
                    execution_feedback=exec_feedback,
                    static_feedback=static_feedback,
                    debug_iteration=iteration,
                )
            )
            if out.error or not out.code:
                logger.warning(
                    "debug_iteration_no_fix iteration=%d error=%s",
                    iteration,
                    out.error,
                )
                break

            self.code = out.code
            await self._emit(
                {
                    "type": "code_ready",
                    "language": self.detected_language,
                    "code": self.code,
                    "metadata": out.data,
                    "iteration": iteration,
                }
            )

            # Re-run the sandbox with the patched code.
            await self._emit(
                {
                    "type": "execution_start",
                    "iteration": iteration,
                }
            )
            new_result = await self.sandbox.execute(
                code=self.code,
                language=self.detected_language,
            )
            new_dict = new_result.to_dict()
            self.execution_results.append(new_dict)
            await self._emit(
                {
                    "type": "execution_result",
                    "result": new_dict,
                    "iteration": iteration,
                }
            )
            last_result = new_dict
            if new_result.success:
                break

        return {
            "iterations": iteration,
            "max_iterations": self.max_debug_iterations,
            "last_result": last_result,
            "final_success": bool(
                self.execution_results and self.execution_results[-1].get("success", False),
            ),
        }

    async def _phase_review(self) -> dict[str, Any]:
        if not self.code:
            self._skip("review", "no code to review")
            return {"skipped": True}
        self._role_setter("critic")
        agent = CriticAgent(self.llm_call, max_tokens=self._budgets["review"])
        exec_feedback = None
        if self.execution_results:
            last = self.execution_results[-1]
            exec_feedback = (
                f"exit={last.get('exit_code')} "
                f"timed_out={last.get('timed_out')} "
                f"stdout(len)={len(last.get('stdout', ''))} "
                f"stderr={last.get('stderr', '')[:600]}"
            )
        static_feedback = self.static_analysis.to_feedback_str() if self.static_analysis else None
        out = await agent.run(
            AgentContext(
                user_prompt=self.prompt,
                plan=self.plan,
                code=self.code,
                language=self.detected_language,
                execution_feedback=exec_feedback,
                static_feedback=static_feedback,
            )
        )
        if out.error or not out.data:
            # Don't fail the whole pipeline if review breaks.
            self.review = {
                "verdict": "approved_with_minor",
                "score": 70,
                "strengths": [],
                "issues": [],
                "security_concerns": [],
                "performance_concerns": [],
                "final_comment": (
                    f"Critic unavailable — defaulting to approved_with_minor. ({out.error})"
                )[:400],
            }
        else:
            self.review = out.data
        await self._emit(
            {
                "type": "review_ready",
                "review": self.review,
            }
        )
        return self.review

    # ── deliverable assembly ──────────────────────────────────────────────

    def _build_deliverable_markdown(self) -> str:
        """Final markdown that lands in the chat history."""
        task_type = (
            (self.plan.get("task_type") or self.triage.get("task_type") or "Code task")
            .replace("_", " ")
            .title()
        )
        title = self.title or "Code task"
        lang = self.detected_language or "text"

        lines: list[str] = []
        lines.append(f"## {task_type} — {title}")
        lines.append("")

        # Plan summary
        if self.plan:
            lines.append("### Plan")
            steps = self.plan.get("plan", []) or []
            if steps:
                for step in steps[:8]:
                    lines.append(
                        f"{step['step']}. **{step['action']}** "
                        f"({step['agent']}) — {step['description']}"
                    )
            else:
                lines.append(self.plan.get("title") or "(no detailed plan)")
            lines.append("")

        # Implementation
        if self.code:
            lines.append("### Implementation")
            lines.append(f"```{lang}")
            lines.append(self.code.rstrip())
            lines.append("```")
            lines.append("")

        # Static analysis (only if interesting)
        if self.static_analysis and self.static_analysis.issues:
            counts = self.static_analysis.severity_counts()
            if any(counts.values()):
                lines.append("### Static Analysis")
                lines.append(
                    f"- {counts['error']} errors, "
                    f"{counts['warning']} warnings, "
                    f"{counts['security']} security issues"
                )
                if self.static_analysis.complexity_score is not None:
                    lines.append(
                        f"- Avg cyclomatic complexity: {self.static_analysis.complexity_score:.1f}"
                    )
                lines.append("")

        # Tests
        if self.tests:
            lines.append("### Tests")
            lines.append(f"```{lang}")
            lines.append(self.tests.rstrip())
            lines.append("```")
            lines.append("")

        # Execution results
        if self.execution_results:
            lines.append("### Execution Results")
            for i, res in enumerate(self.execution_results):
                tag = (
                    "✅ Pass"
                    if res.get("success")
                    else ("⏱ Timeout" if res.get("timed_out") else "❌ Fail")
                )
                attempt = "Initial run" if i == 0 else f"Debug iteration {i}"
                lines.append(
                    f"- **{attempt}** — {tag} "
                    f"(exit={res.get('exit_code')}, "
                    f"{res.get('duration_ms', 0)}ms)"
                )
                stderr = (res.get("stderr") or "").strip()
                if stderr and not res.get("success"):
                    snippet = stderr.splitlines()[0][:200]
                    lines.append(f"  - `{snippet}`")
            lines.append("")

        # Review
        if self.review:
            verdict_label = {
                "approved": "✅ Approved",
                "approved_with_minor": "🟢 Approved with minor comments",
                "needs_revision": "🟠 Needs revision",
                "rejected": "🔴 Rejected",
            }.get(self.review.get("verdict", "approved_with_minor"), "🟢 Approved")
            score = self.review.get("score", 70)
            lines.append("### Code Review")
            lines.append(f"**Score: {score}/100** — {verdict_label}")
            lines.append("")
            comment = self.review.get("final_comment") or ""
            if comment:
                lines.append(comment)
                lines.append("")
            major_issues = [
                i
                for i in self.review.get("issues", [])
                if i.get("severity") in ("critical", "major")
            ]
            if major_issues:
                lines.append("**Notable issues:**")
                for issue in major_issues[:6]:
                    lines.append(f"- *{issue['severity']}* — {issue['description']}")
                lines.append("")

        return "\n".join(lines).strip() + "\n"

    # ── run ───────────────────────────────────────────────────────────────

    async def run(self) -> dict[str, Any]:
        await self._run_phase("triage", self._phase_triage)
        await self._run_phase("model_prep", self._phase_model_prep)
        await self._run_phase("plan", self._phase_plan)

        # If planning failed, we cannot meaningfully continue.
        if self._phase_index["plan"].status != "completed":
            for n in ("implement", "execute", "analyze", "test", "debug", "review"):
                self._skip(n, "plan failed")
            self.deliverable_markdown = self._build_deliverable_markdown()
            return self.snapshot()

        await self._run_phase("implement", self._phase_implement)
        if self._phase_index["implement"].status != "completed":
            for n in ("execute", "analyze", "test", "debug", "review"):
                self._skip(n, "implementation failed")
            self.deliverable_markdown = self._build_deliverable_markdown()
            return self.snapshot()

        # Run execute + analyze concurrently — they're independent.
        await asyncio.gather(
            self._run_phase("execute", self._phase_execute),
            self._run_phase("analyze", self._phase_analyze),
            return_exceptions=True,
        )
        await self._run_phase("test", self._phase_test)
        await self._run_phase("debug", self._phase_debug)
        await self._run_phase("review", self._phase_review)

        self.deliverable_markdown = self._build_deliverable_markdown()
        await self._emit(
            {
                "type": "deliverable_ready",
                "markdown": self.deliverable_markdown,
            }
        )
        return self.snapshot()

    # ── snapshot ──────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "phases": [p.to_dict() for p in self.phases],
            "triage": self.triage,
            "models_used": self.models_used,
            "plan": self.plan,
            "code": self.code,
            "tests": self.tests,
            "language": self.detected_language,
            "title": self.title,
            "execution_results": self.execution_results,
            "static_analysis": (self.static_analysis.to_dict() if self.static_analysis else None),
            "review": self.review,
            "deliverable_markdown": self.deliverable_markdown,
            "debug_iterations": self.debug_iterations_used,
            "task_type": self.plan.get("task_type") or self.triage.get("task_type") or "generation",
        }


# ─────────────────────────────────────────────────────────────────────────────
# noop helper
# ─────────────────────────────────────────────────────────────────────────────


async def _noop_event(_event: dict[str, Any]) -> None:
    return None
