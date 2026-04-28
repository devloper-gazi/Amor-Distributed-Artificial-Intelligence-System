"""
Consortium Mode — meta-orchestrator that chains Code Intelligence
(scope) → Research → Thinking → Code Intelligence (build) into a
single pipeline with quality gates between phases.

100% local — no paid APIs.
"""

from .models import (
    ConsortiumBundle,
    ConsortiumScope,
    ImplementationArtifact,
    ResearchArtifact,
    ThinkingArtifact,
    VerificationGate,
)
from .orchestrator import ConsortiumOrchestrator

__all__ = [
    "ConsortiumBundle",
    "ConsortiumOrchestrator",
    "ConsortiumScope",
    "ImplementationArtifact",
    "ResearchArtifact",
    "ThinkingArtifact",
    "VerificationGate",
]
