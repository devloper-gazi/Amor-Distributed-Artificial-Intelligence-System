"""
Sentinel — Multi-Agent Local Security Intelligence (V1).

A 6th mode on the AMOR homepage: a security-dedicated evolution of
the Consortium pattern.  Sentinel runs a multi-agent swarm
(Auditor / Reasoner / RedTeam / Patcher / Judge) on top of a
classical-ML + static-analysis + RAG-backed pipeline to audit code
for security issues, propose fixes, and emit SARIF / MD / HTML
reports.

Design pillars
--------------

* 100 % local — no external API, no telemetry, no CDN-bound asset.
* Multi-agent self-consistency — Auditor 3× voting, Critic loop on
  patches, Auditor↔RedTeam debate with Judge tie-break.
* Bayesian merging across sources (static / ML / agent) so a single
  weak signal cannot dominate the verdict.
* Fail-soft everywhere — missing tools (semgrep / gitleaks / trivy
  / sklearn / xgboost) just degrade the pipeline; the engine still
  runs and reports.
* No content filters or refusal language in any V1 prompt template.
  Infrastructure security is owned by
  ``code_intelligence/adversarial_reviewer.py`` — that is the only
  layer allowed to block output, and it catches infra issues
  (prompt injection / shell injection / secret leakage), not
  content moderation.

Adapter pattern
---------------

Sentinel mirrors the layout of ``document_processor/consortium/``:
``models.py`` for typed contracts, ``engine.py`` for the
orchestrator, ``prompts.py`` for system prompts, plus dedicated
modules for the static-analysis swarm, ML pipeline, RAG, agents,
tools, scoring, critic loop, self-play, and reporters.  Routes live
at ``document_processor/api/sentinel_routes.py``.

License: MIT (matches repo).
"""

from __future__ import annotations

from .engine import SentinelEngine
from .models import (
    AgentRole,
    AgentVerdict,
    ConfidenceScore,
    Finding,
    RAGContext,
    ScanProfile,
    SentinelBundle,
    SentinelGate,
    SentinelRequest,
    SeverityLevel,
)
# Phase 15 — Evolution Engine.  Imported lazily so a partial install
# (no pyyaml, no peft) still lets the rest of Sentinel boot.
from . import evolution  # noqa: F401


__all__ = [
    "AgentRole",
    "AgentVerdict",
    "ConfidenceScore",
    "Finding",
    "RAGContext",
    "ScanProfile",
    "SentinelBundle",
    "SentinelEngine",
    "SentinelGate",
    "SentinelRequest",
    "SeverityLevel",
    "evolution",
]
