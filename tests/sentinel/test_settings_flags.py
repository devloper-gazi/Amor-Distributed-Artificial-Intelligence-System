"""Smoke tests for the Sentinel settings block."""

from __future__ import annotations

from document_processor.config.settings import Settings, settings


REQUIRED_FLAGS: tuple[str, ...] = (
    "sentinel_enabled",
    "sentinel_router_redirect_complex",
    "sentinel_static_swarm_enabled",
    "sentinel_ml_pipeline_enabled",
    "sentinel_rag_enabled",
    "sentinel_rag_table_cwe",
    "sentinel_rag_table_owasp",
    "sentinel_rag_table_history",
    "sentinel_rag_table_project",
    "sentinel_critic_loop_enabled",
    "sentinel_critic_loop_max_iters",
    "sentinel_self_play_enabled",
    "sentinel_auditor_voting_n",
    "sentinel_auditor_temperature",
    "sentinel_reasoner_temperature",
    "sentinel_redteam_temperature",
    "sentinel_patcher_temperature",
    "sentinel_judge_temperature",
    "sentinel_auditor_model",
    "sentinel_reasoner_model",
    "sentinel_redteam_model",
    "sentinel_patcher_model",
    "sentinel_judge_model",
    "sentinel_embedding_model",
    "sentinel_default_scan_profile",
    "sentinel_quick_timeout_s",
    "sentinel_standard_timeout_s",
    "sentinel_deep_timeout_s",
    "sentinel_paranoid_timeout_s",
    "sentinel_mongo_findings_collection",
    "sentinel_mongo_calibration_collection",
    "sentinel_kafka_topic",
    "sentinel_max_repo_size_mb",
    "sentinel_max_files_per_scan",
    "sentinel_use_codebert_classifier",
    "sentinel_use_xgboost_ranker",
    "sentinel_use_isolation_forest",
)


def test_every_flag_present():
    missing = [k for k in REQUIRED_FLAGS if not hasattr(settings, k)]
    assert not missing, f"Settings missing Sentinel flags: {missing}"


def _fresh() -> Settings:
    return Settings()


def test_master_gate_default_on():
    assert _fresh().sentinel_enabled is True


def test_self_play_default_off():
    assert _fresh().sentinel_self_play_enabled is False


def test_hardware_optional_flags_default_off():
    s = _fresh()
    assert s.sentinel_use_codebert_classifier is False
    assert s.sentinel_use_xgboost_ranker is False
    assert s.sentinel_use_isolation_forest is False


def test_default_scan_profile_is_standard():
    assert _fresh().sentinel_default_scan_profile == "standard"


def test_critic_loop_capped_at_three_by_default():
    assert _fresh().sentinel_critic_loop_max_iters == 3


def test_models_are_known_to_8gb_gpu():
    s = _fresh()
    # All defaults must be 7B-class models — anything bigger blocks
    # the swap on this host.
    for name in (
        s.sentinel_auditor_model, s.sentinel_reasoner_model,
        s.sentinel_redteam_model, s.sentinel_patcher_model,
        s.sentinel_judge_model,
    ):
        assert ":7b" in name, f"{name} is not a 7B-class model"


def test_temperatures_in_unit_interval():
    s = _fresh()
    for t in (
        s.sentinel_auditor_temperature, s.sentinel_reasoner_temperature,
        s.sentinel_redteam_temperature, s.sentinel_patcher_temperature,
        s.sentinel_judge_temperature,
    ):
        assert 0.0 <= t <= 1.5
