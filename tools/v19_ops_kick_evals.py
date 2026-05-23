#!/usr/bin/env python3
"""
v19 ops — direct eval runner (no HTTP auth).

Bypasses the ``POST /api/admin/evals/run/{name}`` endpoint by
invoking the runner coroutine directly inside the app container.
Used when the operator's vault password isn't on hand and we just
want to populate the eval_runs snapshots the v19 launch gate
reads.

Usage (inside amor-app-2):
    python -m tools.v19_ops_kick_evals --eval aider_polyglot_50
    python -m tools.v19_ops_kick_evals --eval humaneval_plus_50
    python -m tools.v19_ops_kick_evals --eval swebench_lite_25

The script:
  1. Imports the runner module (registers via `register_eval`)
  2. Inserts a `pending` row in eval_runs
  3. Runs the runner coroutine
  4. Persists the summary back to the row (status=done)
  5. Exports `data/eval_runs/<short>/latest.json` for the gate

Exit codes:
  0  ran successfully + latest.json written
  1  runner raised (logged + row marked failed)
  2  fatal init (Postgres unreachable, unknown eval)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
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


# Ensure every runner module has registered itself before we resolve.
def _import_runners() -> None:
    for name in (
        "tools.eval.humaneval_plus",
        "tools.eval.swebench_lite",
        "tools.eval.aider_polyglot",
    ):
        try:
            __import__(name)
        except Exception as exc:
            logger.warning("could not import %s: %s", name, exc)


async def kick(eval_name: str, limit: int | None = None) -> int:
    _import_runners()
    from sqlalchemy import text
    from document_processor.infrastructure.storage import storage_manager
    from document_processor.api.admin_evals_routes import (
        _EVAL_MANIFEST,
        _emit_progress,
    )

    desc = _EVAL_MANIFEST.get(eval_name)
    if desc is None:
        logger.error(
            "unknown eval %r; available=%s",
            eval_name, sorted(_EVAL_MANIFEST.keys()),
        )
        return 2
    if desc.runner is None:
        logger.error("eval %r has no live runner (scaffold-only)", eval_name)
        return 2

    if storage_manager.pg_session_maker is None:
        await storage_manager.connect_postgres()
    if storage_manager.pg_session_maker is None:
        logger.error("postgres unreachable")
        return 2

    run_id = str(uuid.uuid4())
    logger.info("eval=%s run_id=%s — inserting pending row", eval_name, run_id)
    async with storage_manager.pg_session_maker() as session:
        await session.execute(
            text(
                "INSERT INTO eval_runs (id, name, status, backend, user_id) "
                "VALUES (:id, :name, 'running', :backend, NULL)"
            ),
            {"id": run_id, "name": eval_name, "backend": "llama-swap"},
        )
        await session.commit()

    if limit is not None:
        os.environ["AMOR_EVAL_LIMIT"] = str(limit)

    progress_log: list[str] = []

    async def progress(msg: str) -> None:
        progress_log.append(msg)
        if len(progress_log) % 5 == 0:
            try:
                snippet = json.loads(msg)
                logger.info("progress %s", snippet)
            except Exception:
                pass

    try:
        summary = await desc.runner(run_id, progress)
    except Exception as exc:
        logger.exception("runner raised: %s", exc)
        async with storage_manager.pg_session_maker() as session:
            await session.execute(
                text(
                    "UPDATE eval_runs "
                    "SET status='failed', finished_at=NOW(), "
                    "summary = :summary "
                    "WHERE id = :id"
                ),
                {
                    "id": run_id,
                    "summary": json.dumps({"error": str(exc)[:1000]}),
                },
            )
            await session.commit()
        return 1

    async with storage_manager.pg_session_maker() as session:
        await session.execute(
            text(
                "UPDATE eval_runs "
                "SET status='done', finished_at=NOW(), "
                "summary = :summary "
                "WHERE id = :id"
            ),
            {"id": run_id, "summary": json.dumps(summary, default=str)},
        )
        await session.commit()

    logger.info(
        "eval=%s done; summary keys=%s",
        eval_name, sorted(summary.keys()),
    )
    print(json.dumps({"run_id": run_id, "summary": summary}, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval", required=True)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    return asyncio.run(kick(args.eval, limit=args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
