"""Cycle F Sprint 2 — tests for coverage_reader.py.

Covers the dict-shape parser (`parse_coverage_json`), the on-disk
loader (`load_coverage_from_workdir`), and the prompt-block
renderer (`format_missed_branches_block`).  All offline — no pytest-cov
invocation, no sandbox.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from document_processor.code_intelligence.coverage_reader import (
    BranchCoverageReport,
    MissedBranch,
    format_missed_branches_block,
    load_coverage_from_workdir,
    parse_coverage_json,
)


# ─── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def fully_covered_payload() -> dict:
    """coverage.py JSON for code with 100% line + branch coverage."""

    return {
        "meta": {"version": "7.0.0"},
        "files": {
            "main.py": {
                "executed_lines": [1, 2, 3, 4, 5],
                "missing_lines": [],
                "excluded_lines": [],
                "executed_branches": [[2, 3], [2, 4]],
                "missing_branches": [],
                "summary": {
                    "covered_lines": 5,
                    "num_statements": 5,
                    "num_branches": 2,
                    "num_partial_branches": 0,
                    "covered_branches": 2,
                    "missing_branches": 0,
                    "percent_covered": 100.0,
                    "percent_covered_display": "100",
                },
            },
        },
        "totals": {
            "covered_lines": 5,
            "num_statements": 5,
            "num_branches": 2,
            "num_partial_branches": 0,
            "covered_branches": 2,
            "missing_branches": 0,
            "missing_lines": 0,
            "percent_covered": 100.0,
            "percent_covered_display": "100",
        },
    }


@pytest.fixture
def partial_coverage_payload() -> dict:
    """Branch coverage at 50% — half the branches missed."""

    return {
        "files": {
            "main.py": {
                "executed_lines": [1, 2, 3, 4],
                "missing_lines": [5],
                "executed_branches": [[2, 3]],
                "missing_branches": [[2, 4], [4, 5]],
                "summary": {
                    "num_statements": 5,
                    "num_branches": 4,
                    "covered_branches": 2,
                    "missing_branches": 2,
                    "percent_covered": 80.0,
                },
            },
            "helper.py": {
                "missing_branches": [[10, 12]],
                "summary": {},
            },
        },
        "totals": {
            "num_statements": 10,
            "num_branches": 4,
            "covered_branches": 2,
            "missing_branches": 2,
            "percent_covered": 80.0,
        },
    }


# ─── parse_coverage_json ────────────────────────────────────────────


def test_parse_returns_available_true_for_real_payload(fully_covered_payload):
    rep = parse_coverage_json(fully_covered_payload)
    assert rep.available is True


def test_parse_full_coverage_yields_ratio_1():
    rep = parse_coverage_json({
        "totals": {"num_branches": 5, "covered_branches": 5, "percent_covered": 100.0},
        "files": {},
    })
    assert rep.branch_coverage_ratio == 1.0
    assert rep.line_coverage_ratio == 1.0


def test_parse_zero_branches_treated_as_fully_covered():
    """Vacuous truth: code with no branches has 100% branch coverage."""

    rep = parse_coverage_json({
        "totals": {"num_branches": 0, "covered_branches": 0, "percent_covered": 100.0},
        "files": {},
    })
    assert rep.branch_coverage_ratio == 1.0
    assert rep.num_branches == 0


def test_parse_extracts_missed_branches(partial_coverage_payload):
    rep = parse_coverage_json(partial_coverage_payload)
    assert rep.branch_coverage_ratio == 0.5
    assert len(rep.missed_branches) == 3  # 2 in main.py + 1 in helper.py
    files = {mb.file for mb in rep.missed_branches}
    assert files == {"main.py", "helper.py"}


def test_parse_missing_totals_returns_unavailable():
    rep = parse_coverage_json({"files": {"main.py": {}}})
    assert rep.available is False


def test_parse_garbage_payload_returns_unavailable():
    assert parse_coverage_json([]).available is False
    assert parse_coverage_json("not a dict").available is False
    assert parse_coverage_json(None).available is False


def test_parse_filters_malformed_missing_branches():
    """Malformed missing_branches entries (non-list, wrong arity) skipped."""

    rep = parse_coverage_json({
        "totals": {"num_branches": 2, "covered_branches": 1, "percent_covered": 50.0},
        "files": {
            "x.py": {
                "missing_branches": [
                    [1, 2],          # ok
                    "garbage",       # skipped
                    [3],             # skipped (wrong arity)
                    [4, 5, 6],       # skipped (wrong arity)
                    ["a", "b"],      # skipped (non-int)
                ],
            },
        },
    })
    assert rep.available is True
    assert len(rep.missed_branches) == 1
    assert rep.missed_branches[0].file == "x.py"
    assert rep.missed_branches[0].from_line == 1
    assert rep.missed_branches[0].to_line == 2


def test_parse_clamps_ratios_to_unit_interval():
    """Sanity: weird inputs shouldn't produce ratios outside [0, 1]."""

    rep = parse_coverage_json({
        "totals": {"num_branches": 1, "covered_branches": 10, "percent_covered": 200.0},
        "files": {},
    })
    assert 0.0 <= rep.branch_coverage_ratio <= 1.0
    assert 0.0 <= rep.line_coverage_ratio <= 1.0


