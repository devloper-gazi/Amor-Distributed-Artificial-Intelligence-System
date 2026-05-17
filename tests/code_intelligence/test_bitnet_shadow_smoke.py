"""Sprint H.1 smoke — verify the bitnet_shadow → admin endpoint wire
is operational without requiring a running bitnet.cpp server.

Mirrors what ``tools/bitnet_shadow_smoke.py`` does, captured as a
proper unit test so CI can guard against regressions in the ring
buffer / promotion-eligibility logic.
"""

from __future__ import annotations

import asyncio

import pytest


def test_bitnet_shadow_smoke_in_process_records_samples():
    """The in-process smoke records N samples, stats reports N + the
    promotion-eligibility flag stays False while samples < 200."""
    from document_processor.code_intelligence import bitnet_shadow
    bitnet_shadow.reset_stats()
    for i in range(5):
        bitnet_shadow.record_shadow_outcome(
            request_id=f"smoke-{i}",
            main_plan={"summary": f"main {i}"},
            shadow_plan={"summary": f"main {i}"},   # identical → agreement
            latency_ms=3500.0 + i * 100,
        )
    stats = bitnet_shadow.get_shadow_stats()
    assert stats["samples"] == 5
    assert stats["agreement_rate"] == 1.0
    assert stats["promotion_eligible"] is False   # < 200 samples
    bitnet_shadow.reset_stats()


def test_bitnet_shadow_smoke_disagreement_rate():
    """Disagreement detection — when shadow_plan diverges from main,
    agreement_rate drops accordingly.  Plan-agent locked: ≥85%
    required for promotion (gate condition #2)."""
    from document_processor.code_intelligence import bitnet_shadow
    bitnet_shadow.reset_stats()
    # 4 agree + 1 disagree → 80% (below 85% threshold)
    for i in range(4):
        bitnet_shadow.record_shadow_outcome(
            request_id=f"agree-{i}",
            main_plan={"plan": "x"},
            shadow_plan={"plan": "x"},
            latency_ms=4000.0,
        )
    bitnet_shadow.record_shadow_outcome(
        request_id="diverge",
        main_plan={"plan": "x"},
        shadow_plan={"plan": "y"},
        latency_ms=4500.0,
    )
    stats = bitnet_shadow.get_shadow_stats()
    assert stats["samples"] == 5
    assert stats["agreement_rate"] == pytest.approx(0.80, abs=0.01)
    bitnet_shadow.reset_stats()


def test_bitnet_shadow_smoke_timeout_fallback_tracked():
    """Timeout + fallback paths feed `timeout_rate` + `fallback_rate`
    — these are operator-actionable signals (BitNet too slow → bump
    timeout_s; BitNet unreliable → keep main planner exclusive)."""
    from document_processor.code_intelligence import bitnet_shadow
    bitnet_shadow.reset_stats()
    bitnet_shadow.record_shadow_outcome(
        request_id="ok",
        main_plan={"x": 1},
        shadow_plan={"x": 1},
        latency_ms=4000.0,
    )
    bitnet_shadow.record_shadow_outcome(
        request_id="timed-out",
        main_plan={"x": 1},
        shadow_plan={},
        latency_ms=8000.0,
        timed_out=True,
        fell_back=True,
    )
    stats = bitnet_shadow.get_shadow_stats()
    assert stats["samples"] == 2
    assert stats["timeout_rate"] == 0.5
    assert stats["fallback_rate"] == 0.5
    bitnet_shadow.reset_stats()


def test_bitnet_shadow_smoke_endpoint_round_trip():
    """End-to-end: record samples → call admin endpoint → verify the
    payload matches the in-process stats.  Same shape v20 gate reads."""
    from document_processor.code_intelligence import bitnet_shadow
    from document_processor.api import admin_llm_routes
    bitnet_shadow.reset_stats()
    for i in range(8):
        bitnet_shadow.record_shadow_outcome(
            request_id=f"rt-{i}",
            main_plan={"p": "a"},
            shadow_plan={"p": "a"},
            latency_ms=3000.0 + i * 50,
        )
    direct = bitnet_shadow.get_shadow_stats()
    endpoint = asyncio.run(admin_llm_routes.get_bitnet_shadow_stats(_user=None))
    # Endpoint must surface the same payload.
    assert endpoint["samples"] == direct["samples"] == 8
    assert endpoint["agreement_rate"] == direct["agreement_rate"]
    assert endpoint["p95_ms"] == direct["p95_ms"]
    bitnet_shadow.reset_stats()
