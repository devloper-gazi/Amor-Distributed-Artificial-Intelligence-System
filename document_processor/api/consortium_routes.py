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
# v7 — separate cancel channel so a /cancel POST landing on a
# different replica than the bg task can still propagate the signal.
# The bg task subscribes; the cancel route publishes.
_CONSORTIUM_CANCEL_CHANNEL = "amor:consortium:cancel:{session_id}"


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


# v7 — heartbeat staleness threshold. A session whose last on_event
# was more than this many seconds ago AND is still "started" is a
# zombie (its bg task died with a container restart). The startup +
# periodic sweepers mark it as "interrupted" so /status doesn't keep
# claiming it's running and the user can launch a fresh pipeline.
_ZOMBIE_HEARTBEAT_THRESHOLD_S = 90


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


async def _load(
    session_id: str,
    *,
    prefer_redis: bool = False,
) -> Optional[Dict[str, Any]]:
    """Load a session payload.

    By default returns the local in-memory copy when present (the bg
    task's own mutations are reflected here without a Redis round-trip),
    falling back to Redis on a cache miss.

    v7 — when ``prefer_redis=True``, always query Redis first. Used by
    the read-only status / events / cancel endpoints to avoid serving
    stale data when a request lands on a *different* replica than the
    one running the bg task. Cross-replica caches diverge naturally
    (each replica only updates its own local dict), so the only source
    of truth across replicas is Redis."""
    if not prefer_redis:
        session = _sessions.get(session_id)
        if session:
            return session
    try:
        cached = await cache_manager.get_json(_cache_key(session_id))
        if isinstance(cached, dict):
            # Don't overwrite the local cache when prefer_redis=True —
            # the bg task on this replica might be mid-mutation and the
            # Redis copy is a strict subset of those local mutations.
            if not prefer_redis:
                _sessions[session_id] = cached
            return cached
    except Exception:  # pragma: no cover
        pass
    # Last resort — return local cache even if we asked for Redis.
    return _sessions.get(session_id)


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
    implementation_engine: str = Field(
        "code_intelligence",
        max_length=24,
        description=(
            "Which engine to use for the Implement phase. "
            "`code_intelligence` = full 9-phase pipeline (default). "
            "`quick_code` = 5-phase reasoning-first lite pipeline."
        ),
    )


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
    impl_engine = (body.implementation_engine or "code_intelligence").strip().lower()
    if impl_engine not in {"code_intelligence", "quick_code"}:
        impl_engine = "code_intelligence"
    scope = ConsortiumScope(
        goal=body.goal.strip(),
        depth=depth,                  # type: ignore[arg-type]
        language=body.language,
        deliverable_type=body.deliverable_type,
        allow_external_research=body.allow_external_research,
        research_depth=_normalize_tier(body.research_depth, depth),  # type: ignore[arg-type]
        thinking_effort=_normalize_tier(body.thinking_effort, depth),  # type: ignore[arg-type]
        implementation_effort=_normalize_tier(body.implementation_effort, depth),  # type: ignore[arg-type]
        implementation_engine=impl_engine,  # type: ignore[arg-type]
    )

    artifact_dir = _ARTIFACT_ROOT / session_id

    session: Dict[str, Any] = {
        "session_id": session_id,
        "user_id": user_id,
        "client_id": client_id,
        "status": "started",
        "started_at": _now(),
        "last_heartbeat_at": _now(),
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
        # v7 — heartbeat. Stamps "I am alive" so the periodic zombie
        # sweeper knows this session is still running (vs orphaned by
        # a container restart). Updated on every event = at every
        # phase boundary or finer.
        session["last_heartbeat_at"] = _now()
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

    # v7 — sidecar task subscribed to the cancel pub/sub channel. When
    # a /cancel hits a different replica than this one, the cancel
    # route publishes here and we lift the signal into the local
    # `scope.cancel_requested` so the orchestrator picks it up at the
    # next phase boundary (or asyncio cancels the task outright).
    cancel_channel = _CONSORTIUM_CANCEL_CHANNEL.format(session_id=session_id)
    task = asyncio.current_task()
    cancel_pump_task: Optional[asyncio.Task] = None

    async def _cancel_pump() -> None:
        """Listen for cross-replica cancel signals.

        Two delivery paths:
          1. Live pub/sub on ``cancel_channel`` — fast (<10ms typical)
             but loses messages published before our subscribe.
          2. Periodic Redis GET on the persisted session payload — catches
             the race window where the cancel POST landed *before* our
             pub/sub subscription was established.
        """
        async def _do_cancel(reason: str) -> None:
            scope.cancel_requested = True
            session["cancel_requested"] = True
            logger.info(
                "consortium_cancel_signal_received session=%s via=%s",
                session_id, reason,
            )
            if task and not task.done():
                task.cancel()

        # Path (2) — initial pre-subscribe poll. Handles "cancel POSTed
        # before subscribe completed" within the first ~10ms after task
        # entry. After this we rely on pub/sub.
        try:
            cached = await cache_manager.get_json(_cache_key(session_id))
            if isinstance(cached, dict) and cached.get("cancel_requested"):
                await _do_cancel("redis-pre-subscribe-check")
                return
        except Exception as exc:
            logger.debug("consortium_cancel_pre_check_failed: %s", exc)

        # Path (1) — long-lived pub/sub subscription.
        try:
            async for _evt in cache_manager.subscribe_events(cancel_channel):
                await _do_cancel("pubsub")
                return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("consortium_cancel_pump_failed: %s", exc)

    cancel_pump_task = asyncio.create_task(_cancel_pump())

    # v7 — heartbeat pump. Long LLM calls (CPU qwen2.5:7b takes 60-90s
    # per call) leave gaps where the bg task emits no consortium
    # events, which means on_event doesn't fire and the heartbeat
    # stamp doesn't refresh. Without this, the zombie sweeper would
    # mark a perfectly-healthy session as interrupted just because it's
    # mid-LLM-call. Stamping every 20s keeps live sessions visibly
    # alive (well below the 90s zombie threshold).
    async def _heartbeat_pump() -> None:
        try:
            while True:
                await asyncio.sleep(20)
                session["last_heartbeat_at"] = _now()
                await _persist(session_id, session)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("consortium_heartbeat_pump_failed: %s", exc)
    heartbeat_pump_task = asyncio.create_task(_heartbeat_pump())

    if task:
        _active_tasks[session_id] = task

    try:
        bundle: ConsortiumBundle = await orchestrator.run()
        session["bundle"] = bundle.to_dict()
        # Status was already set in on_event when "consortium_completed"
        # arrived — keep whatever it landed on (ok / error / cancelled).
    except asyncio.CancelledError:
        # External cancel (asyncio task.cancel()). Mark + emit so SSE
        # subscribers see the terminal frame instead of dangling.
        logger.info("consortium_runner_cancelled session=%s", session_id)
        session["status"] = "cancelled"
        session["completed_at"] = _now()
        await on_event({
            "type": "consortium_cancelled",
            "session_id": session_id,
        })
        await on_event({
            "type": "consortium_completed",
            "status": "cancelled",
            "session_id": session_id,
        })
    except Exception as exc:
        logger.exception("consortium_runner_failed session=%s", session_id)
        session["status"] = "error"
        session["error"] = str(exc)
    finally:
        _active_tasks.pop(session_id, None)
        if cancel_pump_task and not cancel_pump_task.done():
            cancel_pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cancel_pump_task
        if heartbeat_pump_task and not heartbeat_pump_task.done():
            heartbeat_pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_pump_task
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
    # v7 — Redis-first so cross-replica subscribers get the right
    # snapshot to seed the SSE stream.
    session = await _load(session_id, prefer_redis=True)
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
    # v7 — always query Redis so cross-replica reads see fresh data
    # (the bg task's own replica updates Redis on every event, but
    # other replicas' local caches are frozen at session creation).
    session = await _load(session_id, prefer_redis=True)
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
    # v7 — Redis-first because we need to set cancel_requested in the
    # canonical Redis copy regardless of which replica the bg task is
    # on. The pub/sub publish below propagates the signal to the bg
    # task's replica.
    session = await _load(session_id, prefer_redis=True)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_owner(session, user)
    session["cancel_requested"] = True
    await _persist(session_id, session)

    # v7 — three-tier cancel propagation:
    #   1. Local replica: cancel the asyncio task directly so a
    #      mid-LLM-call `await` unblocks immediately.
    #   2. Cross-replica: publish to the cancel pub/sub channel; the
    #      bg task on the other replica's _cancel_pump picks it up,
    #      sets scope.cancel_requested, and cancels its own task.
    #   3. Persisted state: cancel_requested=True is written to Redis
    #      so even a future replica that loads the session sees it.
    task = _active_tasks.get(session_id)
    if task and not task.done():
        task.cancel()
    try:
        await cache_manager.publish_event(
            _CONSORTIUM_CANCEL_CHANNEL.format(session_id=session_id),
            {"type": "cancel", "session_id": session_id},
        )
    except Exception as exc:
        logger.debug("consortium_cancel_publish_failed: %s", exc)
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
    # v7 — Redis-first read so we see the most recent artifact_dir +
    # status, even when the request lands on the non-running replica.
    session = await _load(session_id, prefer_redis=True)
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


# ─── v7 zombie sweeper (heartbeat-based) ──────────────────────────────────
#
# When an app container is restarted mid-pipeline, the bg task dies but
# the session payload in Redis stays "started". Without this sweeper
# /status would forever claim the session is running and the user would
# never see a clean "interrupted" state. Two entry points:
#
#   * ``mark_zombies_at_startup`` — run once at lifespan startup. Every
#     "started" session whose heartbeat is older than the threshold
#     gets marked "interrupted" with a synthetic terminal event.
#   * ``sweep_zombies_periodic`` — run by the existing _sse_queue_sweeper
#     every 5 minutes for the same effect on long-running deployments.


async def _enumerate_session_keys() -> list[str]:
    """List every consortium_session:* key in Redis."""
    keys: list[str] = []
    try:
        if not cache_manager.redis:
            await cache_manager.connect()
        cursor = 0
        while True:
            cursor, batch = await cache_manager.redis.scan(
                cursor=cursor,
                match=f"{SESSION_CACHE_PREFIX}*",
                count=200,
            )
            if batch:
                keys.extend(
                    k.decode("utf-8") if isinstance(k, bytes) else str(k)
                    for k in batch
                )
            if cursor == 0:
                break
    except Exception as exc:
        logger.debug("consortium_enumerate_keys_failed: %s", exc)
    return keys


def _is_zombie(session: Dict[str, Any], threshold_s: int) -> bool:
    """A session is a zombie when it claims to be running but its
    heartbeat is older than ``threshold_s``. Sessions that already
    completed / failed / cancelled are not zombies."""
    if (session.get("status") or "started") not in {"started", "running"}:
        return False
    hb = session.get("last_heartbeat_at") or session.get("started_at")
    if not hb:
        return True  # Missing heartbeat is itself zombie evidence.
    try:
        # Parse ISO-8601 timestamp.
        from datetime import datetime as _dt  # noqa: PLC0415
        hb_dt = _dt.fromisoformat(hb.replace("Z", "+00:00"))
    except Exception:
        return True
    age = (datetime.now(timezone.utc) - hb_dt).total_seconds()
    return age > threshold_s


async def _mark_session_interrupted(session_id: str, session: Dict[str, Any]) -> None:
    """Flip a zombie session to ``interrupted`` + persist + emit a
    terminal SSE event so any reconnected client closes its stream."""
    session["status"] = "interrupted"
    session["completed_at"] = _now()
    session["error"] = (
        "Pipeline was interrupted (likely an app container restart). "
        "Start a new session to continue."
    )
    await _persist(session_id, session)
    # Best-effort terminal event over Redis pub/sub so any subscribed
    # SSE client unblocks.
    try:
        await cache_manager.publish_event(
            _CONSORTIUM_EVENT_CHANNEL.format(session_id=session_id),
            {
                "type": "consortium_completed",
                "status": "interrupted",
                "session_id": session_id,
                "event_id": uuid4().hex,
            },
        )
    except Exception as exc:
        logger.debug("consortium_terminal_publish_failed: %s", exc)


async def mark_zombies_at_startup() -> int:
    """Lifespan-hook entry point. Returns the number of sessions
    marked interrupted. Failure-quiet — startup never blocks on this."""
    threshold = _ZOMBIE_HEARTBEAT_THRESHOLD_S
    keys = await _enumerate_session_keys()
    flipped = 0
    for key in keys:
        try:
            session = await cache_manager.get_json(key)
            if not isinstance(session, dict):
                continue
            if _is_zombie(session, threshold):
                sid = key[len(SESSION_CACHE_PREFIX):]
                await _mark_session_interrupted(sid, session)
                flipped += 1
                logger.info("consortium_zombie_marked session=%s", sid)
        except Exception as exc:
            logger.debug("consortium_zombie_check_failed key=%s err=%s", key, exc)
    if flipped:
        logger.info("consortium_zombies_swept count=%d", flipped)
    return flipped


async def sweep_zombies_periodic() -> int:
    """Periodic-sweeper entry point. Same logic, different name so the
    main lifespan loop can call this from its 5-minute tick."""
    return await mark_zombies_at_startup()


async def sweep_stale_event_queues() -> int:
    """Drop event queues whose backing session has reached a terminal
    state. Mirrors the helper in code_intelligence_routes / thinking_routes.
    """
    dropped = 0
    for sid in list(_event_queues.keys()):
        try:
            session = await _load(sid)
            if not session:
                _event_queues.pop(sid, None)
                dropped += 1
                continue
            status = session.get("status")
            if status in {"ok", "error", "cancelled", "interrupted"}:
                _event_queues.pop(sid, None)
                dropped += 1
        except Exception:
            pass
    return dropped
