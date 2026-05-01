"""
Smoke tests for the Quick Code V2 settings block.

These exist for two reasons:

1. Catch typos / drift between ``settings.py`` and the engine before
   it shows up at runtime — the V2 work touches a lot of flags and
   a single mismatched name silently disables the feature.
2. Pin defaults so a future PR cannot accidentally enable
   hardware-incompatible flags (the 32B specialist + speculative
   decoding) on hosts that cannot run them.
"""

from __future__ import annotations

from document_processor.config.settings import Settings, settings


# ─────────────────────────────────────────────────────────────────────
# Flag presence
# ─────────────────────────────────────────────────────────────────────


REQUIRED_FLAGS: tuple[str, ...] = (
    "quick_v2_enabled",
    "quick_v2_router_enabled",
    "quick_v2_router_model",
    "quick_v2_router_redirect_to_pro",
    "quick_v2_striatum_enabled",
    "quick_v2_striatum_threshold",
    "quick_v2_striatum_ttl_s",
    "quick_v2_striatum_salt",
    "quick_v2_sk_enabled",
    "quick_v2_sk_alpha_floor",
    "quick_v2_sk_top_k",
    "quick_v2_symcode_enabled",
    "quick_v2_symcode_timeout_s",
    "quick_v2_parsel_enabled",
    "quick_v2_parsel_max_depth",
    "quick_v2_use_mcts",
    "quick_v2_mcts_max_iters",
    "quick_v2_mcts_c",
    "quick_v2_use_seeker",
    "quick_v2_anton_brain_enabled",
    "quick_v2_anton_brain_budget",
    "quick_v2_orpo_enabled",
    "quick_v2_orpo_collection",
    "quick_v2_sandbox_quick_mem_mb",
    "quick_v2_sandbox_quick_timeout_s",
    "quick_v2_sandbox_pro_mem_mb",
    "quick_v2_sandbox_pro_timeout_s",
    "quick_v2_specialist_32b_enabled",
    "quick_v2_specialist_32b_model",
    "quick_v2_speculative_decoding_enabled",
    "quick_v2_speculative_draft_model",
)


def test_every_required_flag_is_present():
    missing = [name for name in REQUIRED_FLAGS if not hasattr(settings, name)]
    assert not missing, f"Settings is missing V2 flag(s): {missing}"


# ─────────────────────────────────────────────────────────────────────
# Default values
# ─────────────────────────────────────────────────────────────────────


# A fresh Settings() ignores monkeypatched values on the singleton —
# we use it to verify *defaults* rather than runtime overrides.
def _fresh() -> Settings:
    return Settings()


def test_master_gate_default_on():
    assert _fresh().quick_v2_enabled is True


def test_hardware_incompatible_flags_default_off():
    """The 32B specialist + speculative decoding ship off because
    the dev host has 8 GB GPU. Anyone flipping these on must do it
    consciously via env var on a ≥24 GB host."""
    s = _fresh()
    assert s.quick_v2_specialist_32b_enabled is False
    assert s.quick_v2_speculative_decoding_enabled is False


def test_mcts_default_off_in_quick_mode():
    """MCTS adds latency; Pro mode opts in via the request, not the
    global default."""
    assert _fresh().quick_v2_use_mcts is False


def test_orpo_collector_default_off():
    """ORPO export ships off until the offline trainer is ready."""
    assert _fresh().quick_v2_orpo_enabled is False


def test_sandbox_tier_limits_match_plan():
    s = _fresh()
    assert s.quick_v2_sandbox_quick_mem_mb == 256
    assert s.quick_v2_sandbox_quick_timeout_s == 15
    assert s.quick_v2_sandbox_pro_mem_mb == 512
    assert s.quick_v2_sandbox_pro_timeout_s == 45


def test_striatum_threshold_in_unit_interval():
    """Striatum is a cosine fast-path; its threshold must lie in
    [0, 1] or downstream comparisons silently underflow."""
    assert 0.0 <= _fresh().quick_v2_striatum_threshold <= 1.0


def test_sk_alpha_floor_in_unit_interval():
    assert 0.0 <= _fresh().quick_v2_sk_alpha_floor <= 1.0


def test_anton_brain_budget_reasonable():
    """Anton-Brain budgets the prompt; a too-small value would clip
    every system message. Plan calls for 3200 tokens."""
    assert 1000 <= _fresh().quick_v2_anton_brain_budget <= 16_000


# ─────────────────────────────────────────────────────────────────────
# Per-feature flag matrix — every combination loads cleanly
# ─────────────────────────────────────────────────────────────────────


def test_disable_master_does_not_crash_settings_construction():
    """When the master gate is False, a fresh Settings() must still
    load — the engine relies on this to bypass every V2 phase."""
    s = Settings(quick_v2_enabled=False)  # type: ignore[call-arg]
    assert s.quick_v2_enabled is False


def test_per_feature_flags_are_independent():
    """Toggle each per-feature flag individually; all 12 combos must
    construct successfully."""
    keys = (
        "quick_v2_router_enabled",
        "quick_v2_striatum_enabled",
        "quick_v2_sk_enabled",
        "quick_v2_symcode_enabled",
        "quick_v2_parsel_enabled",
        "quick_v2_use_mcts",
        "quick_v2_use_seeker",
        "quick_v2_anton_brain_enabled",
        "quick_v2_orpo_enabled",
        "quick_v2_router_redirect_to_pro",
        "quick_v2_specialist_32b_enabled",
        "quick_v2_speculative_decoding_enabled",
    )
    for key in keys:
        for value in (True, False):
            s = Settings(**{key: value})  # type: ignore[arg-type]
            assert getattr(s, key) is value, f"{key}={value} did not stick"
