"""
Cycle F Sprint 2 — pytest-cov branch-coverage reader for the Reflexion loop.

Parses the JSON report produced by `pytest --cov=. --cov-branch
--cov-report=json:.coverage.json` and emits two complementary surfaces:

* `BranchCoverageReport` — structured numbers (ratio, missed-branch
  records) that `_score_candidate` can include in the breakdown dict.
* `format_missed_branches_block(report)` — a coder-facing prompt block
  (`MISSED_BRANCHES`) injected into the reflexion feedback bundle when
  branch coverage is below `code_branch_coverage_threshold`.

The signal does NOT alter the existing quality-score weighting (the
Cycle D 35+25+15+25=100 weights are preserved) — it surfaces as
extra fields on `breakdown` for operator visibility and as feedback
text for the coder retry.  Reflexion is a feedback loop, not a gate;
the existing critic + threshold remain the trigger.

Stdlib-only.  Zero new pip deps in the AMOR app container (the
sandbox container is the one that needs `coverage` / `pytest-cov`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# coverage.py JSON-report schema:
#   {
#     "meta": {...},
#     "files": {
#       "<rel-path>": {
#         "executed_lines": [...],
#         "missing_lines": [...],
#         "excluded_lines": [...],
#         "executed_branches": [[from, to], ...],
#         "missing_branches": [[from, to], ...],
#         "summary": {
#           "covered_lines": int,
#           "num_statements": int,
#           "num_branches": int,
#           "num_partial_branches": int,
#           "covered_branches": int,
#           "missing_branches": int,
#           "percent_covered": float,
#           "percent_covered_display": str,
#         },
#       },
#       ...
#     },
#     "totals": { same summary keys + missing_lines },
#   }


@dataclass
class MissedBranch:
    """One missed branch (`from line` → `to line`) inside a file."""

    file: str
    from_line: int
    to_line: int

    def render(self) -> str:
        if self.to_line < 0:
            return f"{self.file}:{self.from_line} -> exit (branch never taken)"
        return f"{self.file}:{self.from_line} -> {self.file}:{self.to_line}"


@dataclass
class BranchCoverageReport:
    """Structured per-run coverage signal."""

    branch_coverage_ratio: float = 1.0   # 0.0-1.0
    line_coverage_ratio: float = 1.0     # 0.0-1.0
    num_branches: int = 0
    covered_branches: int = 0
    missed_branches: list[MissedBranch] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    available: bool = False              # False = coverage didn't run

    def to_breakdown_dict(self) -> dict[str, Any]:
        """Compact dict for inclusion in `_score_candidate.breakdown`."""

        return {
            "available": self.available,
            "branch_coverage": round(self.branch_coverage_ratio, 3),
            "line_coverage": round(self.line_coverage_ratio, 3),
            "num_branches": self.num_branches,
            "covered_branches": self.covered_branches,
            "missed_branch_count": len(self.missed_branches),
        }


# ─── Parsing ────────────────────────────────────────────────────────


def parse_coverage_json(payload: dict[str, Any]) -> BranchCoverageReport:
    """Pure function: dict → report.  Easy to unit-test."""

    if not isinstance(payload, dict) or "totals" not in payload:
        return BranchCoverageReport(available=False)

    totals = payload.get("totals") or {}
    files_section = payload.get("files") or {}

    num_branches = int(totals.get("num_branches") or 0)
    covered_branches = int(totals.get("covered_branches") or 0)
    if num_branches > 0:
        branch_ratio = covered_branches / num_branches
    else:
        # No branches detected — vacuously fully-covered.
        branch_ratio = 1.0

    # coverage.py reports `percent_covered` 0-100 for lines.
    line_pct = totals.get("percent_covered")
    if isinstance(line_pct, (int, float)):
        line_ratio = float(line_pct) / 100.0
    else:
        line_ratio = 1.0

    missed: list[MissedBranch] = []
    for path, info in files_section.items():
        if not isinstance(info, dict):
            continue
        for entry in info.get("missing_branches") or []:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            try:
                frm, to = int(entry[0]), int(entry[1])
            except (TypeError, ValueError):
                continue
            missed.append(MissedBranch(file=path, from_line=frm, to_line=to))

    return BranchCoverageReport(
        branch_coverage_ratio=max(0.0, min(1.0, branch_ratio)),
        line_coverage_ratio=max(0.0, min(1.0, line_ratio)),
        num_branches=num_branches,
        covered_branches=covered_branches,
        missed_branches=missed,
        files=sorted(files_section.keys()),
        available=True,
    )


def load_coverage_from_workdir(workdir: Path | str) -> BranchCoverageReport:
    """Best-effort: locate `.coverage.json` under `workdir` and parse it.

    Returns an `available=False` report if the file is missing /
    malformed — caller is responsible for treating that as "coverage
    didn't run" rather than "coverage was zero".
    """

    workdir = Path(workdir)
    candidates = [
        workdir / ".coverage.json",
        workdir / "coverage.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return BranchCoverageReport(available=False)
        return parse_coverage_json(data)
    return BranchCoverageReport(available=False)


# ─── Prompt-block renderer (feedback to coder retry) ────────────────


def format_missed_branches_block(
    report: BranchCoverageReport,
    *,
    max_branches: int = 8,
    threshold: float = 0.80,
) -> str:
    """Render a `MISSED_BRANCHES:` prompt block for the reflexion bundle.

    Returns an empty string if branch coverage is at/above the threshold
    so the coder retry isn't cluttered when branch coverage is fine.
    """

    if not report.available:
        return ""
    if report.branch_coverage_ratio >= threshold:
        return ""
    if not report.missed_branches:
        return ""

    lines = [
        "MISSED_BRANCHES:",
        (f"Branch coverage: {report.branch_coverage_ratio:.0%} "
         f"({report.covered_branches}/{report.num_branches} branches "
         f"covered; threshold {threshold:.0%})."),
        "",
        "The following branches are NOT exercised by the current "
        "tests.  Add tests that drive each unexercised branch "
        "(typically by varying inputs or conditions) before the "
        "next review pass:",
    ]
    for mb in report.missed_branches[:max_branches]:
        lines.append(f"  - {mb.render()}")
    if len(report.missed_branches) > max_branches:
        extra = len(report.missed_branches) - max_branches
        lines.append(f"  ... and {extra} more missed branch(es).")
    return "\n".join(lines)


# ─── Public surface ─────────────────────────────────────────────────


__all__ = [
    "BranchCoverageReport",
    "MissedBranch",
    "format_missed_branches_block",
    "load_coverage_from_workdir",
    "parse_coverage_json",
]
