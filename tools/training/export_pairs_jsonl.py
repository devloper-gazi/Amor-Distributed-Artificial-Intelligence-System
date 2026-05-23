#!/usr/bin/env python3
"""
Cycle C Sprint 6 Day 2 — preference-pair JSONL exporter.

Reads the ``preference_pairs`` table (Sprint 6 Day 1) and writes one
JSONL line per row in the shape ORPOTrainer expects::

    {"prompt": "...", "chosen": "...", "rejected": "..."}

Privacy
-------
By default ``--require-opt-in`` is True — we only export rows where
the user explicitly opted in to raw-text persistence.  Pass
``--allow-hash-only`` to include hash-only rows (the trainer treats
them as no-op skips since the prompt/chosen/rejected fields are
empty strings, but the count is useful for debugging).

Usage
-----

    python tools/training/export_pairs_jsonl.py \\
        --out /app/data/training/pairs_2026-05-04.jsonl \\
        --since 30d \\
        --mode build \\
        --max-rows 1000

Exits 0 on success and prints the row count to stdout.  Exits 2 on
DB / IO errors (so a CI pipeline can branch on it).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _parse_since(spec: str) -> datetime | None:
    """``--since`` accepts ``30d``, ``12h``, ``2026-01-01``, or ``all``.
    Returns the cut-off timestamp (None for "all")."""
    if spec.lower() == "all":
        return None
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
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--since must be ``30d``, ``12h``, ``YYYY-MM-DD``, or ``all`` ({exc})",
        ) from exc


async def run(args: argparse.Namespace) -> int:
    """Async core — opens a Postgres session via storage_manager and
    streams rows directly to JSONL without buffering everything."""
    sys.path.insert(0, "/app")
    from sqlalchemy import text  # noqa: PLC0415

    from document_processor.infrastructure.storage import (  # noqa: PLC0415
        storage_manager,
    )

    if storage_manager.pg_session_maker is None:
        # Lazy bootstrap — when this script is invoked outside the
        # app process the engine isn't initialised yet.  Storage
        # exposes ``connect_postgres`` (singular for each backend).
        await storage_manager.connect_postgres()

    if storage_manager.pg_session_maker is None:
        logger.error("Postgres unavailable — cannot export")
        return 2

    where: list[str] = []
    params: dict[str, object] = {}
    if args.require_opt_in:
        where.append("opt_in_raw IS TRUE")
    if args.untrained_only:
        where.append("trained_in IS NULL")
    if args.mode:
        where.append("mode = :mode")
        params["mode"] = args.mode
    cutoff = _parse_since(args.since)
    if cutoff is not None:
        where.append("created_at >= :cutoff")
        params["cutoff"] = cutoff
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params["limit"] = args.max_rows

    sql = text(
        f"""
        SELECT id, prompt, chosen, rejected, mode, created_at
        FROM preference_pairs
        {where_sql}
        ORDER BY created_at DESC
        LIMIT :limit
        """,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n_total = 0
    n_skipped = 0
    n_written = 0
    async with storage_manager.pg_session_maker() as session:
        result = await session.execute(sql, params)
        rows = result.mappings().all()

    with out_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            n_total += 1
            prompt = r["prompt"] or ""
            chosen = r["chosen"] or ""
            rejected = r["rejected"] or ""
            if not args.allow_hash_only and not (prompt and (chosen or rejected)):
                n_skipped += 1
                continue
            fh.write(
                json.dumps(
                    {
                        "id": str(r["id"]),
                        "prompt": prompt,
                        "chosen": chosen,
                        "rejected": rejected,
                        "mode": r["mode"],
                    },
                    ensure_ascii=False,
                ),
            )
            fh.write("\n")
            n_written += 1

    print(
        json.dumps(
            {
                "out": str(out_path),
                "total_matched": n_total,
                "written": n_written,
                "skipped_hash_only": n_skipped,
                "since": args.since,
                "mode": args.mode,
            },
            indent=2,
        ),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Export preference pairs from Postgres to ORPO JSONL",
    )
    p.add_argument("--out", required=True, help="output JSONL path")
    p.add_argument("--since", default="30d", help="window (30d | 12h | YYYY-MM-DD | all)")
    p.add_argument("--mode", default=None, help="filter by mode (build|research|...)")
    p.add_argument("--max-rows", type=int, default=10_000, help="LIMIT cap")
    p.add_argument(
        "--allow-hash-only",
        action="store_true",
        help="include rows that have only the SHA-256 (no raw text)",
    )
    p.add_argument(
        "--no-opt-in",
        dest="require_opt_in",
        action="store_false",
        help="(unsafe) include rows with opt_in_raw=False — privacy violation",
    )
    p.set_defaults(require_opt_in=True)
    p.add_argument(
        "--include-trained",
        dest="untrained_only",
        action="store_false",
        help="include rows already attributed to a training run",
    )
    p.set_defaults(untrained_only=True)
    return p


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except Exception as exc:  # pragma: no cover
        logger.error("export crashed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
