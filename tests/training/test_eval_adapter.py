"""
Cycle C Sprint 6 Day 3 — adapter eval-delta diff tests.

The diff function is pure (input: two JSON dicts, output: a third);
this is its acceptance suite.  The lora-adapters HTTP toggle and
the live corpus rerun are exercised by the live smoke probe in
``docs/sprint6_results.md`` (operator-driven).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.training.eval_adapter import diff_runs


def _run(prompts):
    return {"prompts": prompts}


def _p(pid: str, *, correctness: float | None, completeness: float | None, latency: float | None):
    payload = {"id": pid, "metrics": {}}
    if correctness is not None and completeness is not None:
        payload["judge"] = {"correctness": correctness, "completeness": completeness}
    if latency is not None:
        payload["metrics"]["e2e_wall_clock_ms"] = latency
    return payload


def test_diff_neutral_no_change():
    before = _run([_p("a", correctness=4, completeness=4, latency=1000)])
    after = _run([_p("a", correctness=4, completeness=4, latency=1000)])
    rep = diff_runs(before, after)
    s = rep["summary"]
    assert s["mean_judge_delta"] == 0
    assert s["worst_judge_delta"] == 0
    assert s["p50_latency_pct"] == 0
    assert s["promote_ok"] is True


def test_diff_uniform_improvement_promotes():
    before = _run([
        _p("a", correctness=3, completeness=3, latency=1000),
        _p("b", correctness=3, completeness=3, latency=1000),
    ])
    after = _run([
        _p("a", correctness=4, completeness=4, latency=950),
        _p("b", correctness=4, completeness=4, latency=900),
    ])
    rep = diff_runs(before, after)
    s = rep["summary"]
    assert s["mean_judge_delta"] == pytest.approx(1.0)
    assert s["worst_judge_delta"] == pytest.approx(1.0)
    assert s["p50_latency_pct"] < 0  # got faster
    assert s["promote_ok"] is True


def test_diff_one_big_regression_blocks_promote():
    """Plan caveat: ``worst_judge_delta`` must stay ≥ -1.  A single
    -2 prompt still vetoes promote even when the mean is positive."""
    before = _run([
        _p("a", correctness=4, completeness=4, latency=1000),
        _p("b", correctness=4, completeness=4, latency=1000),
        _p("c", correctness=3, completeness=3, latency=1000),
    ])
    after = _run([
        _p("a", correctness=5, completeness=5, latency=1000),
        _p("b", correctness=5, completeness=5, latency=1000),
        _p("c", correctness=1, completeness=1, latency=1000),
    ])
    rep = diff_runs(before, after)
    s = rep["summary"]
    # Two +1's and one -2 → mean is exactly 0, which clears the
    # mean-gate, but the worst per-prompt delta of -2 vetoes promote.
    assert s["mean_judge_delta"] == pytest.approx(0.0)
    assert s["worst_judge_delta"] == pytest.approx(-2.0)
    assert s["promote_ok"] is False


def test_diff_latency_blowout_blocks_promote():
    """A 50% slower run is a regression even with judge improvements."""
    before = _run([
        _p("a", correctness=3, completeness=3, latency=1000),
        _p("b", correctness=3, completeness=3, latency=1000),
    ])
    after = _run([
        _p("a", correctness=4, completeness=4, latency=1500),
        _p("b", correctness=4, completeness=4, latency=1600),
    ])
    rep = diff_runs(before, after)
    assert rep["summary"]["p50_latency_pct"] > 20.0
    assert rep["summary"]["promote_ok"] is False


def test_diff_handles_missing_judge_fields():
    """A prompt with no ``judge`` block must NOT crash; it just
    contributes no delta to the summary."""
    before = _run([_p("a", correctness=None, completeness=None, latency=1000)])
    after = _run([_p("a", correctness=4, completeness=4, latency=1000)])
    rep = diff_runs(before, after)
    assert rep["per_prompt"][0]["judge_delta"] is None
    # No judge data → summary is None and the gate refuses to promote.
    assert rep["summary"]["mean_judge_delta"] is None
    assert rep["summary"]["promote_ok"] is False


def test_diff_emits_per_prompt_rows():
    before = _run([
        _p("a", correctness=4, completeness=4, latency=900),
        _p("b", correctness=3, completeness=3, latency=800),
    ])
    after = _run([
        _p("a", correctness=4, completeness=5, latency=1000),
        _p("b", correctness=3, completeness=3, latency=800),
    ])
    rep = diff_runs(before, after)
    by_id = {p["id"]: p for p in rep["per_prompt"]}
    assert by_id["a"]["judge_delta"] == pytest.approx(0.5)
    assert by_id["a"]["latency_delta_pct"] == pytest.approx((1000 - 900) / 900 * 100)
    assert by_id["b"]["judge_delta"] == pytest.approx(0.0)


def test_diff_includes_final_score_shape():
    """When the judge persists ``final_score`` directly (Sprint 0
    Day 3 alternative shape), use it as-is."""
    before = {"prompts": [{"id": "a", "judge": {"final_score": 6.0}, "metrics": {"e2e_wall_clock_ms": 1000}}]}
    after = {"prompts": [{"id": "a", "judge": {"final_score": 7.0}, "metrics": {"e2e_wall_clock_ms": 1000}}]}
    rep = diff_runs(before, after)
    assert rep["per_prompt"][0]["judge_before"] == 6.0
    assert rep["per_prompt"][0]["judge_after"] == 7.0
    assert rep["summary"]["mean_judge_delta"] == 1.0
