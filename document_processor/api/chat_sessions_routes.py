"""
Chat session persistence API.

This API is used by the web UI to reliably store and retrieve chat history
from MongoDB (instead of relying on browser localStorage).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from ..infrastructure.chat_store import chat_store
from ..config.logging_config import logger


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _require_client_id(x_client_id: Optional[str]) -> str:
    if not x_client_id or not x_client_id.strip():
        raise HTTPException(status_code=400, detail="Missing X-Client-Id header")
    return x_client_id.strip()


def _normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m not in {"research", "thinking", "coding"}:
        raise HTTPException(status_code=400, detail="Invalid mode")
    return m


def _dt_utc(dt: datetime) -> datetime:
    """
    Ensure datetimes serialize with an explicit UTC offset.

    MongoDB/PyMongo often returns naive datetimes that are intended to be UTC.
    If we return them as-is, browsers may parse them as local time.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SessionCreateRequest(BaseModel):
    mode: str = Field(..., description="research | thinking | coding")
    title: Optional[str] = Field(None, description="Optional session title")


class SessionUpdateRequest(BaseModel):
    title: Optional[str] = None
    mode: Optional[str] = None
    archived: Optional[bool] = None
    folder_id: Optional[str] = None
    pinned: Optional[bool] = None


class MessageAppendRequest(BaseModel):
    role: str = Field(..., description="user | assistant | system")
    content: str = Field(..., description="Message content (text or HTML)")
    format: str = Field("text", description="text | html")
    aiType: Optional[str] = Field(None, description="claude | local-ai | etc")
    extras: Optional[Dict[str, Any]] = Field(default_factory=dict)


class SessionSummary(BaseModel):
    id: str
    mode: str
    title: str
    created_at: datetime
    updated_at: datetime
    archived: bool = False
    folder_id: Optional[str] = None
    pinned: bool = False


class SessionListResponse(BaseModel):
    sessions: List[SessionSummary]


class SessionDetailResponse(BaseModel):
    id: str
    mode: str
    title: str
    created_at: datetime
    updated_at: datetime
    archived: bool = False
    folder_id: Optional[str] = None
    pinned: bool = False
    messages: List[Dict[str, Any]]