# ─── load_coverage_from_workdir ─────────────────────────────────────


def test_load_picks_up_dot_coverage_json(tmp_path: Path, fully_covered_payload):
    (tmp_path / ".coverage.json").write_text(
        json.dumps(fully_covered_payload), encoding="utf-8"
    )
    rep = load_coverage_from_workdir(tmp_path)
    assert rep.available is True
    assert rep.branch_coverage_ratio == 1.0


def test_load_picks_up_coverage_json_fallback(tmp_path: Path):
    (tmp_path / "coverage.json").write_text(
        json.dumps({
            "totals": {"num_branches": 2, "covered_branches": 1, "percent_covered": 50.0},
            "files": {},
        }),
        encoding="utf-8",
    )
    rep = load_coverage_from_workdir(tmp_path)
    assert rep.available is True
    assert rep.branch_coverage_ratio == 0.5


def test_load_missing_file_returns_unavailable(tmp_path: Path):
    rep = load_coverage_from_workdir(tmp_path)
    assert rep.available is False


def test_load_malformed_json_returns_unavailable(tmp_path: Path):
    (tmp_path / ".coverage.json").write_text("not json {", encoding="utf-8")
    rep = load_coverage_from_workdir(tmp_path)
    assert rep.available is False


# ─── format_missed_branches_block ───────────────────────────────────


def test_format_returns_empty_when_above_threshold():
    rep = BranchCoverageReport(
        branch_coverage_ratio=0.95,
        num_branches=20,
        covered_branches=19,
        missed_branches=[MissedBranch("x.py", 1, 2)],
        available=True,
    )
    assert format_missed_branches_block(rep, threshold=0.80) == ""


def test_format_returns_block_when_below_threshold():
    rep = BranchCoverageReport(
        branch_coverage_ratio=0.40,
        num_branches=10,
        covered_branches=4,
        missed_branches=[
            MissedBranch("a.py", 5, 7),
            MissedBranch("a.py", 10, 12),
            MissedBranch("b.py", 3, -1),  # branch-never-taken
        ],
        available=True,
    )
    out = format_missed_branches_block(rep, threshold=0.80)
    assert "MISSED_BRANCHES:" in out
    assert "40%" in out  # ratio formatted
    assert "a.py:5 -> a.py:7" in out
    assert "a.py:10 -> a.py:12" in out
    assert "b.py:3 -> exit" in out  # negative target rendered as exit


def test_format_truncates_to_max_branches():
    rep = BranchCoverageReport(
        branch_coverage_ratio=0.10,
        num_branches=100,
        covered_branches=10,
        missed_branches=[MissedBranch(f"f.py", i, i + 1) for i in range(20)],
        available=True,
    )
    out = format_missed_branches_block(rep, max_branches=5, threshold=0.80)
    # Should include the truncation hint.
    assert "and 15 more" in out


def test_format_returns_empty_when_unavailable():
    rep = BranchCoverageReport(available=False)
    assert format_missed_branches_block(rep) == ""


# ─── BranchCoverageReport.to_breakdown_dict ─────────────────────────


def test_breakdown_dict_shape(partial_coverage_payload):
    rep = parse_coverage_json(partial_coverage_payload)
    d = rep.to_breakdown_dict()
    assert d["available"] is True
    assert d["branch_coverage"] == 0.5
    assert d["num_branches"] == 4
    assert d["covered_branches"] == 2
    assert d["missed_branch_count"] == 3
