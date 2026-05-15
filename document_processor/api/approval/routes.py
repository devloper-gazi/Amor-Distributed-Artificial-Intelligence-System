"""
Cycle F Sprint 5 — `POST /api/approval/{request_id}` endpoint.

Receives the browser's approve/deny decision and resolves the
matching pending future (locally or cross-replica via Redis).

Wire shape:

    POST /api/approval/{request_id}
        Content-Type: application/json
        Body: {"approved": <bool>, "note": "<optional reason>"}

    -> 200 {"resolved": true}            # local future resolved
    -> 200 {"resolved": false, "via": "redis"}   # broadcast to others
    -> 404 {"detail": "unknown request_id"}
    -> 410 {"detail": "request already resolved"}
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from .bridge import (
    _PENDING,  # not exported by __init__; OK in same package
    _broadcast_decision,
    resolve_approval,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/approval", tags=["approval"])


class _ApprovalBody(BaseModel):
    approved: bool = Field(..., description="Final user decision.")
    note: Optional[str] = Field(
        None, max_length=500,
        description="Free-text reason (audit log).",
    )


@router.post("/{request_id}")
async def approval_decision(request_id: str, body: _ApprovalBody):
    if not request_id or len(request_id) > 64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid request_id",
        )

    locally = resolve_approval(request_id, body.approved)
    await _broadcast_decision(request_id, body.approved)

    logger.info(
        "approval_decision request_id=%s approved=%s "
        "resolved_locally=%s note=%s",
        request_id, body.approved, locally, (body.note or "")[:80],
    )

    if locally:
        return {"resolved": True, "approved": body.approved}

    # The request was registered on a different replica; the
    # broadcast woke them up.  Return 202-style payload.
    return {"resolved": False, "approved": body.approved, "via": "redis"}


@router.get("/_pending")
async def list_pending():
    """Debug endpoint: dump the local pending registry.  Useful
    for operators tracing why a tool dispatch is hanging."""

    out = []
    for req_id, req in _PENDING.items():
        out.append({
            "request_id": req_id,
            "session_id": req.session_id,
            "tool_name": req.tool_name,
            "category": req.category,
            "actor_role": req.actor_role,
            "timeout_s": req.timeout_s,
        })
    return {"pending": out, "count": len(out)}


def register_approval_routes(app) -> None:
    """One-call FastAPI include — mirrors the pattern used by
    other Sprint 5 / Cycle E routers."""

    app.include_router(router)


__all__ = ["router", "register_approval_routes", "approval_decision"]
