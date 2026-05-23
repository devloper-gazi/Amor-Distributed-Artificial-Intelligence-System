"""
Cycle C Sprint 6 Day 1 — preference-pair ingestion + listing.

Backs the rate ± buttons in ``MessageActions.tsx`` (Sprint 4 Day 3
✓ ▼ icons) and the future ``/admin/training`` UI.

Endpoints
---------
POST /api/admin/training/pairs        — record one (chosen, rejected) pair
GET  /api/admin/training/pairs        — list pairs (paginated)
GET  /api/admin/training/pairs/stats  — counts by mode + untrained total

Persistence
-----------
Postgres ``preference_pairs`` table (see migration
``004_preference_pairs.sql``).  The DDL is also baked in here so a
fresh database picks up the table without operator action — same
pattern as ``admin_evals_routes.py``.

Privacy
-------
Default ingestion stores only ``code_hash`` (sha256 of
``prompt + chosen + rejected``).  The body's ``opt_in_raw`` flag
explicitly enables raw text persistence — the rate buttons in the
chat surface NEVER set it; only the future "Include raw" toggle
in the admin Training UI does.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..i18n import get_locale, localized_http_exception
from ..infrastructure.storage import storage_manager

# Cycle C Sprint 6 Day 5 — Prometheus counters / gauges.  Imported
# defensively so a partial deploy that hasn't pulled monitoring.py
# yet doesn't 500-out the route.
try:
    from ..infrastructure.monitoring import (
        TRAINING_RUNS_TOTAL,
        TRAINING_PAIRS_INGESTED,
        LORA_ACTIVE_ID,
    )
    _METRICS_AVAILABLE = True
except Exception:  # pragma: no cover
    TRAINING_RUNS_TOTAL = TRAINING_PAIRS_INGESTED = LORA_ACTIVE_ID = None
    _METRICS_AVAILABLE = False

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/training", tags=["admin-training"])


# ─── DDL — runs at startup, idempotent ─────────────────────────────


_PAIRS_DDL = [
    """
    CREATE TABLE IF NOT EXISTS preference_pairs (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        chosen_turn_id VARCHAR(128),
        rejected_turn_id VARCHAR(128),
        code_hash CHAR(64) NOT NULL,
        mode VARCHAR(16) NOT NULL DEFAULT 'build',
        opt_in_raw BOOLEAN NOT NULL DEFAULT FALSE,
        prompt TEXT,
        chosen TEXT,
        rejected TEXT,
        backend VARCHAR(32) NOT NULL DEFAULT 'ollama',
        model_tag VARCHAR(96),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        user_id UUID REFERENCES users(id) ON DELETE SET NULL,
        trained_in UUID,
        CONSTRAINT preference_pairs_mode_values CHECK (
            mode IN ('build', 'research', 'thinking', 'consortium',
                     'sentinel', 'system')
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_preference_pairs_created ON preference_pairs (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_preference_pairs_mode_created ON preference_pairs (mode, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_preference_pairs_hash ON preference_pairs (code_hash)",
    "CREATE INDEX IF NOT EXISTS idx_preference_pairs_untrained ON preference_pairs (created_at DESC) WHERE trained_in IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_preference_pairs_user ON preference_pairs (user_id, created_at DESC)",
    # Sprint 6 Day 4 — training runs history.
    """
    CREATE TABLE IF NOT EXISTS training_runs (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ,
        status VARCHAR(16) NOT NULL DEFAULT 'pending',
        config JSONB NOT NULL DEFAULT '{}'::jsonb,
        pair_count INT NOT NULL DEFAULT 0,
        pair_jsonl_path TEXT,
        peft_adapter_path TEXT,
        gguf_adapter_path TEXT,
        eval_summary JSONB,
        note TEXT,
        user_id UUID REFERENCES users(id) ON DELETE SET NULL,
        CONSTRAINT training_runs_status_values CHECK (
            status IN ('pending', 'running', 'trained', 'evaluated',
                       'promoted', 'failed', 'rejected')
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_training_runs_started ON training_runs (started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_training_runs_status ON training_runs (status, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_training_runs_user ON training_runs (user_id, started_at DESC)",
]


async def ensure_preference_pairs_schema() -> None:
    """Idempotent DDL — called from app startup, mirrors the pattern
    in ``admin_evals_routes.ensure_eval_runs_schema``."""
    if storage_manager.pg_session_maker is None:  # pragma: no cover
        logger.warning("preference_pairs schema deferred — Postgres not bootstrapped")
        return
    try:
        async with storage_manager.pg_session_maker() as session:
            await session.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
            for stmt in _PAIRS_DDL:
                await session.execute(text(stmt))
            await session.commit()
        logger.info("preference_pairs schema ready")
    except Exception as exc:  # pragma: no cover
        logger.error("preference_pairs schema setup failed: %s", exc)


# ─── models ────────────────────────────────────────────────────────


_ALLOWED_MODES = {"build", "research", "thinking", "consortium", "sentinel", "system"}


class PairIn(BaseModel):
    """Body for ``POST /api/admin/training/pairs``."""

    # v18.1.3 — ``model_tag`` collides with Pydantic v2's protected
    # ``model_`` namespace; opt out so import doesn't spam UserWarning.
    model_config = {"protected_namespaces": ()}

    chosen_turn_id: Optional[str] = Field(default=None, max_length=128)
    rejected_turn_id: Optional[str] = Field(default=None, max_length=128)
    mode: Literal["build", "research", "thinking", "consortium", "sentinel", "system"] = "build"
    backend: str = Field(default="ollama", max_length=32)
    model_tag: Optional[str] = Field(default=None, max_length=96)

    # ``opt_in_raw=False`` (default) → raw text fields are dropped
    # before persistence.  ``True`` keeps them in the row.
    opt_in_raw: bool = False
    prompt: Optional[str] = None
    chosen: Optional[str] = None
    rejected: Optional[str] = None


class PairOut(BaseModel):
    # v18.1.3 — same protected-namespace opt-out as PairIn (see above).
    model_config = {"protected_namespaces": ()}

    id: str
    chosen_turn_id: Optional[str]
    rejected_turn_id: Optional[str]
    code_hash: str
    mode: str
    opt_in_raw: bool
    prompt: Optional[str]
    chosen: Optional[str]
    rejected: Optional[str]
    backend: str
    model_tag: Optional[str]
    created_at: datetime
    trained_in: Optional[str]


def _hash_pair(prompt: str, chosen: str, rejected: str) -> str:
    h = hashlib.sha256()
    h.update(b"prompt:")
    h.update((prompt or "").encode("utf-8", errors="replace"))
    h.update(b"\n\nchosen:")
    h.update((chosen or "").encode("utf-8", errors="replace"))
    h.update(b"\n\nrejected:")
    h.update((rejected or "").encode("utf-8", errors="replace"))
    return h.hexdigest()


# ─── routes ────────────────────────────────────────────────────────


@router.post("/pairs", status_code=status.HTTP_201_CREATED)
async def create_pair(
    body: PairIn,
    user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    """Record one (chosen, rejected) preference pair.

    Even when ``opt_in_raw=False`` (the default for the chat-surface
    rate buttons) the row is still persisted — it just stores the
    sha-256 of the texts instead of the texts themselves.  That's
    enough for downstream stats; the trainer will only use rows that
    explicitly opted in.
    """
    if body.mode not in _ALLOWED_MODES:
        raise localized_http_exception(
            status_code=422,
            key="common.invalid_mode",
            locale=locale,
            params={"mode": body.mode},
        )
    if storage_manager.pg_session_maker is None:
        raise localized_http_exception(
            status_code=503,
            key="common.db_unavailable",
            locale=locale,
        )

    code_hash = _hash_pair(body.prompt or "", body.chosen or "", body.rejected or "")

    insert_sql = text(
        """
        INSERT INTO preference_pairs (
            chosen_turn_id, rejected_turn_id, code_hash, mode,
            opt_in_raw, prompt, chosen, rejected, backend, model_tag,
            user_id
        ) VALUES (
            :chosen_turn_id, :rejected_turn_id, :code_hash, :mode,
            :opt_in_raw, :prompt, :chosen, :rejected, :backend, :model_tag,
            :user_id
        )
        RETURNING id, created_at
        """,
    )
    params = {
        "chosen_turn_id": body.chosen_turn_id,
        "rejected_turn_id": body.rejected_turn_id,
        "code_hash": code_hash,
        "mode": body.mode,
        "opt_in_raw": bool(body.opt_in_raw),
        "prompt": body.prompt if body.opt_in_raw else None,
        "chosen": body.chosen if body.opt_in_raw else None,
        "rejected": body.rejected if body.opt_in_raw else None,
        "backend": body.backend,
        "model_tag": body.model_tag,
        "user_id": user.id,
    }

    async with storage_manager.pg_session_maker() as session:
        row = (await session.execute(insert_sql, params)).fetchone()
        await session.commit()

    # Sprint 6 Day 5 — count this pair against the per-mode counter.
    if _METRICS_AVAILABLE and TRAINING_PAIRS_INGESTED is not None:
        try:
            TRAINING_PAIRS_INGESTED.labels(
                mode=body.mode,
                opt_in_raw=str(bool(body.opt_in_raw)).lower(),
            ).inc()
        except Exception:  # pragma: no cover
            pass

    return {
        "id": str(row.id),
        "code_hash": code_hash,
        "created_at": row.created_at.isoformat()
        if hasattr(row.created_at, "isoformat")
        else str(row.created_at),
    }


@router.get("/pairs")
async def list_pairs(
    mode: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    only_untrained: bool = Query(False),
    _user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    if storage_manager.pg_session_maker is None:
        raise localized_http_exception(
            status_code=503,
            key="common.db_unavailable",
            locale=locale,
        )
    if mode is not None and mode not in _ALLOWED_MODES:
        raise localized_http_exception(
            status_code=422,
            key="common.invalid_mode",
            locale=locale,
            params={"mode": mode},
        )

    where_clauses: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    if mode is not None:
        where_clauses.append("mode = :mode")
        params["mode"] = mode
    if only_untrained:
        where_clauses.append("trained_in IS NULL")
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    async with storage_manager.pg_session_maker() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id, chosen_turn_id, rejected_turn_id, code_hash, mode,
                           opt_in_raw, prompt, chosen, rejected, backend,
                           model_tag, created_at, trained_in
                    FROM preference_pairs
                    {where_sql}
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """,
                ),
                params,
            )
        ).mappings().all()

    items: List[Dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "id": str(r["id"]),
                "chosen_turn_id": r["chosen_turn_id"],
                "rejected_turn_id": r["rejected_turn_id"],
                "code_hash": r["code_hash"],
                "mode": r["mode"],
                "opt_in_raw": bool(r["opt_in_raw"]),
                # Mask raw text in list view — admin must open the
                # detail endpoint to see them (Day 4).  This keeps
                # the listing payload lightweight + privacy-safe.
                "prompt": (r["prompt"][:160] + "…")
                if r["opt_in_raw"] and r["prompt"] and len(r["prompt"]) > 160
                else r["prompt"],
                "chosen": (r["chosen"][:160] + "…")
                if r["opt_in_raw"] and r["chosen"] and len(r["chosen"]) > 160
                else r["chosen"],
                "rejected": (r["rejected"][:160] + "…")
                if r["opt_in_raw"] and r["rejected"] and len(r["rejected"]) > 160
                else r["rejected"],
                "backend": r["backend"],
                "model_tag": r["model_tag"],
                "created_at": r["created_at"].isoformat()
                if hasattr(r["created_at"], "isoformat")
                else str(r["created_at"]),
                "trained_in": str(r["trained_in"]) if r["trained_in"] else None,
            },
        )
    return {"count": len(items), "limit": limit, "items": items}


