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

    # ─── Phase 5b: Reactor verify (v10) ─────────────────────────────────

    async def _phase_reactor_verify(self) -> dict[str, Any] | None:
        """Run the v10 reactor's empirical verification (symbolic +
        bench + property tests) on the generated code. Returns the
        ReactorBundle dict on success, None when the reactor is
        disabled or fails. Always fail-soft."""
        try:
            from ..config.settings import settings  # noqa: PLC0415
            if not getattr(settings, "code_reactor_enabled", True):
                return None
        except Exception:
            return None
        if not (self.bundle.code or "").strip():
            return None
        try:
            from ..code_intelligence.reactor import (  # noqa: PLC0415
                CodeSynthesisReactor, ReactorConfig,
            )
        except Exception as exc:
            logger.debug("quick_code_reactor_import_failed: %s", exc)
            return None

        self._check_cancel()
        await self._emit({"type": "quick_code_phase_start", "phase": "reactor"})

        try:
            llm = await self._ensure_llm()
            sandbox = await self._ensure_sandbox()
            reactor = CodeSynthesisReactor(
                config=ReactorConfig.from_settings(),
                llm_call=llm,
                sandbox=sandbox,
                role_setter=self._role_setter,
            )
            # Phase 1B — prefer the LogicEngine's complexity_hint as
            # the source of truth (it's the value Z3 verified against,
            # not a stochastic LLM guess). Fall back to the reasoning
            # specialist's claim, then the empty string.
            claimed = ""
            skel = self.bundle.logic_skeleton or {}
            if skel.get("complexity_hint"):
                claimed = str(skel["complexity_hint"])
            elif self.bundle.reasoning and self.bundle.reasoning.chosen:
                claimed = (
                    self.bundle.reasoning.chosen.complexity_estimate or ""
                )
            reactor_bundle = await reactor.verify_implementation(
                code=self.bundle.code,
                tests=self.bundle.tests,
                user_prompt=self.request.prompt,
                triage=self.bundle.triage,
                claimed_complexity=claimed,
                language=(self.bundle.triage.get("language")
                          or self.request.language or "python"),
            )
        except Exception as exc:
            logger.warning("quick_code_reactor_verify_failed: %s", exc)
            return None

        bundle_dict = reactor_bundle.to_dict()
        self.bundle.reactor_bundle = bundle_dict
        await self._emit({
            "type": "quick_code_phase_complete",
            "phase": "reactor",
            "reactor": bundle_dict,
        })
        gate = self._gate_reactor(reactor_bundle)
        self.bundle.gates.append(gate)
        await self._emit({"type": "quick_code_gate", "gate": gate.to_dict()})
        return bundle_dict

    def _gate_reactor(self, rb: Any) -> QuickCodeGate:
        """Score the reactor pass: base 70, +10 if symbolic ran, +10
        if benchmark ran without claim mismatch, +10 if property tests
        all passed. Caps 0..100."""
        score = 70.0
        findings: list[str] = list(getattr(rb, "findings", []) or [])
        if getattr(rb, "symbolic", None):
            score += 10
        bench = getattr(rb, "benchmark", None) or {}
        if bench:
            score += 10
            if bench.get("claim_vs_measured") == -1:
                score -= 15
        props = getattr(rb, "property_tests", None) or {}
        if props:
            if props.get("all_passed"):
                score += 10
            else:
                num_failed = int(props.get("num_failed", 0) or 0)
                score -= 5 * num_failed
        score = max(0.0, min(100.0, score))
        status: Any = (
            "passed"      if score >= 80
            else "passed_warn" if score >= 60
            else "failed"
        )
        return QuickCodeGate(
            phase="reactor", status=status, score=round(score, 1),
            findings=findings,
            summary=(
                f"sym={'ran' if getattr(rb, 'symbolic', None) else 'n/a'} · "
                f"bench={'ran' if bench else 'n/a'} · "
                f"props={'pass' if props.get('all_passed') else ('fail' if props else 'n/a')}"
            ),
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

    # ─── Phase 1B: Cognitive upgrade ────────────────────────────────────

    async def _phase_episodic_recall(self) -> dict[str, Any] | None:
        """Look up past sessions similar to the current prompt.

        Phase 1B records the routing decision (reuse / seed / fresh)
        on the bundle but does NOT short-circuit the pipeline yet —
        the engine always runs the full flow this round so we can
        observe the reuse-rate without committing to skip work.
        Future rounds can flip the short-circuit once we trust the
        retrieval quality.
        """
        if not self._cognitive_phase_1b_enabled():
            return None
        try:
            from ..config.settings import settings  # noqa: PLC0415
            if not getattr(settings, "episodic_memory_enabled", True):
                return None
            from local_ai.episodic_memory import (  # noqa: PLC0415
                EpisodicMemoryStore, hash_embedder,
            )
        except Exception as exc:
            logger.debug("quick_code_episodic_import_failed: %s", exc)
            return None

        self._check_cancel()
        await self._emit({
            "type": "quick_code_phase_start", "phase": "episodic_recall",
        })
        try:
            store = self._get_episodic_store()
            decision = await store.decide(self.request.prompt)
        except Exception as exc:
            logger.debug("quick_code_episodic_decide_failed: %s", exc)
            return None
        decision_dict = decision.to_dict()
        self.bundle.episodic_decision = decision_dict
        await self._emit({
            "type": "quick_code_phase_complete", "phase": "episodic_recall",
            "decision": decision_dict,
        })
        return decision_dict

    async def _phase_logic_skeleton(self) -> dict[str, Any] | None:
        """Run LogicEngine + Z3Verifier on the user prompt.

        Both outputs land on the bundle. The skeleton's
        ``complexity_hint`` later seeds the Reactor's claimed-Big-O
        check so the benchmark can flag a candidate that's measurably
        slower than the verified contract.
        """
        if not self._cognitive_phase_1b_enabled():
            return None
        try:
            from ..config.settings import settings  # noqa: PLC0415
            if not getattr(settings, "z3_verification_enabled", True):
                return None
            from local_ai.logic_engine import LogicEngine  # noqa: PLC0415
            from local_ai.z3_verifier import Z3Verifier  # noqa: PLC0415
        except Exception as exc:
            logger.debug("quick_code_logic_import_failed: %s", exc)
            return None

        self._check_cancel()
        await self._emit({
            "type": "quick_code_phase_start", "phase": "logic_skeleton",
        })
        try:
            strategy = getattr(settings, "logic_engine_strategy", "rule_based")
            engine = LogicEngine(strategy=strategy)
            skeleton = await engine.generate(self.request.prompt)
            self.bundle.logic_skeleton = skeleton.to_dict()
            verdict_dict: dict[str, Any] | None = None
            if skeleton.verifier_skeleton is not None:
                verifier = Z3Verifier(
                    timeout_ms=int(
                        getattr(settings, "z3_timeout_seconds", 30) * 1000
                    ),
                )
                report = verifier.verify_skeleton(skeleton.verifier_skeleton)
                verdict_dict = report.to_dict()
                self.bundle.z3_verification = verdict_dict
        except Exception as exc:
            logger.debug("quick_code_logic_skeleton_failed: %s", exc)
            return None

        await self._emit({
            "type": "quick_code_phase_complete", "phase": "logic_skeleton",
            "complexity_hint": skeleton.complexity_hint,
            "matched_template": skeleton.matched_template,
            "z3_overall": (verdict_dict or {}).get("overall"),
        })
        return self.bundle.logic_skeleton

    async def _phase_persist_episode(self) -> dict[str, Any] | None:
        """Write the (verified, passing) session to EpisodicMemory.

        Only stored when the verification gate passed AND we have
        non-empty code. Best-effort: storage failure is logged and
        the run completes normally.
        """
        if not self._cognitive_phase_1b_enabled():
            return None
        try:
            from ..config.settings import settings  # noqa: PLC0415
            if not getattr(settings, "episodic_memory_enabled", True):
                return None
            from local_ai.episodic_memory import (  # noqa: PLC0415
                EpisodicMemoryEntry,
            )
        except Exception as exc:
            logger.debug("quick_code_episodic_store_import_failed: %s", exc)
            return None

        if not (self.bundle.code or "").strip():
            return None
        v = self.bundle.verification
        pass_rate = self._compute_test_pass_rate()
        # Don't persist failures or skipped runs — they pollute the
        # similarity search with low-quality matches.
        min_rate = float(getattr(settings, "rlef_min_pass_rate", 0.8))
        if pass_rate < min_rate:
            return None

        try:
            entry = EpisodicMemoryEntry(
                session_id=self.session_id,
                user_query=self.request.prompt,
                algorithm_skeleton=self.bundle.logic_skeleton or {},
                final_code=self.bundle.code or "",
                test_pass_rate=pass_rate,
                language=(
                    self.bundle.triage.get("language")
                    or self.request.language or "python"
                ),
                complexity=(
                    (self.bundle.logic_skeleton or {}).get("complexity_hint")
                    or ""
                ),
                tags=self._derive_tags(),
            )
            store = self._get_episodic_store()
            await store.store(entry)
            await self._emit({
                "type": "quick_code_phase_complete", "phase": "episodic_store",
                "stored": True, "content_hash": entry.content_hash,
            })
            return {"stored": True, "content_hash": entry.content_hash}
        except Exception as exc:
            logger.debug("quick_code_episodic_store_failed: %s", exc)
            return None

    async def _phase_emit_rlef(self) -> dict[str, Any] | None:
        """Build + persist + publish the RLEF reward at end of run."""
        if not self._cognitive_phase_1b_enabled():
            return None
        try:
            from ..config.settings import settings  # noqa: PLC0415
            if not getattr(settings, "rlef_enabled", True):
                return None
            from local_ai.rlef_collector import RLEFCollector  # noqa: PLC0415
        except Exception as exc:
            logger.debug("quick_code_rlef_import_failed: %s", exc)
            return None

        try:
            collector = self._get_rlef_collector()
            v = self.bundle.verification
            exec_data = (v.execution if v else {}) or {}
            static_data = (v.static if v else {}) or {}
            severity_counts = static_data.get("severity_counts") or {}
            had_runtime_error = bool(
                exec_data and not exec_data.get("skipped")
                and not exec_data.get("success")
            )
            z3_passed = (
                (self.bundle.z3_verification or {}).get("overall") == "pass"
            )
            reward = collector.build_reward(
                session_id=self.session_id,
                code_hash=self._code_hash(),
                test_pass_rate=self._compute_test_pass_rate(),
                compilation_success=(
                    not had_runtime_error and bool(self.bundle.code)
                ),
                runtime_error=(
                    str(exec_data.get("stderr") or exec_data.get("error") or "")[:400]
                    if had_runtime_error else None
                ),
                execution_time_ms=float(exec_data.get("duration_ms", 0.0) or 0.0),
                z3_was_verified=z3_passed,
                mcts_iterations_used=int(self.bundle.refine_iterations or 0),
                language=(
                    self.bundle.triage.get("language")
                    or self.request.language or "python"
                ),
                task_type=str(self.bundle.triage.get("task_type") or ""),
                extras={
                    "static_errors": int(severity_counts.get("error", 0) or 0),
                    "static_security": int(severity_counts.get("security", 0) or 0),
                },
            )
            sink_result = await collector.collect(reward)
            payload = {
                **reward.to_dict(),
                "sink_result": sink_result,
            }
            self.bundle.rlef_reward = payload
            await self._emit({
                "type": "quick_code_phase_complete", "phase": "rlef_emit",
                "reward_score": reward.reward_score,
                "sink_result": sink_result,
            })
            return payload
        except Exception as exc:
            logger.debug("quick_code_rlef_emit_failed: %s", exc)
            return None

    # ── Phase 1B helpers ───────────────────────────────────────────

    @staticmethod
    def _cognitive_phase_1b_enabled() -> bool:
        try:
            from ..config.settings import settings  # noqa: PLC0415
            return bool(getattr(settings, "cognitive_phase_1b_enabled", True))
        except Exception:
            return True

    def _get_episodic_store(self) -> Any:
        """Lazy singleton per engine instance."""
        if getattr(self, "_episodic_store", None) is not None:
            return self._episodic_store
        from local_ai.episodic_memory import (  # noqa: PLC0415
            EpisodicMemoryStore, hash_embedder,
        )
        from ..config.settings import settings  # noqa: PLC0415
        # Best-effort Mongo collection wiring. The recorder is fail-
        # soft if Mongo is offline.
        coll = None
        try:
            from ..infrastructure.storage import storage_manager  # noqa: PLC0415
            db = getattr(storage_manager, "mongo_db", None)
            if db is not None:
                coll = db["episodic_memory"]
        except Exception:
            pass
        self._episodic_store = EpisodicMemoryStore(
            collection=coll,
            embedder=hash_embedder(),     # placeholder until v1B+ wires nomic
            reuse_threshold=float(
                getattr(settings, "episodic_reuse_threshold", 0.85)
            ),
            seed_threshold=float(
                getattr(settings, "episodic_seed_threshold", 0.60)
            ),
        )
        return self._episodic_store

    def _get_rlef_collector(self) -> Any:
        if getattr(self, "_rlef_collector", None) is not None:
            return self._rlef_collector
        from local_ai.rlef_collector import RLEFCollector  # noqa: PLC0415
        from ..config.settings import settings  # noqa: PLC0415
        coll = None
        try:
            from ..infrastructure.storage import storage_manager  # noqa: PLC0415
            db = getattr(storage_manager, "mongo_db", None)
            if db is not None:
                coll = db[
                    getattr(settings, "rlef_mongo_collection", "rlef_rewards")
                ]
        except Exception:
            pass
        self._rlef_collector = RLEFCollector(
            mongo_collection=coll,
            kafka_producer=None,  # wired in a follow-up round
            kafka_topic=getattr(
                settings, "rlef_kafka_topic", "task.rlef_reward",
            ),
            fast_threshold_ms=float(
                getattr(settings, "rlef_fast_threshold_ms", 5000.0),
            ),
        )
        return self._rlef_collector

    def _compute_test_pass_rate(self) -> float:
        """Derive the pass rate from existing bundle signals.

        Order of preference:
          1. Reactor's property-test outcomes (Phase 1A invariants run
             against the actual generated code) — most accurate, but
             only when the harness ACTUALLY ran. We exclude outcomes
             that failed because the sandbox didn't emit PROPERTY_RESULT
             lines (those mean "harness didn't run", not "invariant
             violated").
          2. Verification execution + static analysis — broad signal.
          3. Default 0.0 when nothing usable.
        """
        rb = self.bundle.reactor_bundle or {}
        prop = rb.get("property_tests") or {}
        outcomes = prop.get("outcomes") or []
        # Filter out "harness never emitted" outcomes — those are
        # noise from a sandbox that couldn't run the script, not
        # signal about the code itself.
        runnable = [
            o for o in outcomes
            if not (o.get("error") or "").startswith("no PROPERTY_RESULT")
        ]
        if runnable:
            passed = sum(1 for o in runnable if o.get("passed"))
            return round(passed / max(1, len(runnable)), 4)
        v = self.bundle.verification
        if v is None:
            return 0.0
        exec_data = v.execution or {}
        if exec_data and exec_data.get("skipped"):
            return 0.5  # neutral — execution didn't run
        if exec_data and exec_data.get("success"):
            return 1.0
        return 0.0

    def _code_hash(self) -> str:
        import hashlib  # noqa: PLC0415
        h = hashlib.sha256((self.bundle.code or "").encode("utf-8", "replace"))
        return h.hexdigest()[:16]

    def _derive_tags(self) -> list[str]:
        """Tags help the future learner cluster similar episodes."""
        tags: list[str] = []
        triage = self.bundle.triage or {}
        if triage.get("language"):
            tags.append(f"lang:{triage['language']}")
        if triage.get("task_type"):
            tags.append(f"task:{triage['task_type']}")
        skel = self.bundle.logic_skeleton or {}
        if skel.get("matched_template"):
            tags.append(f"template:{skel['matched_template']}")
        return tags

    # ─── V2 helpers ─────────────────────────────────────────────────
    #
    # The Quick Code V2 phases are implemented as a thin layer on top
    # of the existing engine. Every method below is a no-op when the
    # ``quick_v2_enabled`` master flag is False or the matching
    # per-feature flag is False, and every method is fail-soft: any
    # internal exception is swallowed (logged at DEBUG) and the
    # phase reports nothing on the bundle. The end goal is byte-
    # identical behaviour with V2 disabled.

    def _v2_enabled(self) -> bool:
        try:
            from ..config.settings import settings  # noqa: PLC0415
            return bool(getattr(settings, "quick_v2_enabled", False))
        except Exception:
            return False

    def _v2_setting(self, key: str, default: Any) -> Any:
        try:
            from ..config.settings import settings  # noqa: PLC0415
            return getattr(settings, key, default)
        except Exception:
            return default

    def _v2_request_mode(self) -> str:
        return (getattr(self.request, "mode", "quick") or "quick").lower()

    def _v2_complexity_hint(self) -> str | None:
        return getattr(self.request, "complexity_hint", None)

    async def _v2_emit(self, event: str, payload: dict[str, Any]) -> None:
        await self._emit({"type": event, **payload})

    # Lazy singletons.  Each module is imported on first use so the
    # engine stays light when V2 is disabled.

    def _get_router(self) -> Any:
        if getattr(self, "_v2_router_cached", None) is not None:
            return self._v2_router_cached
        from .router import TaskClassifier  # noqa: PLC0415
        self._v2_router_cached = TaskClassifier(
            llm_call=self._llm_call,
            model=self._v2_setting("quick_v2_router_model", "qwen2.5:1.5b"),
            redirect_to_pro=bool(
                self._v2_setting("quick_v2_router_redirect_to_pro", True)
            ),
        )
        return self._v2_router_cached

    def _get_striatum(self) -> Any:
        if getattr(self, "_v2_striatum_cached", None) is not None:
            return self._v2_striatum_cached
        from .striatum import Striatum  # noqa: PLC0415
        cache = None
        try:
            from ..infrastructure.cache import cache_manager  # noqa: PLC0415
            cache = cache_manager
        except Exception:
            cache = None
        self._v2_striatum_cached = Striatum(
            cache=cache,
            threshold=float(self._v2_setting("quick_v2_striatum_threshold", 0.95)),
            ttl_s=int(self._v2_setting("quick_v2_striatum_ttl_s", 86_400)),
            salt=int(self._v2_setting("quick_v2_striatum_salt", 1)),
        )
        return self._v2_striatum_cached

    def _get_parsel(self) -> Any:
        if getattr(self, "_v2_parsel_cached", None) is not None:
            return self._v2_parsel_cached
        from .parsel import ParselDecomposer  # noqa: PLC0415
        self._v2_parsel_cached = ParselDecomposer(
            llm_call=self._llm_call,
            max_depth=int(self._v2_setting("quick_v2_parsel_max_depth", 2)),
        )
        return self._v2_parsel_cached

    def _get_sk_coder(self) -> Any:
        if getattr(self, "_v2_sk_cached", None) is not None:
            return self._v2_sk_cached
        from .sk_coder import SkCoder  # noqa: PLC0415
        # Empty corpus by default.  A future round can wire the real
        # CodeCorpusRAG corpus in here; the floor + hint behaviour
        # still works as documented.
        self._v2_sk_cached = SkCoder(
            corpus=[],
            alpha_floor=float(self._v2_setting("quick_v2_sk_alpha_floor", 0.35)),
            top_k=int(self._v2_setting("quick_v2_sk_top_k", 5)),
        )
        return self._v2_sk_cached

    def _get_symcode(self) -> Any:
        if getattr(self, "_v2_symcode_cached", None) is not None:
            return self._v2_symcode_cached
        from .symcode import SymCode  # noqa: PLC0415
        self._v2_symcode_cached = SymCode(
            timeout_s=int(self._v2_setting("quick_v2_symcode_timeout_s", 10)),
        )
        return self._v2_symcode_cached

    def _get_mcts(self) -> Any:
        if getattr(self, "_v2_mcts_cached", None) is not None:
            return self._v2_mcts_cached
        from .mcts import MCTSRunner  # noqa: PLC0415
        self._v2_mcts_cached = MCTSRunner(
            c=float(self._v2_setting("quick_v2_mcts_c", 1.41)),
            max_iters=int(self._v2_setting("quick_v2_mcts_max_iters", 16)),
        )
        return self._v2_mcts_cached

    def _get_orpo_exporter(self) -> Any:
        if getattr(self, "_v2_orpo_cached", None) is not None:
            return self._v2_orpo_cached
        from .preferences import ORPOExporter  # noqa: PLC0415
        mongo_db = None
        try:
            from ..infrastructure.storage import storage_manager  # noqa: PLC0415
            mongo_db = getattr(storage_manager, "mongo_db", None)
        except Exception:
            mongo_db = None
        self._v2_orpo_cached = ORPOExporter(
            enabled=bool(self._v2_setting("quick_v2_orpo_enabled", False)),
            mongo_collection=str(
                self._v2_setting("quick_v2_orpo_collection", "orpo_pairs")
            ),
            mongo_db=mongo_db,
        )
        return self._v2_orpo_cached

    # ─── V2 phases ──────────────────────────────────────────────────

    async def _phase_v2_classify(self) -> bool:
        """Run the router.  Returns True when the engine should
        short-circuit ``run()`` with the redirect-to-pro sentinel."""
        if not self._v2_enabled():
            return False
        if not bool(self._v2_setting("quick_v2_router_enabled", True)):
            return False
        try:
            from .contracts import TaskComplexity  # noqa: PLC0415
            router = self._get_router()
        except Exception as exc:
            logger.debug("quick_code_v2_router_import_failed: %s", exc)
            return False
        await self._v2_emit("quick_code_phase_start", {"phase": "classify"})
        try:
            hint = self._v2_complexity_hint()
            if hint:
                verdict = TaskComplexity.coerce(hint) or await router.classify(
                    self.request.prompt, self.request.language
                )
            else:
                verdict = await router.classify(
                    self.request.prompt, self.request.language
                )
            redirect = await router.should_redirect_to_pro(
                verdict, self._v2_request_mode()
            )
        except Exception as exc:
            logger.debug("quick_code_v2_classify_failed: %s", exc)
            await self._v2_emit("quick_code_phase_complete", {
                "phase": "classify", "skipped": True,
            })
            return False
        decision = {
            "complexity": verdict.value,
            "redirect_to_pro": bool(redirect),
            "from_mode": self._v2_request_mode(),
            "hint": hint,
        }
        if redirect:
            decision["target"] = "/api/code/start"
        self.bundle.router_decision = decision
        await self._v2_emit("quick_code_phase_complete", {
            "phase": "classify",
            "decision": decision,
        })
        return bool(redirect)

    async def _phase_v2_striatum_lookup(self) -> bool:
        """Cosine fast-path.  Returns True when a hit back-fills the
        bundle and the rest of the pipeline should be skipped."""
        if not self._v2_enabled():
            return False
        if not bool(self._v2_setting("quick_v2_striatum_enabled", True)):
            return False
        try:
            striatum = self._get_striatum()
        except Exception as exc:
            logger.debug("quick_code_v2_striatum_import_failed: %s", exc)
            return False
        await self._v2_emit("quick_code_phase_start", {"phase": "striatum"})
        try:
            cached = await striatum.lookup(self.request.prompt)
        except Exception as exc:
            logger.debug("quick_code_v2_striatum_lookup_failed: %s", exc)
            cached = None
        if not cached:
            await self._v2_emit("quick_code_phase_complete", {
                "phase": "striatum", "hit": False,
            })
            return False
        # Back-fill bundle from cached entry.  We keep the existing
        # session_id + started_at so the SSE feed still ties to *this*
        # request, but copy the working artefacts.
        meta = cached.get("striatum_meta") or {}
        for key in ("triage", "code", "tests", "deliverable_markdown"):
            value = cached.get(key)
            if value is not None:
                setattr(self.bundle, key, value)
        if cached.get("verification") is not None:
            from .models import QuickCodeVerification  # noqa: PLC0415
            v = cached["verification"]
            self.bundle.verification = QuickCodeVerification(
                execution=v.get("execution"),
                static=v.get("static"),
                score=float(v.get("score") or 0.0),
                severities=dict(v.get("severities") or {}),
            )
        self.bundle.striatum_hit = {
            "score": float(meta.get("score") or 0.0),
            "threshold": float(meta.get("threshold") or 0.0),
            "stored_at": meta.get("stored_at"),
        }
        await self._v2_emit("quick_code_phase_complete", {
            "phase": "striatum",
            "hit": True,
            "score": self.bundle.striatum_hit["score"],
        })
        return True

    async def _phase_v2_parsel_decompose(self) -> dict[str, Any] | None:
        if not self._v2_enabled():
            return None
        if not bool(self._v2_setting("quick_v2_parsel_enabled", True)):
            return None
        try:
            from .contracts import TaskIR  # noqa: PLC0415
            parsel = self._get_parsel()
        except Exception as exc:
            logger.debug("quick_code_v2_parsel_import_failed: %s", exc)
            return None
        await self._v2_emit("quick_code_phase_start", {"phase": "parsel_decompose"})
        try:
            ir = TaskIR.from_quick_code_request(
                self.request,
                ir_id=self.session_id,
                triage=self.bundle.triage,
            )
            decomposed = await parsel.decompose(ir)
        except Exception as exc:
            logger.debug("quick_code_v2_parsel_failed: %s", exc)
            await self._v2_emit("quick_code_phase_complete", {
                "phase": "parsel_decompose", "skipped": True,
            })
            return None
        dumps = [st.model_dump() for st in decomposed.subtasks]
        self.bundle.parsel_subtasks = dumps
        await self._v2_emit("quick_code_phase_complete", {
            "phase": "parsel_decompose",
            "count": len(dumps),
        })
        return {"subtasks": dumps}

    async def _phase_v2_sk_retrieve(self) -> dict[str, Any] | None:
        if not self._v2_enabled():
            return None
        if not bool(self._v2_setting("quick_v2_sk_enabled", True)):
            return None
        try:
            from .contracts import TaskIR  # noqa: PLC0415
            sk = self._get_sk_coder()
        except Exception as exc:
            logger.debug("quick_code_v2_sk_import_failed: %s", exc)
            return None
        if len(sk) == 0:
            # Empty corpus → nothing to retrieve.  Skip silently so we
            # don't litter the SSE stream with no-op events.
            return None
        await self._v2_emit("quick_code_phase_start", {"phase": "sk_retrieve"})
        try:
            ir = TaskIR.from_quick_code_request(
                self.request,
                ir_id=self.session_id,
                triage=self.bundle.triage,
            )
            snippets, hint = await sk.retrieve_or_hint(ir)
        except Exception as exc:
            logger.debug("quick_code_v2_sk_failed: %s", exc)
            await self._v2_emit("quick_code_phase_complete", {
                "phase": "sk_retrieve", "skipped": True,
            })
            return None
        self.bundle.sk_snippets = [s.model_dump() for s in snippets]
        self.bundle.sk_hint = hint
        await self._v2_emit("quick_code_phase_complete", {
            "phase": "sk_retrieve",
            "count": len(snippets),
            "hint": hint,
        })
        return {"snippets": self.bundle.sk_snippets, "hint": hint}

    async def _phase_v2_symcode_validate(self) -> dict[str, Any] | None:
        if not self._v2_enabled():
            return None
        if not bool(self._v2_setting("quick_v2_symcode_enabled", True)):
            return None
        triage = self.bundle.triage or {}
        # Run only on math-flavoured tasks; the router's MATH bucket
        # AND the triage's task_type both opt-in.
        is_math = (
            (self.bundle.router_decision or {}).get("complexity") == "math"
            or "math" in str(triage.get("task_type") or "").lower()
        )
        if not is_math or not (self.bundle.code or "").strip():
            return None
        try:
            symcode = self._get_symcode()
        except Exception as exc:
            logger.debug("quick_code_v2_symcode_import_failed: %s", exc)
            return None
        await self._v2_emit("quick_code_phase_start", {"phase": "symcode_validate"})
        try:
            result = await symcode.validate(self.bundle.code or "")
        except Exception as exc:
            logger.debug("quick_code_v2_symcode_failed: %s", exc)
            await self._v2_emit("quick_code_phase_complete", {
                "phase": "symcode_validate", "skipped": True,
            })
            return None
        dump = result.model_dump()
        self.bundle.symcode_result = dump
        await self._v2_emit("quick_code_phase_complete", {
            "phase": "symcode_validate",
            "ok": bool(dump.get("ok")),
            "equivalence_class": dump.get("equivalence_class"),
        })
        return dump

    async def _phase_v2_mcts_select(self) -> dict[str, Any] | None:
        if not self._v2_enabled():
            return None
        # MCTS is opt-in: pro mode by default, or settings override.
        use_mcts = (
            self._v2_request_mode() == "pro"
            or bool(self._v2_setting("quick_v2_use_mcts", False))
        )
        if not use_mcts:
            return None
        # We need at least 2 alternatives to pick from; otherwise MCTS
        # has nothing to do.
        reasoning = self.bundle.reasoning
        if reasoning is None or len(reasoning.alternatives or []) < 2:
            return None
        try:
            from .contracts import CodeSnippet  # noqa: PLC0415
            mcts = self._get_mcts()
        except Exception as exc:
            logger.debug("quick_code_v2_mcts_import_failed: %s", exc)
            return None
        await self._v2_emit("quick_code_phase_start", {"phase": "mcts_select"})
        # Synthesise candidate "code snippets" from the reasoning
        # alternatives.  Score = composite_score so MCTS converges on
        # the highest-confidence choice.
        snippets = [
            CodeSnippet(
                source=alt.summary or alt.label,
                score=float(alt.composite),
                language=self.request.language or "python",
            )
            for alt in reasoning.alternatives
        ]

        async def scorer(s: CodeSnippet) -> float:
            return float(s.score)

        try:
            _winner, nodes = await mcts.select(snippets, scorer)
        except Exception as exc:
            logger.debug("quick_code_v2_mcts_failed: %s", exc)
            await self._v2_emit("quick_code_phase_complete", {
                "phase": "mcts_select", "skipped": True,
            })
            return None
        audit = [n.model_dump() for n in nodes]
        self.bundle.mcts_audit = audit
        await self._v2_emit("quick_code_phase_complete", {
            "phase": "mcts_select",
            "iterations": sum(n.get("visit_count", 0) for n in audit),
        })
        return {"audit": audit}

    async def _phase_v2_export_orpo(self) -> dict[str, Any] | None:
        if not self._v2_enabled():
            return None
        if not bool(self._v2_setting("quick_v2_orpo_enabled", False)):
            return None
        # We can only build a useful preference pair when the engine
        # actually refined a failing candidate into a passing one.
        # Approximate that: refine_iterations > 0 AND verification ok.
        if self.bundle.refine_iterations <= 0:
            return None
        if not (self.bundle.code or "").strip():
            return None
        # Without a "rejected" baseline we cannot build a pair.  In
        # this V1 we skip when no baseline is available; a future
        # round can stash the pre-refine code for richer pairs.
        rejected = (self.bundle.triage or {}).get("baseline_rejected") or ""
        if not rejected:
            return None
        try:
            exporter = self._get_orpo_exporter()
        except Exception as exc:
            logger.debug("quick_code_v2_orpo_import_failed: %s", exc)
            return None
        await self._v2_emit("quick_code_phase_start", {"phase": "orpo_export"})
        try:
            pairs = await exporter.export_from_bundle(
                self.bundle.to_dict(),
                prompt=self.request.prompt,
                rejected=rejected,
                chosen=self.bundle.code or "",
            )
        except Exception as exc:
            logger.debug("quick_code_v2_orpo_failed: %s", exc)
            await self._v2_emit("quick_code_phase_complete", {
                "phase": "orpo_export", "skipped": True,
            })
            return None
        dumps = [p.model_dump() for p in pairs]
        self.bundle.orpo_pairs = dumps
        await self._v2_emit("quick_code_phase_complete", {
            "phase": "orpo_export", "count": len(dumps),
        })
        return {"pairs": dumps}

    async def _phase_v2_striatum_store(self) -> None:
        if not self._v2_enabled():
            return
        if not bool(self._v2_setting("quick_v2_striatum_enabled", True)):
            return
        if not (self.bundle.code or "").strip():
            return
        # Only persist runs whose verification passed (or wasn't
        # available) — never cache a known-bad run.
        v = self.bundle.verification
        if v is not None and v.score < 50.0:
            return
        try:
            striatum = self._get_striatum()
            await striatum.store(self.request.prompt, self.bundle.to_dict())
        except Exception as exc:
            logger.debug("quick_code_v2_striatum_store_failed: %s", exc)

    # ─── run() ─────────────────────────────────────────────────────

    async def run(self) -> QuickCodeBundle:
        await self._emit({
            "type": "quick_code_started",
            "request": self.request.to_dict(),
        })
        try:
            # V2 — classify the task and (when configured) flag it for
            # redirect to the Pro engine.  Short-circuits the rest of
            # ``run()`` only on a redirect; otherwise it just records
            # the verdict on the bundle for downstream phases.
            if await self._phase_v2_classify():
                self.bundle.deliverable_markdown = (
                    self._build_deliverable_markdown()
                )
                self.bundle.completed_at = _now_iso()
                await self._emit({
                    "type": "quick_code_completed",
                    "session_id": self.session_id,
                    "redirect_to_pro": True,
                    "router_decision": self.bundle.router_decision,
                })
                return self.bundle
            self._check_cancel()
            # V2 — Striatum cosine fast-path.  When a hit lands the
            # bundle is back-filled from the cached entry and the rest
            # of the pipeline is skipped.
            if await self._phase_v2_striatum_lookup():
                self.bundle.deliverable_markdown = (
                    self.bundle.deliverable_markdown
                    or self._build_deliverable_markdown()
                )
                self.bundle.completed_at = _now_iso()
                await self._emit({
                    "type": "quick_code_completed",
                    "session_id": self.session_id,
                    "striatum_hit": True,
                    "code_chars": len(self.bundle.code or ""),
                })
                return self.bundle
            self._check_cancel()
            # Phase 1B — informational episodic recall (no short-circuit yet).
            await self._phase_episodic_recall()
            self._check_cancel()
            await self._phase_triage()
            self._check_cancel()
            # Phase 1B — neuro-symbolic skeleton + Z3 sanity gate. The
            # skeleton's complexity_hint feeds the Reactor's claimed-
            # Big-O check; the Z3 verdict rides the bundle for the UI.
            await self._phase_logic_skeleton()
            self._check_cancel()
            # V2 — divide-and-conquer + Design-by-Contract decomposition.
            await self._phase_v2_parsel_decompose()
            self._check_cancel()
            await self._phase_reason()
            self._check_cancel()
            # V2 — BM25 + cosine retrieval; populates bundle.sk_snippets
            # so the implement phase can ride along on proven patterns.
            await self._phase_v2_sk_retrieve()
            self._check_cancel()
            await self._phase_implement()
            self._check_cancel()
            # V2 — SymPy equivalence check for math tasks.  Fail-soft;
            # adds a sanity gate without blocking non-math runs.
            await self._phase_v2_symcode_validate()
            self._check_cancel()
            await self._phase_verify()
            self._check_cancel()
            # V2 — UCT picker over the candidate set.  Pro mode only.
            await self._phase_v2_mcts_select()
            self._check_cancel()
            if self.request.max_refine > 0 and self.request.allow_refine:
                await self._phase_refine_if_needed()
            # v10 — Code Synthesis Reactor empirical verification.
            # Runs symbolic complexity + bench + property tests against
            # the (possibly refined) winner. Fail-soft: returns None if
            # disabled or any subsystem errors.
            self._check_cancel()
            await self._phase_reactor_verify()
            # v9 — Multi-ML Mesh post-processing. Each phase is fail-soft
            # (returns None on any error) so the bundle still ships even
            # if the mesh is misconfigured.
            self._check_cancel()
            await self._phase_code_audit()
            self._check_cancel()
            await self._phase_meta_arbiter()
            # Phase 1B — persist + emit reward. Both happen AFTER
            # everything else so they see the final state of the bundle.
            self._check_cancel()
            await self._phase_persist_episode()
            self._check_cancel()
            await self._phase_emit_rlef()
            # V2 — preference-pair export + Striatum store.  Run last
            # so they see the final, post-refine bundle state.
            self._check_cancel()
            await self._phase_v2_export_orpo()
            self._check_cancel()
            await self._phase_v2_striatum_store()
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
