"""Cycle K.2 — JEPA plan predictor coverage.

Tests focus on the acceptance decision logic + dataset loader + CLI
surface.  The real torch-based training is operator-only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ─── compute_jepa_report ────────────────────────────────────────────


def test_jepa_report_pass_when_val_loss_below_target():
    """val_loss < target_loss + no overfitting → promote."""
    from tools.training.jepa_plan_predictor import compute_jepa_report
    r = compute_jepa_report(
        train_loss=0.30, val_loss=0.38, target_loss=0.40, epochs_completed=20,
    )
    assert r.promotion_ready is True
    assert "≤ target" in r.note


def test_jepa_report_fail_when_val_loss_exceeds_target():
    """val_loss > target → ABORT regardless of train_loss."""
    from tools.training.jepa_plan_predictor import compute_jepa_report
    r = compute_jepa_report(
        train_loss=0.30, val_loss=0.45, target_loss=0.40, epochs_completed=20,
    )
    assert r.promotion_ready is False
    assert "EXCEEDS" in r.note


def test_jepa_report_fail_on_extreme_overfitting():
    """val_loss / train_loss > 1.5 → overfitting ABORT even if both
    are individually below target.  Plan-agent locked: never promote
    a predictor that won't generalise."""
    from tools.training.jepa_plan_predictor import compute_jepa_report
    r = compute_jepa_report(
        train_loss=0.10, val_loss=0.39, target_loss=0.40, epochs_completed=20,
    )
    # ratio 3.9 — way past 1.5
    assert r.promotion_ready is False
    assert "overfit" in r.note.lower()


def test_jepa_report_handles_zero_train_loss():
    """train_loss=0 (perfect fit, suspicious) → require val_loss only."""
    from tools.training.jepa_plan_predictor import compute_jepa_report
    r = compute_jepa_report(
        train_loss=0.0, val_loss=0.20, target_loss=0.40, epochs_completed=10,
    )
    assert r.promotion_ready is True


def test_jepa_report_exact_target_match():
    """val_loss == target_loss exactly → pass (≤ comparison)."""
    from tools.training.jepa_plan_predictor import compute_jepa_report
    r = compute_jepa_report(
        train_loss=0.30, val_loss=0.40, target_loss=0.40, epochs_completed=20,
    )
    assert r.promotion_ready is True


# ─── Dataset loader ────────────────────────────────────────────────


def test_load_plan_pairs_filters_rows_without_embeddings(tmp_path):
    """Rows without ``chosen_embedding`` / ``rejected_embedding``
    are silently dropped — they can't drive the JEPA loss."""
    from tools.training.jepa_plan_predictor import load_plan_pairs
    src = tmp_path / "build.jsonl"
    rows = [
        # Has embeddings → keep
        json.dumps({"prompt": "x", "chosen": "y", "rejected": "z",
                    "chosen_embedding": [0.1, 0.2, 0.3]}),
        # No embedding → drop
        json.dumps({"prompt": "x", "chosen": "y", "rejected": "z"}),
        # Has rejected_embedding → keep
        json.dumps({"prompt": "x", "rejected_embedding": [0.4, 0.5, 0.6]}),
    ]
    src.write_text("\n".join(rows), encoding="utf-8")
    out = load_plan_pairs(src)
    assert len(out) == 2


def test_load_plan_pairs_raises_when_missing():
    from tools.training.jepa_plan_predictor import load_plan_pairs
    with pytest.raises(FileNotFoundError):
        load_plan_pairs(Path("/nonexistent/path.jsonl"))


def test_load_plan_pairs_handles_corrupt_lines(tmp_path):
    """Malformed JSON lines are skipped silently — the rest of the
    file still loads."""
    from tools.training.jepa_plan_predictor import load_plan_pairs
    src = tmp_path / "build.jsonl"
    src.write_text(
        '{"chosen_embedding": [0.1, 0.2]}\nnot-json\n{"chosen_embedding": [0.3]}\n',
        encoding="utf-8",
    )
    out = load_plan_pairs(src)
    assert len(out) == 2


def test_split_train_val_is_deterministic(tmp_path):
    """Same seed → same train/val partition.  Operator can compare
    epoch-by-epoch runs reliably."""
    from tools.training.jepa_plan_predictor import split_train_val
    rows = [{"i": i, "chosen_embedding": [float(i)]} for i in range(100)]
    train1, val1 = split_train_val(rows, val_split=0.20, seed=42)
    train2, val2 = split_train_val(rows, val_split=0.20, seed=42)
    assert [r["i"] for r in train1] == [r["i"] for r in train2]
    assert [r["i"] for r in val1] == [r["i"] for r in val2]
    assert len(val1) == 20      # 20% of 100


# ─── CLI ────────────────────────────────────────────────────────────


def test_dry_run_prints_config(capsys):
    from tools.training.jepa_plan_predictor import build_parser, run
    args = build_parser().parse_args([
        "--train", "data/preference_pairs/build.jsonl",
        "--epochs", "10",
        "--dry-run",
    ])
    rc = run(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["epochs"] == 10


def test_simulate_pass_returns_zero(capsys):
    from tools.training.jepa_plan_predictor import build_parser, run
    args = build_parser().parse_args([
        "--simulate",
        "--train-loss", "0.30",
        "--val-loss", "0.38",
        "--target-loss", "0.40",
    ])
    rc = run(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["promotion_ready"] is True


def test_simulate_fail_returns_nonzero(capsys):
    from tools.training.jepa_plan_predictor import build_parser, run
    args = build_parser().parse_args([
        "--simulate",
        "--train-loss", "0.05",
        "--val-loss", "0.50",
        "--target-loss", "0.40",
    ])
    rc = run(args)
    assert rc == 1


def test_simulate_requires_train_and_val_loss():
    from tools.training.jepa_plan_predictor import build_parser, run
    args = build_parser().parse_args(["--simulate"])
    rc = run(args)
    assert rc == 2
