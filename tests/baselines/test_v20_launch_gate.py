"""Cycle H Phase A close-out — v20.0.0 launch gate runner coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ─── Threshold table (Plan-agent locked) ───────────────────────────


def test_v20_thresholds_match_plan():
    """Plan-agent locked targets; regression test pins them so a
    future commit can't quietly relax v20 without leaving a trail."""
    from tools.run_v20_launch_gate import V20_GATE
    expected = {
        "substrate_count":                          (">=",    3.0),
        "bitnet_agreement_rate_pct":                (">=",   85.0),
        "bitnet_p95_latency_ms":                    ("<=", 6000.0),
        "grpo_property_failure_reduction_pct":      (">=",   10.0),
        "lazygraphrag_ndcg_uplift_pct":             (">=",   15.0),
        "vram_peak_gb":                             ("<=",    7.2),
    }
    actual = {t.name: (t.operator, t.target) for t in V20_GATE}
    assert actual == expected


def test_v20_gate_all_skipped_returns_incomplete(monkeypatch, tmp_path):
    """Fresh repo (no telemetry snapshots) → every condition skipped
    → verdict INCOMPLETE, NOT FAIL (distinguish 'didn't run' from
    'ran and failed')."""
    import tools.run_v20_launch_gate as gate
    # Redirect ALL roots to an empty tmp_path
    monkeypatch.setattr(gate, "BASELINES_ROOT", tmp_path / "baselines")
    monkeypatch.setattr(gate, "EVAL_RUNS_ROOT", tmp_path / "eval_runs")
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)  # so substrate-count fallback finds nothing

    # Also kill the bitnet_shadow live stats (it might have samples from earlier tests)
    try:
        from document_processor.code_intelligence import bitnet_shadow
        bitnet_shadow.reset_stats()
    except Exception:
        pass

    card = gate.run_gate()
    assert card.verdict == "INCOMPLETE"
    statuses = {c.status for c in card.conditions}
    assert "fail" not in statuses


def test_v20_gate_pass_when_all_thresholds_met(monkeypatch, tmp_path):
    """End-to-end PASS path with stub data files for every condition."""
    import tools.run_v20_launch_gate as gate
    baselines = tmp_path / "baselines"
    baselines.mkdir()
    monkeypatch.setattr(gate, "BASELINES_ROOT", baselines)
    monkeypatch.setattr(gate, "EVAL_RUNS_ROOT", tmp_path / "evals")
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)

    # Condition 1: 3 substrates declared
    (baselines / "substrate_count_latest.json").write_text(
        json.dumps({"active_substrates": 3}), encoding="utf-8",
    )

    # Conditions 2 + 3: BitNet shadow stats — use the live bitnet_shadow
    # since the resolver tries that first.
    from document_processor.code_intelligence import bitnet_shadow
    bitnet_shadow.reset_stats()
    for i in range(180):
        bitnet_shadow.record_shadow_outcome(
            f"req-{i}", {"p": "a"}, {"p": "a"}, latency_ms=4000,
        )
    for i in range(20):
        bitnet_shadow.record_shadow_outcome(
            f"req-fail-{i}", {"p": "a"}, {"p": "b"}, latency_ms=5000,
        )

    # Condition 4: GRPO uplift with statistical rigor
    (baselines / "grpo_vs_orpo_latest.json").write_text(
        json.dumps({
            "property_failure_reduction_pct": 12.5,
            "p_value": 0.02,
            "seeds": 3,
        }),
        encoding="utf-8",
    )

    # Condition 5: LazyGraphRAG benchmark
    (baselines / "lazygraphrag_bench_latest.json").write_text(
        json.dumps({"ndcg_uplift_pct": 18.3}),
        encoding="utf-8",
    )

    # Condition 6: VRAM envelope
    (baselines / "vram_envelope_latest.json").write_text(
        json.dumps({"peak_vram_mb": 7000}),     # 6.84 GB ≤ 7.2 GB
        encoding="utf-8",
    )

    card = gate.run_gate()
    assert card.verdict == "PASS", [c.to_dict() for c in card.conditions]
    bitnet_shadow.reset_stats()