@router.post("", response_model=SessionDetailResponse)
async def create_session(
    request: SessionCreateRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    client_id = _require_client_id(x_client_id)
    mode = _normalize_mode(request.mode)

    session = await chat_store.create_session(
        client_id=client_id,
        user_id=x_user_id,
        mode=mode,
        title=request.title,
    )

    return SessionDetailResponse(
        id=session.id,
        mode=session.mode,
        title=session.title,
        created_at=_dt_utc(session.created_at),
        updated_at=_dt_utc(session.updated_at),
        archived=bool(getattr(session, "archived", False)),
        folder_id=getattr(session, "folder_id", None),
        pinned=bool(getattr(session, "pinned", False)),
        messages=[],
    )


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    mode: str = Query(..., description="research | thinking | coding"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_archived: bool = Query(True, description="Whether to include archived sessions"),
    folder_id: Optional[str] = Query(None, description="Filter sessions by folder_id"),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    client_id = _require_client_id(x_client_id)
    mode = _normalize_mode(mode)

    sessions = await chat_store.list_sessions(
        client_id=client_id,
        user_id=x_user_id,
        mode=mode,
        limit=limit,
        offset=offset,
        include_archived=include_archived,
        folder_id=folder_id,
    )

    return SessionListResponse(
        sessions=[
            SessionSummary(
                id=s["id"],
                mode=s["mode"],
                title=s.get("title") or "Untitled Chat",
                created_at=_dt_utc(s["created_at"]),
                updated_at=_dt_utc(s["updated_at"]),
                archived=bool(s.get("archived", False)),
                folder_id=s.get("folder_id"),
                pinned=bool(s.get("pinned", False)),
            )
            for s in sessions
        ]
    )


@router.get("/all", response_model=SessionListResponse)
async def list_sessions_all(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_archived: bool = Query(False, description="Whether to include archived sessions"),
    folder_id: Optional[str] = Query(None, description="Filter sessions by folder_id"),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    client_id = _require_client_id(x_client_id)

    sessions = await chat_store.list_sessions_all(
        client_id=client_id,
        user_id=x_user_id,
        limit=limit,
        offset=offset,
        include_archived=include_archived,
        folder_id=folder_id,
    )

    return SessionListResponse(
        sessions=[
            SessionSummary(
                id=s["id"],
                mode=s["mode"],
                title=s.get("title") or "Untitled Chat",
                created_at=_dt_utc(s["created_at"]),
                updated_at=_dt_utc(s["updated_at"]),
                archived=bool(s.get("archived", False)),
                folder_id=s.get("folder_id"),
                pinned=bool(s.get("pinned", False)),
            )
            for s in sessions
        ]
    )


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: str,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    client_id = _require_client_id(x_client_id)
    try:
        session = await chat_store.get_session(
            client_id=client_id,
            user_id=x_user_id,
            session_id=session_id,
            include_messages=True,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")

    # Normalize any naive timestamps (treat as UTC) so frontend grouping is correct.
    messages = session.get("messages", [])
    for m in messages:
        created = m.get("createdAt")
        if isinstance(created, datetime):
            m["createdAt"] = _dt_utc(created)

    return SessionDetailResponse(
        id=session["id"],
        mode=session["mode"],
        title=session.get("title") or "Untitled Chat",
        created_at=_dt_utc(session["created_at"]),
        updated_at=_dt_utc(session["updated_at"]),
        archived=bool(session.get("archived", False)),
        folder_id=session.get("folder_id"),
        pinned=bool(session.get("pinned", False)),
        messages=messages,
    )


@router.post("/{session_id}/messages")
async def append_message(
    session_id: str,
    request: MessageAppendRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    client_id = _require_client_id(x_client_id)

    role = (request.role or "").strip().lower()
    if role not in {"user", "assistant", "system"}:
        raise HTTPException(status_code=400, detail="Invalid role")

    fmt = (request.format or "text").strip().lower()
    if fmt not in {"text", "html"}:
        raise HTTPException(status_code=400, detail="Invalid format")

    try:
        message_id = await chat_store.append_message(
            client_id=client_id,
            user_id=x_user_id,
            session_id=session_id,
            role=role,
            content=request.content,
            format=fmt,
            ai_type=request.aiType,
            extras=request.extras or {},
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")
    except Exception as e:
        logger.error("append_message_failed", error=str(e), session_id=session_id)
        raise HTTPException(status_code=500, detail="Failed to append message")

    return {"ok": True, "message_id": message_id}


@router.patch("/{session_id}")
async def update_session(
    session_id: str,
    request: SessionUpdateRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    client_id = _require_client_id(x_client_id)
    mode = _normalize_mode(request.mode) if request.mode is not None else None

    try:
        fields_set = getattr(request, "model_fields_set", getattr(request, "__fields_set__", set()))
        extra_updates: Dict[str, Any] = {}
        if "archived" in fields_set:
            # Treat null as False (unarchive) for robustness.
            extra_updates["archived"] = bool(request.archived)
        if "folder_id" in fields_set:
            # Allow null to remove folder assignment.
            extra_updates["folder_id"] = request.folder_id
        if "pinned" in fields_set:
            # Treat null as False (unpin) for robustness.
            extra_updates["pinned"] = bool(request.pinned)

        await chat_store.update_session(
            client_id=client_id,
            user_id=x_user_id,
            session_id=session_id,
            title=request.title,
            mode=mode,
            **extra_updates,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"ok": True}


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    client_id = _require_client_id(x_client_id)
    await chat_store.delete_session(
        client_id=client_id,
        user_id=x_user_id,
        session_id=session_id,
    )
    return {"ok": True}

