"""
ConsortiumOrchestrator — the meta-pipeline.

Composes the three existing engines (AdvancedResearcher, ThinkingEngine,
CodeIntelligenceEngine) into a single end-to-end run:

    Scope (Code Intelligence triage)
      ↓
    Research (AdvancedResearcher with depth from scope)
      ↓
    Thinking (ThinkingEngine with research summary as context)
      ↓
    Implementation (CodeIntelligenceEngine with research+thinking as context)
      ↓
    Verification gates after each phase
      ↓
    Bundle (ConsortiumBundle persisted to Mongo + filesystem artifact dir)

Design notes
------------
* **In-process composition** — the orchestrator imports the engines
  directly and wires their `on_event` callbacks to a single shared
  channel.  No HTTP self-calls.
* **100% local** — every LLM call goes through the v3/v4 Ollama path
  (``call_ollama`` + ContextVar profile + per-role routing). The
  ``provider`` is hard-coded to ``"local"`` everywhere; paid APIs are
  refused at construction time.
* **Quality gates** — between phases a small validator scores the
  artifact (citation density / decision groundedness / static
  analysis severity). Failure does not abort by default; it adds a
  ``VerificationGate`` to the bundle so the user can see what was
  flagged and the orchestrator can optionally retry on the next call.
* **Cancellation** — every phase consults ``scope.cancel_requested``
  before starting; mid-phase cancellation is surfaced via the engine
  hooks they already expose.
* **Event prefix** — every nested event is re-emitted with the prefix
  ``consortium:<phase>:<original_type>`` so the SSE consumer can
  follow the whole pipeline in one stream without ambiguity.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .models import (
    ConsortiumBundle,
    ConsortiumScope,
    ImplementationArtifact,
    ResearchArtifact,
    ThinkingArtifact,
    VerificationGate,
)

logger = logging.getLogger(__name__)


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


async def _noop_event(_event: dict[str, Any]) -> None:
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# Per-tier mapping for the global "depth" knob → per-phase efforts.
# Picked so the user feels a clear progression: basic = quick scan,
# ultra = exhaustive multi-hour build. The orchestrator uses this when
# the user doesn't override individual tiers.
_DEPTH_TO_PHASE_EFFORTS: dict[str, dict[str, str]] = {
    "basic":  {"research": "basic",  "thinking": "basic",  "implementation": "basic"},
    "medium": {"research": "medium", "thinking": "medium", "implementation": "medium"},
    "deep":   {"research": "deep",   "thinking": "deep",   "implementation": "deep"},
    "expert": {"research": "expert", "thinking": "expert", "implementation": "expert"},
    "ultra":  {"research": "ultra",  "thinking": "ultra",  "implementation": "ultra"},
}


class ConsortiumOrchestrator:
    """One instance per session."""

    def __init__(
        self,
        *,
        session_id: str,
        scope: ConsortiumScope,
        on_event: EventCallback | None = None,
        artifact_dir: Path | None = None,
    ) -> None:
        if not scope.goal or not scope.goal.strip():
            raise ValueError("ConsortiumScope.goal is required")
        self.session_id = session_id
        self.scope = scope
        self._on_event = on_event or _noop_event
        self._artifact_dir = artifact_dir
        self._started_at = _now_iso()

        self.research: ResearchArtifact | None = None
        self.thinking: ThinkingArtifact | None = None
        self.implementation: ImplementationArtifact | None = None
        self.verifications: list[VerificationGate] = []

    # ─── helpers ────────────────────────────────────────────────────────

    async def _emit(self, event: dict[str, Any]) -> None:
        try:
            # v6 — every event gets a stable `event_id` BEFORE fan-out
            # so the SSE stream can dedupe between the local in-memory
            # queue and the Redis pub/sub channel (both deliver the
            # same event to the same SSE subscriber on the same
            # replica). Without this the user sees every event twice.
            event = {
                **event,
                "session_id": self.session_id,
                "ts": _now_iso(),
                "event_id": event.get("event_id") or uuid4().hex,
            }
            await self._on_event(event)
        except Exception:  # pragma: no cover
            logger.exception("consortium.on_event raised")

    def _make_inner_event_relay(self, phase: str) -> EventCallback:
        """Build an `on_event` callback that prefixes all nested events
        with the consortium phase. Lets the SSE consumer follow nested
        progress without losing the parent context."""

        async def relay(inner: dict[str, Any]) -> None:
            inner_type = str(inner.get("type") or "event")
            relayed = {**inner}
            relayed["type"] = f"consortium:{phase}:{inner_type}"
            relayed["consortium_phase"] = phase
            await self._emit(relayed)

        return relay

    def _check_cancel(self) -> None:
        if self.scope.cancel_requested:
            raise asyncio.CancelledError(
                f"Consortium session {self.session_id} cancelled by user",
            )

    # ─── Phase 1: Scope definition ──────────────────────────────────────

    async def _phase_scope(self) -> ConsortiumScope:
        """Run a small triage call to fill in title, summary, constraints,
        success criteria, language, and per-phase efforts.

        Reuses ``run_triage`` from the code-intelligence agents because
        it already extracts task_type / language / complexity from a
        free-text prompt with the local LLM."""
        await self._emit({"type": "consortium_phase_start", "phase": "scope"})

        try:
            from ..api.local_ai_routes_simple import call_ollama  # noqa: PLC0415
            from ..code_intelligence.agents import run_triage  # noqa: PLC0415

            triage = await run_triage(
                call_ollama, prompt=self.scope.goal,
                code_context=None, max_tokens=600,
            )
        except Exception as exc:
            logger.warning("consortium_scope_triage_failed: %s", exc)
            triage = {
                "task_type": "module",
                "language": self.scope.language or "python",
                "complexity": "moderate",
            }

        # Always feed a clean title + summary even when the LLM is offline
        # — derived deterministically from the goal.
        title = self._derive_title(self.scope.goal)
        # Per-phase efforts: respect any explicit overrides set on the
        # scope, otherwise translate from the global depth knob.
        depth_map = _DEPTH_TO_PHASE_EFFORTS.get(self.scope.depth, _DEPTH_TO_PHASE_EFFORTS["medium"])
        if self.scope.research_depth == "medium" and self.scope.depth != "medium":
            self.scope.research_depth = depth_map["research"]
        if self.scope.thinking_effort == "medium" and self.scope.depth != "medium":
            self.scope.thinking_effort = depth_map["thinking"]
        if self.scope.implementation_effort == "medium" and self.scope.depth != "medium":
            self.scope.implementation_effort = depth_map["implementation"]

        self.scope.title = title
        self.scope.summary = self._truncate(self.scope.goal, 320)
        self.scope.research_query = self._distill_research_query(self.scope.goal)
        self.scope.language = self.scope.language or str(triage.get("language") or "python")
        # Derive constraints + success criteria from a single triage call;
        # if any are missing we fall back to a minimal default so the
        # downstream phases always have *something* to work against.
        self.scope.constraints = (
            self.scope.constraints
            or self._extract_bullets(self.scope.goal, kind="constraints")
            or ["100% local-only inference (no paid APIs)"]
        )
        self.scope.success_criteria = (
            self.scope.success_criteria
            or self._extract_bullets(self.scope.goal, kind="success_criteria")
            or [
                "Generated code compiles and the smoke tests pass",
                "Static analysis reports no critical issues",
                "Deliverable includes a runnable example",
            ]
        )

        await self._emit({
            "type": "consortium_phase_complete",
            "phase": "scope",
            "scope": self.scope.to_dict(),
        })
        return self.scope

    @staticmethod
    def _truncate(s: str, n: int) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else s[: n - 1].rstrip() + "…"

    @staticmethod
    def _derive_title(goal: str) -> str:
        text = re.sub(r"\s+", " ", (goal or "").strip())
        if not text:
            return "Consortium project"
        if len(text) <= 60:
            return text[:1].upper() + text[1:]
        cut = text[:60]
        sp = cut.rfind(" ")
        if sp > 40:
            cut = cut[:sp]
        return cut[:1].upper() + cut[1:].rstrip(",.;:") + "…"

    @staticmethod
    def _distill_research_query(goal: str) -> str:
        # Strip imperative verbs at the front so "Build me a thing that
        # does X" becomes "X". A research query reads better that way.
        text = (goal or "").strip()
        for prefix in (
            "build me a", "build me", "build a", "build",
            "make me a", "make a", "make",
            "create me a", "create a", "create",
            "design a", "design",
            "implement a", "implement",
            "write a", "write",
        ):
            if text.lower().startswith(prefix + " "):
                text = text[len(prefix) + 1 :]
                break
        return text[:240]

    @staticmethod
    def _extract_bullets(goal: str, kind: str) -> list[str]:
        """Extract obvious bullet/comma-separated requirements from the
        free-text goal. Cheap heuristic; the orchestrator falls back to
        sensible defaults when this returns []."""
        lines = [line.strip(" -•*\t") for line in (goal or "").splitlines()]
        out = [line for line in lines if 8 < len(line) <= 200]
        if len(out) >= 2:
            return out[:6]
        # Comma-separated single-line shape: "do X, do Y, do Z"
        single = (goal or "").replace("\n", " ")
        parts = [p.strip() for p in single.split(",") if 8 < len(p.strip()) <= 200]
        return parts[:5] if len(parts) >= 2 else []

    # ─── Phase 2: Research ──────────────────────────────────────────────

    async def _phase_research(self) -> ResearchArtifact:
        self._check_cancel()
        await self._emit({"type": "consortium_phase_start", "phase": "research"})

        if not self.scope.allow_external_research:
            # Return an empty artifact — the user explicitly disabled web
            # search.  Thinking + Implementation will still run with the
            # scope-derived context.
            self.research = ResearchArtifact(
                query=self.scope.research_query, depth=self.scope.research_depth,
                summary_markdown="*Research disabled by scope.*",
            )
            await self._emit({
                "type": "consortium_phase_complete",
                "phase": "research", "research": self.research.to_dict(),
            })
            # Emit a synthetic "skipped" gate so every phase has a
            # verification entry — keeps the bundle + UI consistent.
            skipped_gate = VerificationGate(
                phase="research",
                status="passed_warn",
                score=50.0,
                findings=["Web research disabled by scope"],
                summary="research skipped (allow_external_research=False)",
            )
            self.verifications.append(skipped_gate)
            await self._emit({"type": "consortium_gate", "gate": skipped_gate.to_dict()})
            return self.research

        relay = self._make_inner_event_relay("research")

        try:
            from ..api.local_ai_routes_simple import (  # noqa: PLC0415
                call_ollama, scrape_multiple_urls, search_web,
            )
            from ..research import AdvancedResearcher  # noqa: PLC0415
        except Exception as exc:
            logger.warning("consortium_research_imports_failed: %s", exc)
            self.research = ResearchArtifact(
                query=self.scope.research_query, depth=self.scope.research_depth,
                summary_markdown=f"*Research module unavailable: {exc}*",
            )
            await self._emit({
                "type": "consortium_phase_complete",
                "phase": "research", "research": self.research.to_dict(),
            })
            return self.research

        async def _search(q: str, max_results: int):
            return await search_web(q, max_results)

        async def _scrape(urls: list[str], concurrency: int):
            return await scrape_multiple_urls(urls, max_concurrent=concurrency)

        researcher = AdvancedResearcher(
            query=self.scope.research_query,
            depth=self.scope.research_depth,
            llm_call=call_ollama,
            web_search=_search,
            web_scrape=_scrape,
            on_event=relay,
        )
        try:
            result = await researcher.run()
        except Exception as exc:
            logger.warning("consortium_research_failed: %s", exc)
            self.research = ResearchArtifact(
                query=self.scope.research_query, depth=self.scope.research_depth,
                summary_markdown=f"*Research failed: {exc}*",
            )
            await self._emit({
                "type": "consortium_phase_complete",
                "phase": "research", "research": self.research.to_dict(),
            })
            return self.research

        self.research = ResearchArtifact(
            query=self.scope.research_query,
            depth=self.scope.research_depth,
            summary_markdown=str(result.get("report") or ""),
            sources=list(result.get("sources") or []),
            sub_questions=list(result.get("sub_questions") or []),
            citation_count=int(
                len(re.findall(r"\[\d+\]", str(result.get("report") or "")))
            ),
        )
        await self._emit({
            "type": "consortium_phase_complete",
            "phase": "research",
            "summary_chars": len(self.research.summary_markdown),
            "sources": len(self.research.sources),
            "citations": self.research.citation_count,
        })
        # Quality gate.
        gate = self._gate_research(self.research)
        self.verifications.append(gate)
        await self._emit({"type": "consortium_gate", "gate": gate.to_dict()})
        return self.research

    @staticmethod
    def _gate_research(r: ResearchArtifact) -> VerificationGate:
        """Cheap deterministic quality gate for research output."""
        findings: list[str] = []
        score = 60.0
        if r.citation_count == 0:
            findings.append("No inline citations [n] found in summary")
            score -= 25
        elif r.citation_count >= 3:
            score += 20
        if not r.sources:
            findings.append("Zero sources gathered — likely web search outage")
            score -= 30
        elif len(r.sources) >= 5:
            score += 15
        if len(r.summary_markdown) < 600:
            findings.append("Summary is unusually short (< 600 chars)")
            score -= 10
        elif len(r.summary_markdown) > 4000:
            score += 5
        score = max(0.0, min(100.0, score))
        if score >= 70:
            status: str = "passed"
        elif score >= 40:
            status = "passed_warn"
        else:
            status = "failed"
        return VerificationGate(
            phase="research",
            status=status,  # type: ignore[arg-type]
            score=round(score, 1),
            findings=findings,
            summary=(
                f"{len(r.sources)} sources · {r.citation_count} citations · "
                f"{len(r.summary_markdown)} chars"
            ),
        )

    # ─── Phase 3: Analyze & Think ───────────────────────────────────────

    async def _phase_thinking(self) -> ThinkingArtifact:
        self._check_cancel()
        await self._emit({"type": "consortium_phase_start", "phase": "thinking"})

        relay = self._make_inner_event_relay("thinking")
        # Augment the user's goal with the research summary so the
        # ThinkingEngine has the full evidence base.
        composed = self._compose_thinking_prompt()

        try:
            from ..api.local_ai_routes_simple import call_ollama  # noqa: PLC0415
            from ..thinking import ThinkingEngine  # noqa: PLC0415

            engine = ThinkingEngine(
                prompt=composed,
                clarifications={},
                deliverable="design_document",
                effort=self.scope.thinking_effort,
                provider="local",
                llm_call=call_ollama,
                on_event=relay,
            )
            result = await engine.run()
        except Exception as exc:
            logger.warning("consortium_thinking_failed: %s", exc)
            self.thinking = ThinkingArtifact(
                deliverable_markdown=f"*Thinking failed: {exc}*",
            )
            await self._emit({
                "type": "consortium_phase_complete",
                "phase": "thinking", "thinking": self.thinking.to_dict(),
            })
            return self.thinking

        self.thinking = ThinkingArtifact(
            deliverable_markdown=str(result.get("deliverable_markdown") or ""),
            understanding=dict(result.get("understanding") or {}),
            sub_questions=list(result.get("sub_questions") or []),
            alternatives=list(result.get("alternatives") or []),
            decision=dict(result.get("decision") or {}),
            critique=dict(result.get("critique") or {}),
        )
        await self._emit({
            "type": "consortium_phase_complete",
            "phase": "thinking",
            "alternatives": len(self.thinking.alternatives),
            "decision_present": bool(self.thinking.decision),
        })
        gate = self._gate_thinking(self.thinking)
        self.verifications.append(gate)
        await self._emit({"type": "consortium_gate", "gate": gate.to_dict()})
        return self.thinking

    def _compose_thinking_prompt(self) -> str:
        """Build a context-rich prompt by stitching the goal + scope +
        research summary together. The ThinkingEngine treats this as
        an opaque prompt — no schema needed, just rich context."""
        parts = [
            f"# Project Goal\n{self.scope.goal}",
            f"\n# Title\n{self.scope.title}",
        ]
        if self.scope.constraints:
            parts.append("\n# Constraints\n" +
                         "\n".join(f"- {c}" for c in self.scope.constraints))
        if self.scope.success_criteria:
            parts.append("\n# Success criteria\n" +
                         "\n".join(f"- {c}" for c in self.scope.success_criteria))
        if self.research and self.research.summary_markdown:
            parts.append("\n# Research summary\n" +
                         self.research.summary_markdown[:6000])
        parts.append(
            "\n# Task\n"
            "Decide the architecture, evaluate at least two alternatives, "
            "and produce a design document the implementation phase can "
            "consume directly. Stay 100% local — no paid APIs, no external "
            "services."
        )
        return "\n".join(parts)

    @staticmethod
    def _gate_thinking(t: ThinkingArtifact) -> VerificationGate:
        findings: list[str] = []
        score = 60.0
        if not t.alternatives:
            findings.append("No explicit alternatives evaluated")
            score -= 25
        elif len(t.alternatives) >= 2:
            score += 15
        if not t.decision or not (t.decision.get("chosen") or t.decision.get("rationale")):
            findings.append("Decision lacks a chosen option / rationale")
            score -= 20
        elif t.decision.get("rationale"):
            score += 10
        if len(t.deliverable_markdown) < 400:
            findings.append("Design document is unusually short")
            score -= 10
        else:
            score += 5
        if t.critique and t.critique.get("issues"):
            score += 5
        score = max(0.0, min(100.0, score))
        status = ("passed" if score >= 70 else
                  "passed_warn" if score >= 40 else "failed")
        return VerificationGate(
            phase="thinking",
            status=status,  # type: ignore[arg-type]
            score=round(score, 1),
            findings=findings,
            summary=f"{len(t.alternatives)} alternatives · "
                    f"decision={'present' if t.decision else 'missing'}",
        )

    # ─── Phase 4: Implementation ────────────────────────────────────────

    async def _phase_implementation(self) -> ImplementationArtifact:
        self._check_cancel()
        await self._emit({"type": "consortium_phase_start", "phase": "implementation"})

        relay = self._make_inner_event_relay("implementation")
        composed_context = self._compose_implementation_context()

        try:
            from ..api.code_intelligence_routes import (  # noqa: PLC0415
                _llm_call_local, get_sandbox, get_static_harness,
            )
            from ..code_intelligence import CodeIntelligenceEngine  # noqa: PLC0415

            sandbox = get_sandbox()
            engine = CodeIntelligenceEngine(
                prompt=self.scope.goal,
                code_context=composed_context,
                language=self.scope.language,
                effort=self.scope.implementation_effort,
                provider="local",
                llm_call=_llm_call_local,
                sandbox=sandbox,
                static_harness=get_static_harness(),
                enable_execution=sandbox is not None,
                enable_static_analysis=True,
                enable_testing=True,
                on_event=relay,
            )
            result = await engine.run()
        except Exception as exc:
            logger.warning("consortium_implementation_failed: %s", exc)
            self.implementation = ImplementationArtifact(
                language=self.scope.language or "python",
                deliverable_markdown=f"*Implementation failed: {exc}*",
            )
            await self._emit({
                "type": "consortium_phase_complete",
                "phase": "implementation",
                "implementation": self.implementation.to_dict(),
            })
            return self.implementation

        self.implementation = ImplementationArtifact(
            code=result.get("code"),
            tests=result.get("tests"),
            language=str(result.get("language") or self.scope.language or "python"),
            plan=dict(result.get("plan") or {}),
            triage=dict(result.get("triage") or {}),
            static_analysis=(
                dict(result.get("static_analysis"))
                if isinstance(result.get("static_analysis"), dict)
                else None
            ),
            execution_results=list(result.get("execution_results") or []),
            review=dict(result.get("review") or {}),
            deliverable_markdown=str(result.get("deliverable_markdown") or ""),
            models_used=dict(result.get("models_used") or {}),
            debug_iterations=int(result.get("debug_iterations") or 0),
        )
        await self._emit({
            "type": "consortium_phase_complete",
            "phase": "implementation",
            "code_chars": len(self.implementation.code or ""),
            "tests_chars": len(self.implementation.tests or ""),
        })
        gate = self._gate_implementation(self.implementation)
        self.verifications.append(gate)
        await self._emit({"type": "consortium_gate", "gate": gate.to_dict()})
        return self.implementation

    def _compose_implementation_context(self) -> str:
        parts: list[str] = []
        if self.scope.constraints:
            parts.append("Constraints:\n" +
                         "\n".join(f"- {c}" for c in self.scope.constraints))
        if self.scope.success_criteria:
            parts.append("Success criteria:\n" +
                         "\n".join(f"- {c}" for c in self.scope.success_criteria))
        if self.thinking and self.thinking.deliverable_markdown:
            parts.append("Design document (from thinking phase):\n" +
                         self.thinking.deliverable_markdown[:6000])
        if self.research and self.research.summary_markdown:
            parts.append("Research summary:\n" +
                         self.research.summary_markdown[:3000])
        return "\n\n".join(parts)

    @staticmethod
    def _gate_implementation(i: ImplementationArtifact) -> VerificationGate:
        findings: list[str] = []
        score = 60.0
        if not i.code or not i.code.strip():
            findings.append("No code produced")
            score -= 40
        else:
            score += 10
        if not i.tests:
            findings.append("No tests produced")
            score -= 10
        # Static analysis severity check.
        if isinstance(i.static_analysis, dict):
            critical = int(i.static_analysis.get("critical_count") or 0)
            high = int(i.static_analysis.get("high_count") or 0)
            if critical:
                findings.append(f"{critical} critical static-analysis findings")
                score -= 30
            if high:
                findings.append(f"{high} high-severity static-analysis findings")
                score -= 10
        # Execution: any failure is a warning, all passing is a bonus.
        # v8 — sandbox-skipped runs (docker CLI unavailable) are
        # ignored so a missing local sandbox doesn't lower the gate
        # score on every implementation. The skipped flag is set by
        # ExecutionSandbox.execute when docker isn't on PATH.
        if i.execution_results:
            real_results = [
                r for r in i.execution_results
                if isinstance(r, dict) and not r.get("skipped", False)
            ]
            skipped_count = len(i.execution_results) - len(real_results)
            if skipped_count and not real_results:
                findings.append(
                    f"{skipped_count} sandbox run(s) skipped — "
                    "docker CLI unavailable in app container"
                )
                # No score penalty when we couldn't even try.
            elif real_results:
                failed = sum(
                    1 for r in real_results
                    if not r.get("success", True)
                )
                if failed:
                    findings.append(f"{failed} execution result(s) failed")
                    score -= 10
                else:
                    score += 10
        if i.review and i.review.get("verdict") == "approve":
            score += 5
        score = max(0.0, min(100.0, score))
        status = ("passed" if score >= 70 else
                  "passed_warn" if score >= 40 else "failed")
        return VerificationGate(
            phase="implementation",
            status=status,  # type: ignore[arg-type]
            score=round(score, 1),
            findings=findings,
            summary=(
                f"code={len(i.code or '')}c · tests={len(i.tests or '')}c · "
                f"exec={len(i.execution_results)}"
            ),
        )

    # ─── Bundle + persist ───────────────────────────────────────────────

    async def _bundle(self) -> ConsortiumBundle:
        readme = self._build_readme_markdown()
        bundle = ConsortiumBundle(
            session_id=self.session_id,
            scope=self.scope,
            research=self.research,
            thinking=self.thinking,
            implementation=self.implementation,
            verifications=list(self.verifications),
            readme_markdown=readme,
            started_at=self._started_at,
            completed_at=_now_iso(),
        )
        if self._artifact_dir:
            try:
                self._write_artifact_dir(bundle, self._artifact_dir)
            except Exception as exc:
                logger.warning("consortium_artifact_write_failed: %s", exc)
        return bundle

    def _build_readme_markdown(self) -> str:
        """Synthesize the top-level README.

        Sections (in order): title, one-line summary, Quick start (so the
        user can run `bash run.sh` and see something), Project structure
        (so they know where each artifact lives), Scope, Phase results,
        Implementation deliverable, Design document, Research summary.
        """
        s = self.scope
        is_python = (s.language or "python") == "python"
        impl = self.implementation

        rows: list[str] = []
        rows.append(f"# {s.title or 'Consortium project'}\n")
        if s.summary:
            rows.append(f"> {s.summary}\n")

        # Quick start — give the user a copy-pasteable command first.
        rows.append("## Quick start\n")
        if is_python and impl and impl.code:
            rows.append("```bash")
            rows.append("# 1. Bootstrap venv + install requirements")
            rows.append("bash run.sh setup")
            rows.append("")
            rows.append("# 2. Run the entry point")
            rows.append("bash run.sh run")
            if impl.tests:
                rows.append("")
                rows.append("# 3. Run the test suite")
                rows.append("bash run.sh test")
            rows.append("```\n")
        else:
            rows.append("_No runnable entry point detected — see "
                        "`docs/design.md` for the design and `src/` for "
                        "the generated artefact._\n")

        # Project structure — explicit map so the user knows where things live.
        rows.append("## Project structure\n")
        rows.append("```")
        rows.append(".")
        rows.append("├── README.md                    ← this file")
        if is_python and impl and impl.code:
            rows.append("├── requirements.txt             ← auto-detected from imports")
            rows.append("├── run.sh                       ← venv bootstrap + run + test")
            rows.append("├── pyproject.toml               ← project metadata")
            rows.append("├── .gitignore")
        rows.append("├── scope.json                   ← user goal + triage results")
        rows.append("├── verifications.json           ← per-phase quality gates")
        rows.append("├── bundle.json                  ← full structured envelope")
        if impl and impl.code:
            rows.append("├── src/")
            rows.append(f"│   └── main.{'py' if is_python else 'txt'}")
        if impl and impl.tests:
            rows.append("├── tests/")
            rows.append(f"│   └── test_main.{'py' if is_python else 'txt'}")
        rows.append("├── docs/")
        if self.thinking:
            rows.append("│   ├── design.md                ← decision + alternatives")
            rows.append("│   ├── alternatives.json")
        if self.research:
            rows.append("│   ├── research_summary.md      ← citations + summary")
            rows.append("│   └── research_sources.json")
        if impl:
            rows.append("│   └── review.md                ← adversarial review")
        if impl and (impl.static_analysis or impl.execution_results):
            rows.append("└── reports/")
            rows.append("    ├── static_analysis.json")
            rows.append("    └── execution_results.json")
        rows.append("```\n")

        rows.append("## Scope\n")
        rows.append(f"- **Depth**: `{s.depth}`")
        rows.append(f"- **Language**: `{s.language or 'python'}`")
        rows.append(f"- **Deliverable**: `{s.deliverable_type}`")
        if s.constraints:
            rows.append("\n### Constraints\n" +
                        "\n".join(f"- {c}" for c in s.constraints))
        if s.success_criteria:
            rows.append("\n### Success criteria\n" +
                        "\n".join(f"- {c}" for c in s.success_criteria))

        rows.append("\n## Phase results\n")
        for v in self.verifications:
            badge = {"passed": "✓", "passed_warn": "⚠", "failed": "✗"}.get(
                v.status, "?",
            )
            rows.append(f"- {badge} **{v.phase}** — score {v.score} · {v.summary}")
            for f in v.findings:
                rows.append(f"    - {f}")

        if impl and impl.deliverable_markdown:
            rows.append("\n## Implementation deliverable\n")
            rows.append(impl.deliverable_markdown)
        if self.thinking and self.thinking.deliverable_markdown:
            rows.append("\n## Design document\n")
            rows.append(self.thinking.deliverable_markdown)
        if self.research and self.research.summary_markdown:
            rows.append("\n## Research summary\n")
            rows.append(self.research.summary_markdown)
        return "\n".join(rows)

    # ─── Artifact-bundle helpers ────────────────────────────────────────

    # Mapping from import-name to pip-name for the well-known mismatches
    # the consortium tends to hit. Anything not listed maps 1:1.
    _IMPORT_TO_PIP_NAME: dict[str, str] = {
        "PIL": "Pillow",
        "cv2": "opencv-python",
        "sklearn": "scikit-learn",
        "yaml": "PyYAML",
        "bs4": "beautifulsoup4",
        "skimage": "scikit-image",
        "Crypto": "pycryptodome",
        "OpenGL": "PyOpenGL",
        "magic": "python-magic",
        "dateutil": "python-dateutil",
        "serial": "pyserial",
        "lxml": "lxml",
        "wx": "wxPython",
    }

    @staticmethod
    def _extract_python_requirements(*sources: str | None) -> list[str]:
        """Parse Python source(s) and return third-party pip names.

        Walks the AST so we don't get fooled by strings/comments. Strips
        out anything in ``sys.stdlib_module_names`` (3.10+) plus a small
        backstop set for older runtimes, then maps known import-name →
        pip-name aliases.
        """
        modules: set[str] = set()
        for src in sources:
            if not src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                # Code may be partially invalid — skip rather than break the bundle.
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name:
                            modules.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    # Relative imports (level > 0) reference local files,
                    # not packages — skip them.
                    if node.level == 0 and node.module:
                        modules.add(node.module.split(".")[0])

        stdlib = set(getattr(sys, "stdlib_module_names", set()))
        # Safety net for a few names that aren't in stdlib_module_names
        # but should never end up in requirements.txt.
        stdlib.update({"__future__", "typing_extensions", "tests", "test"})

        third_party = (
            m for m in modules
            if m and m not in stdlib and not m.startswith("_")
        )
        # Sort by pip-name (post-aliasing) so requirements.txt is
        # alphabetical the way a human reads it, not by the import name.
        return sorted({
            ConsortiumOrchestrator._IMPORT_TO_PIP_NAME.get(m, m)
            for m in third_party
        })

    @staticmethod
    def _build_run_sh(*, has_tests: bool) -> str:
        """Generate a venv-bootstrapped run.sh for Python projects.

        The script is idempotent: ``setup`` only creates the venv if it
        doesn't exist, ``run`` calls setup transitively, ``clean`` wipes
        the venv. All paths are relative to the script's own directory
        so it works regardless of where the user invokes it from.
        """
        test_clause = (
            '"$VENV/bin/python" -m pytest tests/ -v "$@"'
            if has_tests
            else 'echo "no tests in this bundle"'
        )
        return f"""#!/usr/bin/env bash
