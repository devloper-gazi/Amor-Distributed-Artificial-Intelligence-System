"""
Sprint 0 admin endpoints for the baseline corpus dashboard.

Endpoints
---------
GET  /api/admin/baselines/latest      — return data/baselines/sprint0_latest.json
GET  /api/admin/baselines/runs        — list available run files (id, mtime, size)
GET  /api/admin/baselines/runs/{id}   — return a specific run's JSONL contents

The dashboard UI at ``/admin/baselines`` (SolidJS) consumes
``/latest`` for the side-by-side table.  ``/runs`` supports a future
history viewer; not required for Sprint 0 acceptance.

Auth: all endpoints require an authenticated user (``get_current_user``).
There is no admin-role check yet — AMOR is single-tenant — but the
endpoints are namespaced under ``/api/admin`` so a future role gate
slots in cleanly without breaking the URL contract.

Cycle C Sprint 0 Day 3.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth.dependencies import get_current_user
from ..auth.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/baselines", tags=["admin-baselines"])


# ─── filesystem layout ───────────────────────────────────────────────


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _baselines_dir() -> Path:
    """``data/baselines/`` under the repo root.  Created lazily."""
    d = _repo_root() / "data" / "baselines"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_run_path(run_id: str) -> Optional[Path]:
    """Resolve a run-id to a path under data/baselines/, defending
    against path traversal.  Returns None if the file doesn't exist
    or escapes the directory."""
    base = _baselines_dir().resolve()
    candidate = (base / run_id).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


# ─── handlers ────────────────────────────────────────────────────────


@router.get("/latest")
async def get_latest_baseline(
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return ``data/baselines/sprint0_latest.json`` verbatim.

    Schema: see ``tests/baselines/sprint0_schema.json`` (top-level
    ``{meta, rows[]}``).  When no baseline has been recorded yet,
    returns ``{"meta": null, "rows": []}`` with HTTP 200 so the UI
    can render a friendly empty state instead of a 404.
    """
    path = _baselines_dir() / "sprint0_latest.json"
    if not path.is_file():
        return {"meta": None, "rows": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("admin/baselines/latest read failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"latest baseline unreadable: {exc}",
        ) from exc


@router.get("/runs")
async def list_baseline_runs(
    user: User = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """List archived runs sorted newest-first.  Each entry:
    ``{filename, size_bytes, mtime_utc, kind}`` where ``kind`` is
    ``"jsonl"`` or ``"latest"``."""
    out: List[Dict[str, Any]] = []
    for entry in _baselines_dir().iterdir():
        if not entry.is_file():
            continue
        if not (entry.suffix in {".jsonl", ".json"}):
            continue
        if not entry.name.startswith("sprint0_"):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        out.append(
            {
                "filename": entry.name,
                "size_bytes": stat.st_size,
                "mtime_utc": stat.st_mtime,
                "kind": "latest" if entry.suffix == ".json" else "jsonl",
            },
        )
    out.sort(key=lambda r: r["mtime_utc"], reverse=True)
    return out


@router.get("/runs/{run_filename}")
async def get_baseline_run(
    run_filename: str,
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the contents of a specific run.

    For ``.json`` files: parsed object.  For ``.jsonl``: a wrapper
    ``{"rows": [...], "meta": null}`` so the UI consumes one shape.
    """
    path = _safe_run_path(run_filename)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"run not found: {run_filename}",
        )
    try:
        if path.suffix == ".jsonl":
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            return {"meta": None, "rows": rows}
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(
            "admin/baselines/runs/%s read failed: %s", run_filename, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"run unreadable: {exc}",
        ) from exc


__all__ = ["router"]
