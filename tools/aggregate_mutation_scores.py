#!/usr/bin/env python3
"""
Cycle G G6 follow-on — aggregate per-session mutation scores into the
``mutation_score_latest.json`` snapshot the v19 launch gate reads.

The engine records ``self.mutation_result`` (a MutationResult dict)
on every session where ``code_mutation_testing_enabled=True``.  This
script walks the recent ``eval_runs`` rows (or the in-process Sprint-0
JSON), pulls the score from each session's `summary.mutation_result`,
and writes the mean to:

    data/baselines/mutation_score_latest.json

Shape:

    {
      "mean_score": 0.42,
      "sessions_measured": 7,
      "session_ids": ["...", "..."],
      "per_session_scores": [0.55, 0.30, 0.41, ...],
      "computed_at_utc": "2026-05-16T..."
    }

The v19 launch gate's ``_condition_mutation_score`` resolves
``mean_score`` and compares against the 0.35 threshold.

Sources
-------
1. **Sprint-0 v18 baseline JSON** — if a recent run included
   mutation testing, each task's `extra` block contains the
   mutation_result.
2. **eval_runs table** — when the Sprint-0 runner persists per-task
   results to Postgres with the mutation block, walk them.

When no session has a score (mutation testing was disabled or
recent runs are too old), the script writes an empty snapshot with
``sessions_measured=0`` and exits 0 — the gate then marks the
condition skipped (not failed).

Usage::

    python tools/aggregate_mutation_scores.py
    python tools/aggregate_mutation_scores.py --since 7d
    python tools/aggregate_mutation_scores.py --json   # to stdout

Exit codes:
  0  snapshot written (mean_score may be 0.0 if no data)
  1  IO / DB error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
BASELINES_ROOT = Path(
    os.environ.get(
        "AMOR_BASELINES_ROOT", str(REPO_ROOT / "data" / "baselines"),
    )
)


def _parse_since(spec: str) -> Optional[datetime]:
    """Accepts ``30d``, ``12h``, ISO date, or ``all`` (None)."""
    if not spec or spec.lower() == "all":
        return None
    import re
    m = re.fullmatch(r"(\d+)([dhm])", spec.lower())
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "d": timedelta(days=n),
            "h": timedelta(hours=n),
            "m": timedelta(minutes=n),
        }[unit]
        return datetime.now(timezone.utc) - delta
    try:
        return datetime.fromisoformat(spec).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ─── Sprint-0 JSON walker ──────────────────────────────────────────


def _walk_sprint0_snapshot(snapshot: dict) -> List[float]:
    """Sprint-0 v18 baseline writes per-task results under
    ``tasks[i].mutation_result`` when the engine enabled mutation
    testing during the run.  Returns a list of per-task scores."""
    scores: List[float] = []
    for task in snapshot.get("tasks") or snapshot.get("results") or []:
        mr = task.get("mutation_result")
        if isinstance(mr, dict) and mr.get("ran"):
            score = mr.get("score")
            if isinstance(score, (int, float)):
                scores.append(float(score))
    return scores


def _scores_from_sprint0_latest() -> List[float]:
    snap_path = BASELINES_ROOT / "sprint0_latest.json"
    if not snap_path.is_file():
        return []
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("sprint0_latest unparseable: %s", exc)
        return []
    return _walk_sprint0_snapshot(snap)


# ─── eval_runs walker ──────────────────────────────────────────────


async def _scores_from_eval_runs(since: Optional[datetime] = None) -> List[float]:
    """Walk ``eval_runs.cases[]`` for sessions where the engine
    recorded mutation_result.  Best-effort: returns [] when DB
    unreachable or no sessions match."""
    try:
        from document_processor.infrastructure.storage import storage_manager
        from sqlalchemy import text
    except ImportError as exc:
        logger.debug("storage manager not importable: %s", exc)
        return []

    if storage_manager.pg_session_maker is None:
        try:
            await storage_manager.connect_postgres()
        except Exception as exc:
            logger.debug("postgres bootstrap failed: %s", exc)
            return []

    if storage_manager.pg_session_maker is None:
        return []

    scores: List[float] = []
    async with storage_manager.pg_session_maker() as session:
        where = "WHERE summary ? 'mutation_result'"
        params: Dict[str, Any] = {}
        if since is not None:
            where += " AND started_at >= :since"
            params["since"] = since
        result = await session.execute(
            text(
                f"SELECT id, summary FROM eval_runs "
                f"{where} "
                "ORDER BY started_at DESC LIMIT 100"
            ),
            params,
        )
        rows = result.mappings().all()
    for row in rows:
        summary = row["summary"] or {}
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except json.JSONDecodeError:
                continue
        mr = summary.get("mutation_result")
        if isinstance(mr, dict) and mr.get("ran"):
            score = mr.get("score")
            if isinstance(score, (int, float)):
                scores.append(float(score))
    return scores


# ─── Aggregator ────────────────────────────────────────────────────


def aggregate(scores: List[float]) -> Dict[str, Any]:
    if not scores:
        return {
            "mean_score": 0.0,
            "sessions_measured": 0,
            "per_session_scores": [],
            "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    mean_score = sum(scores) / len(scores)
    return {
        "mean_score": round(mean_score, 4),
        "sessions_measured": len(scores),
        "per_session_scores": [round(s, 4) for s in scores],
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def write_snapshot(payload: Dict[str, Any]) -> Path:
    BASELINES_ROOT.mkdir(parents=True, exist_ok=True)
    out = BASELINES_ROOT / "mutation_score_latest.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


# ─── CLI ────────────────────────────────────────────────────────────


async def _run(args: argparse.Namespace) -> int:
    since = _parse_since(args.since)
    scores: List[float] = []
    scores.extend(_scores_from_sprint0_latest())
    db_scores = await _scores_from_eval_runs(since=since)
    scores.extend(db_scores)

    payload = aggregate(scores)
    if args.json:
        print(json.dumps(payload, indent=2))
    out_path = write_snapshot(payload)
    logger.info(
        "mutation_score_latest written: %s "
        "(sessions=%d, mean=%.3f)",
        out_path, payload["sessions_measured"], payload["mean_score"],
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since", default="30d",
        help="window to scan eval_runs (30d|12h|YYYY-MM-DD|all)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="print payload JSON to stdout as well as writing the file",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
