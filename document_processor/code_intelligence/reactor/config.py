"""
ReactorConfig — knob bundle for the Code Synthesis Reactor v10.

Reads from ``document_processor.config.settings.settings`` lazily so
tests can override individual flags via constructor kwargs without
patching the global Settings singleton.

Every flag has a sensible default and a documented purpose; the
``from_settings()`` classmethod is the one-call helper the engines use
at construction time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Canonical feature ids — keep in sync with `code_reactor_features`
# string in settings.py and with the docs.
ALL_FEATURES: frozenset[str] = frozenset({
    "benchmarker",
    "symbolic_complexity",
    "tournament",
    "property_tests",
    "rag",
    "llm_cache",
    "bandit",
})


def _parse_features(raw: str | None) -> set[str]:
    if not raw:
        return set()
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    return parts & ALL_FEATURES


def _parse_int_list(raw: str | None, fallback: list[int]) -> list[int]:
    if not raw:
        return list(fallback)
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            continue
    return out or list(fallback)


@dataclass
class ReactorConfig:
    """Per-session reactor settings. Immutable after construction —
    the facade never mutates these; new sessions get fresh instances.
    """

    enabled: bool = True
    features: set[str] = field(default_factory=lambda: set(ALL_FEATURES))

    # TournamentRunner
    tournament_n: int = 3
    tournament_max: int = 5

    # PropertyTestGenerator
    property_tests_llm_suggest: bool = True

    # PerformanceBenchmarker
    bench_scales: list[int] = field(
        default_factory=lambda: [10, 100, 1_000, 10_000],
    )
    bench_timeout_per_scale_s: int = 8

    # CodeCorpusRAG
    rag_top_k: int = 3
    rag_similarity_floor: float = 0.55

    # SemanticLLMCache
    llm_cache_ttl_s: int = 86_400
    llm_cache_cosine_threshold: float = 0.92
    cache_salt: int = 1

    # SpecialistBandit
    bandit_temperature: float = 1.0
    bandit_cold_start_threshold: int = 5

    def is_feature_enabled(self, feature: str) -> bool:
        """Master toggle gates everything; per-feature toggle is a
        finer override."""
        if not self.enabled:
            return False
        if feature not in ALL_FEATURES:
            return False
        return feature in self.features

    def normalised(self) -> "ReactorConfig":
        """Apply guardrails — clamp tournament_n to [1, tournament_max],
        ensure bench_scales is sorted + non-empty, etc."""
        n = max(1, min(int(self.tournament_n or 1), int(self.tournament_max or 5)))
        scales = sorted({s for s in self.bench_scales if s > 0})
        if not scales:
            scales = [10, 100, 1_000]
        cosine = max(0.0, min(1.0, float(self.llm_cache_cosine_threshold or 0.92)))
        floor = max(0.0, min(1.0, float(self.rag_similarity_floor or 0.55)))
        temp = max(0.05, float(self.bandit_temperature or 1.0))
        cold = max(1, int(self.bandit_cold_start_threshold or 1))
        # `replace`-style return so callers don't accidentally mutate
        # the original.
        return ReactorConfig(
            enabled=bool(self.enabled),
            features=set(self.features),
            tournament_n=n,
            tournament_max=int(self.tournament_max),
            property_tests_llm_suggest=bool(self.property_tests_llm_suggest),
            bench_scales=scales,
            bench_timeout_per_scale_s=int(self.bench_timeout_per_scale_s or 8),
            rag_top_k=max(1, int(self.rag_top_k)) if self.rag_top_k is not None else 3,
            rag_similarity_floor=floor,
            llm_cache_ttl_s=max(60, int(self.llm_cache_ttl_s) if self.llm_cache_ttl_s else 60),
            llm_cache_cosine_threshold=cosine,
            cache_salt=int(self.cache_salt or 0),
            bandit_temperature=temp,
            bandit_cold_start_threshold=cold,
        )

    @classmethod
    def from_settings(cls, **overrides: Any) -> "ReactorConfig":
        """Build from the global ``settings`` singleton; any keyword
        override wins. Tests pass `enabled=False` etc. to short-circuit
        per-test paths without touching the global config."""
        try:
            from ...config.settings import settings  # noqa: PLC0415
        except Exception:  # pragma: no cover
            settings = None  # type: ignore[assignment]

        if settings is None:
            return cls(**overrides).normalised()

        cfg = cls(
            enabled=getattr(settings, "code_reactor_enabled", True),
            features=_parse_features(
                getattr(settings, "code_reactor_features", None),
            ),
            tournament_n=int(getattr(settings, "code_tournament_n", 3)),
            tournament_max=int(getattr(settings, "code_tournament_max", 5)),
            property_tests_llm_suggest=bool(
                getattr(settings, "code_property_tests_llm_suggest", True),
            ),
            bench_scales=_parse_int_list(
                getattr(settings, "code_bench_scales", None),
                fallback=[10, 100, 1_000, 10_000],
            ),
            bench_timeout_per_scale_s=int(
                getattr(settings, "code_bench_timeout_per_scale_s", 8),
            ),
            rag_top_k=int(getattr(settings, "code_rag_top_k", 3)),
            rag_similarity_floor=float(
                getattr(settings, "code_rag_similarity_floor", 0.55),
            ),
            llm_cache_ttl_s=int(getattr(settings, "code_llm_cache_ttl_s", 86_400)),
            llm_cache_cosine_threshold=float(
                getattr(settings, "code_llm_cache_cosine_threshold", 0.92),
            ),
            cache_salt=int(getattr(settings, "code_reactor_cache_salt", 1)),
            bandit_temperature=float(
                getattr(settings, "code_bandit_temperature", 1.0),
            ),
            bandit_cold_start_threshold=int(
                getattr(settings, "code_bandit_cold_start_threshold", 5),
            ),
        )
        if overrides:
            for k, v in overrides.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        return cfg.normalised()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "features": sorted(self.features),
            "tournament_n": self.tournament_n,
            "tournament_max": self.tournament_max,
            "property_tests_llm_suggest": self.property_tests_llm_suggest,
            "bench_scales": list(self.bench_scales),
            "bench_timeout_per_scale_s": self.bench_timeout_per_scale_s,
            "rag_top_k": self.rag_top_k,
            "rag_similarity_floor": self.rag_similarity_floor,
            "llm_cache_ttl_s": self.llm_cache_ttl_s,
            "llm_cache_cosine_threshold": self.llm_cache_cosine_threshold,
            "cache_salt": self.cache_salt,
            "bandit_temperature": self.bandit_temperature,
            "bandit_cold_start_threshold": self.bandit_cold_start_threshold,
        }
