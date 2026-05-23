"""
Cycle C Sprint 2 Day 1 — admin Eval harness endpoints.

Endpoints
---------
GET  /api/admin/evals/manifest             — supported eval names + metadata
GET  /api/admin/evals/runs                 — last N runs (default 50)
GET  /api/admin/evals/runs/{id}            — one run with cases payload
POST /api/admin/evals/run/{name}           — kick off an eval async
GET  /api/admin/evals/runs/{id}/stream     — SSE progress (skeleton; runner
                                             writes status_message rows)

Persistence: ``eval_runs`` table created at startup if missing
(migration shipped at ``migrations/003_eval_runs.sql``).  The table
schema is also baked into ``_EVAL_RUNS_DDL`` here so a fresh Postgres
gets it without operator action — identical pattern to ``auth/service.py``.

Day 1 ships the routes + persistence + manifest registry.  Days 2-4
will register concrete eval runners (HumanEval+, SWE-bench-Lite-25,
RAGAS) into the manifest.  Today an unrecognised name returns 422.

Auth: every endpoint requires ``get_current_user``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..infrastructure.storage import storage_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/evals", tags=["admin-evals"])


# ─── DDL — runs at startup, idempotent ─────────────────────────────


_EVAL_RUNS_DDL = [
    'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"',
    """
    CREATE TABLE IF NOT EXISTS eval_runs (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        name VARCHAR(64) NOT NULL,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ,
        status VARCHAR(16) NOT NULL DEFAULT 'pending',
        backend VARCHAR(32) NOT NULL DEFAULT 'ollama',
        git_sha CHAR(40),
        summary JSONB NOT NULL DEFAULT '{}'::jsonb,
        cases JSONB,
        note TEXT,
        user_id UUID,
        CONSTRAINT eval_runs_status_values CHECK (
            status IN ('pending', 'running', 'done', 'failed', 'cancelled')
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_eval_runs_name_started
        ON eval_runs (name, started_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_eval_runs_status_started
        ON eval_runs (status, started_at DESC)
    """,
]


async def ensure_eval_runs_schema() -> None:
    """Apply DDL if missing.  Called once at app startup; idempotent
    via ``IF NOT EXISTS``."""
    if storage_manager.pg_session_maker is None:
        logger.warning("eval_runs schema deferred — Postgres not bootstrapped")
        return
    try:
        async with storage_manager.pg_session_maker() as session:
            for stmt in _EVAL_RUNS_DDL:
                await session.execute(text(stmt))
            await session.commit()
        logger.info("eval_runs schema ready")
    except Exception as exc:  # pragma: no cover — startup hardening
        logger.error("eval_runs schema setup failed: %s", exc)


# ─── eval manifest — Days 2-4 register concrete runners here ────────


@dataclass(frozen=True)
class EvalDescriptor:
    """One supported eval.  ``runner`` is an async callable that takes
    a ``run_id`` and a ``progress_cb(message: str)`` and returns the
    final summary dict.  Days 2-4 register these via ``register_eval``."""

    name: str
    title: str
    description: str
    expected_minutes: int
    summary_keys: tuple[str, ...]   # keys that should appear in summary JSON
    runner: Optional[
        Callable[[str, Callable[[str], Awaitable[None]]], Awaitable[Dict[str, Any]]]
    ] = None


_EVAL_MANIFEST: Dict[str, EvalDescriptor] = {
    # Day 2 will register the actual runner; today this is a name-only
    # placeholder so the dashboard can render the list.
    "humaneval_plus_50": EvalDescriptor(
        name="humaneval_plus_50",
        title="HumanEval+ 50",
        description=(
            "EvalPlus HumanEval+ subset (first 50 problems) against the "
            "active LLM backend.  Pass@1 + p50 latency."
        ),
        expected_minutes=25,
        summary_keys=("pass_at_1", "passed", "total", "p50_ms", "p95_ms"),
        runner=None,
    ),
    "swebench_lite_25": EvalDescriptor(
        name="swebench_lite_25",
        title="SWE-bench-Lite 25",
        description=(
            "25-instance SWE-bench-Lite curated subset (5 from each of "
            "django, sympy, pytest, scikit-learn, requests).  Resolved "
            "rate + mean wall."
        ),
        expected_minutes=120,
        summary_keys=("resolved", "total", "resolved_rate", "mean_wall_s"),
        runner=None,
    ),
    "ragas_50": EvalDescriptor(
        name="ragas_50",
        title="RAGAS 50",
        description=(
            "50-query RAGAS sweep over the LanceDB store.  "
            "faithfulness / answer_relevancy / context_precision."
        ),
        expected_minutes=10,
        summary_keys=(
            "faithfulness",
            "answer_relevancy",
            "context_precision",
        ),
        runner=None,
    ),
    "sprint0_corpus": EvalDescriptor(
        name="sprint0_corpus",
        title="Sprint 0 corpus",
        description=(
            "Re-run the canonical 10-prompt Sprint 0 baseline.  Useful "
            "for regression-checking the active LLM backend against the "
            "frozen Sprint 0 reference."
        ),
        expected_minutes=20,
        summary_keys=("completed", "total", "p50_ms", "p95_ms"),
        runner=None,
    ),
}


def register_eval(descriptor: EvalDescriptor) -> None:
    """Register an eval runner.  Called by tools/eval/run_*.py modules
    at import time."""
    _EVAL_MANIFEST[descriptor.name] = descriptor


# ─── progress channel (in-memory; one queue per run_id) ─────────────


_PROGRESS_QUEUES: Dict[str, asyncio.Queue[str]] = {}


def _progress_queue(run_id: str) -> asyncio.Queue[str]:
    q = _PROGRESS_QUEUES.get(run_id)
    if q is None:
        q = asyncio.Queue(maxsize=512)
        _PROGRESS_QUEUES[run_id] = q
    return q


async def _emit_progress(run_id: str, message: str) -> None:
    """Push a progress line to the SSE channel for ``run_id``.  Drops
    on full queue (best-effort streaming)."""
    q = _progress_queue(run_id)
    try:
        q.put_nowait(message)
    except asyncio.QueueFull:
        pass


# ─── handlers ──────────────────────────────────────────────────────


@router.get("/manifest")
async def list_evals(
    user: User = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """List supported evals (Days 2-4 fill the runners)."""
    return [
        {
            "name": d.name,
            "title": d.title,
            "description": d.description,
            "expected_minutes": d.expected_minutes,
            "implemented": d.runner is not None,
            "summary_keys": list(d.summary_keys),
        }
        for d in _EVAL_MANIFEST.values()
    ]


@router.get("/runs")
async def list_runs(
    limit: int = 50,
    name: Optional[str] = None,
    user: User = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Last ``limit`` rows from eval_runs, newest first.  ``name`` filters."""
    if storage_manager.pg_session_maker is None:
        return []
    sql = (
        "SELECT id::text, name, started_at, finished_at, status, "
        "backend, git_sha, summary, note "
        "FROM eval_runs "
        "{where} "
        "ORDER BY started_at DESC LIMIT :limit"
    )
    where = ""
    params: Dict[str, Any] = {"limit": max(1, min(500, limit))}
    if name:
        where = "WHERE name = :name"
        params["name"] = name
    async with storage_manager.pg_session_maker() as session:
        result = await session.execute(text(sql.format(where=where)), params)
        rows = result.mappings().all()
    return [_row_to_dict(r) for r in rows]


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """One run with full ``cases`` payload."""
    _validate_uuid(run_id)
    if storage_manager.pg_session_maker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="postgres not bootstrapped",
        )
    async with storage_manager.pg_session_maker() as session:
        result = await session.execute(
            text(
                "SELECT id::text, name, started_at, finished_at, status, "
                "backend, git_sha, summary, cases, note "
                "FROM eval_runs WHERE id = :id"
            ),
            {"id": run_id},
        )
        row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run not found",
        )
    return _row_to_dict(row, include_cases=True)


@router.post("/run/{name}", status_code=status.HTTP_202_ACCEPTED)
async def kick_run(
    name: str,
    limit: Optional[int] = None,
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Start an async eval run.  Returns ``{run_id, status: "pending"}``.

    Subscribe to progress at ``/runs/{id}/stream`` (SSE).

    ``?limit=N`` caps the number of cases (smoke-testing override).
    Eval runners that honour the cap read it via the
    ``AMOR_EVAL_LIMIT`` env-like contextvar set on dispatch.
    """
    desc = _EVAL_MANIFEST.get(name)
    if desc is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown eval {name!r}; "
                   f"see /api/admin/evals/manifest",
        )
    if desc.runner is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"eval {name!r} is registered but no runner is wired yet "
                f"— see Cycle C Sprint 2 Day {2 if name=='humaneval_plus_50' else 3 if name=='swebench_lite_25' else 4}"
            ),
        )

    if storage_manager.pg_session_maker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="postgres not bootstrapped",
        )

    # Insert pending row.
    run_id = str(uuid.uuid4())
    async with storage_manager.pg_session_maker() as session:
        await session.execute(
            text(
                "INSERT INTO eval_runs (id, name, status, user_id) "
                "VALUES (:id, :name, 'pending', :user_id)"
            ),
            {"id": run_id, "name": name, "user_id": user.id},
        )
        await session.commit()

    # Fire-and-forget background task.
    asyncio.create_task(_run_eval(run_id, desc, limit=limit))
    return {
        "run_id": run_id,
        "status": "pending",
        "name": name,
        "limit": limit,
    }


@router.get("/runs/{run_id}/stream")
async def stream_progress(
    run_id: str,
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """SSE — yields ``data: <progress line>`` per emitted message.
    Closes when the run reaches a terminal state."""
    _validate_uuid(run_id)

    async def _gen():
        q = _progress_queue(run_id)
        # Initial state line.
        if storage_manager.pg_session_maker is not None:
            async with storage_manager.pg_session_maker() as session:
                r = await session.execute(
                    text("SELECT status FROM eval_runs WHERE id = :id"),
                    {"id": run_id},
                )
                row = r.mappings().first()
            if row is None:
                yield "data: " + json.dumps(
                    {"type": "error", "error": "run not found"},
                ) + "\n\n"
                return
            yield "data: " + json.dumps(
                {"type": "init", "status": row["status"]},
            ) + "\n\n"

        # Tail the in-process queue until we see a terminal marker.
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Heartbeat so the SSE connection stays warm.
                yield ": ping\n\n"
                continue
            yield "data: " + msg + "\n\n"
            try:
                payload = json.loads(msg)
                if payload.get("type") in {"done", "failed", "cancelled"}:
                    break
            except Exception:
                pass

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ─── async runner ──────────────────────────────────────────────────


async def _run_eval(
    run_id: str,
    desc: EvalDescriptor,
    *,
    limit: Optional[int] = None,
) -> None:
    """Background task: flip status to running, invoke the runner,
    persist summary + status on completion or failure.

    ``limit`` propagates via the ``AMOR_EVAL_LIMIT`` env var so the
    runner can cap its case count without changing its signature.
    Cleared on exit so other runs aren't affected.
    """
    if storage_manager.pg_session_maker is None:
        return

    import os as _os
    prev_limit = _os.environ.pop("AMOR_EVAL_LIMIT", None)
    if limit is not None and limit > 0:
        _os.environ["AMOR_EVAL_LIMIT"] = str(int(limit))

    async def _progress(msg: str) -> None:
        await _emit_progress(run_id, msg)

    # Mark running.
    async with storage_manager.pg_session_maker() as session:
        await session.execute(
            text(
                "UPDATE eval_runs SET status = 'running' WHERE id = :id"
            ),
            {"id": run_id},
        )
        await session.commit()
    await _progress(json.dumps({"type": "running", "name": desc.name}))

    try:
        assert desc.runner is not None  # gated in kick_run
        summary = await desc.runner(run_id, _progress)
        async with storage_manager.pg_session_maker() as session:
            await session.execute(
                text(
                    "UPDATE eval_runs "
                    "SET status = 'done', finished_at = NOW(), "
                    "    summary = :summary "
                    "WHERE id = :id"
                ),
                {"id": run_id, "summary": json.dumps(summary)},
            )
            await session.commit()
        await _progress(json.dumps({"type": "done", "summary": summary}))
    except asyncio.CancelledError:
        async with storage_manager.pg_session_maker() as session:
            await session.execute(
                text(
                    "UPDATE eval_runs "
                    "SET status = 'cancelled', finished_at = NOW() "
                    "WHERE id = :id"
                ),
                {"id": run_id},
            )
            await session.commit()
        await _progress(json.dumps({"type": "cancelled"}))
        raise
    except Exception as exc:
        logger.exception("eval %s failed: %s", desc.name, exc)
        async with storage_manager.pg_session_maker() as session:
            await session.execute(
                text(
                    "UPDATE eval_runs "
                    "SET status = 'failed', finished_at = NOW(), "
                    "    summary = :summary "
                    "WHERE id = :id"
                ),
                {
                    "id": run_id,
                    "summary": json.dumps({"error": str(exc)[:500]}),
                },
            )
            await session.commit()
        await _progress(json.dumps({"type": "failed", "error": str(exc)[:200]}))
    finally:
        # Restore prior AMOR_EVAL_LIMIT (or unset).
        if prev_limit is None:
            _os.environ.pop("AMOR_EVAL_LIMIT", None)
        else:
            _os.environ["AMOR_EVAL_LIMIT"] = prev_limit


# ─── helpers ───────────────────────────────────────────────────────


def _validate_uuid(s: str) -> None:
    try:
        uuid.UUID(s)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid run id {s!r}",
        ) from exc


def _row_to_dict(row, *, include_cases: bool = False) -> Dict[str, Any]:
    out = {
        "id": row["id"],
        "name": row["name"],
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
        "status": row["status"],
        "backend": row.get("backend"),
        "git_sha": row.get("git_sha"),
        "summary": row.get("summary") or {},
        "note": row.get("note"),
    }
    if include_cases:
        out["cases"] = row.get("cases")
    return out


def _iso(d) -> Optional[str]:
    if d is None:
        return None
    if isinstance(d, datetime):
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.isoformat()
    return str(d)


__all__ = [
    "router",
    "ensure_eval_runs_schema",
    "register_eval",
    "EvalDescriptor",
]