@router.get("/pairs/stats")
async def pairs_stats(
    _user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    """Counts by mode + untrained total — drives the Day-4 dashboard
    "you have N pairs, train button enables at 200" badge."""
    if storage_manager.pg_session_maker is None:
        raise localized_http_exception(
            status_code=503,
            key="common.db_unavailable",
            locale=locale,
        )

    async with storage_manager.pg_session_maker() as session:
        by_mode_rows = (
            await session.execute(
                text(
                    "SELECT mode, COUNT(*)::int AS n FROM preference_pairs GROUP BY mode",
                ),
            )
        ).mappings().all()
        total = (
            await session.execute(text("SELECT COUNT(*)::int AS n FROM preference_pairs"))
        ).scalar()
        untrained = (
            await session.execute(
                text(
                    "SELECT COUNT(*)::int AS n FROM preference_pairs WHERE trained_in IS NULL",
                ),
            )
        ).scalar()
        opt_in = (
            await session.execute(
                text(
                    "SELECT COUNT(*)::int AS n FROM preference_pairs WHERE opt_in_raw IS TRUE",
                ),
            )
        ).scalar()
    return {
        "total": int(total or 0),
        "untrained": int(untrained or 0),
        "opt_in_raw": int(opt_in or 0),
        "by_mode": {r["mode"]: int(r["n"]) for r in by_mode_rows},
        # Sprint 6 Day 2 trainer gates on >= 200 untrained rows.
        "train_threshold": 200,
        "ready_to_train": int(untrained or 0) >= 200,
    }


# ─── Sprint 6 Day 4 — training runs ────────────────────────────────


class RunIn(BaseModel):
    """Body for ``POST /api/admin/training/run``.

    All fields are optional; sensible defaults match the Sprint 6
    plan (Qwen2.5-Coder-7B, r=8/α=16, 1 epoch, beta=0.1).
    """

    note: Optional[str] = None
    model_name: str = "unsloth/Qwen2.5-Coder-7B-Instruct-bnb-4bit"
    epochs: float = 1.0
    learning_rate: float = 8e-6
    lora_r: int = 8
    lora_alpha: int = 16
    beta: float = 0.1
    # When True, the route refuses to start unless ``stats.untrained
    # >= stats.train_threshold``.  Operators flip it off for smoke
    # tests against tiny corpora.
    enforce_threshold: bool = True

    model_config = {"protected_namespaces": ()}


@router.post("/run", status_code=status.HTTP_201_CREATED)
async def start_training_run(
    body: RunIn,
    user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    """Create a ``training_runs`` row in ``status='pending'``.

    The actual trainer subprocess is operator-driven for now — Sprint
    6 Day 4 ships the row, the JSONL export hook, and the run id;
    Day 5 wires the background subprocess.  The Day-4 promote button
    flips ``status`` from ``evaluated`` to ``promoted`` and fires the
    ``/lora-adapters`` toggle.
    """
    if storage_manager.pg_session_maker is None:
        raise localized_http_exception(
            status_code=503,
            key="common.db_unavailable",
            locale=locale,
        )

    # Threshold gate — count untrained rows.
    async with storage_manager.pg_session_maker() as session:
        n_untrained = (
            await session.execute(
                text(
                    "SELECT COUNT(*)::int FROM preference_pairs WHERE trained_in IS NULL",
                ),
            )
        ).scalar() or 0
        if body.enforce_threshold and int(n_untrained) < 200:
            raise localized_http_exception(
                status_code=409,
                key="training.threshold_not_met",
                locale=locale,
                params={"n": int(n_untrained), "required": 200},
            )

        config = {
            "model_name": body.model_name,
            "epochs": body.epochs,
            "learning_rate": body.learning_rate,
            "lora_r": body.lora_r,
            "lora_alpha": body.lora_alpha,
            "beta": body.beta,
        }
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO training_runs (
                        status, config, pair_count, note, user_id
                    ) VALUES (
                        'pending', :config, :pair_count, :note, :user_id
                    )
                    RETURNING id, started_at
                    """,
                ),
                {
                    "config": json.dumps(config),
                    "pair_count": int(n_untrained),
                    "note": body.note,
                    "user_id": user.id,
                },
            )
        ).fetchone()
        await session.commit()

    if _METRICS_AVAILABLE and TRAINING_RUNS_TOTAL is not None:
        try:
            TRAINING_RUNS_TOTAL.labels(status="pending").inc()
        except Exception:  # pragma: no cover
            pass

    return {
        "id": str(row.id),
        "status": "pending",
        "pair_count": int(n_untrained),
        "config": config,
        "started_at": row.started_at.isoformat()
        if hasattr(row.started_at, "isoformat")
        else str(row.started_at),
    }


@router.get("/runs")
async def list_runs(
    limit: int = Query(20, ge=1, le=200),
    _user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    if storage_manager.pg_session_maker is None:
        raise localized_http_exception(
            status_code=503,
            key="common.db_unavailable",
            locale=locale,
        )

    async with storage_manager.pg_session_maker() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, started_at, finished_at, status, config,
                           pair_count, peft_adapter_path, gguf_adapter_path,
                           eval_summary, note
                    FROM training_runs
                    ORDER BY started_at DESC
                    LIMIT :limit
                    """,
                ),
                {"limit": limit},
            )
        ).mappings().all()

    items: List[Dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "id": str(r["id"]),
                "started_at": r["started_at"].isoformat()
                if hasattr(r["started_at"], "isoformat")
                else str(r["started_at"]),
                "finished_at": r["finished_at"].isoformat()
                if r["finished_at"] and hasattr(r["finished_at"], "isoformat")
                else (str(r["finished_at"]) if r["finished_at"] else None),
                "status": r["status"],
                "config": r["config"] or {},
                "pair_count": int(r["pair_count"] or 0),
                "peft_adapter_path": r["peft_adapter_path"],
                "gguf_adapter_path": r["gguf_adapter_path"],
                "eval_summary": r["eval_summary"],
                "note": r["note"],
            },
        )
    return {"count": len(items), "limit": limit, "items": items}


