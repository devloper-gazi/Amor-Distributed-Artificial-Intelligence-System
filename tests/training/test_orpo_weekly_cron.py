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


# ─── v18.1 Step 2: Postgres → JSONL export bridge ──────────────────


def _redirect_pairs_root(cron_module, tmp_path, monkeypatch):
    """Redirect every PAIRS_ROOT-derived constant into tmp_path so we
    can exercise the export step without writing to the real
    data/preference_pairs/.  Sets SHARED_SOURCE_FILE and
    EXPORT_TIMESTAMP_FILE to match."""
    monkeypatch.setattr(cron_module, "PAIRS_ROOT", tmp_path)
    monkeypatch.setattr(
        cron_module, "SHARED_SOURCE_FILE", tmp_path / "build.jsonl",
    )
    monkeypatch.setattr(
        cron_module, "EXPORT_TIMESTAMP_FILE", tmp_path / ".last_export",
    )


def test_export_needs_refresh_true_when_sidecar_missing(
    cron_module, tmp_path, monkeypatch,
):
    from datetime import datetime, timezone
    _redirect_pairs_root(cron_module, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    assert cron_module._export_needs_refresh(now, hours=24) is True


def test_export_needs_refresh_false_when_sidecar_fresh(
    cron_module, tmp_path, monkeypatch,
):
    from datetime import datetime, timezone, timedelta
    _redirect_pairs_root(cron_module, tmp_path, monkeypatch)
    cron_module.EXPORT_TIMESTAMP_FILE.write_text(
        datetime.now(timezone.utc).isoformat(),
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    assert cron_module._export_needs_refresh(now, hours=24) is False


def test_export_needs_refresh_true_when_sidecar_stale(
    cron_module, tmp_path, monkeypatch,
):
    from datetime import datetime, timezone, timedelta
    _redirect_pairs_root(cron_module, tmp_path, monkeypatch)
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    cron_module.EXPORT_TIMESTAMP_FILE.write_text(old, encoding="utf-8")
    now = datetime.now(timezone.utc)
    assert cron_module._export_needs_refresh(now, hours=24) is True


def test_export_skipped_when_recent(cron_module, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    _redirect_pairs_root(cron_module, tmp_path, monkeypatch)
    cron_module.SHARED_SOURCE_FILE.write_text("", encoding="utf-8")
    cron_module.EXPORT_TIMESTAMP_FILE.write_text(
        datetime.now(timezone.utc).isoformat(),
        encoding="utf-8",
    )
    res = cron_module.export_preference_pairs(idempotency_hours=24)
    assert res.status == "skipped_fresh"
    assert "bypass" in res.error
    assert res.path == cron_module.SHARED_SOURCE_FILE


def test_export_invokes_exporter_when_due(cron_module, tmp_path, monkeypatch):
    """Patch the exporter so it just writes a fixture file + returns 0."""
    _redirect_pairs_root(cron_module, tmp_path, monkeypatch)
    fixture_lines = 7

    def fake_call(cmd, cwd=None):
        # Find the --out path in cmd and drop fixture rows there.
        out_index = cmd.index("--out") + 1
        out_path = Path(cmd[out_index])
        _write_pairs(out_path, fixture_lines)
        return 0

    monkeypatch.setattr(cron_module.subprocess, "call", fake_call)
    res = cron_module.export_preference_pairs(force=True)
    assert res.status == "exported"
    assert res.rows_written == fixture_lines
    assert cron_module.EXPORT_TIMESTAMP_FILE.is_file()


def test_export_soft_fails_when_db_unavailable(cron_module, tmp_path, monkeypatch):
    _redirect_pairs_root(cron_module, tmp_path, monkeypatch)
    monkeypatch.setattr(cron_module.subprocess, "call", lambda cmd, cwd=None: 2)
    res = cron_module.export_preference_pairs(force=True)
    assert res.status == "skipped_no_db"
    # No timestamp written when exporter didn't successfully complete.
    assert not cron_module.EXPORT_TIMESTAMP_FILE.is_file()


def test_resolve_pairs_file_prefers_per_role(cron_module, tmp_path, monkeypatch):
    _redirect_pairs_root(cron_module, tmp_path, monkeypatch)
    per_role = tmp_path / "coder.jsonl"
    _write_pairs(per_role, 5)
    cron_module.SHARED_SOURCE_FILE.write_text("dummy\n", encoding="utf-8")
    assert cron_module._resolve_pairs_file("coder") == per_role


def test_resolve_pairs_file_falls_back_to_shared(
    cron_module, tmp_path, monkeypatch,
):
    _redirect_pairs_root(cron_module, tmp_path, monkeypatch)
    # No per-role coder.jsonl exists.
    cron_module.SHARED_SOURCE_FILE.write_text("dummy\n", encoding="utf-8")
    assert (
        cron_module._resolve_pairs_file("coder")
        == cron_module.SHARED_SOURCE_FILE
    )


def test_train_one_role_consumes_shared_source(
    cron_module, tmp_path, monkeypatch,
):
    """When the per-role file is missing, training reads from
    SHARED_SOURCE_FILE (the export step's output)."""
    _redirect_pairs_root(cron_module, tmp_path, monkeypatch)
    _write_pairs(cron_module.SHARED_SOURCE_FILE, 80)
    res = cron_module.train_one_role(
        "coder", timestamp="t", dry_run=True, min_pairs=50,
    )
    assert res.status == "skipped"
    assert res.error == "dry-run"
    assert res.pair_count == 80


def test_diff_report_renders_export_section(cron_module, tmp_path):
    """When the report has an export attribute, the diff markdown
    surfaces its status."""
    report = cron_module.WeeklyRunReport(timestamp_utc="2026-05-15T12:00:00Z")
    report.export = cron_module.ExportResult(
        status="exported",
        rows_written=42,
        path=Path("/fake/build.jsonl"),
    )
    report.results.append(cron_module.RoleTrainingResult(
        role="coder", status="trained", pair_count=42,
        adapter_path=Path("/fake/coder.gguf"),
    ))
    out = cron_module.write_diff_report(report, out_path=tmp_path / "diff.md")
    text = out.read_text(encoding="utf-8")
    assert "Step 0" in text
    assert "exported" in text
    assert "42" in text
    assert "build.jsonl" in text


def test_overall_exit_one_when_export_hard_failed(cron_module):
    """Export status='failed' must escalate to exit 1 even when no
    role-level training failed."""
    report = cron_module.WeeklyRunReport(timestamp_utc="t")
    report.export = cron_module.ExportResult(
        status="failed", error="exporter exited 1",
    )
    report.results.append(cron_module.RoleTrainingResult(
        role="coder", status="skipped", pair_count=0,
    ))
    assert report.overall_exit_code == 1


def test_overall_exit_zero_when_export_skipped_fresh(cron_module):
    """Skipped-fresh is success, not failure — repeat runs of the cron
    within the day shouldn't error out."""
    report = cron_module.WeeklyRunReport(timestamp_utc="t")
    report.export = cron_module.ExportResult(status="skipped_fresh")
    report.results.append(cron_module.RoleTrainingResult(
        role="coder", status="trained", pair_count=100,
    ))
    assert report.overall_exit_code == 0


def test_cli_skip_export_flag_disables_export_step(
    cron_module, tmp_path, monkeypatch,
):
    """`--skip-export` skips the Postgres hit entirely (operator's
    hand-dropped JSONL path)."""
    _redirect_pairs_root(cron_module, tmp_path, monkeypatch)
    monkeypatch.setattr(cron_module, "CANDIDATE_ROOT", tmp_path / "cand")
    monkeypatch.setattr(cron_module, "DIFF_ROOT", tmp_path / "diff")
    # Make TRAINER point at a real file so the early return doesn't fire.
    fake_trainer = tmp_path / "fake_trainer.py"
    fake_trainer.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(cron_module, "TRAINER", fake_trainer)

    # If export were invoked, subprocess.call would be hit.  Track that.
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cron_module.subprocess, "call",
        lambda cmd, cwd=None: (calls.append(list(cmd)) or 0),
    )

    args = cron_module.build_parser().parse_args([
        "--dry-run", "--skip-export",
    ])
    rc = cron_module.run(args)
    assert rc == 0
    # No export call hit — subprocess.call never reached EXPORTER.
    assert all(
        cron_module.EXPORTER.name not in " ".join(c) for c in calls
    ), f"unexpected exporter invocation: {calls}"
