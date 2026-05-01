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

from .benchmarker import (
    BenchmarkFit,
    BenchmarkRecord,
    BenchmarkResult,
    PerformanceBenchmarker,
)
from .config import ReactorConfig
from .property_tests import (
    Invariant,
    InvariantOutcome,
    PropertyTestGenerator,
    PropertyTestResult,
    PropertyTestRunner,
)
from .symbolic_complexity import (
    FunctionComplexity,
    SymbolicComplexity,
    SymbolicComplexityAnalyzer,
)
from .bandit import BanditPosterior, SpecialistBandit
from .facade import CodeSynthesisReactor, ReactorBundle
from .llm_cache import CacheKey, SemanticLLMCache, wrap_llm_call
from .rag import CodeCorpusRAG, CorpusPattern, RetrievalResult
from .speculative import speculative_run
from .tournament import (
    CandidateScore,
    TournamentBundle,
    TournamentCandidate,
    TournamentRunner,
)

__all__ = [
    "BanditPosterior",
    "BenchmarkFit",
    "BenchmarkRecord",
    "BenchmarkResult",
    "CacheKey",
    "CandidateScore",
    "CodeCorpusRAG",
    "CodeSynthesisReactor",
    "CorpusPattern",
    "FunctionComplexity",
    "Invariant",
    "InvariantOutcome",
    "PerformanceBenchmarker",
    "PropertyTestGenerator",
    "PropertyTestResult",
    "PropertyTestRunner",
    "ReactorBundle",
    "ReactorConfig",
    "RetrievalResult",
    "SemanticLLMCache",
    "SpecialistBandit",
    "SymbolicComplexity",
    "SymbolicComplexityAnalyzer",
    "TournamentBundle",
    "TournamentCandidate",
    "TournamentRunner",
    "speculative_run",
    "wrap_llm_call",
]
