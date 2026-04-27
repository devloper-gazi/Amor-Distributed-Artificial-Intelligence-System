"""Code Intelligence Mode — local-only multi-agent code engine."""

from __future__ import annotations

from .engine import (
    CODE_PHASES,
    PHASE_PROGRESS,
    CodeIntelligenceEngine,
)
from .model_registry import (
    CODE_MODEL_CATALOGUE,
    CodeModelRegistry,
    ModelSpec,
    ROLE_STRENGTH_MAP,
)
from .sandbox import ExecutionResult, ExecutionSandbox, LANGUAGE_RUNNERS
from .static_analysis import (
    AnalysisIssue,
    StaticAnalysisHarness,
    StaticAnalysisResult,
)

__all__ = [
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
]
