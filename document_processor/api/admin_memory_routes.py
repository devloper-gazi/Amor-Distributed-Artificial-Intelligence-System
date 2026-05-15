"""
Cycle C Sprint 7 Day 2 — admin Memory routes.

Backs the ``/admin/memory`` viewer (Day 3) and the "Remembered" pill
on assistant messages (Day 3).  All endpoints are auth-gated.

Endpoints
---------
GET    /api/admin/memory/status                — adapter status
GET    /api/admin/memory/search?q=…&limit=…    — hybrid search
GET    /api/admin/memory/all?limit=…           — list all (per user)
DELETE /api/admin/memory/{memory_id}           — drop one entry
POST   /api/admin/memory/add                   — manual add (admin tool)

When Mem0 is not enabled (``AMOR_MEMORY_BACKEND != "mem0"`` or the
package is not installed), every endpoint still resolves — the
adapter returns empty results.  This keeps the UI from blowing up on
fresh deploys before the operator opts in.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..i18n import get_locale, localized_http_exception

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/memory", tags=["admin-memory"])


def _user_adapter(user_id: str):
    """Lazy import + per-request adapter.

    Each request looks up a per-user namespace via ``user.id`` so two
    AMOR users never see each other's memories.  The default
    singleton (``user_id="local"``) is reserved for system-level
    memory the rest of the codebase writes (e.g. Sentinel facts).

    ``local_ai`` is a sibling top-level package (not nested under
    ``document_processor``), so the import is absolute.
    """
    from local_ai.memory.mem0_adapter import Mem0Adapter  # noqa: PLC0415
    return Mem0Adapter(user_id=user_id)


# ─── status ────────────────────────────────────────────────────────


@router.get("/status")
def memory_status(user: User = Depends(get_current_user)) -> Dict[str, Any]:
    adapter = _user_adapter(user.id)
    s = adapter.status()
    return {
        "backend": s.backend,
        "available": s.available,
        "vector_store": s.vector_store,
        "history_db": s.history_db,
        "llm_base_url": s.llm_base_url,
        "llm_model": s.llm_model,
        "graph_enabled": s.graph_enabled,
        "user_namespace": s.user_namespace,
    }


# ─── search ────────────────────────────────────────────────────────


@router.get("/search")
def memory_search(
    q: str = Query(..., min_length=1, max_length=512),
    limit: int = Query(10, ge=1, le=100),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    adapter = _user_adapter(user.id)
    items = adapter.search(q, user_id=user.id, limit=limit)
    return {
        "q": q,
        "limit": limit,
        "count": len(items),
        "available": adapter.status().available,
        "items": [asdict(it) for it in items],
    }


# ─── list ──────────────────────────────────────────────────────────


@router.get("/all")
def memory_all(
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    adapter = _user_adapter(user.id)
    items = adapter.get_all(user_id=user.id, limit=limit)
    return {
        "limit": limit,
        "count": len(items),
        "available": adapter.status().available,
        "items": [asdict(it) for it in items],
    }


# ─── add (admin tool — most writes happen via the chat pipeline) ──


class AddIn(BaseModel):
    """Body for ``POST /api/admin/memory/add``."""

    text: str = Field(..., min_length=1, max_length=4096)
    metadata: Optional[Dict[str, Any]] = None


@router.post("/add", status_code=status.HTTP_201_CREATED)
def memory_add(
    body: AddIn,
    user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    adapter = _user_adapter(user.id)
    if not adapter.status().available:
        raise localized_http_exception(
            status_code=503,
            key="memory.unavailable",
            locale=locale,
        )
    items = adapter.add(body.text, user_id=user.id, metadata=body.metadata or {})
    return {
        "count": len(items),
        "items": [asdict(it) for it in items],
    }


# ─── delete ────────────────────────────────────────────────────────


@router.delete("/{memory_id}")
def memory_delete(
    memory_id: str,
    user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    adapter = _user_adapter(user.id)
    if not adapter.status().available:
        raise localized_http_exception(
            status_code=503,
            key="memory.unavailable",
            locale=locale,
        )
    ok = adapter.delete(memory_id)
    if not ok:
        raise localized_http_exception(
            status_code=404,
            key="memory.delete_failed",
            locale=locale,
        )
    return {"id": memory_id, "deleted": True}
