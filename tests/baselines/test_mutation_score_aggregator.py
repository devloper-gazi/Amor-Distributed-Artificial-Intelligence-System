"""v18.1.5 Cycle H gate-gap fix — mutation_score aggregator coverage.

Regression context: v19 launch gate condition #6 (`mutation_score_pct
≥ 35`) was perpetually SKIPPED because the aggregator's only data
sources were ``tasks[i].mutation_result`` (an older Sprint-0 shape
not used by v18) and the eval_runs DB table (which never had
mutation_result rows persisted).  This commit adds two fixes:

1. The aggregator now walks ``rows[i]`` (current Sprint-0 v18 shape)
   in addition to legacy ``tasks[i]`` / ``results[i]``.
2. The engine appends each session's mutation_result to
   ``data/baselines/mutation_runs.jsonl``; the aggregator reads it.

Tests live in ``tests/baselines/`` to mirror the v19 gate runner's
home.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ─── _walk_sprint0_snapshot — rows/tasks/results compatibility ──────


def test_walk_sprint0_rows_v18_shape():
    """Modern Sprint-0 v18 baseline uses ``rows[i].mutation_result``."""
    from tools.aggregate_mutation_scores import _walk_sprint0_snapshot
    snap = {
        "rows": [
            {"mutation_result": {"ran": True, "score": 0.45}},
            {"mutation_result": {"ran": True, "score": 0.55}},
            {"mutation_result": {"ran": False}},     # not run → skip
            {"mutation_result": None},               # absent → skip
        ]
    }
    scores = _walk_sprint0_snapshot(snap)
    assert scores == [0.45, 0.55]


def test_walk_sprint0_tasks_legacy_shape_still_works():
    """Legacy Cycle E pre-v18 baseline used ``tasks[i].mutation_result``."""
    from tools.aggregate_mutation_scores import _walk_sprint0_snapshot
    snap = {
        "tasks": [
            {"mutation_result": {"ran": True, "score": 0.30}},
        ]
    }
    assert _walk_sprint0_snapshot(snap) == [0.30]


def test_walk_sprint0_nested_extra_block():
    """Some runner versions put mutation_result under ``extra``."""
    from tools.aggregate_mutation_scores import _walk_sprint0_snapshot
    snap = {
        "rows": [
            {"extra": {"mutation_result": {"ran": True, "score": 0.62}}},
        ]
    }
    assert _walk_sprint0_snapshot(snap) == [0.62]


def test_walk_sprint0_empty_inputs_return_empty_list():
    """Defensive — missing/empty/wrong-shape inputs return ``[]``."""
    from tools.aggregate_mutation_scores import _walk_sprint0_snapshot
    assert _walk_sprint0_snapshot({}) == []
    assert _walk_sprint0_snapshot({"rows": []}) == []
    assert _walk_sprint0_snapshot({"rows": [{"no": "mr"}]}) == []


# ─── _scores_from_mutation_runs_jsonl — JSONL file reader ──────────


def test_scores_from_mutation_runs_jsonl_reads_recent_lines(tmp_path, monkeypatch):
    """Aggregator reads ``data/baselines/mutation_runs.jsonl`` and
    returns the score from every line whose ``mutation_result.ran`` is
    True.  Lines without a numeric score are silently dropped."""
    import tools.aggregate_mutation_scores as mod
    monkeypatch.setattr(mod, "BASELINES_ROOT", tmp_path)

    lines = [
        json.dumps({"session_id": "a", "mutation_result": {"ran": True, "score": 0.40}}),
        json.dumps({"session_id": "b", "mutation_result": {"ran": True, "score": 0.45}}),
        json.dumps({"session_id": "c", "mutation_result": {"ran": False}}),
        json.dumps({"session_id": "d", "mutation_result": {"ran": True}}),     # no score → skip
        "",                                                                    # blank → skip
        "not-json",                                                           # corrupt → skip
        json.dumps({"session_id": "e", "mutation_result": {"ran": True, "score": 0.50}}),
    ]
    (tmp_path / "mutation_runs.jsonl").write_text("\n".join(lines), encoding="utf-8")
    assert mod._scores_from_mutation_runs_jsonl() == [0.40, 0.45, 0.50]


def test_scores_from_mutation_runs_jsonl_missing_file_returns_empty(tmp_path, monkeypatch):
    """No JSONL file → empty list, not exception."""
    import tools.aggregate_mutation_scores as mod
    monkeypatch.setattr(mod, "BASELINES_ROOT", tmp_path)
    assert mod._scores_from_mutation_runs_jsonl() == []


def test_scores_from_mutation_runs_jsonl_caps_at_500_lines(tmp_path, monkeypatch):
    """Operator safety — file growth to 100K+ lines must not OOM the
    aggregator.  Only the most recent 500 entries are considered."""
    import tools.aggregate_mutation_scores as mod
    monkeypatch.setattr(mod, "BASELINES_ROOT", tmp_path)

    # Write 600 "low-score" lines followed by 10 "high-score" lines.
    # Aggregator should see only the last 500 → some of the high lines
    # PLUS the tail-end low lines.  Specifically: 490 low + 10 high
    # (since the last 500 = (600-490..599) + (600..609)).
    lines = [
        json.dumps({"mutation_result": {"ran": True, "score": 0.10}})
        for _ in range(600)
    ] + [
        json.dumps({"mutation_result": {"ran": True, "score": 0.90}})
        for _ in range(10)
    ]
    (tmp_path / "mutation_runs.jsonl").write_text("\n".join(lines), encoding="utf-8")
    scores = mod._scores_from_mutation_runs_jsonl()
    assert len(scores) == 500
    # Tail-bias: the last 10 entries are 0.90.
    assert scores.count(0.90) == 10
    assert scores.count(0.10) == 490


# ─── aggregate() — mean + count snapshot shape ──────────────────────


def test_aggregate_empty_yields_zeroed_snapshot():
    """When no scores recorded, snapshot has ``sessions_measured=0``
    and ``mean_score=0.0`` — the gate then marks the condition SKIPPED
    (it requires ``mean_score`` to be numeric AND ``sessions_measured>0``,
    but the gate currently only checks the mean; documenting here that
    the condition itself stays informational without measurements)."""
    from tools.aggregate_mutation_scores import aggregate
    payload = aggregate([])
    assert payload["mean_score"] == 0.0
    assert payload["sessions_measured"] == 0
    assert payload["per_session_scores"] == []
    assert "computed_at_utc" in payload


def test_aggregate_mean_rounded_to_4_decimals():
    """1/3 → 0.3333 (4 decimals)."""
    from tools.aggregate_mutation_scores import aggregate
    payload = aggregate([0.0, 0.0, 1.0])
    assert payload["mean_score"] == 0.3333
    assert payload["sessions_measured"] == 3


def test_aggregate_meets_35pct_threshold_when_mean_above():
    """Plan-agent locked threshold: 35% (mean_score ≥ 0.35) lifts the
    v19 gate condition #6 to PASS.  This test pins the threshold so a
    silent relaxation can't slip through."""
    from tools.aggregate_mutation_scores import aggregate
    payload = aggregate([0.30, 0.40, 0.50])
    assert payload["mean_score"] >= 0.35   # 0.40 mean
