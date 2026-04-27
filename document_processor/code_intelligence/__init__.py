"""Code Intelligence Mode — local-only multi-agent code engine."""

from __future__ import annotations

from .adversarial_reviewer import AdversarialReviewer
from .capability_discoverer import (
    CapabilityCandidate,
    CapabilityDiscoverer,
    CapabilityKind,
    CapabilityRecord,
    CapabilityRegistry,
)
from .engine import (
    CODE_PHASES,
    PHASE_PROGRESS,
    CodeIntelligenceEngine,
)
from .model_registry import (
    CODE_MODEL_CATALOGUE,
    ROLE_STRENGTH_MAP,
    CodeModelRegistry,
    ModelSpec,
)
from .observability import emit_event, traced
from .repomap import RepoMap
from .sandbox import LANGUAGE_RUNNERS, ExecutionResult, ExecutionSandbox
from .static_analysis import (
    AnalysisIssue,
    StaticAnalysisHarness,
    StaticAnalysisResult,
)

__all__ = [  # noqa: RUF022 — grouped by version, not alphabetical
    # v1 — engine + 5 agents foundation
    "CodeIntelligenceEngine",
    "CodeModelRegistry",
    "CODE_MODEL_CATALOGUE",
    "CODE_PHASES",
    "PHASE_PROGRESS",
    "ModelSpec",
    "ROLE_STRENGTH_MAP",
    "ExecutionSandbox",
    "ExecutionResult",
    "LANGUAGE_RUNNERS",
    "StaticAnalysisHarness",
    "StaticAnalysisResult",
    "AnalysisIssue",
    # v2 — RepoMap + Discovery + Adversarial Reviewer + observability
    "RepoMap",
    "CapabilityDiscoverer",
    "CapabilityRegistry",
    "CapabilityCandidate",
    "CapabilityRecord",
    "CapabilityKind",
    "AdversarialReviewer",
    "traced",
    "emit_event",
]
