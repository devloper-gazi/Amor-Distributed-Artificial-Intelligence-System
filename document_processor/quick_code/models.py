"""
QuickCode Mode — typed dataclasses for the 5-phase reasoning-first
pipeline.

The pipeline:

    Triage   → run_triage()              (reused from code_intelligence)
    Reason   → 2-3 alternatives + score  (composite_score weighted picker)
    Implement→ CoderAgent + TesterAgent  (reused)
    Verify   → ExecutionSandbox + StaticAnalysisHarness (deterministic)
    Refine   → DebuggerAgent (capped at max_refine, default 2, max 3)

The dataclasses below carry every artifact end-to-end and serialize
cleanly into JSON for the SSE feed, the artifact bundle, and the
Mongo-backed durable session store.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


# ─────────────────────────────────────────────────────────────────────
# Request — what the API route / CLI / Consortium hands the engine
# ─────────────────────────────────────────────────────────────────────


# Same effort tiers as the rest of AMOR so per-phase budgets stay
# consistent across modes.
EffortTier = Literal["basic", "medium", "deep", "expert", "ultra"]


# Hard cap — the user can ask for more in CLI args but we clamp here.
MAX_REFINE_ITERATIONS = 3


# Composite-score weights for ranking reasoning alternatives. Pinned
# in the dataclass so tests can import the exact constants and the
# engine never has them out of sync with the formula.
COMPOSITE_WEIGHTS: dict[str, float] = {
    "clarity":        0.30,
    "math_soundness": 0.30,
    "performance":    0.20,
    "edge_cases":     0.20,
}


@dataclass
class QuickCodeRequest:
    """Input contract for ``QuickCodeEngine.run()``.

    `prompt` is the only required field. Everything else has a sane
    default; the API route / CLI / Consortium will fill them in based
    on the call site.
    """

    prompt: str
    language: str | None = None
    effort: EffortTier = "medium"
    code_context: str | None = None
    allow_refine: bool = True
    max_refine: int = 2
    role_overrides: dict[str, str] = field(default_factory=dict)
    cancel_requested: bool = False

    def normalize(self) -> "QuickCodeRequest":
        """Clamp `max_refine` to [0, MAX_REFINE_ITERATIONS] and apply
        the `allow_refine=False` short-circuit."""
        n = max(0, min(MAX_REFINE_ITERATIONS, int(self.max_refine or 0)))
        if not self.allow_refine:
            n = 0
        # dataclass replace is too heavy for one field; mutate in place.
        self.max_refine = n
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# Reasoning phase output
# ─────────────────────────────────────────────────────────────────────


@dataclass
class QuickCodeAlternative:
    """One proposed approach with structured scoring."""

    label: str
    summary: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    complexity_estimate: str = ""
    perf_notes: str = ""
    edge_cases: list[str] = field(default_factory=list)

    @classmethod
    def composite_score(cls, scores: dict[str, float] | None) -> float:
        """Compute the weighted composite score (0..1) for a scores dict.

        Pinned formula:
            0.30 * clarity + 0.30 * math_soundness
          + 0.20 * performance + 0.20 * edge_cases

        Missing axes are treated as 0.0 so a half-filled response is
        worse than a fully scored one.
        """
        if not scores:
            return 0.0
        total = 0.0
        for axis, weight in COMPOSITE_WEIGHTS.items():
            try:
                value = float(scores.get(axis, 0.0) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            total += weight * max(0.0, min(1.0, value))
        return round(total, 4)

    @property
    def composite(self) -> float:
        return self.composite_score(self.scores)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["composite_score"] = self.composite
        return d


@dataclass
class QuickCodeReasoning:
    """Output of the Reason phase."""

    alternatives: list[QuickCodeAlternative] = field(default_factory=list)
    chosen_label: str = ""
    rationale: str = ""
    raw_llm: str = ""
    findings: list[str] = field(default_factory=list)

    @property
    def chosen(self) -> QuickCodeAlternative | None:
        for a in self.alternatives:
            if a.label == self.chosen_label:
                return a
        return self.alternatives[0] if self.alternatives else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "alternatives": [a.to_dict() for a in self.alternatives],
            "chosen_label": self.chosen_label,
            "rationale": self.rationale,
            "findings": list(self.findings),
            # raw_llm intentionally omitted from public dict — kept on
            # the dataclass for debugging but not surfaced to the SSE
            # feed (it can be hundreds of KB).
        }


# ─────────────────────────────────────────────────────────────────────
# Verification phase output
# ─────────────────────────────────────────────────────────────────────


@dataclass
class QuickCodeVerification:
    """Output of the Verify phase. Both fields may be ``None`` when
    the corresponding subsystem was unavailable (Docker missing,
    static analysis tool missing) — the gate validator treats that
    case as ``passed_warn`` rather than ``failed``."""

    execution: dict[str, Any] | None = None
    static: dict[str, Any] | None = None
    score: float = 0.0
    severities: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# Verification gate — same shape as consortium.models.VerificationGate
# but kept here so quick_code can be imported without consortium.
# ─────────────────────────────────────────────────────────────────────


@dataclass
class QuickCodeGate:
    """Per-phase gate result."""

    phase: str
    status: Literal["passed", "passed_warn", "failed"]
    score: float
    findings: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# Final bundle
# ─────────────────────────────────────────────────────────────────────


@dataclass
class QuickCodeBundle:
    """Everything ``QuickCodeEngine.run()`` returns."""

    session_id: str
    request: QuickCodeRequest
    triage: dict[str, Any] = field(default_factory=dict)
    reasoning: QuickCodeReasoning | None = None
    code: str | None = None
    tests: str | None = None
    verification: QuickCodeVerification | None = None
    refine_iterations: int = 0
    gates: list[QuickCodeGate] = field(default_factory=list)
    models_used: dict[str, str] = field(default_factory=dict)
    deliverable_markdown: str = ""
    started_at: str = ""
    completed_at: str = ""

    # ─── Adapter into Consortium's ImplementationArtifact ─────────────

    def to_implementation_artifact(self) -> Any:
        """Adapter so ``ConsortiumOrchestrator._phase_implementation``
        can swap QuickCodeEngine in as a drop-in for the existing
        9-phase Code Intelligence engine without bespoke conversion
        code at the call site.

        Imports lazily to avoid the circular import:
            consortium.models -> quick_code.models -> consortium.models
        """
        from ..consortium.models import ImplementationArtifact  # noqa: PLC0415

        # Synthesize the review payload from the reasoning + verification
        # summaries so downstream gate scoring + bundle writing has the
        # shape it expects (verdict, summary, strengths, weaknesses).
        review: dict[str, Any] = {}
        if self.reasoning and self.reasoning.chosen:
            chosen = self.reasoning.chosen
            review["verdict"] = (
                "approve" if (self.verification and self.verification.score >= 70)
                else "approve_with_changes"
            )
            review["summary"] = (
                f"Chose approach {chosen.label} ({chosen.complexity_estimate}). "
                f"{self.reasoning.rationale[:240]}"
            )
            strengths: list[str] = []
            if chosen.composite >= 0.75:
                strengths.append(f"high composite score ({chosen.composite:.2f})")
            if (chosen.scores or {}).get("math_soundness", 0) >= 0.8:
                strengths.append("strong mathematical/numerical reasoning")
            if (chosen.scores or {}).get("edge_cases", 0) >= 0.8:
                strengths.append("good edge-case coverage")
            review["strengths"] = strengths
            weaknesses: list[dict[str, str]] = []
            if (chosen.scores or {}).get("performance", 0) < 0.5:
                weaknesses.append({
                    "title": "performance unproven",
                    "detail": chosen.perf_notes or "no perf rationale supplied",
                })
            if not strengths:
                weaknesses.append({
                    "title": "low composite score",
                    "detail": f"{chosen.composite:.2f} — review carefully",
                })
            review["weaknesses"] = weaknesses

        return ImplementationArtifact(
            code=self.code,
            tests=self.tests,
            language=self.request.language or "python",
            plan={
                "engine": "quick_code",
                "chosen_label": (
                    self.reasoning.chosen_label if self.reasoning else ""
                ),
                "alternatives_considered": (
                    len(self.reasoning.alternatives) if self.reasoning else 0
                ),
            },
            triage=dict(self.triage),
            static_analysis=(
                dict(self.verification.static)
                if (self.verification and self.verification.static is not None)
                else None
            ),
            execution_results=(
                [dict(self.verification.execution)]
                if (self.verification and self.verification.execution)
                else []
            ),
            review=review,
            deliverable_markdown=self.deliverable_markdown,
            models_used=dict(self.models_used),
            debug_iterations=self.refine_iterations,
        )

    # ─── Serialisation ────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "request": self.request.to_dict(),
            "triage": dict(self.triage),
            "reasoning": self.reasoning.to_dict() if self.reasoning else None,
            "code": self.code,
            "tests": self.tests,
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "refine_iterations": self.refine_iterations,
            "gates": [g.to_dict() for g in self.gates],
            "models_used": dict(self.models_used),
            "deliverable_markdown": self.deliverable_markdown,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