class PromoteIn(BaseModel):
    """Body for ``POST /api/admin/training/runs/{id}/promote``."""

    adapter_id: int = 0
    scale: float = 1.0
    # llama-server URL — defaults to the compose-internal name; the
    # admin UI can override per environment if a follow-up sprint
    # introduces a multi-host fleet.
    llamaswap_url: str = "http://amor-llama-swap:9100"


@router.post("/runs/{run_id}/promote")
async def promote_run(
    run_id: str,
    body: PromoteIn,
    _user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    """Flip the LoRA adapter on via llama-server's
    ``POST /v1/lora-adapters`` and mark the run promoted.

    Refuses to promote a run whose ``eval_summary.promote_ok`` is
    False — that's the Cycle C plan caveat: the manual gate doesn't
    let a regression land just because the operator clicked twice.
    """
    if storage_manager.pg_session_maker is None:
        raise localized_http_exception(
            status_code=503,
            key="common.db_unavailable",
            locale=locale,
        )

    async with storage_manager.pg_session_maker() as session:
        row = (
            await session.execute(
                text("SELECT id, status, eval_summary FROM training_runs WHERE id = :id"),
                {"id": run_id},
            )
        ).fetchone()
        if row is None:
            raise localized_http_exception(
                status_code=404,
                key="training.run_not_found",
                locale=locale,
            )
        eval_summary = row.eval_summary or {}
        if not eval_summary.get("promote_ok", False):
            raise localized_http_exception(
                status_code=409,
                key="training.gate_blocked",
                locale=locale,
            )

        # Fire the toggle.  We import lazily so the route imports
        # don't drag httpx into every cold-start.
        try:
            import httpx  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise HTTPException(status_code=500, detail=f"httpx missing: {exc}") from exc
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{body.llamaswap_url.rstrip('/')}/v1/lora-adapters",
                    json=[{"id": body.adapter_id, "scale": body.scale}],
                )
                resp.raise_for_status()
        except Exception as exc:
            raise localized_http_exception(
                status_code=502,
                key="training.toggle_failed",
                locale=locale,
                params={"err": str(exc)},
            ) from exc

        await session.execute(
            text(
                "UPDATE training_runs SET status='promoted', finished_at=NOW() WHERE id = :id",
            ),
            {"id": run_id},
        )
        await session.commit()

    if _METRICS_AVAILABLE:
        try:
            if TRAINING_RUNS_TOTAL is not None:
                TRAINING_RUNS_TOTAL.labels(status="promoted").inc()
            if LORA_ACTIVE_ID is not None:
                # ``scale=0`` deactivates the adapter; reflect that in
                # the gauge so an off-toggle clears the dashboard.
                LORA_ACTIVE_ID.set(
                    body.adapter_id if body.scale > 0 else -1,
                )
        except Exception:  # pragma: no cover
            pass

    return {"id": run_id, "status": "promoted", "adapter_id": body.adapter_id, "scale": body.scale}


