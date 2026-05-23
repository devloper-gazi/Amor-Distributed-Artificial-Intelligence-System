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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

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
EXPORTER = REPO_ROOT / "tools" / "training" / "export_pairs_jsonl.py"

# v18.1 Step 2 — Postgres → JSONL bridge.  Until the preference_pairs
# table gains a `role` column (Cycle G G5 sub-agent attribution), all
# 3 role-level trainings read the same source JSONL.  The cron emits
# `data/preference_pairs/build.jsonl` and `train_one_role()` falls
# back to it when the per-role file (`coder.jsonl` etc) is missing.
SHARED_SOURCE_FILE = PAIRS_ROOT / "build.jsonl"
EXPORT_TIMESTAMP_FILE = PAIRS_ROOT / ".last_export"
EXPORT_IDEMPOTENCY_HOURS = 24

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
    export: "ExportResult | None" = None
    results: list[RoleTrainingResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp_utc": self.timestamp_utc,
            "export": (
                {
                    "status": self.export.status,
                    "rows_written": self.export.rows_written,
                    "error": self.export.error,
                    "path": str(self.export.path) if self.export.path else None,
                }
                if self.export
                else None
            ),
            "results": [r.to_dict() for r in self.results],
        }

    @property
    def overall_exit_code(self) -> int:
        # 0 unless at least one role failed OR the export hard-failed.
        any_failed = any(r.status == "failed" for r in self.results)
        export_failed = self.export is not None and self.export.status == "failed"
        return 1 if (any_failed or export_failed) else 0


# ─── Step 0: Postgres → JSONL bridge (v18.1 Step 2) ────────────────


@dataclass
class ExportResult:
    """Result of the Postgres-to-JSONL export step."""

    status: str               # "exported" | "skipped_fresh" | "failed" | "skipped_no_db"
    rows_written: int = 0
    error: str = ""
    path: Path | None = None


def _export_needs_refresh(now: datetime, hours: int) -> bool:
    """True when the timestamp sidecar is missing or older than `hours`."""
    if not EXPORT_TIMESTAMP_FILE.is_file():
        return True
    try:
        last_iso = EXPORT_TIMESTAMP_FILE.read_text(encoding="utf-8").strip()
        last = datetime.fromisoformat(last_iso)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (OSError, ValueError):
        return True
    return (now - last) >= timedelta(hours=hours)


def export_preference_pairs(
    *,
    dry_run: bool = False,
    force: bool = False,
    since: str = "30d",
    mode: str = "build",
    max_rows: int = 10_000,
    idempotency_hours: int = EXPORT_IDEMPOTENCY_HOURS,
) -> ExportResult:
    """Step 0 of the weekly cron — invoke export_pairs_jsonl.py to
    refresh `SHARED_SOURCE_FILE` from Postgres.

    Idempotent: when the `.last_export` sidecar is fresher than
    `idempotency_hours`, the export is skipped (`status="skipped_fresh"`)
    so re-running the cron within the day doesn't re-hit the DB.
    Pass `force=True` to bypass the freshness check.
    """

    now = datetime.now(timezone.utc)
    if not force and not _export_needs_refresh(now, idempotency_hours):
        return ExportResult(
            status="skipped_fresh",
            error=(
                f"last export <{idempotency_hours}h old "
                f"({EXPORT_TIMESTAMP_FILE}); pass --force-export to bypass"
            ),
            path=SHARED_SOURCE_FILE if SHARED_SOURCE_FILE.is_file() else None,
        )

    if not EXPORTER.is_file():
        return ExportResult(
            status="failed",
            error=f"exporter missing at {EXPORTER}",
        )

    PAIRS_ROOT.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(EXPORTER),
        "--out", str(SHARED_SOURCE_FILE),
        "--since", since,
        "--mode", mode,
        "--max-rows", str(max_rows),
    ]

    if dry_run:
        logger.info("[export] would run: %s", " ".join(cmd))
        return ExportResult(
            status="skipped_fresh",
            error="dry-run",
            path=SHARED_SOURCE_FILE,
        )

    logger.info("[export] launching: %s", " ".join(cmd))
    try:
        rc = subprocess.call(cmd, cwd=str(REPO_ROOT))
    except FileNotFoundError as exc:
        return ExportResult(status="failed", error=f"exec failed: {exc}")

    if rc != 0:
        # rc == 2 = Postgres unavailable.  Treat as soft-fail so the
        # training pass can still use any existing JSONL on disk.
        return ExportResult(
            status="skipped_no_db" if rc == 2 else "failed",
            error=f"exporter exited {rc}",
            path=SHARED_SOURCE_FILE if SHARED_SOURCE_FILE.is_file() else None,
        )

    rows = _pair_count(SHARED_SOURCE_FILE)
    EXPORT_TIMESTAMP_FILE.write_text(
        now.isoformat(), encoding="utf-8",
    )
    logger.info(
        "[export] %d rows written to %s",
        rows, SHARED_SOURCE_FILE,
    )
    return ExportResult(
        status="exported",
        rows_written=rows,
        path=SHARED_SOURCE_FILE,
    )


# ─── One role ──────────────────────────────────────────────────────


def _pair_count(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.open(encoding="utf-8") if line.strip())


