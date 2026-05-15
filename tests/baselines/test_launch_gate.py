"""Cycle F Sprint 6 — tests for tools/run_v18_launch_gate.py.

Covers condition evaluation, the scorecard structure, the verdict
roll-up, and the artefact persistence path.  No external dependencies
exercised — the eval-runner integration is a docs-only "trigger via
admin route" until Sprint 6's async pipeline work lands.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def gate_module():
    """Load the gate runner as a module so we can call its
    condition functions directly."""

    src = REPO_ROOT / "tools" / "run_v18_launch_gate.py"
    assert src.is_file()
    spec = importlib.util.spec_from_file_location("v18_launch_gate_test", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["v18_launch_gate_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── Helpers to build a synthetic sprint0_latest payload ───────────


def _make_sprint0(rows) -> dict:
    return {
        "version": "test",
        "rows": rows,
    }


def _row(
    pid: str,
    mode: str,
    c: float | None,
    k: float | None,
    wall_ms: float = 60_000,
) -> dict:
    judge_score: dict = {}
    if c is not None:
        judge_score["correctness"] = c
    if k is not None:
        judge_score["completeness"] = k
    return {
        "prompt_id": pid,
        "mode": mode,
        "judge_score": judge_score or {"error": "no score"},
        "metrics": {"wall_clock_ms": wall_ms},
    }


# ─── Condition 1: correctness mean ──────────────────────────────────


def test_correctness_passes_when_above_threshold(gate_module):
    payload = _make_sprint0([
        _row("a", "Build", 4, 4),
        _row("b", "Research", 4, 4),
        _row("c", "Thinking", 5, 5),
    ])
    # mean correctness = 4.33; ×2 = 8.66 → above 7.2
    res = gate_module.condition_sprint0_correctness(payload)
    assert res.status == "pass"
    assert res.measured == 8.67


def test_correctness_fails_when_below_threshold(gate_module):
    payload = _make_sprint0([
        _row("a", "Build", 2, 2),
        _row("b", "Research", 3, 3),
    ])
    # mean = 2.5; ×2 = 5.0 → below 7.2
    res = gate_module.condition_sprint0_correctness(payload)
    assert res.status == "fail"
    assert res.measured == 5.0


def test_correctness_skipped_when_no_payload(gate_module):
    res = gate_module.condition_sprint0_correctness(None)
    assert res.status == "skipped"
    assert res.measured is None


def test_correctness_skipped_when_no_judged_rows(gate_module):
    payload = _make_sprint0([
        # All errored — no correctness fields.
        {"prompt_id": "a", "mode": "Build",
         "judge_score": {"error": "pass-1 score missing"},
         "metrics": {"wall_clock_ms": 60_000}},
    ])
    res = gate_module.condition_sprint0_correctness(payload)
    assert res.status == "skipped"


# ─── Condition 3: per-mode floor ────────────────────────────────────


def test_per_mode_floor_passes_when_lowest_mode_above_threshold(gate_module):
    payload = _make_sprint0([
        _row("a", "Build", 4, 4),         # 8.0
        _row("b", "Research", 4, 4),      # 8.0
        _row("c", "Thinking", 5, 5),      # 10.0
    ])
    res = gate_module.condition_sprint0_per_mode_floor(payload)
    assert res.status == "pass"
    assert res.measured == 8.0


def test_per_mode_floor_fails_when_one_mode_collapses(gate_module):
    payload = _make_sprint0([
        _row("a", "Build", 4, 4),         # 8.0
        _row("b", "Research", 1, 1),      # 2.0 — collapses
        _row("c", "Thinking", 5, 5),      # 10.0
    ])
    res = gate_module.condition_sprint0_per_mode_floor(payload)
    assert res.status == "fail"
    assert res.measured == 2.0


# ─── Condition 4: pipeline median latency ───────────────────────────


def test_pipeline_latency_passes_under_ceiling(gate_module):
    payload = _make_sprint0([
        _row("a", "Build", 4, 4, wall_ms=30_000),
        _row("b", "Research", 4, 4, wall_ms=45_000),
        _row("c", "Thinking", 5, 5, wall_ms=60_000),
    ])
    res = gate_module.condition_pipeline_median_latency(payload)
    assert res.status == "pass"
    assert res.measured == 45.0


def test_pipeline_latency_fails_when_median_over_ceiling(gate_module):
    payload = _make_sprint0([
        _row("a", "Build", 4, 4, wall_ms=80_000),
        _row("b", "Research", 4, 4, wall_ms=100_000),
        _row("c", "Thinking", 5, 5, wall_ms=120_000),
    ])
    res = gate_module.condition_pipeline_median_latency(payload)
    assert res.status == "fail"
    assert res.measured == 100.0


# ─── Conditions 5 + 6: eval runner skips when no data ──────────────


def test_humaneval_plus_skipped_when_no_latest(gate_module, monkeypatch):
    monkeypatch.setattr(gate_module, "_latest_eval_run", lambda name: None)
    res = gate_module.condition_humaneval_plus(force_run=False, shallow=False)
    assert res.status == "skipped"


def test_humaneval_plus_passes_with_high_pass_rate(gate_module, monkeypatch):
    monkeypatch.setattr(
        gate_module, "_latest_eval_run",
        lambda name: {"summary": {"pass_at_1_percent": 75.5, "total": 50}},
    )
    res = gate_module.condition_humaneval_plus(force_run=False, shallow=False)
    assert res.status == "pass"
    assert res.measured == 75.5


def test_humaneval_plus_fails_when_below_threshold(gate_module, monkeypatch):
    monkeypatch.setattr(
        gate_module, "_latest_eval_run",
        lambda name: {"summary": {"pass_at_1_percent": 65.0, "total": 50}},
    )
    res = gate_module.condition_humaneval_plus(force_run=False, shallow=False)
    assert res.status == "fail"
    assert res.measured == 65.0


def test_humaneval_plus_shallow_skips(gate_module, monkeypatch):
    monkeypatch.setattr(
        gate_module, "_latest_eval_run",
        lambda name: {"summary": {"pass_at_1_percent": 90.0, "total": 50}},
    )
    res = gate_module.condition_humaneval_plus(force_run=False, shallow=True)
    assert res.status == "skipped"
    assert "shallow" in res.notes.lower()


def test_swebench_passes_with_high_resolved(gate_module, monkeypatch):
    monkeypatch.setattr(
        gate_module, "_latest_eval_run",
        lambda name: {"summary": {"resolved_rate_percent": 32.0, "total": 25}},
    )
    res = gate_module.condition_swebench_lite(force_run=False, shallow=False)
    assert res.status == "pass"


def test_swebench_fails_below_threshold(gate_module, monkeypatch):
    monkeypatch.setattr(
        gate_module, "_latest_eval_run",
        lambda name: {"summary": {"resolved_rate_percent": 20.0, "total": 25}},
    )
    res = gate_module.condition_swebench_lite(force_run=False, shallow=False)
    assert res.status == "fail"


# ─── Scorecard verdict roll-up ──────────────────────────────────────


def test_scorecard_pass_when_every_condition_passes(gate_module):
    """Roll-up: all-pass conditions → verdict = 'pass'."""

    card = gate_module.GateScorecard(timestamp_utc="2026-05-15T10:00:00Z")
    card.conditions.append(gate_module.ConditionResult(
        name="a", threshold=1.0, threshold_op=">=", measured=2.0, status="pass",
    ))
    card.conditions.append(gate_module.ConditionResult(
        name="b", threshold=1.0, threshold_op=">=", measured=3.0, status="pass",
    ))
    assert card.all_pass is True
    assert card.num_failed == 0
    assert card.num_skipped == 0


def test_scorecard_fail_when_any_condition_fails(gate_module):
    card = gate_module.GateScorecard(timestamp_utc="t")
    card.conditions.append(gate_module.ConditionResult(
        name="ok", threshold=1.0, threshold_op=">=", measured=2.0, status="pass",
    ))
    card.conditions.append(gate_module.ConditionResult(
        name="bad", threshold=1.0, threshold_op=">=", measured=0.5, status="fail",
    ))
    assert card.all_pass is False
    assert card.num_failed == 1


def test_scorecard_to_dict_round_trip(gate_module):
    card = gate_module.GateScorecard(timestamp_utc="t")
    card.conditions.append(gate_module.ConditionResult(
        name="x", threshold=1.0, threshold_op=">=", measured=2.0, status="pass",
    ))
    card.verdict = "pass"
    d = card.to_dict()
    assert d["verdict"] == "pass"
    assert d["num_passed"] == 1
    assert len(d["conditions"]) == 1
    assert d["conditions"][0]["name"] == "x"


# ─── Scorecard persistence ──────────────────────────────────────────


def test_persist_scorecard_writes_json(tmp_path, gate_module, monkeypatch):
    monkeypatch.setattr(gate_module, "BASELINES_DIR", tmp_path)
    card = gate_module.GateScorecard(timestamp_utc="2026-05-15T10:00:00Z")
    card.conditions.append(gate_module.ConditionResult(
        name="a", threshold=1.0, threshold_op=">=", measured=2.0, status="pass",
    ))
    card.verdict = "pass"
    out = gate_module.persist_scorecard(card)
    assert out.is_file()
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert parsed["verdict"] == "pass"


# ─── Thresholds match the plan file ─────────────────────────────────


def test_thresholds_pinned_to_plan(gate_module):
    """Guard against accidental loosening of the v18 launch gate."""

    t = gate_module.THRESHOLDS
    assert t["sprint0_correctness_mean"] == 7.2
    assert t["sprint0_completeness_mean"] == 7.2
    assert t["sprint0_per_mode_floor"] == 6.5
    assert t["humaneval_plus_pass_at_1"] == 72.0
    assert t["swebench_lite_resolved_rate"] == 28.0
    assert t["pipeline_median_latency_s"] == 75.0
    assert t["deliverable_rubric_pass_rate"] == 70.0