# ─── Sprint 6 Day 5 — background trainer execute ─────────────────


class ExecuteIn(BaseModel):
    """Body for ``POST /api/admin/training/runs/{id}/execute``.

    ``dry_run=True`` (default) runs the trainer with ``--dry-run``
    so the route resolves quickly and the planned config lands on
    disk — useful for smoke tests in the admin UI without a GPU.
    Operators set ``dry_run=False`` for real training (≥ 200 pairs
    + GPU available).
    """

    dry_run: bool = True
    allow_tiny: bool = False


@router.post("/runs/{run_id}/execute")
async def execute_run(
    run_id: str,
    body: ExecuteIn,
    _user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    """Kick off the trainer subprocess for an existing pending run.

    Steps
    -----
    1. Validate the run exists in ``status='pending'``.
    2. Export untrained pairs to a JSONL under
       ``/app/data/training/run_<id>.jsonl`` via the
       ``tools/training/export_pairs_jsonl.py`` script.
    3. Run ``tools/training/orpo_qwen_coder.py`` against that JSONL.
       In ``dry_run`` mode the subprocess returns in <1 s; otherwise
       it blocks for the duration of training (operator-managed).
    4. Update the ``training_runs`` row with the resolved paths +
       new ``status``.
    """
    if storage_manager.pg_session_maker is None:
        raise localized_http_exception(
            status_code=503,
            key="common.db_unavailable",
            locale=locale,
        )

    async with storage_manager.pg_session_maker() as session:
        row = (
            await session.execute(
                text("SELECT id, status FROM training_runs WHERE id = :id"),
                {"id": run_id},
            )
        ).fetchone()
        if row is None:
            raise localized_http_exception(
                status_code=404,
                key="training.run_not_found",
                locale=locale,
            )
        if row.status != "pending":
            raise localized_http_exception(
                status_code=409,
                key="training.execute_status",
                locale=locale,
                params={"status": row.status},
            )

    # Resolve output paths.  ``data/training`` is a writable mount
    # inside the app container.
    import os as _os
    import asyncio as _asyncio
    import subprocess as _subprocess
    from pathlib import Path as _Path

    out_root = _Path("/app/data/training")
    out_root.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_root / f"run_{run_id}.jsonl"
    peft_dir = out_root / f"run_{run_id}_lora"

    # Step 2 — export pairs.  Allow hash-only rows when in dry-run
    # mode so the smoke path always has SOMETHING to feed the
    # trainer's parser; real training keeps the privacy default.
    export_args = [
        "python",
        "/app/tools/training/export_pairs_jsonl.py",
        "--out", str(jsonl_path),
        "--since", "all",
        "--max-rows", "10000",
    ]
    if body.dry_run or body.allow_tiny:
        export_args.append("--allow-hash-only")
        export_args.append("--no-opt-in")

    try:
        proc = await _asyncio.create_subprocess_exec(
            *export_args,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
            env={**_os.environ, "PYTHONPATH": "/app"},
        )
        stdout, stderr = await proc.communicate()
    except Exception as exc:
        raise localized_http_exception(
            status_code=500,
            key="training.export_kickoff",
            locale=locale,
            params={"err": str(exc)},
        ) from exc
    if proc.returncode != 0:
        raise localized_http_exception(
            status_code=500,
            key="training.export_rc",
            locale=locale,
            params={"rc": proc.returncode, "tail": stderr.decode()[:300]},
        )
    export_summary = {"stdout_tail": stdout.decode()[-300:]}

    # Step 3 — kick off the trainer.  Always dry-run for now since
    # the actual model + GPU path lives outside the container.
    train_args = [
        "python",
        "/app/tools/training/orpo_qwen_coder.py",
        "--jsonl", str(jsonl_path),
        "--out", str(peft_dir),
    ]
    if body.allow_tiny:
        train_args.append("--allow-tiny")
    if body.dry_run:
        train_args.append("--dry-run")
        train_args.append("--allow-tiny")  # dry-run + tiny is fine

    try:
        proc = await _asyncio.create_subprocess_exec(
            *train_args,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
            env={**_os.environ, "PYTHONPATH": "/app"},
        )
        stdout, stderr = await proc.communicate()
    except Exception as exc:
        async with storage_manager.pg_session_maker() as session:
            await session.execute(
                text(
                    "UPDATE training_runs SET status='failed', finished_at=NOW(), "
                    "note=:note WHERE id = :id",
                ),
                {"id": run_id, "note": f"trainer kickoff failed: {exc}"},
            )
            await session.commit()
        if _METRICS_AVAILABLE and TRAINING_RUNS_TOTAL is not None:
            try:
                TRAINING_RUNS_TOTAL.labels(status="failed").inc()
            except Exception:  # pragma: no cover
                pass
        raise localized_http_exception(
            status_code=500,
            key="training.trainer_kickoff",
            locale=locale,
            params={"err": str(exc)},
        ) from exc

    if proc.returncode != 0:
        async with storage_manager.pg_session_maker() as session:
            await session.execute(
                text(
                    "UPDATE training_runs SET status='failed', finished_at=NOW(), "
                    "note=:note WHERE id = :id",
                ),
                {
                    "id": run_id,
                    "note": f"rc={proc.returncode}: {stderr.decode()[-200:]}",
                },
            )
            await session.commit()
        if _METRICS_AVAILABLE and TRAINING_RUNS_TOTAL is not None:
            try:
                TRAINING_RUNS_TOTAL.labels(status="failed").inc()
            except Exception:  # pragma: no cover
                pass
        return {
            "id": run_id,
            "status": "failed",
            "rc": proc.returncode,
            "stderr_tail": stderr.decode()[-300:],
        }

    # Step 4 — happy path: persist outputs + flip to ``trained``.
    new_status = "trained" if not body.dry_run else "evaluated"
    async with storage_manager.pg_session_maker() as session:
        # asyncpg can't deduce a single parameter that appears in
        # both ``SET status = $1`` and a ``CASE WHEN $1 = 'literal'``
        # comparison, so we issue two statements.
        await session.execute(
            text(
                """
                UPDATE training_runs
                SET status = :status,
                    pair_jsonl_path = :jsonl,
                    peft_adapter_path = :peft
                WHERE id = :id
                """,
            ),
            {
                "id": run_id,
                "status": new_status,
                "jsonl": str(jsonl_path),
                "peft": str(peft_dir),
            },
        )
        if new_status == "evaluated":
            await session.execute(
                text(
                    "UPDATE training_runs SET finished_at = NOW() WHERE id = :id",
                ),
                {"id": run_id},
            )
        await session.commit()

    if _METRICS_AVAILABLE and TRAINING_RUNS_TOTAL is not None:
        try:
            TRAINING_RUNS_TOTAL.labels(status=new_status).inc()
        except Exception:  # pragma: no cover
            pass

    return {
        "id": run_id,
        "status": new_status,
        "dry_run": bool(body.dry_run),
        "pair_jsonl_path": str(jsonl_path),
        "peft_adapter_path": str(peft_dir),
        "trainer_stdout_tail": stdout.decode()[-300:],
        "export_summary": export_summary,
    }