def _resolve_pairs_file(role: str) -> Path:
    """Return the JSONL file `train_one_role` should consume.

    v18.1: per-role files (`coder.jsonl` / `tester.jsonl` / `debugger.jsonl`)
    take precedence if present, otherwise fall back to the shared
    `build.jsonl` produced by Step 0.  When the schema gains a `role`
    column (Cycle G G5), per-role files come back as the canonical
    source and this fallback can be removed.
    """
    per_role = PAIRS_ROOT / f"{role}.jsonl"
    if per_role.is_file():
        return per_role
    return SHARED_SOURCE_FILE


def train_one_role(
    role: str,
    *,
    timestamp: str,
    dry_run: bool = False,
    min_pairs: int = 50,
    trainer_type: str = "orpo",
    pairs_file_override: Optional[Path] = None,
) -> RoleTrainingResult:
    """Train a single role.  Result populates the WeeklyRunReport."""

    pairs_file = pairs_file_override or _resolve_pairs_file(role)
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
    # Cycle H.0.3 — forward trainer-type to the launcher.  ORPO mode
    # keeps the legacy command shape identical (no flag added).
    if trainer_type and trainer_type.lower() != "orpo":
        cmd.extend(["--trainer-type", trainer_type.lower()])
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
    if report.export is not None:
        lines.append("## Step 0: Postgres → JSONL export")
        lines.append("")
        lines.append(f"- status: **{report.export.status}**")
        lines.append(f"- rows: {report.export.rows_written}")
        if report.export.path:
            lines.append(f"- output: `{report.export.path}`")
        if report.export.error:
            lines.append(f"- note: {report.export.error}")
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
    # v18.1 Step 2 — export step controls
    p.add_argument(
        "--force-export", action="store_true",
        help="Bypass the 24h export idempotency check; re-hit Postgres.",
    )
    p.add_argument(
        "--skip-export", action="store_true",
        help=(
            "Skip the Step 0 Postgres → JSONL export entirely.  Use when "
            "operator has dropped a JSONL into preference_pairs/ by hand."
        ),
    )
    p.add_argument(
        "--export-mode", default="build",
        help="--mode forwarded to export_pairs_jsonl.py (default 'build').",
    )
    p.add_argument(
        "--export-since", default="30d",
        help="--since forwarded to export_pairs_jsonl.py (default '30d').",
    )
    # Cycle H.0.3 — verifier-reward annotation + GRPO mode.
    p.add_argument(
        "--trainer-type",
        choices=("orpo", "grpo"),
        default="orpo",
        help=(
            "Forwarded to orpo_qwen_coder.py.  Default 'orpo' keeps "
            "Cycle F semantics; 'grpo' opts into TRL>=0.18 GRPOTrainer "
            "with verifier-derived scalar rewards."
        ),
    )
    p.add_argument(
        "--skip-reward-annotation", action="store_true",
        help=(
            "Skip Step 0a verifier_rewards annotation even when "
            "--trainer-type=grpo.  Use when the JSONL was hand-annotated "
            "or recorded reward fields upstream."
        ),
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

    # v18.1 Step 2 — Step 0: Postgres → JSONL bridge.
    if not args.skip_export:
        report.export = export_preference_pairs(
            dry_run=args.dry_run,
            force=args.force_export,
            since=args.export_since,
            mode=args.export_mode,
        )
        if report.export.status == "failed":
            logger.error("export step failed: %s", report.export.error)
            # Continue to training anyway — operator may have hand-dropped
            # JSONL that's still usable.

    # Cycle H.0.3 — Step 0a: verifier-reward annotation.  Only fires
    # when GRPO mode is selected; ORPO ignores reward columns so the
    # extra disk/CPU pass would be wasted.  Per-role JSONL is rewritten
    # in-place with `.rewards.jsonl` suffix and the next train_one_role
    # call points at the annotated path.
    trainer_type = getattr(args, "trainer_type", "orpo").lower()
    annotated_paths: Dict[str, Path] = {}
    if (
        trainer_type == "grpo"
        and not getattr(args, "skip_reward_annotation", False)
        and not args.dry_run
    ):
        try:
            from tools.training.verifier_rewards import (  # noqa: PLC0415
                annotate_jsonl_file,
            )
        except ImportError as exc:
            logger.warning(
                "verifier_rewards module unavailable — GRPO will need "
                "pre-annotated JSONL (%s)", exc,
            )
            annotate_jsonl_file = None
        if annotate_jsonl_file is not None:
            for role in args.role or list(ROLES):
                src = _resolve_pairs_file(role)
                if not src.is_file():
                    continue
                dst = src.with_suffix(".rewards.jsonl")
                try:
                    stats = annotate_jsonl_file(src, dst)
                    annotated_paths[role] = dst
                    logger.info(
                        "[%s] verifier_rewards annotated %d rows -> %s",
                        role, stats.get("rows_in", 0), dst,
                    )
                except Exception as exc:
                    logger.warning(
                        "[%s] verifier_rewards annotation failed: %s",
                        role, exc,
                    )

    selected_roles = args.role if args.role else list(ROLES)

    for role in selected_roles:
        result = train_one_role(
            role,
            timestamp=timestamp.replace(":", "").replace("-", ""),
            dry_run=args.dry_run,
            min_pairs=args.min_pairs,
            trainer_type=trainer_type,
            pairs_file_override=annotated_paths.get(role),
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
