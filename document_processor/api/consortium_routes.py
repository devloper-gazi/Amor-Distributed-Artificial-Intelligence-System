"""
Consortium Mode HTTP routes — ``/api/consortium/*``.

Endpoints
---------
POST  /api/consortium/start             — kick off a pipeline session
GET   /api/consortium/{sid}/events      — SSE stream of pipeline events
GET   /api/consortium/{sid}/status      — current snapshot
GET   /api/consortium/{sid}             — alias for /status
POST  /api/consortium/{sid}/cancel      — request cancellation
GET   /api/consortium/{sid}/artifact    — download the bundled artifact (zip)

Mirrors the per-mode route patterns: TTL-bounded in-memory session
cache, Redis-backed durable storage, asyncio.Queue with sliding-window
drop, optional auth (JWT-or-X-Client-Id), 5-tier effort budgeting.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth.dependencies import get_optional_user
from ..auth.models import User
from ..consortium import (
    ConsortiumBundle,
    ConsortiumOrchestrator,
    ConsortiumScope,
)
from ..infrastructure.cache import cache_manager

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/consortium", tags=["consortium"])


# ─── session storage (mirrors thinking_routes / code_intelligence_routes) ────


SESSION_CACHE_PREFIX = "consortium_session:"
SESSION_CACHE_TTL_SECONDS = 7 * 24 * 3600  # one week

try:
    from cachetools import TTLCache
    _sessions: Dict[str, Dict[str, Any]] = TTLCache(maxsize=128, ttl=7800)
    _event_queues: Dict[str, asyncio.Queue] = TTLCache(maxsize=128, ttl=7800)
except ImportError:  # pragma: no cover
    _sessions = {}
    _event_queues = {}

_EVENT_QUEUE_MAXSIZE = 500
_CONSORTIUM_EVENT_CHANNEL = "amor:consortium:events:{session_id}"


# Optional active-cancel registry — we keep the asyncio.Task so the
# /cancel endpoint can also trip the orchestrator's check_cancel hook
# from outside the running task.
_active_tasks: Dict[str, asyncio.Task] = {}


# Where artifact bundles live on disk so /artifact can stream a zip
# without re-running the pipeline. One subdir per session_id.
_ARTIFACT_ROOT = Path(tempfile.gettempdir()) / "amor_consortium_artifacts"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_key(session_id: str) -> str:
    return f"{SESSION_CACHE_PREFIX}{session_id}"


def _require_client_id(x_client_id: Optional[str]) -> str:
    if not x_client_id or not x_client_id.strip():
        raise HTTPException(status_code=400, detail="Missing X-Client-Id header")
    return x_client_id.strip()


def _require_owner(session: Dict[str, Any], user: Optional[User]) -> None:
    """Reject reads for sessions owned by another authenticated user.

    Anonymous sessions (``user_id=None``) are accessible to anyone with
    the session_id — same posture as the chat-store routes. Mirrors
    ``code_intelligence_routes._require_owner``.
    """
    owner = session.get("user_id")
    # If the session has an authenticated owner, callers must
    # authenticate as that owner. A 404 (not 403) avoids leaking the
    # existence of the session.
    if owner and (user is None or str(owner) != str(user.id)):
        raise HTTPException(status_code=404, detail="Session not found")


async def _persist(session_id: str, session: Dict[str, Any]) -> None:
    try:
        await cache_manager.set_json(
            _cache_key(session_id), session, ttl=SESSION_CACHE_TTL_SECONDS,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("consortium_persist_failed: %s", exc)


async def _load(session_id: str) -> Optional[Dict[str, Any]]:
    session = _sessions.get(session_id)
    if session:
        return session
    try:
        cached = await cache_manager.get_json(_cache_key(session_id))
        if isinstance(cached, dict):
            _sessions[session_id] = cached
            return cached
    except Exception:  # pragma: no cover
        pass
    return None


def _event_queue(session_id: str) -> asyncio.Queue:
    q = _event_queues.get(session_id)
    if q is None:
        q = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)
        _event_queues[session_id] = q
    return q


# ─── request / response models ──────────────────────────────────────────────


VALID_DEPTHS = {"basic", "medium", "deep", "expert", "ultra"}


class ConsortiumStartRequest(BaseModel):
    """Body for ``POST /start``. The user submits a free-text goal and
    optional knobs; the orchestrator's Scope phase fills in the rest."""

    goal: str = Field(..., min_length=8, max_length=8000,
                      description="Free-text project goal")
    depth: str = Field(
        "medium",
        description="basic | medium | deep | expert | ultra — the global tier knob",
    )
    language: Optional[str] = Field(None, max_length=40,
                                    description="Preferred language (default python)")
    deliverable_type: str = Field("code_module", max_length=40)
    allow_external_research: bool = Field(
        True,
        description="Set false to skip web search and run a fully offline build",
    )
    research_depth: Optional[str] = Field(None, max_length=20)
    thinking_effort: Optional[str] = Field(None, max_length=20)
    implementation_effort: Optional[str] = Field(None, max_length=20)


