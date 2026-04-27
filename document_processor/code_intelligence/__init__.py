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
from .hooks import ChainedHooks, NoopHooks, PhaseHooks, TelemetryHooks
from .model_registry import (
    CODE_MODEL_CATALOGUE,
    ROLE_STRENGTH_MAP,
    CodeModelRegistry,
    ModelSpec,
)
from .observability import emit_event, traced
from .registries import (
    AgentRegistry,
    CapabilitySourceRegistry,
    SandboxTier,
    SandboxTierRegistry,
    agent_registry,
    capability_source_registry,
    register_default_sources,
    register_default_tiers,
    register_defaults,
    sandbox_tier_registry,
)
from .repomap import RepoMap
from .sandbox import LANGUAGE_RUNNERS, ExecutionResult, ExecutionSandbox
from .schema import (
    CURRENT_SCHEMA_VERSIONS,
    VersionedModel,
    ensure_schema_version,
    schema_version_of,
)
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
    # Charter §6 mandates 1, 3, 6
    "AgentRegistry",
    "SandboxTierRegistry",
    "CapabilitySourceRegistry",
    "SandboxTier",
    "agent_registry",
    "sandbox_tier_registry",
    "capability_source_registry",
    "register_defaults",
    "register_default_tiers",
    "register_default_sources",
    "PhaseHooks",
    "NoopHooks",
    "ChainedHooks",
    "TelemetryHooks",
    "VersionedModel",
    "CURRENT_SCHEMA_VERSIONS",
    "ensure_schema_version",
    "schema_version_of",
]