def test_v20_gate_fail_when_one_threshold_missed(monkeypatch, tmp_path):
    """A single FAIL flips the verdict, even with 5 PASS."""
    import tools.run_v20_launch_gate as gate
    baselines = tmp_path / "baselines"
    baselines.mkdir()
    monkeypatch.setattr(gate, "BASELINES_ROOT", baselines)
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)

    (baselines / "substrate_count_latest.json").write_text(
        json.dumps({"active_substrates": 3}), encoding="utf-8",
    )

    from document_processor.code_intelligence import bitnet_shadow
    bitnet_shadow.reset_stats()
    for i in range(200):
        bitnet_shadow.record_shadow_outcome(
            f"req-{i}", {"p": "a"}, {"p": "a"}, latency_ms=4000,
        )

    (baselines / "grpo_vs_orpo_latest.json").write_text(
        json.dumps({
            "property_failure_reduction_pct": 12.5,
            "p_value": 0.02,
            "seeds": 3,
        }),
        encoding="utf-8",
    )
    (baselines / "lazygraphrag_bench_latest.json").write_text(
        json.dumps({"ndcg_uplift_pct": 18.3}),
        encoding="utf-8",
    )
    # VRAM OVER threshold → fail
    (baselines / "vram_envelope_latest.json").write_text(
        json.dumps({"peak_vram_mb": 8000}),     # 7.81 GB > 7.2 GB
        encoding="utf-8",
    )

    card = gate.run_gate()
    assert card.verdict == "FAIL"
    vram_cond = next(c for c in card.conditions if c.name == "vram_peak_gb")
    assert vram_cond.status == "fail"
    bitnet_shadow.reset_stats()


def test_v20_grpo_requires_statistical_rigor(monkeypatch, tmp_path):
    """Plan-agent guardrail: ≥10% reduction is NECESSARY but the
    condition ALSO requires n≥3 seeds + p<0.05.  Without those,
    the condition fails even with high measured uplift."""
    import tools.run_v20_launch_gate as gate
    baselines = tmp_path / "baselines"
    baselines.mkdir()
    monkeypatch.setattr(gate, "BASELINES_ROOT", baselines)

    # 15% reduction (above threshold) but only 1 seed + no p-value
    (baselines / "grpo_vs_orpo_latest.json").write_text(
        json.dumps({
            "property_failure_reduction_pct": 15.0,
            "seeds": 1,
        }),
        encoding="utf-8",
    )

    cond = gate._condition_grpo_uplift()
    assert cond.measured == 15.0
    assert cond.status == "fail"
    assert "statistical rigor" in cond.note


def test_v20_bitnet_blocked_when_samples_below_200(monkeypatch, tmp_path):
    """Plan-agent locked: shadow window must accumulate ≥200 samples
    before promotion can be decided.  Statistical validity check."""
    import tools.run_v20_launch_gate as gate
    monkeypatch.setattr(gate, "BASELINES_ROOT", tmp_path)

    from document_processor.code_intelligence import bitnet_shadow
    bitnet_shadow.reset_stats()
    for i in range(50):  # only 50 samples
        bitnet_shadow.record_shadow_outcome(
            f"req-{i}", {"p": "a"}, {"p": "a"}, latency_ms=4000,
        )

    cond = gate._condition_bitnet_agreement()
    assert cond.status == "skipped"
    assert "<200 samples" in cond.note
    bitnet_shadow.reset_stats()


def test_v20_vram_reads_peak_mb_and_converts_to_gb(monkeypatch, tmp_path):
    """Conversion sanity: 7373 MB = ~7.2 GB.  Off-by-one in MB → GB
    arithmetic would silently misreport gate."""
    import tools.run_v20_launch_gate as gate
    baselines = tmp_path / "baselines"
    baselines.mkdir()
    monkeypatch.setattr(gate, "BASELINES_ROOT", baselines)
    (baselines / "vram_envelope_latest.json").write_text(
        json.dumps({"peak_vram_mb": 7373}),     # 7373/1024 = 7.2002 GB
        encoding="utf-8",
    )
    cond = gate._condition_vram_envelope()
    assert cond.measured is not None
    # 7373/1024 = 7.20 GB rounded to 2 decimals
    assert abs(cond.measured - 7.20) < 0.01
    # Strictly above 7.2 by 0.001 → fail
    assert cond.status == "fail"


def test_v20_scorecard_persist_writes_json(monkeypatch, tmp_path):
    """The scorecard JSON must be archive-able for later audit."""
    import tools.run_v20_launch_gate as gate
    monkeypatch.setattr(gate, "BASELINES_ROOT", tmp_path)
    monkeypatch.setattr(gate, "EVAL_RUNS_ROOT", tmp_path / "evals")
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    card = gate.run_gate()
    out = gate.persist_scorecard(card, out_root=tmp_path / "scorecards")
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "verdict" in data
    assert isinstance(data["conditions"], list)
    assert len(data["conditions"]) == 6