class ConsortiumStartResponse(BaseModel):
    success: bool
    session_id: str
    message: str = ""


class VerificationGateView(BaseModel):
    phase: str
    status: str
    score: float
    findings: list[str]
    summary: str


# ─── start ────────────────────────────────────────────────────────────────


def _normalize_tier(value: Optional[str], fallback: str) -> str:
    v = (value or fallback or "medium").strip().lower()
    return v if v in VALID_DEPTHS else "medium"


@router.post("/start", response_model=ConsortiumStartResponse)
async def start_consortium(
    body: ConsortiumStartRequest,
    background: BackgroundTasks,
    http_request: Request,
    user: Optional[User] = Depends(get_optional_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> ConsortiumStartResponse:
    """Kick off a Consortium pipeline session. Returns immediately with
    a ``session_id``; the actual work runs in a background task.

    Auth is *optional* — the endpoint accepts anonymous clients via
    ``X-Client-Id`` so the CLI can run without a JWT."""
    client_id = _require_client_id(x_client_id)
    user_id = user.id if user else None
    session_id = str(uuid4())

    depth = _normalize_tier(body.depth, "medium")
    scope = ConsortiumScope(
        goal=body.goal.strip(),
        depth=depth,                  # type: ignore[arg-type]
        language=body.language,
        deliverable_type=body.deliverable_type,
        allow_external_research=body.allow_external_research,
        research_depth=_normalize_tier(body.research_depth, depth),  # type: ignore[arg-type]
        thinking_effort=_normalize_tier(body.thinking_effort, depth),  # type: ignore[arg-type]
        implementation_effort=_normalize_tier(body.implementation_effort, depth),  # type: ignore[arg-type]
    )

    artifact_dir = _ARTIFACT_ROOT / session_id

    session: Dict[str, Any] = {
        "session_id": session_id,
        "user_id": user_id,
        "client_id": client_id,
        "status": "started",
        "started_at": _now(),
        "completed_at": None,
        "scope": scope.to_dict(),
        "phases": [
            {"name": "scope",          "label": "Defining scope",      "status": "pending"},
            {"name": "research",       "label": "Conducting research", "status": "pending"},
            {"name": "thinking",       "label": "Analyzing & thinking", "status": "pending"},
            {"name": "implementation", "label": "Implementing",         "status": "pending"},
        ],
        "current_phase": None,
        "verifications": [],
        "bundle": None,
        "artifact_dir": str(artifact_dir),
        "cancel_requested": False,
        "error": None,
    }
    _sessions[session_id] = session
    await _persist(session_id, session)

    background.add_task(_run_session, session_id)

    return ConsortiumStartResponse(
        success=True, session_id=session_id, message="Consortium pipeline started",
    )


# ─── background runner ──────────────────────────────────────────────────────


async def _run_session(session_id: str) -> None:
    session = _sessions.get(session_id)
    if not session:
        return

    artifact_dir = Path(session.get("artifact_dir") or
                        (_ARTIFACT_ROOT / session_id))
    scope_dict = session.get("scope") or {}
    scope = ConsortiumScope(**{
        k: v for k, v in scope_dict.items()
        if k in ConsortiumScope.__dataclass_fields__  # type: ignore[attr-defined]
    })

    queue = _event_queue(session_id)

    async def on_event(event: Dict[str, Any]) -> None:
        # v6 backstop — make sure every event has an event_id BEFORE
        # we fan out to local queue + Redis. The orchestrator already
        # stamps one in `_emit`, but any future caller that bypasses
        # the orchestrator (e.g., a route-side phase pre-flight emit)
        # would otherwise leak un-dedup-able events.
        if not event.get("event_id"):
            event = {**event, "event_id": uuid4().hex}
        # Apply cancel signal to the scope so the orchestrator notices
        # at the next phase boundary.
        if session.get("cancel_requested"):
            scope.cancel_requested = True
        # Record phase transitions on the session payload.
        etype = str(event.get("type") or "")
        if etype == "consortium_phase_start":
            phase = str(event.get("phase") or "")
            session["current_phase"] = phase
            for p in session["phases"]:
                if p["name"] == phase:
                    p["status"] = "in_progress"
                    p["started_at"] = _now()
        elif etype == "consortium_phase_complete":
            phase = str(event.get("phase") or "")
            for p in session["phases"]:
                if p["name"] == phase:
                    p["status"] = "completed"
                    p["completed_at"] = _now()
        elif etype == "consortium_gate":
            gate = event.get("gate") or {}
            session["verifications"].append(gate)
        elif etype == "consortium_completed":
            session["status"] = str(event.get("status") or "ok")
            session["completed_at"] = _now()
            session["current_phase"] = None
        elif etype == "consortium_cancelled":
            session["status"] = "cancelled"

        # Persist the snapshot every event — replicas + reload reads stay current.
        await _persist(session_id, session)

        # Fan out to the local SSE queue.
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

        # v6 — cross-replica fan-out via Redis pub/sub. Mirrors the
        # pattern in code_intelligence_routes._publish so SSE clients
        # connecting to a *different* replica than the one running the
        # bg task still see live events. Failure-quiet — Redis offline
        # just degrades to single-replica behaviour.
        try:
            await cache_manager.publish_event(
                _CONSORTIUM_EVENT_CHANNEL.format(session_id=session_id),
                event,
            )
        except Exception as exc:
            logger.debug("consortium_publish_failed: %s", exc)

    orchestrator = ConsortiumOrchestrator(
        session_id=session_id,
        scope=scope,
        on_event=on_event,
        artifact_dir=artifact_dir,
    )

    task = asyncio.current_task()
    if task:
        _active_tasks[session_id] = task

    try:
        bundle: ConsortiumBundle = await orchestrator.run()
        session["bundle"] = bundle.to_dict()
        # Status was already set in on_event when "consortium_completed"
        # arrived — keep whatever it landed on (ok / error / cancelled).
    except Exception as exc:
        logger.exception("consortium_runner_failed session=%s", session_id)
        session["status"] = "error"
        session["error"] = str(exc)
    finally:
        _active_tasks.pop(session_id, None)
        await _persist(session_id, session)


# ─── SSE stream ─────────────────────────────────────────────────────────────


@router.get("/{session_id}/events")
async def event_stream(
    session_id: str,
    request: Request,
    user: Optional[User] = Depends(get_optional_user),
):
    """SSE feed of consortium events. Replays cached events on
    reconnect so a flaky client doesn't miss the start of the pipeline.

    v6 — also subscribes to Redis pub/sub so a client connecting to a
    different replica than the one running the bg task still sees live
    events. The local in-memory queue and the Redis subscription are
    drained concurrently; either source can deliver an event first and
    the other path is deduped via ``event_id``.
    """
    session = await _load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_owner(session, user)

    queue = _event_queue(session_id)
    channel = _CONSORTIUM_EVENT_CHANNEL.format(session_id=session_id)

    async def stream():
        # Replay current snapshot so a fresh subscriber sees scope + phases.
        snapshot = {
            "type": "consortium_snapshot",
            "session_id": session_id,
            "status": session.get("status"),
            "scope": session.get("scope"),
            "phases": session.get("phases"),
            "current_phase": session.get("current_phase"),
            "verifications": session.get("verifications"),
            "completed_at": session.get("completed_at"),
        }
        yield f"data: {json.dumps(snapshot)}\n\n"

        seen_ids: set[str] = set()
        # Drain BOTH the in-process queue and the Redis pub/sub channel
        # in parallel. Whichever fires first delivers; the other path
        # gets deduped by event_id.
        sub_task: Optional[asyncio.Task] = None
        sub_queue: asyncio.Queue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)

        async def _redis_pump():
            try:
                async for evt in cache_manager.subscribe_events(channel):
                    if isinstance(evt, dict):
                        try:
                            sub_queue.put_nowait(evt)
                        except asyncio.QueueFull:
                            with contextlib.suppress(
                                asyncio.QueueEmpty, asyncio.QueueFull,
                            ):
                                sub_queue.get_nowait()
                                sub_queue.put_nowait(evt)
            except Exception as exc:
                logger.debug("consortium_subscribe_failed: %s", exc)

        sub_task = asyncio.create_task(_redis_pump())

        try:
            while True:
                if await request.is_disconnected():
                    break
                # Race the local queue + the redis-pump'd queue.
                getters = [
                    asyncio.create_task(queue.get()),
                    asyncio.create_task(sub_queue.get()),
                ]
                try:
                    done, _pending = await asyncio.wait(
                        getters, timeout=15.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    # Always cancel the losing tasks so we don't leak
                    # a pending queue.get() across iterations.
                    for t in getters:
                        if not t.done():
                            t.cancel()
                if not done:
                    yield ": keep-alive\n\n"
                    continue
                # Take the first finished result.
                event = None
                for t in done:
                    if event is None:
                        try:
                            event = t.result()
                        except Exception:
                            event = None
                if event is None:
                    yield ": keep-alive\n\n"
                    continue
                eid = event.get("event_id")
                if eid:
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                yield f"data: {json.dumps(event)}\n\n"
                etype = event.get("type")
                if etype in {"consortium_completed",
                             "consortium_error",
                             "consortium_cancelled"}:
                    break
        finally:
            if sub_task and not sub_task.done():
                sub_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sub_task

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── status / get ───────────────────────────────────────────────────────────


def _public_snapshot(session: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": session.get("session_id"),
        "status": session.get("status"),
        "scope": session.get("scope"),
        "phases": session.get("phases"),
        "current_phase": session.get("current_phase"),
        "verifications": session.get("verifications"),
        "started_at": session.get("started_at"),
        "completed_at": session.get("completed_at"),
        "error": session.get("error"),
        "bundle": session.get("bundle"),
    }


@router.get("/{session_id}/status")
async def get_status(
    session_id: str,
    user: Optional[User] = Depends(get_optional_user),
):
    session = await _load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_owner(session, user)
    return _public_snapshot(session)


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    user: Optional[User] = Depends(get_optional_user),
):
    return await get_status(session_id, user)


# ─── cancel ─────────────────────────────────────────────────────────────────


@router.post("/{session_id}/cancel")
async def cancel_session(
    session_id: str,
    user: Optional[User] = Depends(get_optional_user),
):
    session = await _load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_owner(session, user)
    session["cancel_requested"] = True
    await _persist(session_id, session)
    # If the task is still running on this replica, cancel directly so
    # mid-engine waits unblock immediately. Otherwise the orchestrator
    # picks up the flag at its next phase boundary.
    task = _active_tasks.get(session_id)
    if task and not task.done():
        task.cancel()
    return {"ok": True, "session_id": session_id}


# ─── artifact (ZIP download) ────────────────────────────────────────────────


@router.get("/{session_id}/artifact")
async def download_artifact(
    session_id: str,
    user: Optional[User] = Depends(get_optional_user),
):
    """Stream a zip of the consortium artifact directory.

    The orchestrator wrote ``README.md``, ``scope.json``, the per-phase
    folders (research/, thinking/, code/), ``verifications.json``, and
    ``bundle.json``. This endpoint zips them on the fly so the user
    gets a single downloadable file."""
    session = await _load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_owner(session, user)
    artifact_dir = Path(session.get("artifact_dir") or "")
    if not artifact_dir.exists():
        raise HTTPException(
            status_code=404,
            detail="Artifact not yet written — pipeline is still running or failed early",
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in artifact_dir.rglob("*"):
            if not path.is_file():
                continue
            arcname = path.relative_to(artifact_dir).as_posix()
            zf.write(path, arcname=arcname)
    buf.seek(0)
    filename = f"consortium-{session_id[:8]}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
