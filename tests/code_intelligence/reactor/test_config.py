"""
Tests for ReactorConfig — defaults, normalisation, settings reader,
feature gating.
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence.reactor.config import (
    ALL_FEATURES,
    ReactorConfig,
    _parse_features,
    _parse_int_list,
)


# ── parsers ─────────────────────────────────────────────────────────


def test_parse_features_returns_known_subset():
    raw = "tournament, rag,bandit, made_up"
    parsed = _parse_features(raw)
    assert parsed == {"tournament", "rag", "bandit"}
    # Made-up feature ids are silently dropped — no exception.
    assert "made_up" not in parsed


def test_parse_features_handles_empty_and_none():
    assert _parse_features("") == set()
    assert _parse_features(None) == set()


def test_parse_features_strips_case_insensitive():
    assert _parse_features("Tournament,RAG") == {"tournament", "rag"}


def test_parse_int_list_uses_fallback_when_empty():
    assert _parse_int_list("", [1, 2]) == [1, 2]
    assert _parse_int_list(None, [1, 2]) == [1, 2]


def test_parse_int_list_drops_invalid_tokens():
    assert _parse_int_list("10, x, 100, !!", [1]) == [10, 100]


# ── defaults + feature gate ────────────────────────────────────────


def test_default_config_has_all_features_enabled():
    cfg = ReactorConfig()
    assert cfg.enabled is True
    assert cfg.features == set(ALL_FEATURES)
    for f in ALL_FEATURES:
        assert cfg.is_feature_enabled(f)


def test_disabled_master_gate_disables_every_feature():
    cfg = ReactorConfig(enabled=False)
    for f in ALL_FEATURES:
        assert cfg.is_feature_enabled(f) is False


def test_unknown_feature_id_is_not_enabled():
    cfg = ReactorConfig()
    assert cfg.is_feature_enabled("definitely_not_a_feature") is False


def test_feature_subset_disables_only_excluded_features():
    cfg = ReactorConfig(features={"tournament", "rag"})
    assert cfg.is_feature_enabled("tournament")
    assert cfg.is_feature_enabled("rag")
    assert not cfg.is_feature_enabled("benchmarker")
    assert not cfg.is_feature_enabled("bandit")


# ── normalisation guardrails ───────────────────────────────────────


def test_normalised_clamps_tournament_n_to_max():
    cfg = ReactorConfig(tournament_n=99, tournament_max=5).normalised()
    assert cfg.tournament_n == 5


def test_normalised_floors_tournament_n_to_one():
    cfg = ReactorConfig(tournament_n=0).normalised()
    assert cfg.tournament_n == 1


def test_normalised_sorts_and_dedups_bench_scales():
    cfg = ReactorConfig(bench_scales=[100, 10, 100, 1000, -5]).normalised()
    assert cfg.bench_scales == [10, 100, 1_000]


def test_normalised_uses_default_scales_when_empty():
    cfg = ReactorConfig(bench_scales=[]).normalised()
    assert cfg.bench_scales == [10, 100, 1_000]


def test_normalised_clamps_cosine_threshold():
    cfg = ReactorConfig(llm_cache_cosine_threshold=2.5).normalised()
    assert cfg.llm_cache_cosine_threshold == 1.0
    cfg = ReactorConfig(llm_cache_cosine_threshold=-1.0).normalised()
    assert cfg.llm_cache_cosine_threshold == 0.0


def test_normalised_floors_bandit_temperature():
    cfg = ReactorConfig(bandit_temperature=0.0).normalised()
    # Temperature must stay > 0; floored to 0.05 (anything > 0 is fine)
    assert cfg.bandit_temperature >= 0.05


def test_normalised_floors_cold_start_threshold():
    cfg = ReactorConfig(bandit_cold_start_threshold=0).normalised()
    assert cfg.bandit_cold_start_threshold == 1


def test_normalised_floors_rag_top_k():
    cfg = ReactorConfig(rag_top_k=0).normalised()
    assert cfg.rag_top_k == 1


# ── from_settings ────────────────────────────────────────────────


def test_from_settings_reads_global_singleton():
    cfg = ReactorConfig.from_settings()
    assert isinstance(cfg, ReactorConfig)
    # Defaults from settings.py make tournament_n=3.
    assert cfg.tournament_n == 3
    assert cfg.enabled is True
    # Default feature string includes all 7.
    assert cfg.features == set(ALL_FEATURES)


def test_from_settings_kwarg_override():
    cfg = ReactorConfig.from_settings(enabled=False, tournament_n=2)
    assert cfg.enabled is False
    assert cfg.tournament_n == 2


def test_from_settings_unknown_kwarg_silently_ignored():
    """Forward-compat — adding a kwarg the dataclass doesn't have
    must not break older callers."""
    cfg = ReactorConfig.from_settings(unknown_field="x")
    assert isinstance(cfg, ReactorConfig)


# ── to_dict (serialisation) ──────────────────────────────────────


def test_to_dict_round_trip_keys():
    cfg = ReactorConfig().normalised()
    d = cfg.to_dict()
    assert "enabled" in d
    assert "features" in d
    # features serialise as a sorted list (set isn't JSON-friendly).
    assert isinstance(d["features"], list)
    assert d["features"] == sorted(d["features"])
    # bench_scales serialise as a list.
    assert isinstance(d["bench_scales"], list)
