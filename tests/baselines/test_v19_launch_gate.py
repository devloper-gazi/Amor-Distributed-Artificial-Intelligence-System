"""Cycle G G6 — coverage for the v19 launch gate runner + GPU exporter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ─── v19 launch gate ───────────────────────────────────────────────


def test_v19_gate_thresholds_match_plan():
    """Plan-agent locked targets; regression test pins them in code
    so a future commit can't quietly relax v19 without leaving a
    trail."""
    from tools.run_v19_launch_gate import V19_GATE
    expected = {
        "sprint0_correctness_mean":     (">=",  8.1),
        "pipeline_median_latency_s":    ("<=", 95.0),
        "swebench_lite_25_resolved_pct": (">=", 16.0),
        "humaneval_plus_pass_at_1_pct": (">=", 80.0),
        "aider_polyglot_50_pass_pct":   (">=", 25.0),
        "mutation_score_pct":           (">=", 35.0),
    }
    actual = {t.name: (t.operator, t.target) for t in V19_GATE}
    assert actual == expected


def test_v19_gate_all_skipped_when_no_data(monkeypatch, tmp_path):
    """Fresh repo with no snapshots: every condition skipped + verdict
    INCOMPLETE (not FAIL — distinguish 'didn't run' from 'ran and
    failed')."""
    import tools.run_v19_launch_gate as gate
    monkeypatch.setattr(gate, "BASELINES_ROOT", tmp_path / "baselines")
    monkeypatch.setattr(gate, "EVAL_RUNS_ROOT", tmp_path / "eval_runs")
    card = gate.run_gate()
    assert card.verdict == "INCOMPLETE"
    assert all(c.status == "skipped" for c in card.conditions)
    assert len(card.conditions) == 6


def test_v19_gate_pass_when_all_thresholds_met(monkeypatch, tmp_path):
    """End-to-end PASS path with stub data files for every condition."""
    import tools.run_v19_launch_gate as gate
    baselines = tmp_path / "baselines"
    eval_runs = tmp_path / "eval_runs"
    monkeypatch.setattr(gate, "BASELINES_ROOT", baselines)
    monkeypatch.setattr(gate, "EVAL_RUNS_ROOT", eval_runs)
    baselines.mkdir()
    eval_runs.mkdir()
    (baselines / "sprint0_latest.json").write_text(json.dumps({
        "summary": {
            "correctness": {"mean": 8.30},
            "latency": {"median_s": 90.0},
        },
    }), encoding="utf-8")
    (eval_runs / "humaneval_plus").mkdir()
    (eval_runs / "humaneval_plus" / "latest.json").write_text(json.dumps({
        "summary": {"pass_at_1_percent": 82.0},
    }), encoding="utf-8")
    (eval_runs / "swebench_lite").mkdir()
    (eval_runs / "swebench_lite" / "latest.json").write_text(json.dumps({
        "summary": {"resolved_rate_percent": 18.0},
    }), encoding="utf-8")
    (eval_runs / "aider_polyglot").mkdir()
    (eval_runs / "aider_polyglot" / "latest.json").write_text(json.dumps({
        "summary": {"pass_rate_percent": 30.0},
    }), encoding="utf-8")
    (baselines / "mutation_score_latest.json").write_text(json.dumps({
        "mean_score": 0.40,
        "sessions_measured": 5,
    }), encoding="utf-8")

    card = gate.run_gate()
    assert card.verdict == "PASS", [c.to_dict() for c in card.conditions]
    assert all(c.status == "pass" for c in card.conditions)


def test_v19_gate_fail_when_one_threshold_missed(monkeypatch, tmp_path):
    """A single FAIL flips the verdict, even with 5 PASS."""
    import tools.run_v19_launch_gate as gate
    baselines = tmp_path / "baselines"
    eval_runs = tmp_path / "eval_runs"
    monkeypatch.setattr(gate, "BASELINES_ROOT", baselines)
    monkeypatch.setattr(gate, "EVAL_RUNS_ROOT", eval_runs)
    baselines.mkdir()
    eval_runs.mkdir()
    # Latency UNDER target → fail
    (baselines / "sprint0_latest.json").write_text(json.dumps({
        "summary": {
            "correctness": {"mean": 8.30},
            "latency": {"median_s": 140.0},   # > 95s → fail
        },
    }), encoding="utf-8")
    (eval_runs / "humaneval_plus").mkdir()
    (eval_runs / "humaneval_plus" / "latest.json").write_text(json.dumps({
        "summary": {"pass_at_1_percent": 82.0},
    }), encoding="utf-8")
    (eval_runs / "swebench_lite").mkdir()
    (eval_runs / "swebench_lite" / "latest.json").write_text(json.dumps({
        "summary": {"resolved_rate_percent": 18.0},
    }), encoding="utf-8")
    (eval_runs / "aider_polyglot").mkdir()
    (eval_runs / "aider_polyglot" / "latest.json").write_text(json.dumps({
        "summary": {"pass_rate_percent": 30.0},
    }), encoding="utf-8")
    (baselines / "mutation_score_latest.json").write_text(json.dumps({
        "mean_score": 0.40,
    }), encoding="utf-8")

    card = gate.run_gate()
    assert card.verdict == "FAIL"
    latency = next(c for c in card.conditions if c.name == "pipeline_median_latency_s")
    assert latency.status == "fail"
    assert latency.measured == 140.0


def test_v19_gate_falls_back_to_fraction_when_percent_absent(monkeypatch, tmp_path):
    """HumanEval+ snapshot stores pass_at_1 as a fraction (0.78); the
    gate must scale to percent for threshold comparison."""
    import tools.run_v19_launch_gate as gate
    eval_runs = tmp_path / "eval_runs"
    monkeypatch.setattr(gate, "EVAL_RUNS_ROOT", eval_runs)
    monkeypatch.setattr(gate, "BASELINES_ROOT", tmp_path / "baselines")
    (eval_runs / "humaneval_plus").mkdir(parents=True)
    (eval_runs / "humaneval_plus" / "latest.json").write_text(json.dumps({
        "summary": {"pass_at_1": 0.82},  # NO _percent suffix
    }), encoding="utf-8")
    card = gate.run_gate()
    he = next(c for c in card.conditions if c.name == "humaneval_plus_pass_at_1_pct")
    assert he.measured == 82.0
    assert he.status == "pass"


def test_v19_gate_persist_scorecard_writes_file(monkeypatch, tmp_path):
    """Verify the scorecard JSON makes it to disk + has the expected
    shape (verdict + conditions list).  Future operators rely on
    these files being archive-able."""
    import tools.run_v19_launch_gate as gate
    monkeypatch.setattr(gate, "BASELINES_ROOT", tmp_path)
    monkeypatch.setattr(gate, "EVAL_RUNS_ROOT", tmp_path / "evals")
    monkeypatch.setattr(gate, "SCORECARD_ROOT", tmp_path / "scorecards")
    card = gate.run_gate()
    out = gate.persist_scorecard(card, out_root=tmp_path / "scorecards")
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "verdict" in data
    assert isinstance(data["conditions"], list)
    assert len(data["conditions"]) == 6


# ─── nvidia-smi exporter ───────────────────────────────────────────


def test_nvidia_smi_parse_csv_basic():
    from monitoring.nvidia_smi_exporter import parse_nvidia_smi_csv
    csv_text = "0, NVIDIA RTX 4060 Laptop, 4096, 4096, 8192, 50, 30, 65, 75.5\n"
    samples = parse_nvidia_smi_csv(csv_text)
    assert len(samples) == 1
    s = samples[0]
    assert s.index == 0
    assert s.name == "NVIDIA RTX 4060 Laptop"
    assert s.memory_used_mb == 4096
    assert s.memory_total_mb == 8192
    assert s.utilization_gpu_pct == 50
    assert s.temperature_c == 65
    assert s.power_draw_w == 75.5


def test_nvidia_smi_parse_handles_na_fields():
    """Some GPUs/drivers report `[N/A]` for unsupported fields.  Map
    to 0.0 instead of crashing the whole sample."""
    from monitoring.nvidia_smi_exporter import parse_nvidia_smi_csv
    csv_text = "0, GPU, 100, 100, 200, [N/A], 30, 50, Not Supported\n"
    samples = parse_nvidia_smi_csv(csv_text)
    assert len(samples) == 1
    assert samples[0].utilization_gpu_pct == 0.0
    assert samples[0].power_draw_w == 0.0


def test_nvidia_smi_parse_skips_malformed_rows():
    """A row missing fields gets dropped (don't poison the whole
    poll because one row is incomplete)."""
    from monitoring.nvidia_smi_exporter import parse_nvidia_smi_csv
    csv_text = (
        "0, GPU, 100, 100, 200, 50, 30, 65, 75\n"
        "incomplete row\n"
        "1, GPU2, 200, 200, 400, 60, 40, 70, 85\n"
    )
    samples = parse_nvidia_smi_csv(csv_text)
    assert len(samples) == 2
    assert {s.index for s in samples} == {0, 1}


def test_nvidia_smi_parse_handles_multiple_gpus():
    from monitoring.nvidia_smi_exporter import parse_nvidia_smi_csv
    csv_text = (
        "0, GPU0, 100, 100, 200, 50, 30, 65, 75\n"
        "1, GPU1, 200, 200, 400, 60, 40, 70, 85\n"
    )
    samples = parse_nvidia_smi_csv(csv_text)
    assert len(samples) == 2


def test_nvidia_smi_render_metrics_includes_all_gauges():
    from monitoring.nvidia_smi_exporter import GPUSample, render_metrics
    s = GPUSample(
        index=0, name="GPU0",
        memory_used_mb=1024, memory_free_mb=7168, memory_total_mb=8192,
        utilization_gpu_pct=42, utilization_memory_pct=10,
        temperature_c=55, power_draw_w=65.5,
    )
    text = render_metrics([s], poll_failures=3, poll_duration_s=0.045)
    # Every metric family present
    for name in (
        "amor_gpu_memory_used_mb",
        "amor_gpu_memory_free_mb",
        "amor_gpu_memory_total_mb",
        "amor_gpu_utilization_pct",
        "amor_gpu_memory_utilization_pct",
        "amor_gpu_temperature_c",
        "amor_gpu_power_draw_w",
        "amor_gpu_poll_failures_total",
        "amor_gpu_poll_duration_seconds",
    ):
        assert name in text
    # Counter value
    assert "amor_gpu_poll_failures_total 3" in text
    # Per-GPU label
    assert 'index="0"' in text


def test_nvidia_smi_render_metrics_empty_samples_still_includes_meta_counters():
    """No GPU available → still emit poll_failures + poll_duration so
    operators see the exporter's own health on the dashboard."""
    from monitoring.nvidia_smi_exporter import render_metrics
    text = render_metrics([], poll_failures=10, poll_duration_s=0.0)
    assert "amor_gpu_poll_failures_total 10" in text
    assert "amor_gpu_poll_duration_seconds 0.0" in text
    # No per-GPU rows
    assert 'index="' not in text


def test_nvidia_smi_available_false_when_binary_missing(monkeypatch):
    from monitoring import nvidia_smi_exporter as exporter
    monkeypatch.setattr(exporter.shutil, "which", lambda name: None)
    assert exporter.nvidia_smi_available() is False


def test_nvidia_smi_available_true_when_query_succeeds(monkeypatch):
    from monitoring import nvidia_smi_exporter as exporter
    monkeypatch.setattr(exporter.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    class FakeRun:
        returncode = 0
        stdout = b"0\n"
    monkeypatch.setattr(
        exporter.subprocess, "run", lambda *a, **k: FakeRun(),
    )
    assert exporter.nvidia_smi_available() is True


def test_nvidia_smi_poll_gpus_returns_empty_on_failure(monkeypatch):
    from monitoring import nvidia_smi_exporter as exporter

    class FailedRun:
        returncode = 1
        stdout = b""
    monkeypatch.setattr(
        exporter.subprocess, "run", lambda *a, **k: FailedRun(),
    )
    assert exporter.poll_gpus() == []