# Auto-generated by AMOR Consortium.
# Bootstraps a Python virtualenv and runs the project.
set -euo pipefail

HERE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
cd "$HERE"
VENV="$HERE/.venv"
PY="${{PYTHON:-python3}}"

setup() {{
    if [[ ! -d "$VENV" ]]; then
        "$PY" -m venv "$VENV"
    fi
    "$VENV/bin/pip" install --upgrade pip --quiet
    if [[ -s requirements.txt ]]; then
        "$VENV/bin/pip" install -r requirements.txt
    fi
}}

run() {{
    "$VENV/bin/python" src/main.py "$@"
}}

run_tests() {{
    {test_clause}
}}

case "${{1:-run}}" in
    setup)  setup ;;
    run)    setup; shift || true; run "$@" ;;
    test)   setup; shift || true; run_tests "$@" ;;
    clean)  rm -rf "$VENV" ;;
    *)      echo "usage: $0 {{setup|run|test|clean}}" >&2; exit 1 ;;
esac
"""

    @staticmethod
    def _build_pyproject_toml(*, name: str, summary: str,
                              requirements: list[str]) -> str:
        """Minimal pyproject.toml — enough that pip/uv recognises the project."""
        # Slugify name for the [project] block.
        slug = re.sub(r"[^a-z0-9_-]+", "-", (name or "project").lower()).strip("-")
        if not slug:
            slug = "project"
        # Quote summary safely for TOML
        safe_summary = (summary or "").replace("\\", "\\\\").replace('"', '\\"')
        deps_block = ",\n".join(f'    "{r}"' for r in requirements)
        if deps_block:
            deps_block = f"[\n{deps_block},\n]"
        else:
            deps_block = "[]"
        return f"""[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{slug}"
version = "0.1.0"
description = "{safe_summary}"
requires-python = ">=3.10"
dependencies = {deps_block}

[tool.setuptools.packages.find]
where = ["src"]
"""

    @staticmethod
    def _build_gitignore() -> str:
        return (
            ".venv/\n"
            "__pycache__/\n"
            "*.pyc\n"
            "*.pyo\n"
            ".pytest_cache/\n"
            ".mypy_cache/\n"
            ".coverage\n"
            "*.egg-info/\n"
            "dist/\n"
            "build/\n"
        )

    @staticmethod
    def _render_review_markdown(review: dict[str, Any]) -> str:
        """Turn the adversarial reviewer's dict into proper markdown.

        The review schema (from CodeIntelligenceEngine) carries:
          verdict, summary, strengths[], weaknesses[], suggestions[],
          severity (sometimes), score (sometimes).

        Anything we don't recognize gets dumped as fenced JSON at the
        bottom so nothing is silently lost.
        """
        if not review:
            return "# Review\n\n_No review produced for this run._\n"

        rows: list[str] = ["# Adversarial review\n"]
        verdict = str(review.get("verdict") or "").strip()
        if verdict:
            badge = {
                "approve":         "✓",
                "approve_with_changes": "⚠",
                "request_changes": "⚠",
                "reject":          "✗",
            }.get(verdict, "•")
            rows.append(f"**Verdict**: {badge} `{verdict}`")
        score = review.get("score")
        if score is not None:
            rows.append(f"**Score**: {score}")
        severity = review.get("severity")
        if severity:
            rows.append(f"**Severity**: {severity}")
        summary = str(review.get("summary") or "").strip()
        if summary:
            rows.append(f"\n## Summary\n\n{summary}")

        for label, key in (
            ("Strengths",   "strengths"),
            ("Weaknesses",  "weaknesses"),
            ("Suggestions", "suggestions"),
            ("Risks",       "risks"),
        ):
            items = review.get(key)
            if isinstance(items, list) and items:
                rows.append(f"\n## {label}\n")
                for it in items:
                    if isinstance(it, dict):
                        title = it.get("title") or it.get("issue") or ""
                        detail = it.get("detail") or it.get("description") or ""
                        line = f"- **{title}**" if title else "- "
                        if detail:
                            line += f" — {detail}" if title else detail
                        rows.append(line)
                    else:
                        rows.append(f"- {it}")

        # Anything unhandled gets dumped at the bottom for debugging.
        known = {"verdict", "score", "severity", "summary",
                 "strengths", "weaknesses", "suggestions", "risks"}
        extra = {k: v for k, v in review.items() if k not in known}
        if extra:
            rows.append("\n## Raw review payload\n")
            rows.append("```json")
            rows.append(json.dumps(extra, indent=2, ensure_ascii=False))
            rows.append("```")
        return "\n".join(rows) + "\n"

    @staticmethod
    def _write_artifact_dir(bundle: ConsortiumBundle, dst: Path) -> None:
        """Write a runnable, conventional project layout to ``dst``.

        Layout::

            <dst>/
              README.md, requirements.txt, run.sh, pyproject.toml, .gitignore
              scope.json, verifications.json, bundle.json     ← top-level metadata
              src/main.<ext>
              tests/__init__.py, tests/test_main.<ext>
              docs/design.md, docs/alternatives.json,
              docs/research_summary.md, docs/research_sources.json,
              docs/review.md
              reports/static_analysis.json, reports/execution_results.json

        Non-Python deliverables skip the Python-specific scaffolding
        (requirements.txt, run.sh, pyproject.toml).
        """
        dst.mkdir(parents=True, exist_ok=True)
        impl = bundle.implementation
        language = (impl.language if impl else None) or bundle.scope.language or "python"
        is_python = language == "python"
        ext = "py" if is_python else "txt"

        # ── Top-level metadata ──────────────────────────────────────────
        (dst / "README.md").write_text(bundle.readme_markdown, encoding="utf-8")
        (dst / "scope.json").write_text(
            json.dumps(bundle.scope.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (dst / "verifications.json").write_text(
            json.dumps([v.to_dict() for v in bundle.verifications],
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (dst / "bundle.json").write_text(
            json.dumps(bundle.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # ── src/ + tests/ ───────────────────────────────────────────────
        if impl and impl.code:
            src_dir = dst / "src"
            src_dir.mkdir(exist_ok=True)
            (src_dir / f"main.{ext}").write_text(impl.code, encoding="utf-8")
        if impl and impl.tests:
            tests_dir = dst / "tests"
            tests_dir.mkdir(exist_ok=True)
            if is_python:
                (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            (tests_dir / f"test_main.{ext}").write_text(impl.tests, encoding="utf-8")

        # ── docs/ ───────────────────────────────────────────────────────
        docs_dir = dst / "docs"
        docs_written = False
        if bundle.thinking:
            docs_dir.mkdir(exist_ok=True)
            docs_written = True
            (docs_dir / "design.md").write_text(
                bundle.thinking.deliverable_markdown or "# Design\n", encoding="utf-8",
            )
            (docs_dir / "alternatives.json").write_text(
                json.dumps(bundle.thinking.alternatives, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        if bundle.research:
            docs_dir.mkdir(exist_ok=True)
            docs_written = True
            (docs_dir / "research_summary.md").write_text(
                bundle.research.summary_markdown or "# Research\n", encoding="utf-8",
            )
            (docs_dir / "research_sources.json").write_text(
                json.dumps(bundle.research.sources, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        if impl:
            docs_dir.mkdir(exist_ok=True)
            docs_written = True
            (docs_dir / "review.md").write_text(
                ConsortiumOrchestrator._render_review_markdown(impl.review or {}),
                encoding="utf-8",
            )
        if not docs_written:
            # Touch the dir so the README's tree is honest.
            docs_dir.mkdir(exist_ok=True)

        # ── reports/ ────────────────────────────────────────────────────
        if impl and (impl.static_analysis or impl.execution_results):
            reports_dir = dst / "reports"
            reports_dir.mkdir(exist_ok=True)
            (reports_dir / "static_analysis.json").write_text(
                json.dumps(impl.static_analysis or {}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            (reports_dir / "execution_results.json").write_text(
                json.dumps(impl.execution_results, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        # ── Python-specific scaffolding ─────────────────────────────────
        if is_python and impl and (impl.code or impl.tests):
            requirements = ConsortiumOrchestrator._extract_python_requirements(
                impl.code, impl.tests,
            )
            (dst / "requirements.txt").write_text(
                ("\n".join(requirements) + "\n") if requirements else "",
                encoding="utf-8",
            )
            (dst / "run.sh").write_text(
                ConsortiumOrchestrator._build_run_sh(has_tests=bool(impl.tests)),
                encoding="utf-8",
            )
            try:
                # Make run.sh executable on POSIX. No-op on Windows.
                (dst / "run.sh").chmod(0o755)
            except (OSError, NotImplementedError):
                pass
            (dst / "pyproject.toml").write_text(
                ConsortiumOrchestrator._build_pyproject_toml(
                    name=bundle.scope.title or "consortium-project",
                    summary=bundle.scope.summary or "",
                    requirements=requirements,
                ),
                encoding="utf-8",
            )
            (dst / ".gitignore").write_text(
                ConsortiumOrchestrator._build_gitignore(), encoding="utf-8",
            )

    # ─── Run ────────────────────────────────────────────────────────────

    async def run(self) -> ConsortiumBundle:
        await self._emit({"type": "consortium_started",
                          "scope": self.scope.to_dict()})
        try:
            await self._phase_scope()
            self._check_cancel()
            await self._phase_research()
            self._check_cancel()
            await self._phase_thinking()
            self._check_cancel()
            await self._phase_implementation()
        except asyncio.CancelledError:
            await self._emit({"type": "consortium_cancelled"})
            bundle = await self._bundle()
            await self._emit({"type": "consortium_completed",
                              "status": "cancelled"})
            return bundle
        except Exception as exc:
            logger.exception("consortium_run_failed")
            await self._emit({"type": "consortium_error", "error": str(exc)})
            bundle = await self._bundle()
            await self._emit({"type": "consortium_completed",
                              "status": "error"})
            return bundle

        bundle = await self._bundle()
        await self._emit({"type": "consortium_completed", "status": "ok"})
        return bundle
