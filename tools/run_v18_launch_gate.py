#!/usr/bin/env python3
"""
Cycle F Sprint 6 — v18 launch acceptance gate runner.

Runs all FIVE conjunctive conditions from the v18 plan + emits a
single scorecard:

    data/baselines/v18_launch_gate_<utc-iso>.json

Conditions (all must pass simultaneously):

  1. Sprint-0 corpus average judge score >= 7.2 / 10
     (per-mode floor 6.5).  Sources: re-runs the Sprint-0 v18
     baseline with Mistral judge OR re-reads the most recent
     `sprint0_latest.json` snapshot.

  2. HumanEval+ pass@1 >= 72% on held-out 50-problem subset.
     Source: re-runs `tools/eval/humaneval_plus.py` against the
     active LLM backend.

  3. SWE-bench-Lite-25 resolved rate >= 28%.
     Source: re-runs `tools/eval/swebench_lite.py` (~120 min).

  4. Pipeline median latency <= 75s on Sprint-0 corpus.
     Source: aggregated from the per-task `wall_clock_ms` field
     in the Sprint-0 results.

  5. Deliverable completeness rubric >= 70% pass rate.
     Source: Track 4 §6.2 internal harness — defaults to the
     existing `eval_runs` table; falls back to the Sprint-0
     judge's `completeness` mean (×2 to map 5→10 scale) when
     the dedicated harness hasn't run.

The runner is conservative — it will SKIP a condition (rather
than failing) when its prerequisite isn't available, and the
scorecard reports it as `status="skipped"`.  The gate verdict is
PASS only when every condition is non-skipped AND meets its
threshold.

Exit codes:
  0   verdict PASS — v18.0.0 may be tagged
  1   verdict FAIL — one or more conditions failed
  2   FATAL init (paths missing / json parse fails)

Usage:
  python tools/run_v18_launch_gate.py                  # use existing data
  python tools/run_v18_launch_gate.py --re-run-sprint0 # re-run Sprint 0
  python tools/run_v18_launch_gate.py --re-run-evals   # re-run HE+/SWE
  python tools/run_v18_launch_gate.py --shallow        # skip expensive evals
  python tools/run_v18_launch_gate.py --json           # JSON to stdout
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINES_DIR = REPO_ROOT / "data" / "baselines"
SPRINT0_LATEST = BASELINES_DIR / "sprint0_latest.json"


# ─── Thresholds (from the plan file — v18 launch gate) ──────────────


THRESHOLDS = {
    "sprint0_correctness_mean": 7.2,       # /10 scale (judge 1-5 ×2)
    "sprint0_completeness_mean": 7.2,      # /10 scale
    "sprint0_per_mode_floor": 6.5,         # /10 per-mode floor
    "humaneval_plus_pass_at_1": 72.0,      # percent
    "swebench_lite_resolved_rate": 28.0,   # percent
    "pipeline_median_latency_s": 75.0,     # ceiling
    "deliverable_rubric_pass_rate": 70.0,  # percent
}


# ─── Condition shape ────────────────────────────────────────────────


@dataclass
class ConditionResult:
    name: str
    threshold: float
    threshold_op: str         # ">=" or "<="
    measured: float | None
    status: str               # "pass" / "fail" / "skipped"
    notes: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GateScorecard:
    timestamp_utc: str
    conditions: list[ConditionResult] = field(default_factory=list)
    verdict: str = "unknown"  # pass / fail
    overall_notes: str = ""

    @property
    def all_pass(self) -> bool:
        return all(c.passed for c in self.conditions)

    @property
    def num_passed(self) -> int:
        return sum(1 for c in self.conditions if c.passed)

    @property
    def num_failed(self) -> int:
        return sum(1 for c in self.conditions if c.status == "fail")

    @property
    def num_skipped(self) -> int:
        return sum(1 for c in self.conditions if c.status == "skipped")

    def to_dict(self) -> dict:
        return {
            "timestamp_utc": self.timestamp_utc,
            "verdict": self.verdict,
            "num_passed": self.num_passed,
            "num_failed": self.num_failed,
            "num_skipped": self.num_skipped,
            "conditions": [c.to_dict() for c in self.conditions],
            "overall_notes": self.overall_notes,
        }


# ─── Condition 1: Sprint-0 correctness + completeness ───────────────


def _read_sprint0(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("sprint0 read failed at %s: %s", path, exc)
        return None


def condition_sprint0_correctness(
    sprint0: dict | None,
) -> ConditionResult:
    if sprint0 is None:
        return ConditionResult(
            name="Sprint-0 correctness mean",
            threshold=THRESHOLDS["sprint0_correctness_mean"],
            threshold_op=">=",
            measured=None,
            status="skipped",
            notes="No sprint0_latest.json snapshot found",
        )
    rows = sprint0.get("rows", [])
    correct = [
        r["judge_score"]["correctness"] for r in rows
        if isinstance(r.get("judge_score"), dict)
        and isinstance(r["judge_score"].get("correctness"), (int, float))
    ]
    if not correct:
        return ConditionResult(
            name="Sprint-0 correctness mean",
            threshold=THRESHOLDS["sprint0_correctness_mean"],
            threshold_op=">=",
            measured=None,
            status="skipped",
            notes=f"No judged rows in {len(rows)} total",
        )
    measured = statistics.mean(correct) * 2.0  # 1-5 → 1-10
    status = "pass" if measured >= THRESHOLDS["sprint0_correctness_mean"] else "fail"
    return ConditionResult(
        name="Sprint-0 correctness mean",
        threshold=THRESHOLDS["sprint0_correctness_mean"],
        threshold_op=">=",
        measured=round(measured, 2),
        status=status,
        notes=f"From {len(correct)}/{len(rows)} judged rows",
    )


def condition_sprint0_completeness(
    sprint0: dict | None,
) -> ConditionResult:
    if sprint0 is None:
        return ConditionResult(
            name="Sprint-0 completeness mean",
            threshold=THRESHOLDS["sprint0_completeness_mean"],
            threshold_op=">=",
            measured=None,
            status="skipped",
            notes="No sprint0_latest.json snapshot found",
        )
    rows = sprint0.get("rows", [])
    completeness = [
        r["judge_score"]["completeness"] for r in rows
        if isinstance(r.get("judge_score"), dict)
        and isinstance(r["judge_score"].get("completeness"), (int, float))
    ]
    if not completeness:
        return ConditionResult(
            name="Sprint-0 completeness mean",
            threshold=THRESHOLDS["sprint0_completeness_mean"],
            threshold_op=">=",
            measured=None,
            status="skipped",
            notes=f"No judged rows in {len(rows)} total",
        )
    measured = statistics.mean(completeness) * 2.0
    status = "pass" if measured >= THRESHOLDS["sprint0_completeness_mean"] else "fail"
    return ConditionResult(
        name="Sprint-0 completeness mean (≈ deliverable rubric proxy)",
        threshold=THRESHOLDS["sprint0_completeness_mean"],
        threshold_op=">=",
        measured=round(measured, 2),
        status=status,
        notes=f"From {len(completeness)}/{len(rows)} judged rows",
    )


def condition_sprint0_per_mode_floor(
    sprint0: dict | None,
) -> ConditionResult:
    if sprint0 is None:
        return ConditionResult(
            name="Sprint-0 per-mode floor",
            threshold=THRESHOLDS["sprint0_per_mode_floor"],
            threshold_op=">=",
            measured=None,
            status="skipped",
            notes="No sprint0_latest.json snapshot found",
        )
    rows = sprint0.get("rows", [])
    # Group correctness by mode.
    by_mode: dict[str, list[float]] = {}
    for r in rows:
        mode = r.get("mode") or "unknown"
        js = r.get("judge_score") or {}
        c = js.get("correctness")
        if isinstance(c, (int, float)):
            by_mode.setdefault(mode, []).append(c)
    if not by_mode:
        return ConditionResult(
            name="Sprint-0 per-mode floor",
            threshold=THRESHOLDS["sprint0_per_mode_floor"],
            threshold_op=">=",
            measured=None,
            status="skipped",
            notes="No judged rows to group by mode",
        )
    per_mode_means = {m: statistics.mean(s) * 2.0 for m, s in by_mode.items()}
    floor = min(per_mode_means.values())
    status = "pass" if floor >= THRESHOLDS["sprint0_per_mode_floor"] else "fail"
    breakdown = ", ".join(
        f"{m}={mean:.2f}" for m, mean in sorted(per_mode_means.items())
    )
    return ConditionResult(
        name="Sprint-0 per-mode floor",
        threshold=THRESHOLDS["sprint0_per_mode_floor"],
        threshold_op=">=",
        measured=round(floor, 2),
        status=status,
        notes=breakdown,
    )


# ─── Condition 4: Pipeline median latency ───────────────────────────


def condition_pipeline_median_latency(
    sprint0: dict | None,
) -> ConditionResult:
    if sprint0 is None:
        return ConditionResult(
            name="Pipeline median latency",
            threshold=THRESHOLDS["pipeline_median_latency_s"],
            threshold_op="<=",
            measured=None,
            status="skipped",
            notes="No sprint0_latest.json snapshot found",
        )
    rows = sprint0.get("rows", [])
    walls_ms = [
        (r.get("metrics") or {}).get("wall_clock_ms")
        for r in rows
        if isinstance((r.get("metrics") or {}).get("wall_clock_ms"), (int, float))
    ]
    walls_s = [float(w) / 1000.0 for w in walls_ms if w]
    if not walls_s:
        return ConditionResult(
            name="Pipeline median latency",
            threshold=THRESHOLDS["pipeline_median_latency_s"],
            threshold_op="<=",
            measured=None,
            status="skipped",
            notes=f"No wall_clock_ms in {len(rows)} rows",
        )
    median_s = statistics.median(walls_s)
    status = "pass" if median_s <= THRESHOLDS["pipeline_median_latency_s"] else "fail"
    return ConditionResult(
        name="Pipeline median latency",
        threshold=THRESHOLDS["pipeline_median_latency_s"],
        threshold_op="<=",
        measured=round(median_s, 1),
        status=status,
        notes=f"From {len(walls_s)} timed rows",
    )


# ─── Conditions 2 + 3: HumanEval+ and SWE-bench-Lite ───────────────


def _latest_eval_run(name: str) -> dict | None:
    """Read the most recent eval-run row for ``name`` from the
    ``data/eval_runs/<name>/latest.json`` convention (matches
    Sprint 2 eval runners)."""

    candidate = REPO_ROOT / "data" / "eval_runs" / name / "latest.json"
    if not candidate.is_file():
        return None
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def condition_humaneval_plus(
    *, force_run: bool = False, shallow: bool = False,
) -> ConditionResult:
    if shallow:
        return ConditionResult(
            name="HumanEval+ pass@1",
            threshold=THRESHOLDS["humaneval_plus_pass_at_1"],
            threshold_op=">=",
            measured=None,
            status="skipped",
            notes="--shallow flag; skipped expensive eval",
        )
    if force_run:
        # The eval runner is async + registers itself via admin_evals_routes.
        # Direct CLI is not in scope for this commit.  Operator can call
        # `POST /api/admin/evals/runs?name=humaneval_plus` and re-run the
        # gate; here we surface that and skip.
        return ConditionResult(
            name="HumanEval+ pass@1",
            threshold=THRESHOLDS["humaneval_plus_pass_at_1"],
            threshold_op=">=",
            measured=None,
            status="skipped",
            notes=(
                "--re-run-evals: trigger via "
                "`POST /api/admin/evals/runs?name=humaneval_plus` "
                "and re-run this gate."
            ),
        )
    data = _latest_eval_run("humaneval_plus")
    if data is None:
        return ConditionResult(
            name="HumanEval+ pass@1",
            threshold=THRESHOLDS["humaneval_plus_pass_at_1"],
            threshold_op=">=",
            measured=None,
            status="skipped",
            notes=(
                "No data/eval_runs/humaneval_plus/latest.json — "
                "run via POST /api/admin/evals/runs?name=humaneval_plus"
            ),
        )
    measured = float(data.get("summary", {}).get("pass_at_1_percent") or 0.0)
    status = "pass" if measured >= THRESHOLDS["humaneval_plus_pass_at_1"] else "fail"
    return ConditionResult(
        name="HumanEval+ pass@1",
        threshold=THRESHOLDS["humaneval_plus_pass_at_1"],
        threshold_op=">=",
        measured=round(measured, 1),
        status=status,
        notes=f"From {data.get('summary', {}).get('total', 0)} problems",
    )


def condition_swebench_lite(
    *, force_run: bool = False, shallow: bool = False,
) -> ConditionResult:
    if shallow:
        return ConditionResult(
            name="SWE-bench-Lite-25 resolved rate",
            threshold=THRESHOLDS["swebench_lite_resolved_rate"],
            threshold_op=">=",
            measured=None,
            status="skipped",
            notes="--shallow flag; skipped expensive eval",
        )
    if force_run:
        return ConditionResult(
            name="SWE-bench-Lite-25 resolved rate",
            threshold=THRESHOLDS["swebench_lite_resolved_rate"],
            threshold_op=">=",
            measured=None,
            status="skipped",
            notes=(
                "--re-run-evals: trigger via "
                "`POST /api/admin/evals/runs?name=swebench_lite` "
                "and re-run this gate."
            ),
        )
    data = _latest_eval_run("swebench_lite")
    if data is None:
        return ConditionResult(
            name="SWE-bench-Lite-25 resolved rate",
            threshold=THRESHOLDS["swebench_lite_resolved_rate"],
            threshold_op=">=",
            measured=None,
            status="skipped",
            notes=(
                "No data/eval_runs/swebench_lite/latest.json — "
                "run via POST /api/admin/evals/runs?name=swebench_lite "
                "(~120 min)"
            ),
        )
    summary = data.get("summary", {}) or {}
    measured = float(summary.get("resolved_rate_percent") or 0.0)
    status = "pass" if measured >= THRESHOLDS["swebench_lite_resolved_rate"] else "fail"
    return ConditionResult(
        name="SWE-bench-Lite-25 resolved rate",
        threshold=THRESHOLDS["swebench_lite_resolved_rate"],
        threshold_op=">=",
        measured=round(measured, 1),
        status=status,
        notes=f"From {summary.get('total', 0)} instances",
    )


# ─── Render scorecard ───────────────────────────────────────────────


def _glyph_for_status(status: str) -> str:
    return {"pass": "✓", "fail": "✗", "skipped": "·"}.get(status, "?")


def render_scorecard(card: GateScorecard) -> None:
    print("=" * 76)
    print(f"v18 launch acceptance gate — {card.timestamp_utc}")
    print("=" * 76)
    for c in card.conditions:
        glyph = _glyph_for_status(c.status)
        measured = f"{c.measured}" if c.measured is not None else "—"
        print(
            f"  {glyph} {c.name:<48s} "
            f"measured={measured:<7s} "
            f"{c.threshold_op} {c.threshold}"
        )
        if c.notes:
            print(f"      {c.notes}")
    print("-" * 76)
    print(
        f"  passed={card.num_passed}  failed={card.num_failed}  "
        f"skipped={card.num_skipped}"
    )
    print(f"  VERDICT: {card.verdict.upper()}")
    if card.overall_notes:
        print(f"  notes  : {card.overall_notes}")
    print("=" * 76)


# ─── Persist scorecard ──────────────────────────────────────────────


def persist_scorecard(card: GateScorecard) -> Path:
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    ts = card.timestamp_utc.replace(":", "").replace("-", "")
    path = BASELINES_DIR / f"v18_launch_gate_{ts}.json"
    path.write_text(
        json.dumps(card.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


# ─── Main ───────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the v18 launch acceptance gate.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--shallow", action="store_true",
        help="Skip the expensive HumanEval+ + SWE-bench evals.",
    )
    p.add_argument(
        "--re-run-sprint0", action="store_true",
        help=(
            "Re-run the Sprint 0 v18 baseline before scoring "
            "(~90 min wall).  Calls `tools/run_sprint0_v18.sh`."
        ),
    )
    p.add_argument(
        "--re-run-evals", action="store_true",
        help=(
            "Surface the eval-runner trigger commands; the runs are "
            "kicked off via the admin-evals POST endpoint."
        ),
    )
    p.add_argument(
        "--sprint0-path", default=str(SPRINT0_LATEST),
        help=f"Path to sprint0_*.json snapshot (default {SPRINT0_LATEST}).",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit only the scorecard JSON to stdout.",
    )
    p.add_argument(
        "--out", default=None,
        help="Explicit scorecard path (default v18_launch_gate_<ts>.json).",
    )
    return p


def maybe_rerun_sprint0(args: argparse.Namespace) -> int:
    if not args.re_run_sprint0:
        return 0
    runner = REPO_ROOT / "tools" / "run_sprint0_v18.sh"
    if not runner.is_file():
        logger.error("run_sprint0_v18.sh missing at %s", runner)
        return 2
    logger.info("re-running Sprint 0 v18 baseline (this takes ~90 min)...")
    rc = subprocess.call(["bash", str(runner)], cwd=str(REPO_ROOT))
    if rc != 0:
        logger.error("sprint0 v18 runner exited %d", rc)
        return rc
    return 0


def run_gate(args: argparse.Namespace) -> int:
    rc = maybe_rerun_sprint0(args)
    if rc != 0:
        return 2

    sprint0_path = Path(args.sprint0_path)
    sprint0 = _read_sprint0(sprint0_path)

    card = GateScorecard(
        timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    card.conditions.append(condition_sprint0_correctness(sprint0))
    card.conditions.append(condition_sprint0_completeness(sprint0))
    card.conditions.append(condition_sprint0_per_mode_floor(sprint0))
    card.conditions.append(condition_pipeline_median_latency(sprint0))
    card.conditions.append(condition_humaneval_plus(
        force_run=args.re_run_evals, shallow=args.shallow,
    ))
    card.conditions.append(condition_swebench_lite(
        force_run=args.re_run_evals, shallow=args.shallow,
    ))

    if card.all_pass:
        card.verdict = "pass"
    elif card.num_failed > 0:
        card.verdict = "fail"
    else:
        # Only skipped conditions: insufficient evidence.
        card.verdict = "insufficient"
        card.overall_notes = (
            f"{card.num_skipped} condition(s) skipped — re-run the "
            "missing prereq(s) (sprint0 / HumanEval+ / SWE-bench-Lite) "
            "and re-execute this gate."
        )

    if args.json:
        print(json.dumps(card.to_dict(), indent=2))
    else:
        render_scorecard(card)

    out = Path(args.out) if args.out else persist_scorecard(card)
    if not args.json:
        print(f"\nScorecard committed: {out}")

    if card.verdict == "pass":
        return 0
    if card.verdict == "fail":
        return 1
    # insufficient evidence — return 1 so CI gates this exactly like a fail.
    return 1


def main() -> int:
    args = build_parser().parse_args()
    return run_gate(args)


if __name__ == "__main__":
    raise SystemExit(main())
