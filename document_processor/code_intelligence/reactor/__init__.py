"""
Code Synthesis Reactor v10 — empirical-performance + RAG + tournament
+ semantic-cache + bandit layer that sits on top of the Multi-ML Mesh.

Loads cleanly even when individual features are disabled via settings;
each capability is fail-soft so the engine never aborts because a
dependency (Hypothesis, LanceDB, Mongo, Redis) is missing.

Public API
----------

  ReactorConfig                  — knob bundle, reads from settings
  CodeSynthesisReactor           — single facade Quick + Pro both call
  MeshAndReactorHooks            — PhaseHooks bridge for the Pro engine

The individual feature modules (PerformanceBenchmarker,
SymbolicComplexityAnalyzer, TournamentRunner, PropertyTestGenerator,
CodeCorpusRAG, SemanticLLMCache, SpecialistBandit) are imported lazily
through the facade so a partial install (e.g. no `hypothesis`
package) doesn't poison the whole reactor at import time.
"""

from __future__ import annotations

from .config import ReactorConfig
from .symbolic_complexity import (
    FunctionComplexity,
    SymbolicComplexity,
    SymbolicComplexityAnalyzer,
)

__all__ = [
    "FunctionComplexity",
    "ReactorConfig",
    "SymbolicComplexity",
    "SymbolicComplexityAnalyzer",
]
