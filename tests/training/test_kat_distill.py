"""Cycle J.2 — KAT FFN distillation + kill-switch coverage.

Plan-agent locked: the kill-switch must FAIL CLOSED.  If the
1B Pythia variant doesn't recover ≥95% of the MLP baseline
perplexity, ``proceed`` must be False so the operator never
burns 8 weeks on a 7B training that's destined to fail.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


# ─── kill_switch_decision — happy path ──────────────────────────────


def test_kill_switch_passes_when_recovery_exceeds_target():
    """KAT perplexity 1.04 vs baseline 1.0 → recovery 0.96 > target 0.95."""
    from tools.training.kat_ffn_distill import kill_switch_decision
    v = kill_switch_decision(baseline_ppl=1.0, kat_ppl=1.04, target_ratio=0.95)
    assert v.proceed is True
    assert abs(v.observed_perplexity_ratio - 0.9615) < 0.001
    assert "PASSED" in v.note
    assert "scale to 7B" in v.note


def test_kill_switch_fails_when_recovery_below_target():
    """KAT perplexity 1.20 vs baseline 1.0 → recovery 0.83 < target 0.95."""
    from tools.training.kat_ffn_distill import kill_switch_decision
    v = kill_switch_decision(baseline_ppl=1.0, kat_ppl=1.20, target_ratio=0.95)
    assert v.proceed is False
    assert v.observed_perplexity_ratio < 0.95
    assert "FAILED" in v.note
    assert "ABORT" in v.note
    assert "vapor" in v.note          # dossier §8 routing


def test_kill_switch_passes_exact_match():
    """recovery == target_ratio exactly → proceed=True (>= comparison)."""
    from tools.training.kat_ffn_distill import kill_switch_decision
    # baseline=0.95, kat=1.0 → recovery = 0.95 / 1.0 = 0.95 ⇒ matches target
    v = kill_switch_decision(baseline_ppl=0.95, kat_ppl=1.0, target_ratio=0.95)
    assert v.proceed is True
    assert abs(v.margin) < 1e-9       # right at the threshold


def test_kill_switch_safety_buffer_makes_decision_stricter():
    """A safety_buffer of 0.05 pushes the effective threshold to 1.0
    — recovery 0.95 no longer passes."""
    from tools.training.kat_ffn_distill import kill_switch_decision
    # baseline=0.95, kat=1.0 → recovery 0.95.  target 0.95 + buffer 0.05 = 1.00
    v = kill_switch_decision(
        baseline_ppl=0.95, kat_ppl=1.0,
        target_ratio=0.95, safety_buffer=0.05,
    )
    assert v.proceed is False


def test_kill_switch_handles_zero_or_negative_inputs():
    """Bad inputs (zero/negative perplexity) → fail closed."""
    from tools.training.kat_ffn_distill import kill_switch_decision
    v = kill_switch_decision(baseline_ppl=0, kat_ppl=1.0)
    assert v.proceed is False
    assert "invalid" in v.note
    v = kill_switch_decision(baseline_ppl=1.0, kat_ppl=-0.5)
    assert v.proceed is False


def test_kill_switch_records_extras_for_audit():
    """The verdict carries the raw inputs in ``extras`` so the
    audit trail shows what the kill-switch was reasoning about."""
    from tools.training.kat_ffn_distill import kill_switch_decision
    v = kill_switch_decision(baseline_ppl=1.0, kat_ppl=1.04,
                             target_ratio=0.95, safety_buffer=0.01)
    assert v.extras["baseline_perplexity"] == 1.0
    assert v.extras["kat_perplexity"] == 1.04
    assert v.extras["safety_buffer"] == 0.01


# ─── DistillConfig + CLI ────────────────────────────────────────────


def test_build_distill_config_threads_args():
    """Config dataclass copies the CLI args verbatim."""
    from tools.training.kat_ffn_distill import build_distill_config, build_parser
    args = build_parser().parse_args([
        "--base", "pythia-1b",
        "--target-perplexity-ratio", "0.97",
        "--epochs", "5",
        "--lr", "1e-5",
    ])
    cfg = build_distill_config(args)
    assert cfg.base_model == "pythia-1b"
    assert cfg.target_perplexity_ratio == 0.97
    assert cfg.epochs == 5
    assert cfg.learning_rate == 1e-5


def test_dry_run_prints_config_and_returns_zero(capsys):
    """``--dry-run`` validates the config without launching training."""
    from tools.training.kat_ffn_distill import build_parser, run
    args = build_parser().parse_args([
        "--base", "pythia-1b",
        "--target-perplexity-ratio", "0.95",
        "--dry-run",
    ])
    rc = run(args)
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["base_model"] == "pythia-1b"
    assert payload["target_perplexity_ratio"] == 0.95


def test_simulate_path_routes_through_kill_switch(capsys):
    """--simulate skips real training + applies kill_switch to the
    supplied observed_ppl.  Above-target observed_ppl → exit 0 (PASS)."""
    from tools.training.kat_ffn_distill import build_parser, run
    args = build_parser().parse_args([
        "--base", "pythia-1b",
        "--simulate",
        "--baseline-ppl", "1.0",
        "--observed-ppl", "1.04",
    ])
    rc = run(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"]["proceed"] is True


def test_simulate_failure_path_returns_nonzero(capsys):
    """Below-target observed_ppl → exit 1 (FAIL)."""
    from tools.training.kat_ffn_distill import build_parser, run
    args = build_parser().parse_args([
        "--base", "pythia-1b",
        "--simulate",
        "--baseline-ppl", "1.0",
        "--observed-ppl", "1.30",       # 0.77 recovery — below 0.95
    ])
    rc = run(args)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"]["proceed"] is False
    assert "ABORT" in payload["verdict"]["note"]


def test_simulate_requires_observed_ppl(capsys):
    """--simulate without --observed-ppl is a usage error."""
    from tools.training.kat_ffn_distill import build_parser, run
    args = build_parser().parse_args([
        "--base", "pythia-1b",
        "--simulate",
    ])
    rc = run(args)
    assert rc == 2


def test_out_verdict_persists_payload(tmp_path):
    """--out-verdict writes the structured payload to disk so the
    operator's CI can grep for 'proceed: false'."""
    from tools.training.kat_ffn_distill import build_parser, run
    out_path = tmp_path / "kat_verdict.json"
    args = build_parser().parse_args([
        "--base", "pythia-1b",
        "--simulate",
        "--baseline-ppl", "1.0",
        "--observed-ppl", "1.04",
        "--out-verdict", str(out_path),
    ])
    rc = run(args)
    assert rc == 0
    assert out_path.is_file()
    persisted = json.loads(out_path.read_text(encoding="utf-8"))
    assert "verdict" in persisted
    assert persisted["verdict"]["proceed"] is True
    assert "computed_at_utc" in persisted


def test_run_training_raises_when_torch_missing(monkeypatch):
    """``run_training`` MUST fail fast with a clear error message when
    torch isn't installed (CI / CPU-only host)."""
    from tools.training import kat_ffn_distill as mod

    import sys
    monkeypatch.setitem(sys.modules, "torch", None)
    cfg = mod.DistillConfig(base_model="pythia-1b")
    with pytest.raises(RuntimeError, match="torch"):
        mod.run_training(cfg)
