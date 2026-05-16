"""Cycle H Phase A.1 — BitNet b1.58 2B4T backend + factory tests."""

from __future__ import annotations

import pytest


# ─── Factory registration ──────────────────────────────────────────


def test_make_backend_resolves_bitnet_cpu_kind():
    from local_ai.llm_backend import make_backend
    from local_ai.llm_backend.bitnet_cpu import BitNetCpuBackend

    backend = make_backend("bitnet-cpu")
    assert isinstance(backend, BitNetCpuBackend)
    assert backend.BACKEND_NAME == "bitnet-cpu"


def test_make_backend_accepts_underscore_alias():
    """Match the llama_swap/llama_cpp precedent — multiple slug
    variants resolve to the same backend so env vars + settings
    can use whichever spelling is convenient."""
    from local_ai.llm_backend import make_backend
    from local_ai.llm_backend.bitnet_cpu import BitNetCpuBackend

    for alias in ("bitnet-cpu", "bitnet_cpu", "bitnet", "bitnetcpu", "BitNet-CPU"):
        backend = make_backend(alias)
        assert isinstance(backend, BitNetCpuBackend), (
            f"alias {alias!r} did not resolve to BitNetCpuBackend"
        )


def test_make_backend_with_explicit_url_overrides_default():
    from local_ai.llm_backend import make_backend
    backend = make_backend("bitnet-cpu", url="http://custom:9999")
    assert backend.base_url.startswith("http://custom:9999")


def test_make_backend_default_url_is_8081():
    """Port differs from llama-cpp (8080) + llama-swap (9100) so an
    operator running all three doesn't get a bind conflict."""
    from local_ai.llm_backend import make_backend
    backend = make_backend("bitnet-cpu")
    assert backend.base_url.startswith("http://localhost:8081")


def test_make_backend_unknown_kind_still_raises():
    """Regression guard — adding bitnet-cpu shouldn't accidentally
    mask the value-error path on unknown kinds."""
    from local_ai.llm_backend import make_backend
    with pytest.raises(ValueError, match="unknown llm_backend"):
        make_backend("not-a-real-backend")


# ─── Timeout posture ───────────────────────────────────────────────


def test_default_timeout_is_8s_not_300s():
    """Plan-agent locked: BitNet realistic CPU throughput is 6-10
    tok/s; the parent OpenAICompatibleBackend's 300s default would
    let p99 tail bleed into user wall-clock.  Shadow mode must NEVER
    block — 8s hard cap + fallback."""
    from local_ai.llm_backend.bitnet_cpu import BitNetCpuBackend
    backend = BitNetCpuBackend()
    assert backend.timeout == 8.0


def test_explicit_timeout_override_works():
    from local_ai.llm_backend.bitnet_cpu import BitNetCpuBackend
    backend = BitNetCpuBackend(timeout=15.0)
    assert backend.timeout == 15.0


# ─── Shadow routing helper ─────────────────────────────────────────


def test_should_shadow_disabled_by_default(monkeypatch):
    """Master gate `code_bitnet_planner_enabled=False` (default)
    blocks all shadow routing regardless of traffic_pct."""
    from document_processor.code_intelligence import bitnet_shadow
    from document_processor.config.settings import settings

    monkeypatch.setattr(settings, "code_bitnet_planner_enabled", False, raising=False)
    assert bitnet_shadow.should_shadow_to_bitnet("any-request-id") is False


