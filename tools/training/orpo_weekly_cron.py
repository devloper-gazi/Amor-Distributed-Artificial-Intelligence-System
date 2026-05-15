#!/usr/bin/env python3
"""
Cycle F Sprint 6 piece 3 — weekly ORPO LoRA training cron.

Runs every Sunday 02:00 (host cron / Windows Task Scheduler).
Reads accumulated preference pairs from
`data/preference_pairs/{coder,tester,debugger}.jsonl`, runs ORPO via
the Sprint 3 `orpo_role_adapter.py` driver, writes the candidate
adapter to `models/lora/candidate/<role>-r16-<utc>.gguf` plus a
human-readable diff report at `data/training/diff_<utc>.md`.

Does NOT auto-promote.  Promotion goes through `tools/lora/promote.py`
(this commit lands the cron + promote skeletons; the eval-delta gate
in the diff report is the operator's decision aid).

Usage:
  python tools/training/orpo_weekly_cron.py             # all roles
  python tools/training/orpo_weekly_cron.py --role coder
  python tools/training/orpo_weekly_cron.py --dry-run   # plan only

Exit codes:
  0  all roles trained successfully (or all skipped per min-pairs gate)
  1  one or more roles failed
  2  fatal init (preference-pair root missing, etc.)
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        _r = getattr(_s, "reconfigure", None)
        if _r is not None:
            try:
                _r(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PAIRS_ROOT = REPO_ROOT / "data" / "preference_pairs"
CANDIDATE_ROOT = REPO_ROOT / "models" / "lora" / "candidate"
DIFF_ROOT = REPO_ROOT / "data" / "training"
TRAINER = REPO_ROOT / "tools" / "training" / "orpo_role_adapter.py"

ROLES = ("coder", "tester", "debugger")


# ─── Result shape ───────────────────────────────────────────────────


@dataclass
class RoleTrainingResult:
    role: str
    status: str               # "trained" | "skipped" | "failed"
    pair_count: int = 0
    adapter_path: Path | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "status": self.status,
            "pair_count": self.pair_count,
            "adapter_path": str(self.adapter_path) if self.adapter_path else None,
            "error": self.error,
        }


@dataclass
class WeeklyRunReport:
    timestamp_utc: str
    results: list[RoleTrainingResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp_utc": self.timestamp_utc,
            "results": [r.to_dict() for r in self.results],
        }

    @property
    def overall_exit_code(self) -> int:
        # 0 unless at least one role failed.
        any_failed = any(r.status == "failed" for r in self.results)
        return 1 if any_failed else 0


# ─── One role ──────────────────────────────────────────────────────


def _pair_count(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.open(encoding="utf-8") if line.strip())


def train_one_role(
    role: str,
    *,
    timestamp: str,
    dry_run: bool = False,
    min_pairs: int = 50,
) -> RoleTrainingResult:
    """Train a single role.  Result populates the WeeklyRunReport."""

    pairs_file = PAIRS_ROOT / f"{role}.jsonl"
    pair_n = _pair_count(pairs_file)

    if pair_n == 0:
        return RoleTrainingResult(
            role=role,
            status="skipped",
            pair_count=0,
            error=f"no preference pairs at {pairs_file}",
        )
    if pair_n < min_pairs:
        return RoleTrainingResult(
            role=role,
            status="skipped",
            pair_count=pair_n,
            error=(
                f"insufficient pairs ({pair_n} < {min_pairs}); pass --allow-tiny "
                "or accumulate more feedback before training"
            ),
        )

    candidate_dir = CANDIDATE_ROOT / f"{role}-r16-{timestamp}"

    if dry_run:
        logger.info(
            "[%s] would train: %d pairs -> %s", role, pair_n, candidate_dir,
        )
        return RoleTrainingResult(
            role=role,
            status="skipped",
            pair_count=pair_n,
            adapter_path=candidate_dir,
            error="dry-run",
        )

    cmd = [
        sys.executable, str(TRAINER),
        "--role", role,
        "--jsonl", str(pairs_file),
        "--out", str(candidate_dir),
        "--convert-gguf",
    ]
    logger.info("[%s] launching training: %s", role, " ".join(cmd))
    started = time.monotonic()
    try:
        rc = subprocess.call(cmd, cwd=str(REPO_ROOT))
    except FileNotFoundError as exc:
        return RoleTrainingResult(
            role=role,
            status="failed",
            pair_count=pair_n,
            error=f"trainer not found: {exc}",
        )
    elapsed = time.monotonic() - started
    if rc != 0:
        return RoleTrainingResult(
            role=role,
            status="failed",
            pair_count=pair_n,
            error=f"trainer exited {rc} after {elapsed:.0f}s",
        )

    gguf = candidate_dir.with_suffix(".gguf")
    logger.info(
        "[%s] trained: %d pairs in %.0fs -> %s",
        role, pair_n, elapsed, gguf,
    )
    return RoleTrainingResult(
        role=role,
        status="trained",
        pair_count=pair_n,
        adapter_path=gguf,
    )


# ─── Diff report ────────────────────────────────────────────────────


def write_diff_report(
    report: WeeklyRunReport, *, out_path: Path | None = None,
) -> Path:
    """Render a Markdown report describing what trained, what didn't,
    and a checklist for the operator's promote decision.  Persisted
    at `data/training/diff_<utc>.md` by default."""

    if out_path is None:
        DIFF_ROOT.mkdir(parents=True, exist_ok=True)
        ts = report.timestamp_utc.replace(":", "").replace("-", "")
        out_path = DIFF_ROOT / f"diff_{ts}.md"

    lines: list[str] = []
    lines.append(f"# ORPO weekly cron — {report.timestamp_utc}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| role | status | pair count | adapter | error |")
    lines.append("|---|---|---|---|---|")
    for r in report.results:
        adapter = str(r.adapter_path.name) if r.adapter_path else "—"
        err = r.error or "—"
        lines.append(
            f"| {r.role} | {r.status} | {r.pair_count} | "
            f"{adapter} | {err} |"
        )
    lines.append("")
    lines.append("## Operator promote checklist")
    lines.append("")
    for r in report.results:
        if r.status != "trained":
            continue
        lines.append(f"### {r.role}")
        lines.append("")
        lines.append("- [ ] Inspect adapter sanity (a few spot-check completions)")
        lines.append(
            f"- [ ] Run eval-delta against the in-production adapter via "
            f"`tools/lora/promote.py --role {r.role} --candidate {r.adapter_path}` "
            "(emits a Sprint-0 corpus delta report; ≥ +3 pp threshold "
            "from the Sprint 3 plan)"
        )
        lines.append(
            f"- [ ] If delta ≥ +3 pp role-adherence: promote via "
            f"`tools/lora/promote.py --role {r.role} --candidate {r.adapter_path} --promote`"
        )
        lines.append(
            "- [ ] If delta < +3 pp: leave candidate in place, "
            "accumulate more preference pairs, re-run cron next week"
        )
        lines.append("")
    if not any(r.status == "trained" for r in report.results):
        lines.append("(no roles trained this cycle)")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


# ─── CLI ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--role",
        action="append",
        choices=list(ROLES),
        help=(
            "Limit to one role (repeatable: `--role coder --role tester`).  "
            "Default: train all three."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Plan only — print what would train without invoking the trainer.",
    )
    p.add_argument(
        "--min-pairs", type=int, default=50,
        help="Minimum pair count per role to trigger training (default 50).",
    )
    p.add_argument(
        "--out-dir", default=None,
        help=f"Diff-report output dir (default {DIFF_ROOT}).",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit run report as JSON to stdout in addition to diff_*.md.",
    )
    return p


def run(args: argparse.Namespace) -> int:
    if not PAIRS_ROOT.is_dir():
        # Create on first run — operator may not have feedback yet.
        PAIRS_ROOT.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "preference_pairs root just created at %s — no data yet",
            PAIRS_ROOT,
        )
    CANDIDATE_ROOT.mkdir(parents=True, exist_ok=True)
    DIFF_ROOT.mkdir(parents=True, exist_ok=True)

    if not TRAINER.is_file() and not args.dry_run:
        logger.error("trainer missing at %s", TRAINER)
        return 2

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = WeeklyRunReport(timestamp_utc=timestamp)

    selected_roles = args.role if args.role else list(ROLES)

    for role in selected_roles:
        result = train_one_role(
            role,
            timestamp=timestamp.replace(":", "").replace("-", ""),
            dry_run=args.dry_run,
            min_pairs=args.min_pairs,
        )
        report.results.append(result)

    out_dir = Path(args.out_dir) if args.out_dir else None
    md_path = write_diff_report(
        report,
        out_path=(out_dir / f"diff_{timestamp.replace(':', '').replace('-', '')}.md")
        if out_dir
        else None,
    )
    logger.info("diff report written: %s", md_path)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))

    return report.overall_exit_code


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
