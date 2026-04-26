"""
REST API for the `query_records` collection.

Phase B4 of the deep-architecture overhaul (fancy-swinging-karp.md).
Every long-running query (research / thinking / Claude chat) writes a
record to MongoDB so the resume banner, inline progress card, and
history reload have a durable source of truth — independent of the
ephemeral Redis pipeline state.

Endpoint surface (under `/api`):

  POST   /query-records                              create
  GET    /query-records/{record_id}                  get one
  PATCH  /query-records/{record_id}                  update fields
  POST   /query-records/{record_id}/cancel           cancel
  GET    /sessions/{sid}/active-query                most-recent in-progress
  GET    /sessions/{sid}/query-records               list (paginated)

All endpoints:

* Require `Depends(get_current_user)` — anonymous reads are not allowed.
* Require the `X-Client-Id` header so client-only sessions stay
  reachable when an authenticated user has none of their own yet.
* Enforce strict ownership on read AND write: a record's `user_id`
  must match the caller's, OR — for client-only records — its
  `client_id` must match the header. Cross-account access is denied
  with 404 (NOT 403) to avoid leaking record existence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..config.logging_config import logger
from ..infrastructure.chat_store import chat_store


router = APIRouter(tags=["query-records"])


# ─── helpers ────────────────────────────────────────────────────────────────


def _require_client_id(x_client_id: Optional[str]) -> str:
    if not x_client_id or not x_client_id.strip():
        raise HTTPException(status_code=400, detail="Missing X-Client-Id header")
    return x_client_id.strip()


def _dt_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Force naive UTC datetimes to be timezone-aware so JSON serialisation
    includes the explicit `+00:00` offset (browsers parse naive datetimes
    as local time)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_VALID_STATUSES = {"pending", "running", "completed", "failed", "cancelled"}
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


# ─── pydantic models ────────────────────────────────────────────────────────


class QueryRecordCreateRequest(BaseModel):
    chat_session_id: str = Field(..., min_length=1, max_length=64)
    mode: str = Field(..., description="research | thinking | coding")
    query_text: str = Field(..., min_length=1, max_length=8000)
    provider: str = Field(..., description="claude | local-ai | etc")
    effort: Optional[str] = Field(None, max_length=32)
    idempotency_key: Optional[str] = Field(
        None, max_length=64,
        description="Optional dedupe key — POST safe to retry",
    )


class QueryRecordUpdateRequest(BaseModel):
    """
    Partial update. Only fields present in the JSON body are touched.
    `_id`, `chat_session_id`, `user_id`, `client_id`, `created_at` are
    immutable from this endpoint.
    """
    status: Optional[str] = None
    progress: Optional[int] = None
    current_phase: Optional[str] = None
    current_task: Optional[str] = None
    phases: Optional[List[Dict[str, Any]]] = None
    result_markdown: Optional[str] = None
    result_html: Optional[str] = None
    sources: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None
    thinking_session_id: Optional[str] = None
    research_session_id: Optional[str] = None
    tokens_used: Optional[int] = None


class QueryRecordCancelRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)


class QueryRecordResponse(BaseModel):
    """Public shape of a record. Hides nothing — frontend uses every field."""
    id: str
    chat_session_id: str
    mode: str
    query_text: str
    status: str
    progress: int
    provider: str
    effort: Optional[str] = None
    current_phase: Optional[str] = None
    current_task: Optional[str] = None
    phases: List[Dict[str, Any]] = Field(default_factory=list)
    result_markdown: Optional[str] = None
    result_html: Optional[str] = None
    sources: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None
    thinking_session_id: Optional[str] = None
    research_session_id: Optional[str] = None
    tokens_used: Optional[int] = None
    started_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None


class ActiveQueryResponse(BaseModel):
    active: bool
    record: Optional[QueryRecordResponse] = None


class QueryRecordListResponse(BaseModel):
    records: List[QueryRecordResponse]


def _to_response(doc: Dict[str, Any]) -> QueryRecordResponse:
    return QueryRecordResponse(
        id=doc["_id"],
        chat_session_id=doc["chat_session_id"],
        mode=doc["mode"],
        query_text=doc.get("query_text") or "",
        status=doc.get("status") or "pending",
        progress=int(doc.get("progress") or 0),
        provider=doc.get("provider") or "",
        effort=doc.get("effort"),
        current_phase=doc.get("current_phase"),
        current_task=doc.get("current_task"),
        phases=doc.get("phases") or [],
        result_markdown=doc.get("result_markdown"),
        result_html=doc.get("result_html"),
        sources=doc.get("sources"),
        error=doc.get("error"),
        thinking_session_id=doc.get("thinking_session_id"),
        research_session_id=doc.get("research_session_id"),
        tokens_used=doc.get("tokens_used"),
        started_at=_dt_utc(doc.get("started_at")) or datetime.now(timezone.utc),
        updated_at=_dt_utc(doc.get("updated_at")) or datetime.now(timezone.utc),
        completed_at=_dt_utc(doc.get("completed_at")),
        cancelled_at=_dt_utc(doc.get("cancelled_at")),
    )


# ─── endpoints ──────────────────────────────────────────────────────────────


@router.post("/api/query-records", response_model=QueryRecordResponse)
async def create_query_record(
    request: QueryRecordCreateRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: User = Depends(get_current_user),
):
    """
    Create a query record. With `idempotency_key`, retried POSTs return
    the existing record instead of creating a duplicate. Without one,
    each call creates a fresh record (caller accepts no dedupe).
    """
    client_id = _require_client_id(x_client_id)

    mode = (request.mode or "").strip().lower()
    if mode not in {"research", "thinking", "coding"}:
        raise HTTPException(status_code=400, detail="Invalid mode")

    try:
        record_id = await chat_store.create_query_record(
            chat_session_id=request.chat_session_id,
            user_id=user.id,
            client_id=client_id,
            mode=mode,
            query_text=request.query_text,
            provider=request.provider,
            effort=request.effort,
            idempotency_key=request.idempotency_key,
        )
    except Exception as exc:
        logger.error("query_record_create_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Could not create query record")

    doc = await chat_store.get_query_record(
        record_id, user_id=user.id, client_id=client_id
    )
    if doc is None:
        # Should be unreachable, but fail loudly if the read-after-write
        # disagrees with the create — points at a replication issue.
        raise HTTPException(status_code=500, detail="Read-after-write inconsistency")
    logger.warning(
        "query_record_created",
        record_id=record_id,
        chat_session_id=request.chat_session_id,
        mode=mode,
        provider=request.provider,
    )
    return _to_response(doc)


@router.get("/api/query-records/{record_id}", response_model=QueryRecordResponse)
async def get_query_record(
    record_id: str,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: User = Depends(get_current_user),
):
    client_id = _require_client_id(x_client_id)
    doc = await chat_store.get_query_record(
        record_id, user_id=user.id, client_id=client_id
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Query record not found")
    return _to_response(doc)


@router.patch("/api/query-records/{record_id}", response_model=QueryRecordResponse)
async def update_query_record(
    record_id: str,
    request: QueryRecordUpdateRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: User = Depends(get_current_user),
):
    """
    Partial update. Validates `status` against the allowed set. Refuses
    to mutate a record the caller doesn't own (404).
    """
    client_id = _require_client_id(x_client_id)

    # Ownership check — get_query_record returns None for non-owners.
    existing = await chat_store.get_query_record(
        record_id, user_id=user.id, client_id=client_id
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Query record not found")

    fields = request.model_dump(exclude_unset=True)
    if "status" in fields and fields["status"] not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. "
                            f"Valid: {sorted(_VALID_STATUSES)}")
    if "progress" in fields:
        # Clamp to 0..100 so a buggy caller can't poison the UI.
        try:
            fields["progress"] = max(0, min(100, int(fields["progress"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="progress must be int 0..100")

    # Auto-stamp completed_at when status flips to a terminal value.
    new_status = fields.get("status")
    if new_status in _TERMINAL_STATUSES and existing.get("status") not in _TERMINAL_STATUSES:
        if new_status == "cancelled":
            fields.setdefault("cancelled_at", datetime.now(timezone.utc))
        else:
            fields.setdefault("completed_at", datetime.now(timezone.utc))

    await chat_store.update_query_record(record_id, **fields)
    refreshed = await chat_store.get_query_record(
        record_id, user_id=user.id, client_id=client_id
    )
    return _to_response(refreshed or existing)


@router.post("/api/query-records/{record_id}/cancel", response_model=QueryRecordResponse)
async def cancel_query_record(
    record_id: str,
    request: QueryRecordCancelRequest = QueryRecordCancelRequest(),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: User = Depends(get_current_user),
):
    """
    Mark a record cancelled. Idempotent — already-terminal records
    return their current state unchanged. The matching backend
    pipeline (research / thinking / Claude task) is signalled to stop
    by the mode-specific cancel endpoint added in Phase C2.
    """
    client_id = _require_client_id(x_client_id)
    cancelled = await chat_store.cancel_query_record(
        record_id,
        user_id=user.id,
        client_id=client_id,
        reason=request.reason,
    )
    doc = await chat_store.get_query_record(
        record_id, user_id=user.id, client_id=client_id
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Query record not found")
    if cancelled:
        logger.warning(
            "query_record_cancelled",
            record_id=record_id,
            reason=request.reason or "user-cancelled",
        )
    return _to_response(doc)


# These two endpoints are session-scoped (`/api/sessions/{sid}/...`)
# but live here because they read the query_records collection — keeping
# them with the rest of the query-record API avoids splitting the
# permission/serialisation logic across two files.

@router.get("/api/sessions/{session_id}/active-query", response_model=ActiveQueryResponse)
async def get_active_query(
    session_id: str,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: User = Depends(get_current_user),
):
    """
    Return the most-recent record for this session whose status is still
    `pending` or `running`. Used by the resume banner on session reload.
    """
    client_id = _require_client_id(x_client_id)
    doc = await chat_store.get_active_query(
        session_id,
        user_id=user.id,
        client_id=client_id,
    )
    if doc is None:
        return ActiveQueryResponse(active=False, record=None)
    return ActiveQueryResponse(active=True, record=_to_response(doc))


@router.get("/api/sessions/{session_id}/query-records", response_model=QueryRecordListResponse)
async def list_query_records_for_session(
    session_id: str,
    limit: int = Query(50, ge=1, le=200),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: User = Depends(get_current_user),
):
    """List all query records for a session (newest first)."""
    client_id = _require_client_id(x_client_id)
    docs = await chat_store.list_query_records_for_session(
        session_id,
        user_id=user.id,
        client_id=client_id,
        limit=limit,
    )
    return QueryRecordListResponse(records=[_to_response(d) for d in docs])
