#!/usr/bin/env python3
"""
Cycle F Sprint 6 (Step 5 bridge) — export latest eval_run row from
Postgres to `data/eval_runs/<name>/latest.json`.

The Cycle C Sprint 2 eval runners (`tools/eval/humaneval_plus.py`,
`tools/eval/swebench_lite.py`) persist results to Postgres via
the `eval_runs` table.  The Cycle F v18 launch gate runner
(`tools/run_v18_launch_gate.py`) reads from
`data/eval_runs/<name>/latest.json` to gate conditions #2 + #3.

This script bridges the two: read the most recent
`status="succeeded"` row for the given eval name, normalise into
the gate's expected `{summary: {pass_at_1_percent | resolved_rate_percent, total}}`
shape, and write it to disk.

Usage:
  python tools/eval/export_latest.py humaneval_plus_50
  python tools/eval/export_latest.py swebench_lite_25
  python tools/eval/export_latest.py --all      # both

Exit codes:
  0  exported (file written)
  1  no successful run found for the requested eval
  2  fatal init (postgres unreachable, schema mismatch)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
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


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# Cycle F Sprint 6 — `/app/data/` is a named volume inside the
# container (not bind-mounted to host), so writes there are invisible
# to the v18 launch gate runner that lives on the host filesystem.
# `AMOR_EVAL_OUT_ROOT` env override lets the operator point the
# export at `/data/documents/eval_runs` (which maps to host
# `./data/eval_runs/`) when running from inside amor-app-2.
OUT_ROOT = Path(
    os.environ.get(
        "AMOR_EVAL_OUT_ROOT", str(REPO_ROOT / "data" / "eval_runs"),
    )
)


# Map full eval name → gate's expected directory name.
# The v18 launch gate's `_latest_eval_run()` reads from
# `data/eval_runs/<short_name>/latest.json`.
GATE_NAME_MAP: dict[str, str] = {
    "humaneval_plus_50": "humaneval_plus",
    "swebench_lite_25": "swebench_lite",
}


def _normalise_summary(eval_name: str, raw: dict) -> dict:
    """Coerce the eval_runs.summary JSON into the shape the v18
    launch gate expects.  Different runners stash different keys;
    we surface `pass_at_1_percent` / `resolved_rate_percent` so
    `tools/run_v18_launch_gate.py:condition_humaneval_plus` +
    `condition_swebench_lite` can read directly."""

    if eval_name == "humaneval_plus_50":
        # The Cycle C runner stores `pass_at_1` as a FRACTION (0.0-1.0).
        # The v18 launch gate compares against a percent threshold
        # (e.g. 72).  Normalise here: if the value already looks
        # percent-shaped (>1), pass through; otherwise multiply ×100.
        raw_p = raw.get("pass_at_1_percent") or raw.get("pass_at_1")
        if raw_p is None and raw.get("total"):
            raw_p = raw["passed"] / raw["total"]
        if raw_p is None:
            pass_at_1 = 0.0
        else:
            pass_at_1 = float(raw_p)
            if pass_at_1 <= 1.0:
                pass_at_1 *= 100.0  # fraction → percent
        total = raw.get("total") or 0
        return {
            "pass_at_1_percent": pass_at_1,
            "total": int(total),
            "raw": raw,
        }

    if eval_name == "swebench_lite_25":
        resolved = (
            raw.get("resolved_rate_percent")
            or (
                100.0 * raw["resolved"] / raw["total"]
                if raw.get("total")
                else None
            )
        )
        total = raw.get("total") or 0
        return {
            "resolved_rate_percent": float(resolved or 0.0),
            "total": int(total),
            "raw": raw,
        }

    # Unknown eval — pass raw through verbatim.
    return {"raw": raw}


async def _export(eval_name: str) -> int:
    """Read the most recent successful eval_runs row + write
    `data/eval_runs/<short_name>/latest.json`."""

    try:
        from document_processor.infrastructure.storage import storage_manager
        from sqlalchemy import text
    except ImportError as exc:
        logger.error("storage_manager unavailable: %s", exc)
        return 2

    if storage_manager.pg_session_maker is None:
        # Bootstrap if not already.
        try:
            await storage_manager.connect_postgres()
        except Exception as exc:
            logger.error("postgres bootstrap failed: %s", exc)
            return 2

    if storage_manager.pg_session_maker is None:
        logger.error("postgres still not bootstrapped after connect")
        return 2

    async with storage_manager.pg_session_maker() as session:
        # Cycle C eval_runs uses `status='done'` for terminal-success
        # (and `'failed'` / `'error'` otherwise).  Initial draft of
        # this script filtered on `'succeeded'`, which never matched.
        result = await session.execute(
            text(
                "SELECT id, name, status, summary, started_at, finished_at "
                "FROM eval_runs "
                "WHERE name = :name AND status = 'done' "
                "ORDER BY finished_at DESC LIMIT 1"
            ),
            {"name": eval_name},
        )
        row = result.mappings().first()

    if row is None:
        logger.error(
            "no succeeded eval_runs row for name=%s — kick a run via "
            "POST /api/admin/evals/run/%s first",
            eval_name, eval_name,
        )
        return 1

    raw_summary = row["summary"] or {}
    if isinstance(raw_summary, str):
        try:
            raw_summary = json.loads(raw_summary)
        except json.JSONDecodeError:
            raw_summary = {}

    payload = {
        "name": row["name"],
        "status": row["status"],
        "run_id": str(row["id"]),
        "started_at": str(row["started_at"]) if row["started_at"] else None,
        "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
        "summary": _normalise_summary(eval_name, raw_summary),
    }

    short = GATE_NAME_MAP.get(eval_name, eval_name)
    out_dir = OUT_ROOT / short
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "latest.json"
    out_path.write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info(
        "exported %s -> %s (pass_at_1=%s | resolved=%s)",
        eval_name, out_path,
        payload["summary"].get("pass_at_1_percent"),
        payload["summary"].get("resolved_rate_percent"),
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "eval_name", nargs="?", default=None,
        choices=list(GATE_NAME_MAP.keys()) + [None],
        help="Eval name to export (omit + --all to export every supported eval).",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Export every eval listed in GATE_NAME_MAP.",
    )
    args = parser.parse_args()

    targets: list[str]
    if args.all or args.eval_name is None:
        targets = list(GATE_NAME_MAP.keys())
    else:
        targets = [args.eval_name]

    import asyncio
    overall = 0
    for name in targets:
        rc = asyncio.run(_export(name))
        if rc != 0:
            overall = rc if overall == 0 else overall
    return overall


if __name__ == "__main__":
    raise SystemExit(main())