def test_should_shadow_100pct_routes_every_request(monkeypatch):
    from document_processor.code_intelligence import bitnet_shadow
    from document_processor.config.settings import settings

    monkeypatch.setattr(settings, "code_bitnet_planner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "code_bitnet_shadow_traffic_pct", 100.0, raising=False)
    assert bitnet_shadow.should_shadow_to_bitnet("req-a") is True
    assert bitnet_shadow.should_shadow_to_bitnet("req-b") is True


def test_should_shadow_0pct_routes_no_request(monkeypatch):
    from document_processor.code_intelligence import bitnet_shadow
    from document_processor.config.settings import settings

    monkeypatch.setattr(settings, "code_bitnet_planner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "code_bitnet_shadow_traffic_pct", 0.0, raising=False)
    assert bitnet_shadow.should_shadow_to_bitnet("req-a") is False


def test_should_shadow_explicit_traffic_pct_overrides_settings():
    """`traffic_pct` kwarg bypasses settings — used by tests + ad-hoc
    shadow flips without env mutation."""
    from document_processor.code_intelligence import bitnet_shadow
    # 100% via explicit kwarg ALSO requires settings.code_bitnet_planner_enabled
    # to be True (we don't bypass the master gate); but for tests we
    # can pass an explicit non-zero traffic_pct after enabling.
    assert bitnet_shadow.should_shadow_to_bitnet("req-a", traffic_pct=0.0) is False
    # 100% bypasses the hash check entirely
    assert bitnet_shadow.should_shadow_to_bitnet("req-a", traffic_pct=100.0) is True


def test_should_shadow_hash_split_deterministic(monkeypatch):
    """Same request_id → same decision across invocations.  Survives
    retries + makes the per-session decision auditable."""
    from document_processor.code_intelligence import bitnet_shadow
    from document_processor.config.settings import settings
    monkeypatch.setattr(settings, "code_bitnet_planner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "code_bitnet_shadow_traffic_pct", 10.0, raising=False)

    decisions = {
        bitnet_shadow.should_shadow_to_bitnet(f"req-{i}") for _ in range(3) for i in range(100)
    }
    # If decisions were random, repeating 3× would produce >2 unique
    # values per request_id.  Deterministic means at most 2 values
    # total (True / False) across all calls but each request_id is
    # consistent.
    rid = "consistent-request-id"
    first = bitnet_shadow.should_shadow_to_bitnet(rid)
    for _ in range(20):
        assert bitnet_shadow.should_shadow_to_bitnet(rid) is first


def test_hash_split_distribution_matches_target_pct(monkeypatch):
    """Statistical sanity — 10% traffic_pct on 10K request IDs should
    route ~10% (allow ±2% slack for hash distribution noise)."""
    from document_processor.code_intelligence import bitnet_shadow
    from document_processor.config.settings import settings
    monkeypatch.setattr(settings, "code_bitnet_planner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "code_bitnet_shadow_traffic_pct", 10.0, raising=False)

    n = 10_000
    routed = sum(
        bitnet_shadow.should_shadow_to_bitnet(f"req-{i:06d}") for i in range(n)
    )
    pct = 100.0 * routed / n
    assert 8.0 <= pct <= 12.0, f"expected ~10%, got {pct:.2f}%"


# ─── Outcome recording + stats ─────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_shadow_stats():
    from document_processor.code_intelligence import bitnet_shadow
    bitnet_shadow.reset_stats()
    yield
    bitnet_shadow.reset_stats()


def test_record_outcome_agreement_when_hashes_match():
    from document_processor.code_intelligence import bitnet_shadow
    main_plan = {"task_type": "generation", "language": "python"}
    shadow_plan = {"task_type": "generation", "language": "python"}
    sample = bitnet_shadow.record_shadow_outcome(
        "req-1", main_plan, shadow_plan,
        latency_ms=4523.4,
    )
    assert sample.agreement is True
    assert sample.main_plan_hash == sample.shadow_plan_hash
    assert sample.latency_ms == 4523.4


def test_record_outcome_disagreement_when_plans_differ():
    from document_processor.code_intelligence import bitnet_shadow
    sample = bitnet_shadow.record_shadow_outcome(
        "req-1",
        {"task_type": "generation", "language": "python"},
        {"task_type": "generation", "language": "rust"},
        latency_ms=3000.0,
    )
    assert sample.agreement is False


def test_record_outcome_handles_none_shadow_as_fallback():
    """Shadow timed out → shadow_plan is None → fell_back=True →
    not counted as agreement.  Stats reflect the fallback rate
    separately."""
    from document_processor.code_intelligence import bitnet_shadow
    main_plan = {"task_type": "generation"}
    sample = bitnet_shadow.record_shadow_outcome(
        "req-1", main_plan, None,
        latency_ms=8001.0, timed_out=True, fell_back=True,
    )
    assert sample.agreement is False
    assert sample.timed_out is True
    assert sample.fell_back is True


def test_get_shadow_stats_empty_returns_none_values():
    from document_processor.code_intelligence import bitnet_shadow
    stats = bitnet_shadow.get_shadow_stats()
    assert stats["samples"] == 0
    assert stats["agreement_rate"] is None
    assert stats["promotion_eligible"] is False


def test_get_shadow_stats_after_known_outcomes():
    from document_processor.code_intelligence import bitnet_shadow
    # 85 agreements + 15 disagreements = 85% agreement
    for i in range(85):
        bitnet_shadow.record_shadow_outcome(
            f"req-agree-{i}", {"p": "a"}, {"p": "a"}, latency_ms=3000,
        )
    for i in range(15):
        bitnet_shadow.record_shadow_outcome(
            f"req-disagree-{i}", {"p": "a"}, {"p": "b"}, latency_ms=4000,
        )
    stats = bitnet_shadow.get_shadow_stats()
    assert stats["samples"] == 100
    assert abs(stats["agreement_rate"] - 0.85) < 0.001


def test_promotion_eligible_requires_85pct_agreement_AND_p95_le_6s():
    """v20.0.0 gate condition (ii) — both must hold.  This locks the
    Plan-agent's promotion criteria in code."""
    from document_processor.code_intelligence import bitnet_shadow
    # 90% agreement + p95 = 5000ms + 200 samples → eligible
    for i in range(180):
        bitnet_shadow.record_shadow_outcome(
            f"req-pass-{i}", {"p": "a"}, {"p": "a"}, latency_ms=4000,
        )
    for i in range(20):
        bitnet_shadow.record_shadow_outcome(
            f"req-fail-{i}", {"p": "a"}, {"p": "b"}, latency_ms=5000,
        )
    stats = bitnet_shadow.get_shadow_stats()
    assert stats["samples"] == 200
    assert stats["agreement_rate"] >= 0.85
    assert stats["p95_ms"] is not None and stats["p95_ms"] <= 6000.0
    assert stats["promotion_eligible"] is True


def test_promotion_blocked_when_p95_too_slow():
    """A slow tail kills eligibility even with 100% agreement.
    p95 index over 200 samples = round(0.95 * 199) = 189, so we
    need ≥11 samples in the slow bucket for the 189th sorted value
    to land there.  Use 20 for safety + statistical clarity."""
    from document_processor.code_intelligence import bitnet_shadow
    for i in range(180):
        bitnet_shadow.record_shadow_outcome(
            f"req-fast-{i}", {"p": "a"}, {"p": "a"}, latency_ms=4000,
        )
    # 20 samples at 8000ms push p95 (index 189 of 200) into the slow bucket
    for i in range(20):
        bitnet_shadow.record_shadow_outcome(
            f"req-slow-{i}", {"p": "a"}, {"p": "a"}, latency_ms=8000,
        )
    stats = bitnet_shadow.get_shadow_stats()
    assert stats["agreement_rate"] == 1.0
    assert stats["p95_ms"] > 6000.0
    assert stats["promotion_eligible"] is False


def test_promotion_blocked_when_samples_below_200():
    """Plan-agent required 200-task held-out slice.  Statistical
    rigor — don't promote on noise."""
    from document_processor.code_intelligence import bitnet_shadow
    for i in range(100):
        bitnet_shadow.record_shadow_outcome(
            f"req-{i}", {"p": "a"}, {"p": "a"}, latency_ms=4000,
        )
    stats = bitnet_shadow.get_shadow_stats()
    assert stats["agreement_rate"] == 1.0
    assert stats["promotion_eligible"] is False  # only 100 samples


# ─── Plan hash determinism ─────────────────────────────────────────


def test_plan_hash_handles_none():
    from document_processor.code_intelligence.bitnet_shadow import _plan_hash
    assert _plan_hash(None) == ""


def test_plan_hash_stable_across_key_order():
    """Canonicalisation: same plan dict in different key order →
    same hash.  Otherwise agreement metrics would lie."""
    from document_processor.code_intelligence.bitnet_shadow import _plan_hash
    a = {"task_type": "generation", "language": "python", "effort": "deep"}
    b = {"effort": "deep", "language": "python", "task_type": "generation"}
    assert _plan_hash(a) == _plan_hash(b)


def test_plan_hash_handles_non_serializable_gracefully():
    """A plan with a non-JSON-serializable value (e.g. a tuple of
    ints, or an object with __repr__) shouldn't crash the hash."""
    from document_processor.code_intelligence.bitnet_shadow import _plan_hash
    class Weird:
        def __repr__(self): return "<weird>"
    plan = {"step": Weird()}
    h = _plan_hash(plan)
    assert isinstance(h, str)
    assert len(h) == 16


# ─── Model registry surface ────────────────────────────────────────


def test_bitnet_in_code_model_catalogue():
    """BitNet must appear in CODE_MODEL_CATALOGUE so model_registry
    can route to it when planner role + lightweight tier match."""
    from document_processor.code_intelligence.model_registry import (
        CODE_MODEL_CATALOGUE,
    )
    tags = {spec.ollama_tag for spec in CODE_MODEL_CATALOGUE}
    assert "bitnet:b1.58-2b4t" in tags


def test_bitnet_vram_is_zero_and_tier_is_lightweight():
    """CPU-only model — vram_gb=0 + tier=lightweight means the
    scorer never picks it for coder/critic without explicit opt-in."""
    from document_processor.code_intelligence.model_registry import (
        CODE_MODEL_CATALOGUE,
    )
    spec = next(s for s in CODE_MODEL_CATALOGUE if s.ollama_tag == "bitnet:b1.58-2b4t")
    assert spec.vram_gb == 0
    assert spec.tier == "lightweight"
    assert spec.license == "MIT"


def test_bitnet_strengths_match_planner_role():
    """Plan-agent reuse: leverage existing ROLE_STRENGTH_MAP
    scoring instead of writing separate routing code.  BitNet's
    strengths must overlap with ROLE_STRENGTH_MAP['planner']."""
    from document_processor.code_intelligence.model_registry import (
        CODE_MODEL_CATALOGUE, ROLE_STRENGTH_MAP,
    )
    spec = next(s for s in CODE_MODEL_CATALOGUE if s.ollama_tag == "bitnet:b1.58-2b4t")
    planner_strengths = set(ROLE_STRENGTH_MAP["planner"])
    bitnet_strengths = set(spec.strengths)
    overlap = planner_strengths & bitnet_strengths
    assert len(overlap) >= 1, (
        f"BitNet strengths {bitnet_strengths} have no overlap with "
        f"planner role strengths {planner_strengths} — auto-routing breaks"
    )