def test_v20_telemetry_vram_envelope_metric_included(monkeypatch):
    """Cycle H.0.4 — render_metrics output must include
    `amor_gpu_vram_envelope_mb` and `amor_gpu_vram_envelope_current_mb`
    gauges so the v20 gate's continuous monitoring path can read them.
    """
    from monitoring.nvidia_smi_exporter import (
        GPUSample, render_metrics, reset_envelope_state,
    )
    reset_envelope_state()
    samples = [
        GPUSample(
            index=0, name="RTX 4060 Laptop",
            memory_used_mb=4096.0, memory_free_mb=4096.0, memory_total_mb=8192.0,
            utilization_gpu_pct=70, utilization_memory_pct=50,
            temperature_c=68, power_draw_w=40.0,
        ),
    ]
    out = render_metrics(samples, poll_failures=0, poll_duration_s=0.05)
    assert "amor_gpu_vram_envelope_mb" in out
    assert "amor_gpu_vram_envelope_current_mb" in out
    # Rolling peak after one sample == that sample's used.
    assert "amor_gpu_vram_envelope_mb 4096.0" in out
    assert "amor_gpu_vram_envelope_current_mb 4096.0" in out


def test_v20_telemetry_vram_envelope_persists_peak_across_polls(monkeypatch):
    """The rolling peak survives a poll where memory dropped — the
    envelope reflects the WORST case since exporter boot, not the
    last sample.  That's what gate condition #5 (≤7.2 GB) measures."""
    from monitoring.nvidia_smi_exporter import (
        GPUSample, render_metrics, reset_envelope_state,
    )
    reset_envelope_state()
    high = [GPUSample(
        index=0, name="GPU",
        memory_used_mb=7500.0, memory_free_mb=692.0, memory_total_mb=8192.0,
        utilization_gpu_pct=99, utilization_memory_pct=70,
        temperature_c=80, power_draw_w=80.0,
    )]
    low = [GPUSample(
        index=0, name="GPU",
        memory_used_mb=2000.0, memory_free_mb=6192.0, memory_total_mb=8192.0,
        utilization_gpu_pct=20, utilization_memory_pct=10,
        temperature_c=60, power_draw_w=20.0,
    )]
    render_metrics(high, poll_failures=0, poll_duration_s=0.05)
    out = render_metrics(low, poll_failures=0, poll_duration_s=0.05)
    # Peak should remain 7500 even though current dropped to 2000.
    assert "amor_gpu_vram_envelope_mb 7500.0" in out
    assert "amor_gpu_vram_envelope_current_mb 2000.0" in out
    # reset clears the peak.
    reset_envelope_state()
    out = render_metrics(low, poll_failures=0, poll_duration_s=0.05)
    assert "amor_gpu_vram_envelope_mb 2000.0" in out


def test_v20_telemetry_shadow_stats_endpoint_returns_bitnet_payload():
    """Cycle H.0.4 — `GET /api/admin/llm/bitnet/shadow_stats` must surface
    the same payload `bitnet_shadow.get_shadow_stats()` returns to the
    v20 gate runner.  Operator probes it via curl during the 14-day
    shadow window."""
    from document_processor.api import admin_llm_routes
    from document_processor.code_intelligence import bitnet_shadow
    import asyncio

    bitnet_shadow.reset_stats()
    for i in range(10):
        bitnet_shadow.record_shadow_outcome(
            f"req-{i}", {"p": "a"}, {"p": "a"}, latency_ms=3500,
        )

    # The endpoint is wrapped with Depends(get_current_user); call it
    # bare to verify the underlying logic.  In production, FastAPI's
    # router resolves the dep injection.
    payload = asyncio.run(admin_llm_routes.get_bitnet_shadow_stats(_user=None))
    assert payload["samples"] == 10
    # bitnet_shadow returns `agreement_rate` (0-1 fraction) + `p95_ms`
    # rather than the percent/latency-suffixed names the gate runner
    # synthesises.  Verify the raw payload still carries them.
    assert "agreement_rate" in payload
    assert "p95_ms" in payload
    bitnet_shadow.reset_stats()


def test_v20_substrate_count_fallback_detects_substrates(monkeypatch, tmp_path):
    """When the telemetry snapshot is missing, fallback infers from
    presence of `compose/llama-swap/config.yaml` + `models/bitnet/`
    + `models/lfm2/` directories."""
    import tools.run_v20_launch_gate as gate
    monkeypatch.setattr(gate, "BASELINES_ROOT", tmp_path / "baselines")
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)

    # Create the marker paths
    (tmp_path / "compose" / "llama-swap").mkdir(parents=True)
    (tmp_path / "compose" / "llama-swap" / "config.yaml").write_text("# stub", encoding="utf-8")
    (tmp_path / "models" / "bitnet").mkdir(parents=True)
    (tmp_path / "models" / "lfm2").mkdir(parents=True)

    cond = gate._condition_substrate_count()
    assert cond.measured == 3.0
    assert cond.status == "pass"
    assert "inferred from" in cond.note
