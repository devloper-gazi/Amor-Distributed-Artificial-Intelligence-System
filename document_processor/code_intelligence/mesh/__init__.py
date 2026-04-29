"""
Multi-ML Mesh — agent swarm that produces uniquely strong code via:

  1. **Reasoning mesh** — N specialised reasoners (math / performance /
     edge-case / general) each propose 2-3 approaches in parallel.
     An aggregator merges, dedupes, and re-scores via the QuickCode
     composite formula. The best alternative wins.

  2. **Code-review mesh** — once code is generated, each specialist
     audits it from their own lens and returns a structured verdict.
     The auditors run in parallel against the actual code and tests.

  3. **Meta-arbiter** — a single high-tier model synthesises everything
     (chosen alternative, code, tests, verification, refine iters,
     audit reports) and produces the final production-readiness
     verdict + confidence score + top risks + top strengths.

  4. **Self-evolution metrics** — per-session model performance
     tracked in MongoDB so future runs can weight the ensemble
     towards specialists that historically produced cleaner code.

The mesh is shared infrastructure: both the QuickCode engine and the
classic Code Intelligence engine can call into it. Specialists run
in parallel via ``asyncio.gather`` with per-role model bindings; if a
preferred specialist model isn't installed, the role falls back to
the user's general model with a synthetic system-prompt override.

100% local — every LLM call goes through the existing local Ollama
bridge. No paid APIs.
"""

from .aggregator import MeshAggregator, AggregatedReasoning
from .code_auditors import (
    EdgeCaseCodeAuditor,
    MathCodeAuditor,
    MeshCodeAudit,
    PerformanceCodeAuditor,
)
from .mesh_engine import MultiMLMesh, MeshConfig, MeshOutput
from .meta_arbiter import MetaArbiterAgent, MetaVerdict
from .metrics import MeshMetricsRecorder, record_session_metrics
from .specialists import (
    EdgeCaseReasonerAgent,
    GeneralReasonerAgent,
    MathReasonerAgent,
    PerformanceReasonerAgent,
    SpecialistRoleId,
)

__all__ = [
    "AggregatedReasoning",
    "EdgeCaseCodeAuditor",
    "EdgeCaseReasonerAgent",
    "GeneralReasonerAgent",
    "MathCodeAuditor",
    "MathReasonerAgent",
    "MeshAggregator",
    "MeshCodeAudit",
    "MeshConfig",
    "MeshMetricsRecorder",
    "MeshOutput",
    "MetaArbiterAgent",
    "MetaVerdict",
    "MultiMLMesh",
    "PerformanceCodeAuditor",
    "PerformanceReasonerAgent",
    "SpecialistRoleId",
    "record_session_metrics",
]
