"""
Consortium Mode — typed dataclasses for the meta-pipeline.

The orchestrator chains four logical phases (Scope → Research → Think →
Implement) and a final Verification gate. Each phase produces a
strongly-shaped artifact that the next phase consumes; the dataclasses
in this module make the shape explicit so any LLM/UI/CLI consumer can
read the same envelope.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


# ─────────────────────────────────────────────────────────────────────
# Scope — the contract derived from the user's project goal
# ─────────────────────────────────────────────────────────────────────


# Effort tiers are unified across all four phases of the consortium so
# the user picks ONE knob ("how advanced should this be?") and the
# orchestrator translates that into per-phase budgets.
EffortTier = Literal["basic", "medium", "deep", "expert", "ultra"]


@dataclass
class ConsortiumScope:
    """Captures *what* the user wants the consortium to build.

    Filled in two passes:
      1. The user submits a free-text ``goal`` plus optional overrides
         (depth tiers, language, deliverable_type).
      2. The Scope phase runs a small LLM triage that:
         - infers a one-line ``title`` + a clean ``summary``
         - extracts ``constraints`` and ``success_criteria`` from the goal
         - picks ``language`` if the user didn't specify
         - settles ``research_depth`` / ``thinking_effort`` /
           ``implementation_effort`` from the global ``depth`` knob
    """

    # User-supplied
    goal: str
    depth: EffortTier = "medium"
    language: str | None = None
    deliverable_type: str = "code_module"
    allow_external_research: bool = True
    cancel_requested: bool = False

    # Triage-derived (filled by Phase 1)
    title: str = ""
    summary: str = ""
    research_query: str = ""
    constraints: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    research_depth: EffortTier = "medium"
    thinking_effort: EffortTier = "medium"
    implementation_effort: EffortTier = "medium"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# Phase artifacts — what each phase emits for the next to consume
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ResearchArtifact:
    """Output of the Research phase. Serialisable for Mongo + the bundle."""

    query: str
    depth: str
    summary_markdown: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    sub_questions: list[dict[str, Any]] = field(default_factory=list)
    citation_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ThinkingArtifact:
    """Output of the Analyze & Think phase."""

    deliverable_markdown: str = ""
    understanding: dict[str, Any] = field(default_factory=dict)
    sub_questions: list[dict[str, Any]] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    decision: dict[str, Any] = field(default_factory=dict)
    critique: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImplementationArtifact:
    """Output of the Code Intelligence phase. Mirrors the engine's snapshot."""

    code: str | None = None
    tests: str | None = None
    language: str = "python"
    plan: dict[str, Any] = field(default_factory=dict)
    triage: dict[str, Any] = field(default_factory=dict)
    static_analysis: dict[str, Any] | None = None
    execution_results: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    deliverable_markdown: str = ""
    models_used: dict[str, str] = field(default_factory=dict)
    debug_iterations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationGate:
    """The post-phase quality gate result.

    `status` is one of:
      * "passed"      — meets the threshold
      * "passed_warn" — meets minimums but flagged warnings
      * "failed"      — below threshold; the orchestrator may retry or downgrade
    """

    phase: str
    status: Literal["passed", "passed_warn", "failed"]
    score: float
    findings: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# Final bundle — everything the consortium produces
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ConsortiumBundle:
    """The complete deliverable. ``readme_markdown`` is the synthesized
    top-level document; the other fields are the structured artifacts
    each phase produced. Saved both to Mongo (for resume) and to a
    filesystem directory (for the artifact-download endpoint + CLI)."""

    session_id: str
    scope: ConsortiumScope
    research: ResearchArtifact | None = None
    thinking: ThinkingArtifact | None = None
    implementation: ImplementationArtifact | None = None
    verifications: list[VerificationGate] = field(default_factory=list)
    readme_markdown: str = ""
    started_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "scope": self.scope.to_dict(),
            "research": self.research.to_dict() if self.research else None,
            "thinking": self.thinking.to_dict() if self.thinking else None,
            "implementation": (
                self.implementation.to_dict() if self.implementation else None
            ),
            "verifications": [v.to_dict() for v in self.verifications],
            "readme_markdown": self.readme_markdown,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
