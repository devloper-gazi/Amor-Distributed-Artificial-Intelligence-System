"""
Sentinel HTTP routes — ``/api/sentinel/*``.

Endpoints
---------

* POST  /api/sentinel/start                 — kick off a scan
* GET   /api/sentinel/{sid}/events          — SSE stream
* GET   /api/sentinel/{sid}/status          — snapshot
* POST  /api/sentinel/{sid}/cancel          — request cancellation
* GET   /api/sentinel/{sid}/artifact        — download SARIF / MD / HTML zip

Mirrors ``consortium_routes.py``: in-memory session cache (TTL),
asyncio.Queue-backed SSE fan-out, optional auth via X-Client-Id +
optional JWT, BackgroundTask kicked-off pipeline.

License: MIT.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from ..auth.dependencies import get_optional_user
from ..auth.models import User
from ..sentinel import (
    SentinelEngine,
    SentinelRequest,
    SentinelBundle,
)  # type: ignore[attr-defined]


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# In-memory session store (mirrors consortium pattern)
# ─────────────────────────────────────────────────────────────────────


_SESSIONS: Dict[str, Dict[str, Any]] = {}
_SESSION_TTL_S = 3600  # 1 hour


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client_id_or_400(x_client_id: Optional[str]) -> str:
    cid = (x_client_id or "").strip()
    if not cid:
        raise HTTPException(status_code=400, detail="X-Client-Id header required")
    return cid


def _gc_sessions() -> None:
    """Drop sessions older than TTL.  Called opportunistically on
    each request to keep the store small without a background task."""
    cutoff = (datetime.now(timezone.utc).timestamp() - _SESSION_TTL_S)
    drop: list[str] = []
    for sid, sess in _SESSIONS.items():
        ts = sess.get("started_at_ts") or 0
        if ts < cutoff:
            drop.append(sid)
    for sid in drop:
        _SESSIONS.pop(sid, None)


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────


router = APIRouter(prefix="/api/sentinel", tags=["sentinel"])


class SentinelStartRequest(BaseModel):
    prompt: Optional[str] = Field(None, max_length=4000)
    paths: List[str] = Field(default_factory=list)
    code_context: Optional[str] = Field(None, max_length=120_000)
    language: Optional[str] = Field(None, max_length=40)
    scan_profile: str = Field(
        "standard",
        pattern="^(quick|standard|deep|paranoid)$",
    )


class SentinelStartResponse(BaseModel):
    success: bool
    session_id: str
    scan_profile: str
    message: str = ""


@router.post("/start", response_model=SentinelStartResponse)
async def start_sentinel(
    body: SentinelStartRequest,
    background: BackgroundTasks,
    http_request: Request,
    user: Optional[User] = Depends(get_optional_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> SentinelStartResponse:
    _gc_sessions()
    client_id = _client_id_or_400(x_client_id)
    user_id = user.id if user else None
    session_id = str(uuid4())

    # Light validation: at least one path or code_context.
    if not body.paths and not (body.code_context or "").strip():
        raise HTTPException(
            status_code=400,
            detail="At least one of `paths` or `code_context` is required.",
        )

    sentinel_request = SentinelRequest(
        prompt=body.prompt or "",
        paths=list(body.paths or []),
        code_context=body.code_context,
        language=body.language,
        scan_profile=body.scan_profile,  # type: ignore[arg-type]
    ).normalize()

    queue: asyncio.Queue = asyncio.Queue(maxsize=2048)

    async def _on_event(event: dict) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest event on overflow — sliding-window.
            try:
                _ = queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(event)
            except Exception:
                pass

    engine = SentinelEngine(
        session_id=session_id,
        request=sentinel_request,
        on_event=_on_event,
    )

    session: Dict[str, Any] = {
        "session_id": session_id,
        "user_id": user_id,
        "client_id": client_id,
        "scan_profile": sentinel_request.scan_profile,
        "request": sentinel_request.to_dict(),
        "status": "started",
        "started_at": _now(),
        "started_at_ts": datetime.now(timezone.utc).timestamp(),
        "completed_at": None,
        "queue": queue,
        "engine": engine,
        "bundle": None,
        "phases": [
            {"name": p, "status": "pending"}
            for p in (
                "normalize", "static_swarm", "ml_pipeline", "aggregate",
                "rag_enrich", "agent_pipeline", "critic_loop", "judge",
                "score", "report",
            )
        ],
        "current_phase": None,
        "events_seen": 0,
        "task": None,
    }
    _SESSIONS[session_id] = session

    async def _run_session() -> None:
        try:
            bundle = await engine.run()
            session["bundle"] = bundle
            session["status"] = "ok"
        except asyncio.CancelledError:
            session["status"] = "cancelled"
        except Exception as exc:
            logger.exception("sentinel run crashed")
            session["status"] = "error"
            session["error"] = f"{type(exc).__name__}: {exc}"[:400]
        finally:
            session["completed_at"] = _now()
            try:
                queue.put_nowait({"type": "sentinel_done"})
            except Exception:
                pass

    task = asyncio.create_task(_run_session(), name=f"sentinel:{session_id}")
    session["task"] = task
    background.add_task(lambda: None)  # ensures FastAPI flushes the response

    return SentinelStartResponse(
        success=True,
        session_id=session_id,
        scan_profile=sentinel_request.scan_profile,
        message="Sentinel scan started",
    )


@router.get("/{sid}/events")
async def sentinel_events(
    sid: str,
    user: Optional[User] = Depends(get_optional_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> StreamingResponse:
    session = _SESSIONS.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    queue: asyncio.Queue = session["queue"]

    async def _generator():
        # Replay phase scaffold first so a late subscriber sees state.
        snapshot = {
            "type": "sentinel_snapshot",
            "session_id": sid,
            "phases": session.get("phases") or [],
            "current_phase": session.get("current_phase"),
            "status": session.get("status"),
        }
        yield f"data: {json.dumps(snapshot)}\n\n"

        # Drain the live queue.
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20.0)
            except asyncio.TimeoutError:
                # Heartbeat keeps the connection from being closed by
                # intermediate proxies (nginx default 60s).
                yield "event: heartbeat\ndata: {}\n\n"
                if session.get("status") in ("ok", "error", "cancelled"):
                    break
                continue
            session["events_seen"] = session.get("events_seen", 0) + 1
            phase = event.get("phase")
            etype = event.get("type")
            # Update phase state for late subscribers + status snapshot.
            if etype == "sentinel_phase_start" and phase:
                session["current_phase"] = phase
                for p in session["phases"]:
                    if p["name"] == phase:
                        p["status"] = "in_progress"
                        break
            if etype == "sentinel_phase_complete" and phase:
                for p in session["phases"]:
                    if p["name"] == phase:
                        p["status"] = "completed"
                        break
            if etype == "sentinel_done":
                yield f"data: {json.dumps(event)}\n\n"
                break
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{sid}/status")
async def sentinel_status(
    sid: str,
    user: Optional[User] = Depends(get_optional_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> JSONResponse:
    session = _SESSIONS.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    bundle: SentinelBundle | None = session.get("bundle")
    payload = {
        "session_id": sid,
        "status": session.get("status"),
        "scan_profile": session.get("scan_profile"),
        "phases": session.get("phases") or [],
        "current_phase": session.get("current_phase"),
        "started_at": session.get("started_at"),
        "completed_at": session.get("completed_at"),
        "events_seen": session.get("events_seen", 0),
        "bundle": bundle.to_dict() if bundle else None,
        "error": session.get("error"),
    }
    return JSONResponse(payload)


@router.post("/{sid}/cancel")
async def sentinel_cancel(
    sid: str,
    user: Optional[User] = Depends(get_optional_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> JSONResponse:
    session = _SESSIONS.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    engine: SentinelEngine = session.get("engine")
    if engine is not None:
        engine.cancel()
    task: asyncio.Task | None = session.get("task")
    if task and not task.done():
        task.cancel()
    return JSONResponse({"ok": True, "session_id": sid})


@router.get("/{sid}/artifact")
async def sentinel_artifact(
    sid: str,
    format: str = "zip",
    user: Optional[User] = Depends(get_optional_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> Response:
    session = _SESSIONS.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    bundle: SentinelBundle | None = session.get("bundle")
    if bundle is None:
        raise HTTPException(status_code=409, detail="scan not completed yet")

    fmt = (format or "zip").lower()
    if fmt == "sarif":
        return Response(
            content=bundle.sarif_report or "{}",
            media_type="application/sarif+json",
            headers={
                "Content-Disposition": f'attachment; filename="sentinel-{sid}.sarif"',
            },
        )
    if fmt == "md":
        return Response(
            content=bundle.markdown_report or "",
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="sentinel-{sid}.md"',
            },
        )
    if fmt == "html":
        return Response(
            content=bundle.html_report or "",
            media_type="text/html",
            headers={
                "Content-Disposition": f'attachment; filename="sentinel-{sid}.html"',
            },
        )

    # zip = default — bundle every format + a JSON dump of the bundle.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if bundle.sarif_report:
            zf.writestr(f"sentinel-{sid}.sarif", bundle.sarif_report)
        if bundle.markdown_report:
            zf.writestr(f"sentinel-{sid}.md", bundle.markdown_report)
        if bundle.html_report:
            zf.writestr(f"sentinel-{sid}.html", bundle.html_report)
        zf.writestr(
            f"sentinel-{sid}.json",
            json.dumps(bundle.to_dict(), default=str, indent=2),
        )
    buffer.seek(0)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="sentinel-{sid}.zip"',
        },
    )


__all__ = ["router"]
