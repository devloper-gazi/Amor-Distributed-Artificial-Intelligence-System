"""
MultiMLMesh — the orchestrator that wires specialists, code auditors,
and the meta-arbiter into a single API the engine layer calls.

Public surface (only three methods needed by the engines):

    mesh = MultiMLMesh(llm_call=..., on_event=..., config=MeshConfig())

    # Phase 2 (Reason): fan out reasoning to N specialists in parallel,
    # aggregate into a single ranked alternatives list.
    aggregated = await mesh.run_reasoning(
        user_prompt=..., code_context=..., triage=...,
    )

    # Phase 4.5 (Code Review): once code+tests exist, fan out N auditors
    # in parallel, return their structured verdicts.
    audit = await mesh.run_code_audit(
        user_prompt=..., code=..., tests=..., language=...,
    )

    # Final phase (Arbiter): synthesise everything into a calibrated
    # production-readiness verdict.
    verdict = await mesh.run_meta_arbiter(
        user_prompt=..., chosen_summary=..., chosen_rationale=...,
        code=..., tests=..., execution_summary=..., static_summary=...,
        mesh_audit=audit, refine_iterations=...,
    )

The mesh emits per-step events via ``on_event`` so the SSE stream can
show "math specialist returned 3 alternatives" / "performance auditor
flagged a hotspot" in real time. Each event carries a stable
``event_id`` for SSE dedup.

Cancellation: pass ``cancel_check=lambda: bool`` so a running mesh
short-circuits before the next phase if the parent session was cancelled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .aggregator import AggregatedReasoning, MeshAggregator
from .code_auditors import (
    EdgeCaseCodeAuditor,
    MathCodeAuditor,
    MeshCodeAudit,
    PerformanceCodeAuditor,
    _BaseAuditor,
    run_auditors_parallel,
)
from .meta_arbiter import MetaArbiterAgent, MetaVerdict
from .specialists import (
    EdgeCaseReasonerAgent,
    GeneralReasonerAgent,
    MathReasonerAgent,
    PerformanceReasonerAgent,
    SpecialistRoleId,
    _BaseSpecialist,
    run_specialists_parallel,
)

logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
RoleSetter = Callable[[str | None], Any]
CancelCheck = Callable[[], bool]


# Default specialist roster — keep it modest so even single-model
# deployments (e.g. the test stand) can run the full mesh without
# blowing the wall-clock budget on CPU inference.
DEFAULT_REASONING_ROLES: list[SpecialistRoleId] = [
    "general", "math", "performance", "edge_case",
]
DEFAULT_AUDITOR_ROLES: list[SpecialistRoleId] = [
    "math", "performance", "edge_case",
]


@dataclass
class MeshConfig:
    """Per-engine knobs for which specialists run."""

    reasoning_roles: list[SpecialistRoleId] = field(
        default_factory=lambda: list(DEFAULT_REASONING_ROLES),
    )
    auditor_roles: list[SpecialistRoleId] = field(
        default_factory=lambda: list(DEFAULT_AUDITOR_ROLES),
    )
    enable_meta_arbiter: bool = True
    # Per-role timeout. CPU inference of a 7B model takes 60-120s; the
    # 240s default lets the slowest specialist finish even on slow
    # hosts. Override down for tests.
    specialist_timeout_s: float = 240.0
    auditor_timeout_s: float = 240.0
    # Per-call max tokens — keeps memory pressure bounded on small
    # GPUs where multiple specialists may sit in the same model.
    reason_max_tokens: int = 1500
    audit_max_tokens: int = 1200
    arbiter_max_tokens: int = 1500


@dataclass
class MeshOutput:
    """Bundle of everything the mesh produced for a session.

    The QuickCode engine + Code Intelligence engine both consume this
    same envelope; their adapters convert into their own bundle types.
    """

    aggregated_reasoning: AggregatedReasoning | None = None
    code_audit: MeshCodeAudit | None = None
    meta_verdict: MetaVerdict | None = None
    role_models: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "aggregated_reasoning": (
                self.aggregated_reasoning.to_dict()
                if self.aggregated_reasoning else None
            ),
            "code_audit": (
                self.code_audit.to_dict() if self.code_audit else None
            ),
            "meta_verdict": (
                self.meta_verdict.to_dict() if self.meta_verdict else None
            ),
            "role_models": dict(self.role_models),
        }


# Factory — picks the right concrete agent class for a role id.

def _build_specialist(
    role: SpecialistRoleId, llm_call: LLMCall,
    role_setter: RoleSetter | None,
    max_tokens: int,
) -> _BaseSpecialist:
    cls = {
        "general":     GeneralReasonerAgent,
        "math":        MathReasonerAgent,
        "performance": PerformanceReasonerAgent,
        "edge_case":   EdgeCaseReasonerAgent,
    }[role]
    return cls(llm_call, role_setter=role_setter, max_tokens=max_tokens)


def _build_auditor(
    role: SpecialistRoleId, llm_call: LLMCall,
    role_setter: RoleSetter | None,
    max_tokens: int,
) -> _BaseAuditor:
    cls = {
        "math":        MathCodeAuditor,
        "performance": PerformanceCodeAuditor,
        "edge_case":   EdgeCaseCodeAuditor,
    }[role]
    return cls(llm_call, role_setter=role_setter, max_tokens=max_tokens)


# ─── The orchestrator ───────────────────────────────────────────────


class MultiMLMesh:
    """One instance per code-generation session."""

    def __init__(
        self,
        *,
        llm_call: LLMCall,
        on_event: EventCallback | None = None,
        role_setter: RoleSetter | None = None,
        cancel_check: CancelCheck | None = None,
        config: MeshConfig | None = None,
        session_id: str | None = None,
    ) -> None:
        self._llm = llm_call
        self._on_event = on_event
        self._role_setter = role_setter
        self._cancel_check = cancel_check
        self.config = config or MeshConfig()
        self.session_id = session_id or uuid4().hex
        # Surface which model each role landed on. Populated lazily
        # from `_ACTIVE_ROLE` if the engine bothered to read it. Mostly
        # used by the metrics recorder.
        self.role_models: dict[str, str] = {}

    # ─── helpers ────────────────────────────────────────────────

    async def _emit(self, event: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            stamped = {**event, "event_id": event.get("event_id") or uuid4().hex}
            await self._on_event(stamped)
        except Exception:  # pragma: no cover
            logger.exception("mesh.on_event raised")

    def _check_cancel(self) -> bool:
        if self._cancel_check is None:
            return False
        try:
            return bool(self._cancel_check())
        except Exception:  # pragma: no cover
            return False

    # ─── public phase APIs ──────────────────────────────────────

    async def run_reasoning(
        self,
        *,
        user_prompt: str,
        code_context: str | None = None,
        triage: dict | None = None,
    ) -> AggregatedReasoning:
        """Phase: parallel reasoning across N specialists, then merge."""
        if self._check_cancel():
            return AggregatedReasoning(
                reasoning=_empty_reasoning("cancelled before reasoning"),
                findings=["cancelled"],
            )

        await self._emit({
            "type": "mesh_reasoning_start",
            "session_id": self.session_id,
            "roles": list(self.config.reasoning_roles),
        })

        specialists = [
            _build_specialist(role, self._llm, self._role_setter,
                              self.config.reason_max_tokens)
            for role in self.config.reasoning_roles
        ]
        outputs = await run_specialists_parallel(
            specialists,
            user_prompt=user_prompt,
            code_context=code_context,
            triage=triage,
            timeout_s=self.config.specialist_timeout_s,
        )

        # Per-specialist event so the UI can light up each role as it
        # finishes (currently we emit one per specialist *after* the
        # gather; partial-progress streaming is a follow-up).
        for o in outputs:
            await self._emit({
                "type": "mesh_specialist_complete",
                "session_id": self.session_id,
                "role": o.role,
                "role_label": o.role_label,
                "alt_count": len(o.alternatives()),
                "had_error": bool(o.error),
                "error": o.error,
            })

        aggregator = MeshAggregator()
        aggregated = aggregator.merge(outputs)

        await self._emit({
            "type": "mesh_reasoning_complete",
            "session_id": self.session_id,
            "consensus_count": aggregated.consensus_count,
            "alt_count": len(aggregated.reasoning.alternatives),
            "chosen_label": aggregated.reasoning.chosen_label,
            "per_specialist_picks": dict(aggregated.per_specialist_picks),
        })
        return aggregated

    async def run_code_audit(
        self,
        *,
        user_prompt: str,
        code: str,
        tests: str | None = None,
        language: str = "python",
    ) -> MeshCodeAudit:
        """Phase: parallel auditors review the GENERATED code."""
        if self._check_cancel():
            return MeshCodeAudit(findings=["cancelled before audit"])
        if not (code or "").strip():
            return MeshCodeAudit(findings=["no code to audit"])

        await self._emit({
            "type": "mesh_audit_start",
            "session_id": self.session_id,
            "roles": list(self.config.auditor_roles),
        })

        auditors = [
            _build_auditor(role, self._llm, self._role_setter,
                           self.config.audit_max_tokens)
            for role in self.config.auditor_roles
        ]
        audit = await run_auditors_parallel(
            auditors,
            user_prompt=user_prompt,
            code=code,
            tests=tests,
            language=language,
            timeout_s=self.config.auditor_timeout_s,
        )

        for a in audit.auditors:
            await self._emit({
                "type": "mesh_auditor_complete",
                "session_id": self.session_id,
                "role": a.role,
                "role_label": a.role_label,
                "verdict": a.verdict,
                "confidence": a.confidence,
                "had_error": bool(a.error),
                "summary": a.summary,
            })

        await self._emit({
            "type": "mesh_audit_complete",
            "session_id": self.session_id,
            "any_rejected": audit.any_rejected,
            "any_changes_requested": audit.any_changes_requested,
            "average_confidence": audit.average_confidence,
        })
        return audit

    async def run_meta_arbiter(
        self,
        *,
        user_prompt: str,
        chosen_summary: str,
        chosen_rationale: str,
        code: str,
        tests: str | None,
        execution_summary: str,
        static_summary: str,
        mesh_audit: MeshCodeAudit | None,
        refine_iterations: int,
    ) -> MetaVerdict:
        """Phase: single high-tier call that synthesises everything."""
        if not self.config.enable_meta_arbiter:
            return MetaVerdict(
                summary="meta-arbiter disabled by config",
            )
        if self._check_cancel():
            return MetaVerdict(summary="cancelled before arbiter")

        await self._emit({
            "type": "mesh_arbiter_start",
            "session_id": self.session_id,
        })
        arbiter = MetaArbiterAgent(
            self._llm,
            role_setter=self._role_setter,
            max_tokens=self.config.arbiter_max_tokens,
        )
        verdict = await arbiter.arbitrate(
            user_prompt=user_prompt,
            chosen_summary=chosen_summary,
            chosen_rationale=chosen_rationale,
            code=code, tests=tests,
            execution_summary=execution_summary,
            static_summary=static_summary,
            mesh_audit=mesh_audit,
            refine_iterations=refine_iterations,
        )
        await self._emit({
            "type": "mesh_arbiter_complete",
            "session_id": self.session_id,
            "verdict": verdict.verdict,
            "confidence": verdict.confidence,
            "production_readiness": verdict.production_readiness,
            "had_error": bool(verdict.error),
        })
        return verdict


# ─── helpers ────────────────────────────────────────────────────────


def _empty_reasoning(note: str) -> Any:
    """Build a minimal reasoning fallback used when the mesh skips
    its reasoning phase (cancellation, etc.)."""
    from ...quick_code.models import (  # noqa: PLC0415
        COMPOSITE_WEIGHTS, QuickCodeAlternative, QuickCodeReasoning,
    )
    return QuickCodeReasoning(
        alternatives=[QuickCodeAlternative(
            label="A",
            summary=f"({note} — degraded to single-path)",
            scores={k: 0.5 for k in COMPOSITE_WEIGHTS},
        )],
        chosen_label="A",
        rationale=note,
        findings=[note],
    )
