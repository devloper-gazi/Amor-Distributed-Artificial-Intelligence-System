"""
QuickCodeEngine — 5-phase reasoning-first orchestrator.

Mirrors ConsortiumOrchestrator's shape so it slots cleanly into the
existing eventing / cancel / heartbeat infrastructure:

  * Per-instance, ``async run()`` entry point.
  * ``_emit()`` stamps a UUID ``event_id`` on every event so SSE can
    dedupe across the local queue + Redis pub/sub fanout.
  * ``cancel_requested`` flag honoured at every phase boundary.
  * Event prefix ``quick_code:<phase>:<inner>`` for nested events
    (mirrors consortium's ``consortium:<phase>:<inner>``).

Phases::

    _phase_triage     → run_triage()                      (no LLM role)
    _phase_reason     → reasoning JSON + composite picker (role: reasoner)
    _phase_implement  → CoderAgent + (optional) TesterAgent (role: coder)
    _phase_verify     → ExecutionSandbox + StaticAnalysisHarness (deterministic)
    _phase_refine?    → DebuggerAgent loop, capped at request.max_refine

Each phase ``_check_cancel()``s, emits ``quick_code_phase_start``, runs,
emits ``quick_code_phase_complete``, scores via the matching ``_gate_*``
helper, appends to ``self.bundle.gates``, emits ``quick_code_gate``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable
from uuid import uuid4

from ..code_intelligence.agents import (
    AgentContext,
    CoderAgent,
    DebuggerAgent,
    TesterAgent,
    _extract_json,
    run_triage,
)
from .models import (
    QuickCodeAlternative,
    QuickCodeBundle,
    QuickCodeGate,
    QuickCodeReasoning,
    QuickCodeRequest,
    QuickCodeVerification,
)
from .prompts import REASONING_SYSTEM_PROMPT, reasoning_prompt

logger = logging.getLogger(__name__)


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
LLMCall = Callable[[str, str | None, int], Awaitable[str]]


async def _noop_event(_event: dict[str, Any]) -> None:
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# Effort → max_tokens mapping. Same shape the rest of the pipeline
# uses; keeps reasoning calls bounded under the smaller tiers.
_EFFORT_TO_MAX_TOKENS: dict[str, int] = {
    "basic":  900,
    "medium": 1500,
    "deep":   2200,
    "expert": 3000,
    "ultra":  4000,
}


# Static issue severities counted as "critical" for gate deductions.
# Mirrors the keys StaticAnalysisHarness emits via to_dict().
_CRITICAL_SEVERITIES = {"error", "security"}


class QuickCodeEngine:
    """One instance per session.

    Inject everything at construction time so tests can swap each
    dependency for a mock. The defaults wire up the real Ollama bridge
    and the real sandbox/static-analysis harnesses lazily on first use.
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        request: QuickCodeRequest,
        on_event: EventCallback | None = None,
        llm_call: LLMCall | None = None,
        sandbox: Any | None = None,
        static_harness: Any | None = None,
        role_setter: Callable[[str | None], Any] | None = None,
        mesh: Any | None = None,
    ) -> None:
        if not request.prompt or not request.prompt.strip():
            raise ValueError("QuickCodeRequest.prompt is required")
        self.session_id = session_id or uuid4().hex
        self.request = request.normalize()
        self._on_event = on_event or _noop_event
        self._llm_call = llm_call
        self._sandbox = sandbox
        self._static_harness = static_harness
        self._role_setter = role_setter
        self._started_at = _now_iso()
        # v9 — Multi-ML Mesh. Lazily constructed in _ensure_mesh() so
        # tests that pass `request.use_mesh=False` don't pay the
        # import cost of the mesh package.
        self._mesh = mesh

        self.bundle = QuickCodeBundle(
            session_id=self.session_id,
            request=self.request,
            started_at=self._started_at,
        )

    # ─── helpers ────────────────────────────────────────────────────────

    async def _emit(self, event: dict[str, Any]) -> None:
        try:
            stamped = {
                **event,
                "session_id": self.session_id,
                "ts": _now_iso(),
                "event_id": event.get("event_id") or uuid4().hex,
            }
            await self._on_event(stamped)
        except Exception:  # pragma: no cover
            logger.exception("quick_code.on_event raised")

    def _check_cancel(self) -> None:
        if self.request.cancel_requested:
            raise asyncio.CancelledError(
                f"QuickCode session {self.session_id} cancelled by user",
            )

    def _set_role(self, role: str | None) -> Any:
        """Apply per-role routing via the injected role_setter (or the
        real ContextVar setter when running in-process). Returns a
        token the caller can pass to ``_reset_role`` for cleanup.
        """
        if self._role_setter is not None:
            try:
                return self._role_setter(role)
            except Exception:  # pragma: no cover
                logger.exception("quick_code.role_setter raised")
        return None

    def _reset_role(self, token: Any) -> None:
        if token is None or self._role_setter is None:
            return
        try:
            # If the setter is the real ContextVar.set(), the token has
            # a `.var.reset(token)` shape. Be lenient — when callers
            # supply their own setter we leave reset to them.
            from ..api.local_ai_routes_simple import _ACTIVE_ROLE  # noqa: PLC0415
            _ACTIVE_ROLE.reset(token)
        except Exception:
            pass

    async def _ensure_llm(self) -> LLMCall:
        if self._llm_call is None:
            from ..api.code_intelligence_routes import _llm_call_local  # noqa: PLC0415
            self._llm_call = _llm_call_local
        return self._llm_call

    async def _ensure_mesh(self) -> Any | None:
        """Lazily build a MultiMLMesh when use_mesh=True. Returns None
        if the mesh module is unavailable (import failure) — engine
        then degrades to single-path behaviour."""
        if not self.request.use_mesh:
            return None
        if self._mesh is not None:
            return self._mesh
        try:
            from ..code_intelligence.mesh import MultiMLMesh  # noqa: PLC0415
        except Exception as exc:
            logger.warning("quick_code_mesh_import_failed: %s", exc)
            return None
        llm = await self._ensure_llm()

        def _cancel_check() -> bool:
            return bool(self.request.cancel_requested)

        self._mesh = MultiMLMesh(
            llm_call=llm,
            on_event=self._emit,
            role_setter=self._role_setter,
            cancel_check=_cancel_check,
            session_id=self.session_id,
        )
        return self._mesh

    # ─── Phase 1: Triage ────────────────────────────────────────────────

    async def _phase_triage(self) -> dict[str, Any]:
        self._check_cancel()
        await self._emit({"type": "quick_code_phase_start", "phase": "triage"})
        llm = await self._ensure_llm()
        try:
            triage = await run_triage(
                llm,
                user_prompt=self.request.prompt,
                code_context=self.request.code_context,
                max_tokens=600,
            )
        except Exception as exc:
            logger.warning("quick_code_triage_failed: %s", exc)
            triage = {
                "task_type": "generation",
                "language": self.request.language or "python",
                "complexity": "moderate",
                "needs_execution": True,
                "needs_tests": True,
            }
        # Honour explicit user language even if triage disagrees.
        if self.request.language:
            triage["language"] = self.request.language
        self.bundle.triage = triage
        await self._emit({
            "type": "quick_code_phase_complete",
            "phase": "triage",
            "triage": triage,
        })
        return triage

    # ─── Phase 2: Reason ────────────────────────────────────────────────

    async def _phase_reason(self) -> QuickCodeReasoning:
        self._check_cancel()
        await self._emit({"type": "quick_code_phase_start", "phase": "reason"})

        # v9 — when the Multi-ML Mesh is enabled, fan out reasoning to
        # N parallel specialists (general / math / performance /
        # edge_case). The aggregator merges, dedupes, and re-scores
        # via the composite formula. Failure of any single specialist
        # is non-fatal — the aggregator just sees fewer inputs.
        mesh = await self._ensure_mesh()
        reasoning: QuickCodeReasoning
        if mesh is not None:
            try:
                aggregated = await mesh.run_reasoning(
                    user_prompt=self.request.prompt,
                    code_context=self.request.code_context,
                    triage=self.bundle.triage,
                )
                reasoning = aggregated.reasoning
                # Persist the per-specialist envelope on the bundle so
                # the SSE / artifact / metrics writer can introspect it.
                self.bundle.mesh_reasoning = aggregated.to_dict()
            except Exception as exc:
                logger.warning("quick_code_mesh_reasoning_failed: %s", exc)
                reasoning = await self._reason_single_path()
        else:
            reasoning = await self._reason_single_path()

        # Engine recomputes the chosen alternative locally — the
        # aggregator already does this, but we keep this defensive
        # check so single-path runs and any future mesh changes share
        # the same audit trail.
        chosen = self._pick_best(reasoning.alternatives)
        if chosen is not None and reasoning.chosen_label != chosen.label:
            reasoning.findings.append(
                f"engine override: chosen {reasoning.chosen_label or '?'} "
                f"→ {chosen.label} (composite {chosen.composite:.2f})"
            )
            reasoning.chosen_label = chosen.label
        self.bundle.reasoning = reasoning

        await self._emit({
            "type": "quick_code_phase_complete",
            "phase": "reason",
            "reasoning": reasoning.to_dict(),
        })
        gate = self._gate_reasoning(reasoning)
        self.bundle.gates.append(gate)
        await self._emit({"type": "quick_code_gate", "gate": gate.to_dict()})
        return reasoning

    async def _reason_single_path(self) -> QuickCodeReasoning:
        """Fallback / non-mesh path — one LLM call producing JSON."""
        llm = await self._ensure_llm()
        max_tokens = _EFFORT_TO_MAX_TOKENS.get(self.request.effort, 1500)
        token = self._set_role("reasoner")
        try:
            raw = await llm(
                reasoning_prompt(
                    self.request.prompt,
                    code_context=self.request.code_context,
                    triage=self.bundle.triage,
                ),
                REASONING_SYSTEM_PROMPT,
                max_tokens,
            )
        finally:
            self._reset_role(token)
        return self._parse_reasoning(raw or "")

    def _parse_reasoning(self, raw: str) -> QuickCodeReasoning:
        """Parse the reasoner's JSON. On any parse failure synthesize a
        single fallback alternative so downstream phases can still run
        — gate becomes ``passed_warn`` rather than failed."""
        try:
            data = _extract_json(raw)
        except ValueError:
            return QuickCodeReasoning(
                alternatives=[QuickCodeAlternative(
                    label="A",
                    summary="(reasoning JSON malformed; degraded to single-path)",
                    scores={"clarity": 0.5, "math_soundness": 0.5,
                            "performance": 0.5, "edge_cases": 0.5},
                )],
                chosen_label="A",
                rationale="Reasoning model output was not valid JSON. "
                          "Engine fell back to a single-path baseline.",
                raw_llm=raw[:4000],
                findings=["reasoning JSON malformed; degraded to single-path"],
            )

        alts: list[QuickCodeAlternative] = []
        for raw_alt in (data.get("alternatives") or [])[:3]:
            if not isinstance(raw_alt, dict):
                continue
            scores = raw_alt.get("scores") or {}
            if not isinstance(scores, dict):
                scores = {}
            edge_cases_raw = raw_alt.get("edge_cases") or []
            edge_cases = [str(e)[:200] for e in edge_cases_raw if e][:6]
            alts.append(QuickCodeAlternative(
                label=str(raw_alt.get("label") or "")[:8] or chr(ord("A") + len(alts)),
                summary=str(raw_alt.get("summary") or "")[:400],
                scores={
                    axis: self._clamp_score(scores.get(axis))
                    for axis in ("clarity", "math_soundness",
                                 "performance", "edge_cases")
                },
                complexity_estimate=str(raw_alt.get("complexity_estimate") or "")[:80],
                perf_notes=str(raw_alt.get("perf_notes") or "")[:400],
                edge_cases=edge_cases,
            ))

        if not alts:
            return QuickCodeReasoning(
                alternatives=[QuickCodeAlternative(
                    label="A",
                    summary="(no alternatives parsed; degraded to single-path)",
                    scores={"clarity": 0.5, "math_soundness": 0.5,
                            "performance": 0.5, "edge_cases": 0.5},
                )],
                chosen_label="A",
                rationale=str(data.get("rationale") or "")[:1200],
                raw_llm=raw[:4000],
                findings=["reasoner returned no alternatives"],
            )

        return QuickCodeReasoning(
            alternatives=alts,
            chosen_label=str(data.get("chosen") or alts[0].label)[:8],
            rationale=str(data.get("rationale") or "")[:1200],
            raw_llm=raw[:4000],
        )

    @staticmethod
    def _clamp_score(value: Any) -> float:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, f))

    @staticmethod
    def _pick_best(
        alternatives: list[QuickCodeAlternative],
    ) -> QuickCodeAlternative | None:
        if not alternatives:
            return None
        return max(alternatives, key=lambda a: a.composite)

    def _gate_reasoning(self, r: QuickCodeReasoning) -> QuickCodeGate:
        score = 60.0
        findings = list(r.findings)
        if len(r.alternatives) >= 2:
            score += 15
        else:
            findings.append("only one alternative considered")
        chosen = r.chosen
        if chosen and all(axis in (chosen.scores or {})
                          for axis in ("clarity", "math_soundness",
                                       "performance", "edge_cases")):
            score += 10
        else:
            findings.append("chosen alternative missing one or more score axes")
        if len(r.rationale) >= 80:
            score += 5
        else:
            findings.append("rationale shorter than 80 chars")
        score = max(0.0, min(100.0, score))
        status: Any = (
            "passed" if score >= 80
            else "passed_warn" if score >= 60
            else "failed"
        )
        return QuickCodeGate(
            phase="reason",
            status=status,
            score=round(score, 1),
            findings=findings,
            summary=(
                f"{len(r.alternatives)} alternatives · "
                f"chosen={r.chosen_label or '-'} "
                f"composite={chosen.composite if chosen else 0.0:.2f}"
            ),
        )

    # ─── Phase 3: Implement ─────────────────────────────────────────────

    async def _phase_implement(self) -> tuple[str, str | None]:
        self._check_cancel()
        await self._emit({"type": "quick_code_phase_start", "phase": "implement"})
        llm = await self._ensure_llm()
        language = self.bundle.triage.get("language") or self.request.language or "python"

        # Build a synthetic plan from the reasoning so CoderAgent has
        # the same shape it normally consumes from PlannerAgent.
        chosen_summary = ""
        if self.bundle.reasoning and self.bundle.reasoning.chosen:
            chosen_summary = self.bundle.reasoning.chosen.summary
        plan = {
            "language": language,
            "title": (chosen_summary or self.request.prompt)[:100],
            "deliverable_type": "code_snippet",
            "task_type": self.bundle.triage.get("task_type") or "generation",
            "plan": [{"step": 1, "action": "implement chosen approach",
                      "agent": "coder", "description": chosen_summary,
                      "depends_on": []}],
        }
        ctx = AgentContext(
            user_prompt=self.request.prompt,
            code_context=self.request.code_context,
            triage=self.bundle.triage,
            plan=plan,
            language=language,
        )

        token = self._set_role("coder")
        try:
            coder = CoderAgent(llm)
            coder_out = await coder.run(ctx)
        finally:
            self._reset_role(token)

        if coder_out.error or not coder_out.code:
            raise RuntimeError(f"Coder failed: {coder_out.error or 'no code'}")

        code = coder_out.code
        self.bundle.code = code

        tests: str | None = None
        if self.bundle.triage.get("needs_tests", True):
            ctx.code = code
            token = self._set_role("coder")
            try:
                tester = TesterAgent(llm)
                tester_out = await tester.run(ctx)
            finally:
                self._reset_role(token)
            if not tester_out.error and tester_out.code:
                tests = tester_out.code
                self.bundle.tests = tests

        await self._emit({
            "type": "quick_code_phase_complete",
            "phase": "implement",
            "code_chars": len(code),
            "tests_chars": len(tests or ""),
            "language": language,
        })
        return code, tests

    # ─── Phase 4: Verify ────────────────────────────────────────────────

    async def _phase_verify(self) -> QuickCodeVerification:
        self._check_cancel()
        await self._emit({"type": "quick_code_phase_start", "phase": "verify"})
        verification = QuickCodeVerification()
        language = self.bundle.triage.get("language") or self.request.language or "python"

        # Static analysis (deterministic, no LLM).
        try:
            harness = await self._ensure_static_harness()
            if harness is not None:
                static_result = await harness.analyze(
                    self.bundle.code or "", language=language,
                )
                verification.static = static_result.to_dict()
        except Exception as exc:
            logger.warning("quick_code_static_analysis_failed: %s", exc)
            verification.static = None

        # Sandbox execution (deterministic).
        try:
            sandbox = await self._ensure_sandbox()
            if sandbox is not None and self.bundle.code:
                exec_result = await sandbox.execute(
                    self.bundle.code, language=language,
                )
                verification.execution = exec_result.to_dict()
        except Exception as exc:
            logger.warning("quick_code_sandbox_execute_failed: %s", exc)
            verification.execution = None

        verification.severities = self._count_severities(verification.static)
        verification.score = self._compute_verification_score(verification)
        self.bundle.verification = verification

        await self._emit({
            "type": "quick_code_phase_complete",
            "phase": "verify",
            "verification": verification.to_dict(),
        })
        gate = self._gate_verification(verification)
        self.bundle.gates.append(gate)
        await self._emit({"type": "quick_code_gate", "gate": gate.to_dict()})
        return verification

    async def _ensure_sandbox(self) -> Any | None:
        if self._sandbox is not None:
            return self._sandbox
        try:
            from ..api.code_intelligence_routes import get_sandbox  # noqa: PLC0415
            self._sandbox = get_sandbox()
        except Exception as exc:  # pragma: no cover
            logger.warning("quick_code_sandbox_init_failed: %s", exc)
            self._sandbox = None
        return self._sandbox

    async def _ensure_static_harness(self) -> Any | None:
        if self._static_harness is not None:
            return self._static_harness
        try:
            from ..api.code_intelligence_routes import get_static_harness  # noqa: PLC0415
            self._static_harness = get_static_harness()
        except Exception as exc:  # pragma: no cover
            logger.warning("quick_code_static_harness_init_failed: %s", exc)
            self._static_harness = None
        return self._static_harness

    @staticmethod
    def _count_severities(static: dict[str, Any] | None) -> dict[str, int]:
        if not static or not isinstance(static, dict):
            return {}
        counts = static.get("severity_counts")
        if isinstance(counts, dict):
            return {str(k): int(v) for k, v in counts.items()}
        return {}

    def _compute_verification_score(self, v: QuickCodeVerification) -> float:
        score = 60.0
        # Execution
        if v.execution:
            if v.execution.get("skipped"):
                pass  # neutral
            elif v.execution.get("success"):
                score += 20
            else:
                score -= 10
        # Static analysis
        if v.static is not None:
            score += 10
            critical = sum(v.severities.get(sev, 0) for sev in _CRITICAL_SEVERITIES)
            score -= 10 * critical
        return max(0.0, min(100.0, score))

    def _gate_verification(self, v: QuickCodeVerification) -> QuickCodeGate:
        findings: list[str] = []
        if v.execution and not v.execution.get("skipped") and not v.execution.get("success"):
            err_excerpt = (v.execution.get("stderr") or v.execution.get("error") or "")[:240]
            findings.append(f"execution failed: {err_excerpt}")
        if v.execution and v.execution.get("skipped"):
            findings.append("sandbox unavailable; execution skipped")
        if v.static is None:
            findings.append("static analysis unavailable")
        critical = sum(v.severities.get(sev, 0) for sev in _CRITICAL_SEVERITIES)
        if critical:
            findings.append(f"{critical} critical static issue(s)")
        score = v.score
        # Failed only if exec returned non-zero AND no refine pass remains.
        exec_failed = bool(
            v.execution and not v.execution.get("skipped")
            and not v.execution.get("success")
        )
        if exec_failed and self.request.max_refine == 0:
            status: Any = "failed"
        elif score >= 80:
            status = "passed"
        elif score >= 60:
            status = "passed_warn"
        else:
            status = "failed"
        return QuickCodeGate(
            phase="verify",
            status=status,
            score=round(score, 1),
            findings=findings,
            summary=(
                f"exec={'ok' if not exec_failed else 'fail'} · "
                f"static={'ran' if v.static is not None else 'n/a'} · "
                f"critical={critical}"
            ),
        )

    # ─── Phase 5: Refine ────────────────────────────────────────────────

    async def _phase_refine_if_needed(self) -> tuple[str | None, str | None, int]:
        if self.request.max_refine <= 0:
            return None, None, 0
        v = self.bundle.verification
        if v is None:
            return None, None, 0
        # Only refine when there's a real failure to fix — skipped exec
        # without static issues is not worth a refine pass.
        exec_failed = bool(
            v.execution and not v.execution.get("skipped")
            and not v.execution.get("success")
        )
        critical = sum(v.severities.get(sev, 0) for sev in _CRITICAL_SEVERITIES)
        if not exec_failed and critical == 0:
            return None, None, 0

        self._check_cancel()
        await self._emit({"type": "quick_code_phase_start", "phase": "refine"})
        llm = await self._ensure_llm()
        language = self.bundle.triage.get("language") or self.request.language or "python"

        iteration = 0
        new_code = self.bundle.code
        new_tests = self.bundle.tests
        prior_critical = critical
        while iteration < self.request.max_refine:
            iteration += 1
            self._check_cancel()
            ctx = AgentContext(
                user_prompt=self.request.prompt,
                code_context=self.request.code_context,
                code=new_code,
                tests=new_tests,
                language=language,
                execution_feedback=self._compact_exec_feedback(v),
                static_feedback=self._compact_static_feedback(v),
                debug_iteration=iteration,
            )
            token = self._set_role("coder")
            try:
                debugger = DebuggerAgent(llm)
                debug_out = await debugger.run(ctx)
            finally:
                self._reset_role(token)
            if debug_out.error or not debug_out.code:
                # Stop refining; keep the prior code.
                break
            new_code = debug_out.code
            self.bundle.code = new_code

            # Re-run verify to see if the refine helped.
            v = await self._phase_verify()
            new_critical = sum(v.severities.get(sev, 0) for sev in _CRITICAL_SEVERITIES)
            new_exec_failed = bool(
                v.execution and not v.execution.get("skipped")
                and not v.execution.get("success")
            )
            improved = (new_critical < prior_critical) or (
                exec_failed and not new_exec_failed
            )
            self._check_cancel()
            await self._emit({
                "type": "quick_code_refine_iteration",
                "iteration": iteration,
                "improved": bool(improved),
                "critical": new_critical,
            })
            prior_critical = new_critical
            exec_failed = new_exec_failed
            if not exec_failed and new_critical == 0:
                break  # all clean

        await self._emit({
            "type": "quick_code_phase_complete",
            "phase": "refine",
            "iterations": iteration,
        })
        gate = self._gate_refine(iteration, prior_critical)
        self.bundle.gates.append(gate)
        await self._emit({"type": "quick_code_gate", "gate": gate.to_dict()})
        self.bundle.refine_iterations = iteration
        return new_code, new_tests, iteration

    @staticmethod
    def _compact_exec_feedback(v: QuickCodeVerification) -> str:
        if not v.execution:
            return "(no execution data)"
        e = v.execution
        if e.get("skipped"):
            return "Execution: skipped (sandbox unavailable)"
        if e.get("success"):
            return f"Execution: ok (exit=0, {e.get('duration_ms', 0)}ms)"
        lines = [f"Execution: failed (exit={e.get('exit_code', '?')}, "
                 f"{e.get('duration_ms', 0)}ms)"]
        if e.get("error"):
            lines.append(f"ERROR: {str(e['error'])[:400]}")
        if (e.get("stderr") or "").strip():
            lines.append(f"STDERR:\n{str(e['stderr'])[:1200]}")
        if (e.get("stdout") or "").strip():
            lines.append(f"STDOUT:\n{str(e['stdout'])[:600]}")
        return "\n".join(lines)

    @staticmethod
    def _compact_static_feedback(v: QuickCodeVerification) -> str:
        if not v.static:
            return "(no static analysis)"
        s = v.static
        counts = s.get("severity_counts") or {}
        lines = [
            f"Static: errors={counts.get('error', 0)} "
            f"warnings={counts.get('warning', 0)} "
            f"security={counts.get('security', 0)}"
        ]
        for issue in (s.get("issues") or [])[:10]:
            sev = str(issue.get("severity") or "?").upper()
            line = issue.get("line")
            code = issue.get("code", "")
            msg = str(issue.get("message", ""))[:160]
            lines.append(f"  [{sev}] L{line if line else '?'} {code}: {msg}")
        return "\n".join(lines)

    def _gate_refine(self, iterations: int, final_critical: int) -> QuickCodeGate:
        score = 70.0 if iterations > 0 else 60.0
        findings: list[str] = []
        if final_critical == 0:
            score += 10
        else:
            findings.append(f"{final_critical} critical issue(s) remain after refine")
        score = max(0.0, min(100.0, score))
        status: Any = (
            "passed" if score >= 80
            else "passed_warn" if score >= 60
            else "failed"
        )
        return QuickCodeGate(
            phase="refine",
            status=status,
            score=round(score, 1),
            findings=findings,
            summary=f"iterations={iterations} · critical_remaining={final_critical}",
        )

    # ─── Phase 6: Code Audit (mesh) ─────────────────────────────────────

    async def _phase_code_audit(self) -> dict[str, Any] | None:
        """Mesh phase — N auditors review the FINAL code in parallel."""
        if not self.request.use_mesh:
            return None
        if not (self.bundle.code or "").strip():
            return None
        mesh = await self._ensure_mesh()
        if mesh is None:
            return None
        self._check_cancel()
        await self._emit({"type": "quick_code_phase_start", "phase": "audit"})
        try:
            audit = await mesh.run_code_audit(
                user_prompt=self.request.prompt,
                code=self.bundle.code or "",
                tests=self.bundle.tests,
                language=(self.bundle.triage.get("language")
                          or self.request.language or "python"),
            )
        except Exception as exc:
            logger.warning("quick_code_mesh_audit_failed: %s", exc)
            return None
        audit_dict = audit.to_dict()
        self.bundle.mesh_audit = audit_dict
        await self._emit({
            "type": "quick_code_phase_complete",
            "phase": "audit",
            "audit": audit_dict,
        })
        gate = self._gate_code_audit(audit)
        self.bundle.gates.append(gate)
        await self._emit({"type": "quick_code_gate", "gate": gate.to_dict()})
        return audit_dict

    def _gate_code_audit(self, audit: Any) -> QuickCodeGate:
        """Score the auditor mesh: base 70, +5 per non-error auditor
        with `approve` verdict, −10 per `reject`, capped 0..100."""
        base = 70.0
        findings: list[str] = []
        if not getattr(audit, "auditors", None):
            findings.append("no auditors ran")
        else:
            for a in audit.auditors:
                if a.error:
                    findings.append(f"{a.role_label}: {a.error[:160]}")
                    continue
                if a.verdict == "approve":
                    base += 5
                elif a.verdict == "approve_with_changes":
                    base -= 2
                elif a.verdict == "reject":
                    base -= 10
                    findings.append(
                        f"{a.role_label} REJECT (conf {a.confidence:.2f}): "
                        f"{a.summary[:160]}"
                    )
        score = max(0.0, min(100.0, base))
        status: Any = (
            "passed"      if score >= 80
            else "passed_warn" if score >= 60
            else "failed"
        )
        return QuickCodeGate(
            phase="audit", status=status, score=round(score, 1),
            findings=findings,
            summary=(
                f"avg_conf={getattr(audit, 'average_confidence', 0.0):.2f} · "
                f"any_reject={'yes' if getattr(audit, 'any_rejected', False) else 'no'}"
            ),
        )

    # ─── Phase 7: Meta-arbiter (mesh) ───────────────────────────────────

    async def _phase_meta_arbiter(self) -> dict[str, Any] | None:
        """Mesh phase — single arbiter call synthesises everything."""
        if not self.request.use_mesh:
            return None
        if not (self.bundle.code or "").strip():
            return None
        mesh = await self._ensure_mesh()
        if mesh is None:
            return None
        self._check_cancel()
        await self._emit({"type": "quick_code_phase_start", "phase": "arbiter"})

        # Reconstruct mesh_audit from the dict we stashed earlier.
        from ..code_intelligence.mesh import MeshCodeAudit  # noqa: PLC0415
        from ..code_intelligence.mesh.code_auditors import (  # noqa: PLC0415
            AuditorOutput,
        )
        audit_obj: Any = None
        if self.bundle.mesh_audit:
            try:
                audit_obj = MeshCodeAudit(
                    auditors=[
                        AuditorOutput(**{
                            k: v for k, v in a.items()
                            if k in {"role", "role_label", "verdict",
                                     "confidence", "summary", "payload",
                                     "error"}
                        })
                        for a in (self.bundle.mesh_audit.get("auditors") or [])
                    ],
                    findings=list(self.bundle.mesh_audit.get("findings") or []),
                )
            except Exception:
                audit_obj = None

        chosen_summary = ""
        chosen_rationale = ""
        if self.bundle.reasoning:
            if self.bundle.reasoning.chosen:
                chosen_summary = self.bundle.reasoning.chosen.summary
            chosen_rationale = self.bundle.reasoning.rationale

        try:
            verdict = await mesh.run_meta_arbiter(
                user_prompt=self.request.prompt,
                chosen_summary=chosen_summary,
                chosen_rationale=chosen_rationale,
                code=self.bundle.code or "",
                tests=self.bundle.tests,
                execution_summary=self._compact_exec_feedback(
                    self.bundle.verification or _EmptyVerification(),
                ),
                static_summary=self._compact_static_feedback(
                    self.bundle.verification or _EmptyVerification(),
                ),
                mesh_audit=audit_obj,
                refine_iterations=self.bundle.refine_iterations,
            )
        except Exception as exc:
            logger.warning("quick_code_meta_arbiter_failed: %s", exc)
            return None

        verdict_dict = verdict.to_dict()
        self.bundle.meta_verdict = verdict_dict
        await self._emit({
            "type": "quick_code_phase_complete",
            "phase": "arbiter",
            "meta_verdict": verdict_dict,
        })
        gate = self._gate_meta_arbiter(verdict)
        self.bundle.gates.append(gate)
        await self._emit({"type": "quick_code_gate", "gate": gate.to_dict()})
        return verdict_dict

    def _gate_meta_arbiter(self, verdict: Any) -> QuickCodeGate:
        """Score = production_readiness directly, with a status mapped
        from the verdict string."""
        readiness = float(getattr(verdict, "production_readiness", 0.0) or 0.0)
        status: Any = (
            "passed"      if getattr(verdict, "verdict", "") == "approve" and readiness >= 80
            else "passed_warn" if getattr(verdict, "verdict", "") in {"approve", "approve_with_changes"}
            else "failed"
        )
        findings: list[str] = []
        for r in (getattr(verdict, "top_risks", None) or []):
            findings.append(
                f"[{r.get('severity', '?')}] {r.get('description', '')[:200]}"
            )
        if getattr(verdict, "error", None):
            findings.append(f"arbiter error: {verdict.error[:200]}")
        return QuickCodeGate(
            phase="arbiter", status=status, score=round(readiness, 1),
            findings=findings,
            summary=(
                f"verdict={getattr(verdict, 'verdict', '?')} · "
                f"conf={getattr(verdict, 'confidence', 0.0):.2f} · "
                f"readiness={readiness:.0f}"
            ),
        )

    # ─── Run ────────────────────────────────────────────────────────────

    async def run(self) -> QuickCodeBundle:
        await self._emit({
            "type": "quick_code_started",
            "request": self.request.to_dict(),
        })
        try:
            await self._phase_triage()
            self._check_cancel()
            await self._phase_reason()
            self._check_cancel()
            await self._phase_implement()
            self._check_cancel()
            await self._phase_verify()
            self._check_cancel()
            if self.request.max_refine > 0 and self.request.allow_refine:
                await self._phase_refine_if_needed()
            # v9 — Multi-ML Mesh post-processing. Each phase is fail-soft
            # (returns None on any error) so the bundle still ships even
            # if the mesh is misconfigured.
            self._check_cancel()
            await self._phase_code_audit()
            self._check_cancel()
            await self._phase_meta_arbiter()
        except asyncio.CancelledError:
            self.bundle.deliverable_markdown = "*Cancelled by user.*"
            self.bundle.completed_at = _now_iso()
            await self._emit({"type": "quick_code_cancelled"})
            return self.bundle
        except Exception as exc:
            logger.exception("quick_code_run_failed")
            self.bundle.deliverable_markdown = f"*QuickCode failed: {exc}*"
            self.bundle.completed_at = _now_iso()
            await self._emit({
                "type": "quick_code_error",
                "error": str(exc)[:400],
            })
            return self.bundle

        self.bundle.deliverable_markdown = self._build_deliverable_markdown()
        self.bundle.completed_at = _now_iso()
        await self._emit({
            "type": "quick_code_completed",
            "session_id": self.session_id,
            "code_chars": len(self.bundle.code or ""),
            "tests_chars": len(self.bundle.tests or ""),
            "gates": [g.to_dict() for g in self.bundle.gates],
        })
        return self.bundle

    # ─── Deliverable markdown ───────────────────────────────────────────

    def _build_deliverable_markdown(self) -> str:
        rows: list[str] = []
        title = self._derive_title(self.request.prompt)
        rows.append(f"# {title}\n")
        rows.append(f"> {self._truncate(self.request.prompt, 280)}\n")

        if self.bundle.reasoning and self.bundle.reasoning.alternatives:
            rows.append("## Reasoning\n")
            chosen = self.bundle.reasoning.chosen
            for a in self.bundle.reasoning.alternatives:
                tick = " ← chosen" if (chosen and a.label == chosen.label) else ""
                scores = a.scores or {}
                rows.append(
                    f"- **{a.label}** ({a.complexity_estimate or '?'}) — "
                    f"clarity {scores.get('clarity', 0):.2f} · "
                    f"math {scores.get('math_soundness', 0):.2f} · "
                    f"perf {scores.get('performance', 0):.2f} · "
                    f"edge {scores.get('edge_cases', 0):.2f} → "
                    f"composite **{a.composite:.2f}**{tick}"
                )
                if a.summary:
                    rows.append(f"    - {a.summary}")
            if self.bundle.reasoning.rationale:
                rows.append(f"\n_Rationale_: {self.bundle.reasoning.rationale}\n")

        rows.append("## Verification\n")
        if self.bundle.verification:
            v = self.bundle.verification
            rows.append(f"- score: **{v.score:.0f} / 100**")
            if v.execution:
                e = v.execution
                if e.get("skipped"):
                    rows.append("- execution: skipped (sandbox unavailable)")
                elif e.get("success"):
                    rows.append(f"- execution: ✓ ({e.get('duration_ms', 0)}ms)")
                else:
                    rows.append(f"- execution: ✗ exit={e.get('exit_code', '?')}")
            if v.static is not None:
                counts = v.static.get("severity_counts") or {}
                rows.append(
                    f"- static analysis: errors {counts.get('error', 0)} · "
                    f"warnings {counts.get('warning', 0)} · "
                    f"security {counts.get('security', 0)}"
                )
        else:
            rows.append("_No verification performed._")

        if self.bundle.refine_iterations:
            rows.append(f"\n## Refinement\n- iterations: {self.bundle.refine_iterations}")

        # v9 — Mesh-driven sections come BEFORE the raw gate list so
        # the most actionable signal (production-readiness) is at the
        # top of the human-readable summary.
        if self.bundle.meta_verdict:
            mv = self.bundle.meta_verdict
            rows.append("\n## Production-readiness verdict\n")
            rows.append(
                f"- **Verdict**: `{mv.get('verdict', '?')}` · "
                f"confidence **{float(mv.get('confidence', 0.0) or 0.0):.2f}**"
            )
            rows.append(
                f"- **Production-readiness**: {float(mv.get('production_readiness', 0.0) or 0.0):.0f} / 100"
            )
            summary = mv.get("summary") or ""
            if summary:
                rows.append(f"\n_Arbiter summary_: {summary}")
            risks = mv.get("top_risks") or []
            if risks:
                rows.append("\n### Top risks")
                for r in risks:
                    rows.append(
                        f"- **[{r.get('severity', '?')}]** {r.get('description', '')}"
                    )
            strengths = mv.get("top_strengths") or []
            if strengths:
                rows.append("\n### Top strengths")
                for s in strengths:
                    rows.append(f"- {s}")

        if self.bundle.mesh_audit:
            ma = self.bundle.mesh_audit
            rows.append("\n## Mesh code audit\n")
            for aud in ma.get("auditors") or []:
                badge = {"approve": "✓", "approve_with_changes": "⚠",
                         "reject": "✗", "unknown": "?"}.get(
                            aud.get("verdict") or "unknown", "?")
                role_label = aud.get("role_label", aud.get("role", "?"))
                conf = float(aud.get("confidence", 0.0) or 0.0)
                summary = aud.get("summary") or aud.get("error") or ""
                rows.append(
                    f"- {badge} **{role_label}** "
                    f"(conf {conf:.2f}): {summary[:240]}"
                )

        if self.bundle.mesh_reasoning:
            mr = self.bundle.mesh_reasoning
            picks = mr.get("per_specialist_picks") or {}
            consensus = mr.get("consensus_count")
            if picks or consensus is not None:
                rows.append("\n## Mesh reasoning\n")
                if consensus is not None:
                    rows.append(f"- consensus on {consensus} alternative(s)")
                if picks:
                    picks_str = ", ".join(
                        f"`{role}`→**{label}**"
                        for role, label in sorted(picks.items())
                    )
                    rows.append(f"- per-specialist picks: {picks_str}")

        rows.append("\n## Phase gates\n")
        for g in self.bundle.gates:
            badge = {"passed": "✓", "passed_warn": "⚠", "failed": "✗"}.get(g.status, "?")
            rows.append(f"- {badge} **{g.phase}** — score {g.score} · {g.summary}")

        return "\n".join(rows)

    @staticmethod
    def _derive_title(prompt: str) -> str:
        text = re.sub(r"\s+", " ", (prompt or "").strip())
        if not text:
            return "QuickCode result"
        if len(text) <= 60:
            return text[:1].upper() + text[1:]
        cut = text[:60]
        sp = cut.rfind(" ")
        if sp > 40:
            cut = cut[:sp]
        return cut[:1].upper() + cut[1:].rstrip(",.;:") + "…"

    @staticmethod
    def _truncate(s: str, n: int) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else s[: n - 1].rstrip() + "…"


class _EmptyVerification:
    """Sentinel used when a phase needs to call the
    `_compact_exec_feedback` / `_compact_static_feedback` helpers
    before any real verification has run yet (e.g. cancellation paths).
    Mirrors the QuickCodeVerification shape with empty fields."""
    execution = None
    static = None
    score = 0.0
    severities: dict[str, int] = {}
