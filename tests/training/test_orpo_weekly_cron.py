"""Cycle F Sprint 6 piece 3 — tests for orpo_weekly_cron.py.

Exercises:
  * Pair count gating (skipped when below min)
  * Empty pairs file → skipped, not failed
  * Dry-run mode produces report without invoking trainer
  * Diff-report markdown contains operator checklist
  * Overall exit-code roll-up
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def cron_module():
    src = REPO_ROOT / "tools" / "training" / "orpo_weekly_cron.py"
    assert src.is_file()
    spec = importlib.util.spec_from_file_location("orpo_weekly_cron_test", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orpo_weekly_cron_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_pairs(path: Path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({
                "prompt": f"q{i}",
                "chosen": f"a{i}",
                "rejected": f"b{i}",
            }) + "\n")


# ─── Pair-count gating ─────────────────────────────────────────────


def test_skipped_when_no_pair_file(cron_module, tmp_path, monkeypatch):
    monkeypatch.setattr(cron_module, "PAIRS_ROOT", tmp_path)
    res = cron_module.train_one_role(
        "coder", timestamp="20260515T120000Z", dry_run=True,
    )
    assert res.status == "skipped"
    assert res.pair_count == 0
    assert "no preference pairs" in res.error


def test_skipped_when_below_min_pairs(cron_module, tmp_path, monkeypatch):
    monkeypatch.setattr(cron_module, "PAIRS_ROOT", tmp_path)
    _write_pairs(tmp_path / "coder.jsonl", 10)  # below min=50
    res = cron_module.train_one_role(
        "coder", timestamp="t", dry_run=True, min_pairs=50,
    )
    assert res.status == "skipped"
    assert res.pair_count == 10
    assert "insufficient" in res.error


def test_dry_run_skips_with_full_pair_count(cron_module, tmp_path, monkeypatch):
    monkeypatch.setattr(cron_module, "PAIRS_ROOT", tmp_path)
    _write_pairs(tmp_path / "coder.jsonl", 100)
    res = cron_module.train_one_role(
        "coder", timestamp="t", dry_run=True, min_pairs=50,
    )
    assert res.status == "skipped"
    assert res.error == "dry-run"
    assert res.pair_count == 100


def test_trainer_invocation_when_pairs_sufficient(
    cron_module, tmp_path, monkeypatch,
):
    monkeypatch.setattr(cron_module, "PAIRS_ROOT", tmp_path)
    _write_pairs(tmp_path / "tester.jsonl", 200)

    # Patch subprocess.call to a no-op success.
    monkeypatch.setattr(
        cron_module.subprocess, "call",
        lambda cmd, cwd=None: 0,
    )

    res = cron_module.train_one_role(
        "tester", timestamp="20260515T120000Z", dry_run=False, min_pairs=50,
    )
    assert res.status == "trained"
    assert res.pair_count == 200
    assert res.adapter_path is not None


def test_trainer_failure_surfaces(cron_module, tmp_path, monkeypatch):
    monkeypatch.setattr(cron_module, "PAIRS_ROOT", tmp_path)
    _write_pairs(tmp_path / "debugger.jsonl", 100)

    monkeypatch.setattr(
        cron_module.subprocess, "call",
        lambda cmd, cwd=None: 2,
    )

    res = cron_module.train_one_role(
        "debugger", timestamp="t", dry_run=False, min_pairs=50,
    )
    assert res.status == "failed"
    assert "exited 2" in res.error


# ─── Diff report ───────────────────────────────────────────────────


def test_diff_report_contains_checklist(cron_module, tmp_path):
    report = cron_module.WeeklyRunReport(timestamp_utc="2026-05-15T12:00:00Z")
    report.results.append(cron_module.RoleTrainingResult(
        role="coder",
        status="trained",
        pair_count=150,
        adapter_path=Path("/fake/coder.gguf"),
    ))
    report.results.append(cron_module.RoleTrainingResult(
        role="tester",
        status="skipped",
        pair_count=12,
        error="insufficient pairs",
    ))

    out = cron_module.write_diff_report(
        report, out_path=tmp_path / "diff.md",
    )
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "coder" in text
    assert "tester" in text
    assert "Operator promote checklist" in text
    # Promote-worthy roles get checkboxes.
    assert "[ ]" in text
    # Reference to promote.py with the correct candidate path.
    assert "promote.py" in text


def test_diff_report_with_no_trained_roles_reads_naturally(cron_module, tmp_path):
    report = cron_module.WeeklyRunReport(timestamp_utc="t")
    report.results.append(cron_module.RoleTrainingResult(
        role="coder",
        status="skipped",
        pair_count=0,
        error="no pairs",
    ))
    out = cron_module.write_diff_report(report, out_path=tmp_path / "diff.md")
    text = out.read_text(encoding="utf-8")
    assert "no roles trained" in text.lower()


# ─── Overall exit code roll-up ─────────────────────────────────────


def test_overall_exit_zero_when_all_trained_or_skipped(cron_module):
    report = cron_module.WeeklyRunReport(timestamp_utc="t")
    report.results.append(cron_module.RoleTrainingResult(
        role="coder", status="trained", pair_count=100,
    ))
    report.results.append(cron_module.RoleTrainingResult(
        role="tester", status="skipped", pair_count=5,
    ))
    assert report.overall_exit_code == 0


def test_overall_exit_one_when_any_role_failed(cron_module):
    report = cron_module.WeeklyRunReport(timestamp_utc="t")
    report.results.append(cron_module.RoleTrainingResult(
        role="coder", status="trained", pair_count=100,
    ))
    report.results.append(cron_module.RoleTrainingResult(
        role="tester", status="failed", pair_count=100,
        error="trainer crashed",
    ))
    assert report.overall_exit_code == 1
