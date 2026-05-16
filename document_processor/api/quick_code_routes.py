"""
QuickCode Mode HTTP routes — ``/api/quick-code/*``.

Endpoints
---------
POST  /api/quick-code/start                — kick off a 5-phase QuickCode session
GET   /api/quick-code/{sid}/events         — SSE stream of pipeline events
GET   /api/quick-code/{sid}/status         — current snapshot
GET   /api/quick-code/{sid}                — alias for /status
POST  /api/quick-code/{sid}/cancel         — request cancellation
GET   /api/quick-code/{sid}/artifact       — download the bundled artifact (zip)

Mirrors ``consortium_routes`` — TTL-bounded in-memory session cache,
Redis-backed durable storage, asyncio.Queue with sliding-window drop,
optional auth (JWT-or-X-Client-Id), heartbeat pump, cross-replica
cancel pub/sub, X-Model-Used response header on /start.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
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
    Response,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth.dependencies import get_optional_user
from ..auth.models import User
from ..consortium.orchestrator import ConsortiumOrchestrator  # for artifact helpers
from ..infrastructure.cache import cache_manager
from ..quick_code import (
    QuickCodeBundle,
    QuickCodeEngine,
    QuickCodeRequest,
)
from ..services.model_resolution import resolve_request_model_full

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/quick-code", tags=["quick-code"])


# ─── session storage (mirrors consortium_routes) ─────────────────────────────


SESSION_CACHE_PREFIX = "quick_code_session:"
SESSION_CACHE_TTL_SECONDS = 7 * 24 * 3600  # one week

try:
    from cachetools import TTLCache
    _sessions: Dict[str, Dict[str, Any]] = TTLCache(maxsize=256, ttl=7800)
    _event_queues: Dict[str, asyncio.Queue] = TTLCache(maxsize=256, ttl=7800)
except ImportError:  # pragma: no cover
    _sessions = {}
    _event_queues = {}

_EVENT_QUEUE_MAXSIZE = 500
_QC_EVENT_CHANNEL = "amor:quick_code:events:{session_id}"
_QC_CANCEL_CHANNEL = "amor:quick_code:cancel:{session_id}"

_active_tasks: Dict[str, asyncio.Task] = {}

_ARTIFACT_ROOT = Path(tempfile.gettempdir()) / "amor_quick_code_artifacts"

# Heartbeat: same threshold as consortium so the periodic sweeper can
# treat both session families with a single rule.
_ZOMBIE_HEARTBEAT_THRESHOLD_S = 90


# ─── helpers ─────────────────────────────────────────────────────────────────


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
    the session_id — same posture as the consortium routes."""
    owner = session.get("user_id")
    if owner and (user is None or str(owner) != str(user.id)):
        raise HTTPException(status_code=404, detail="Session not found")


async def _persist(session_id: str, session: Dict[str, Any]) -> None:
    try:
        await cache_manager.set_json(
            _cache_key(session_id), session, ttl=SESSION_CACHE_TTL_SECONDS,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("quick_code_persist_failed: %s", exc)


async def _load(
    session_id: str, *, prefer_redis: bool = False,
) -> Optional[Dict[str, Any]]:
    """Load a session payload. ``prefer_redis=True`` for cross-replica reads."""
    if not prefer_redis:
        local = _sessions.get(session_id)
        if local:
            return local
    try:
        cached = await cache_manager.get_json(_cache_key(session_id))
        if isinstance(cached, dict):
            if not prefer_redis:
                _sessions[session_id] = cached
            return cached
    except Exception:  # pragma: no cover
        pass
    return _sessions.get(session_id)


def _event_queue(session_id: str) -> asyncio.Queue:
    q = _event_queues.get(session_id)
    if q is None:
        q = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)
        _event_queues[session_id] = q
    return q


# ─── request / response models ───────────────────────────────────────────────


VALID_DEPTHS = {"basic", "medium", "deep", "expert", "ultra"}
_MAX_REFINE_HARDCAP = 3


class QuickCodeStartRequest(BaseModel):
    """Body for ``POST /start``."""

    # The picker JS sets `preferred_model`; Pydantic v2 reserves the
    # `model_*` namespace by default and would warn — opt out cleanly.
    model_config = ConfigDict(protected_namespaces=())

    prompt: str = Field(..., min_length=1, max_length=32_000,
                        description="Free-text task description")
    language: Optional[str] = Field(None, max_length=40)
    effort: str = Field("medium")
    code_context: Optional[str] = Field(None, max_length=64_000)
    allow_refine: bool = True
    max_refine: int = Field(2, ge=0, le=_MAX_REFINE_HARDCAP)
    role_overrides: Dict[str, str] = Field(default_factory=dict)
    preferred_model: Optional[str] = Field(None, max_length=120)
    chat_session_id: Optional[str] = Field(None, max_length=80)
    # V2 — Quick / Pro tier toggle.  ``"quick"`` runs with 256 MB / 15 s
    # sandbox limits; ``"pro"`` opens up MCTS + 512 MB / 45 s.  The
    # router auto-redirects ``"quick"`` requests classified as COMPLEX
    # back to the Pro Code Intelligence engine.
    mode: str = Field("quick", pattern="^(quick|pro)$")
    # V2 — optional override for the router's classifier.  Accepts
    # one of ``"trivial"``, ``"simple"``, ``"complex"``, ``"math"``;
    # any other value silently falls back to ``None``.
    complexity_hint: Optional[str] = Field(None, max_length=20)


class QuickCodeStartResponse(BaseModel):
    # v18.1.3 — opt out of Pydantic v2's protected ``model_`` namespace
    # so ``model_used`` doesn't spam UserWarning on every import.  The
    # field is read-only response data, not state — no risk of clash
    # with Pydantic's own model_* methods.
    model_config = {"protected_namespaces": ()}

    success: bool
    session_id: str
    model_used: str = ""
    message: str = ""
    # V2 — when the router classifies a quick-mode prompt as COMPLEX
    # we tell the frontend to retry the request against the Pro
    # endpoint instead of running the lightweight pipeline to a
    # half-baked answer.  Both fields are empty on a normal start.
    redirect_to: str = ""
    redirect_reason: str = ""


def _normalize_tier(value: Optional[str], fallback: str) -> str:
    v = (value or fallback or "medium").strip().lower()
    return v if v in VALID_DEPTHS else "medium"


# ─── start ───────────────────────────────────────────────────────────────────


@router.post("/start", response_model=QuickCodeStartResponse)
async def start_quick_code(
    body: QuickCodeStartRequest,
    background: BackgroundTasks,
    http_request: Request,
    response: Response,
    user: Optional[User] = Depends(get_optional_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> QuickCodeStartResponse:
    """Kick off a QuickCode pipeline session.

    Returns immediately with a ``session_id``; the pipeline runs in
    the background and streams over ``/events``. Auth is *optional* so
    the CLI can run anonymously via ``X-Client-Id``."""
    client_id = _require_client_id(x_client_id)
    user_id = user.id if user else None
    session_id = str(uuid4())

    effort = _normalize_tier(body.effort, "medium")

    # Server-side model resolution. Mode = "quick_code", role = "reasoner"
    # — the reasoning phase is the most demanding LLM step in the
    # pipeline (it has to score four axes structurally), so we resolve
    # against that role first. The engine itself sets _ACTIVE_ROLE per
    # phase so coder/debugger calls still pick up their own bindings.
    resolved_model: Optional[str] = body.preferred_model
    model_reason = "fallback"
    try:
        resolved_model, _profile, model_reason = await resolve_request_model_full(
            request=http_request,
            requested_model=body.preferred_model,
            user_id=str(user_id) if user_id else None,
            client_id=client_id,
            mode="quick_code",
            effort=effort,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("quick_code_resolve_request_model_failed: %s", exc)

    # Spec validation point #1 — surface the resolved tag.
    response.headers["X-Model-Used"] = (
        resolved_model or os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
    )

    artifact_dir = _ARTIFACT_ROOT / session_id
    request = QuickCodeRequest(
        prompt=body.prompt,
        language=body.language,
        effort=effort,  # type: ignore[arg-type]
        code_context=body.code_context,
        allow_refine=body.allow_refine,
        max_refine=body.max_refine,
        role_overrides=dict(body.role_overrides or {}),
        mode=body.mode,  # type: ignore[arg-type]
        complexity_hint=body.complexity_hint,
    ).normalize()

    # V2 — Quick → Pro auto-redirect (synchronous heuristic pre-check).
    # The router's heuristic catches obvious COMPLEX prompts without
    # an LLM call, so we can short-circuit *before* spawning the
    # background task and tell the frontend to retry against
    # ``/api/code/start``.  Falls open: any unexpected exception just
    # lets the regular pipeline run.
    redirect_to = ""
    redirect_reason = ""
    try:
        from ..config.settings import settings as _qc_settings  # noqa: PLC0415

        if (
            getattr(_qc_settings, "quick_v2_enabled", True)
            and getattr(_qc_settings, "quick_v2_router_enabled", True)
            and getattr(_qc_settings, "quick_v2_router_redirect_to_pro", True)
            and request.mode == "quick"
        ):
            from ..quick_code.router import _heuristic  # noqa: PLC0415
            from ..quick_code.contracts import TaskComplexity  # noqa: PLC0415

            verdict, reason = _heuristic(request.prompt)
            if verdict is TaskComplexity.COMPLEX:
                redirect_to = "/api/code/start"
                redirect_reason = reason
    except Exception as exc:  # pragma: no cover - cosmetic
        logger.debug("quick_code_router_pre_check_failed: %s", exc)

    if redirect_to:
        # Don't spawn the background task — the frontend will retry
        # against the Pro engine.  Surface a non-error response so
        # the UI can redirect transparently.
        return QuickCodeStartResponse(
            success=True,
            session_id=session_id,
            model_used=resolved_model
                or os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            message="Routed to Pro Code Intelligence",
            redirect_to=redirect_to,
            redirect_reason=redirect_reason,
        )

    session: Dict[str, Any] = {
        "session_id": session_id,
        "user_id": user_id,
        "client_id": client_id,
        "chat_session_id": body.chat_session_id,
        "status": "started",
        "started_at": _now(),
        "last_heartbeat_at": _now(),
        "completed_at": None,
        "request": request.to_dict(),
        "phases": [
            # V2 — pre-pipeline router + cosine fast-path.
            {"name": "classify",  "label": "Classifying task",      "status": "pending"},
            {"name": "striatum",  "label": "Cache lookup",          "status": "pending"},
            {"name": "triage",    "label": "Triage",                "status": "pending"},
            {"name": "reason",    "label": "Reasoning",             "status": "pending"},
            {"name": "implement", "label": "Implementing",          "status": "pending"},
            {"name": "verify",    "label": "Verifying",             "status": "pending"},
            {"name": "refine",    "label": "Refining (if any)",     "status": "pending"},
            # v10 — empirical performance + property tests on the winner.
            {"name": "reactor",   "label": "Reactor verification",  "status": "pending"},
            # v9 — Multi-ML Mesh post-processing.
            {"name": "audit",     "label": "Mesh code audit",       "status": "pending"},
            {"name": "arbiter",   "label": "Meta-arbiter verdict",  "status": "pending"},
        ],
        "current_phase": None,
        "gates": [],
        "bundle": None,
        "artifact_dir": str(artifact_dir),
        "cancel_requested": False,
        "preferred_model": resolved_model,
        "preferred_model_reason": model_reason,
        "error": None,
    }
    _sessions[session_id] = session
    await _persist(session_id, session)

    background.add_task(_run_session, session_id)

    return QuickCodeStartResponse(
        success=True,
        session_id=session_id,
        model_used=resolved_model or os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
        message="QuickCode pipeline started",
    )


# ─── background runner ───────────────────────────────────────────────────────


async def _run_session(session_id: str) -> None:
    session = _sessions.get(session_id)
    if not session:
        return

    artifact_dir = Path(session.get("artifact_dir") or
                        (_ARTIFACT_ROOT / session_id))
    request_dict = session.get("request") or {}
    request = QuickCodeRequest(**{
        k: v for k, v in request_dict.items()
        if k in QuickCodeRequest.__dataclass_fields__  # type: ignore[attr-defined]
    }).normalize()

    queue = _event_queue(session_id)

    async def on_event(event: Dict[str, Any]) -> None:
        # Backstop event_id stamping — engine already does this, but
        # any future caller bypassing _emit would otherwise leak un-
        # dedup-able events to the SSE stream.
        if not event.get("event_id"):
            event = {**event, "event_id": uuid4().hex}
        session["last_heartbeat_at"] = _now()
        if session.get("cancel_requested"):
            request.cancel_requested = True

        etype = str(event.get("type") or "")
        if etype == "quick_code_phase_start":
            phase = str(event.get("phase") or "")
            session["current_phase"] = phase
            for p in session["phases"]:
                if p["name"] == phase:
                    p["status"] = "in_progress"
                    p["started_at"] = _now()
        elif etype == "quick_code_phase_complete":
            phase = str(event.get("phase") or "")
            for p in session["phases"]:
                if p["name"] == phase:
                    p["status"] = "completed"
                    p["completed_at"] = _now()
        elif etype == "quick_code_gate":
            gate = event.get("gate") or {}
            session["gates"].append(gate)
        elif etype == "quick_code_completed":
            session["status"] = "ok"
            session["completed_at"] = _now()
            session["current_phase"] = None
        elif etype == "quick_code_cancelled":
            session["status"] = "cancelled"
        elif etype == "quick_code_error":
            session["status"] = "error"
            session["error"] = str(event.get("error") or "")[:600]

        await _persist(session_id, session)

        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

        # Cross-replica fanout.
        try:
            await cache_manager.publish_event(
                _QC_EVENT_CHANNEL.format(session_id=session_id),
                event,
            )
        except Exception as exc:
            logger.debug("quick_code_publish_failed: %s", exc)

    engine = QuickCodeEngine(
        session_id=session_id,
        request=request,
        on_event=on_event,
    )

    cancel_channel = _QC_CANCEL_CHANNEL.format(session_id=session_id)
    task = asyncio.current_task()

    async def _cancel_pump() -> None:
        async def _do_cancel(reason: str) -> None:
            request.cancel_requested = True
            session["cancel_requested"] = True
            logger.info("quick_code_cancel_signal session=%s via=%s",
                        session_id, reason)
            if task and not task.done():
                task.cancel()

        try:
            cached = await cache_manager.get_json(_cache_key(session_id))
            if isinstance(cached, dict) and cached.get("cancel_requested"):
                await _do_cancel("redis-pre-subscribe-check")
                return
        except Exception as exc:
            logger.debug("quick_code_cancel_pre_check_failed: %s", exc)

        try:
            async for _evt in cache_manager.subscribe_events(cancel_channel):
                await _do_cancel("pubsub")
                return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("quick_code_cancel_pump_failed: %s", exc)

    cancel_pump_task = asyncio.create_task(_cancel_pump())

    async def _heartbeat_pump() -> None:
        try:
            while True:
                await asyncio.sleep(20)
                session["last_heartbeat_at"] = _now()
                await _persist(session_id, session)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("quick_code_heartbeat_pump_failed: %s", exc)

    heartbeat_pump_task = asyncio.create_task(_heartbeat_pump())

    if task:
        _active_tasks[session_id] = task

    try:
        bundle: QuickCodeBundle = await engine.run()
        session["bundle"] = bundle.to_dict()

        # Write the artifact bundle using the consortium's artifact
        # helpers so requirements.txt / run.sh / pyproject.toml /
        # src,tests,docs layout all come for free. Convert via the
        # ImplementationArtifact adapter then synthesize the rest of
        # a ConsortiumBundle just for the writer.
        try:
            await _write_artifact(bundle, artifact_dir)
        except Exception as exc:
            logger.warning("quick_code_artifact_write_failed: %s", exc)

    except asyncio.CancelledError:
        logger.info("quick_code_runner_cancelled session=%s", session_id)
        session["status"] = "cancelled"
        session["completed_at"] = _now()
        await on_event({
            "type": "quick_code_cancelled",
            "session_id": session_id,
        })
        await on_event({
            "type": "quick_code_completed",
            "status": "cancelled",
            "session_id": session_id,
        })
    except Exception as exc:
        logger.exception("quick_code_runner_failed session=%s", session_id)
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


async def _write_artifact(bundle: QuickCodeBundle, dst: Path) -> None:
    """Reuse the ConsortiumOrchestrator artifact-writer to produce a
    runnable project layout (src/, tests/, docs/, reports/,
    requirements.txt, run.sh, pyproject.toml, .gitignore).

    QuickCode's bundle adapts cleanly to ImplementationArtifact, and
    the writer expects a ConsortiumBundle envelope — synthesize a
    minimal one in-memory just for the write.
    """
    from ..consortium.models import (  # noqa: PLC0415
        ConsortiumBundle, ConsortiumScope, VerificationGate,
    )

    impl = bundle.to_implementation_artifact()
    scope = ConsortiumScope(
        goal=bundle.request.prompt[:1000],
        depth=bundle.request.effort,  # type: ignore[arg-type]
        language=bundle.request.language or "python",
        deliverable_type="code_snippet",
        title=ConsortiumOrchestrator._derive_title(bundle.request.prompt),
        summary=bundle.request.prompt[:280],
    )
    # Map QuickCode gates → consortium VerificationGate dataclass shape.
    verifications = [
        VerificationGate(
            phase=g.phase, status=g.status,  # type: ignore[arg-type]
            score=g.score, findings=list(g.findings),
            summary=g.summary,
        )
        for g in bundle.gates
    ]
    # Build the README via consortium's renderer so layout + Quick Start
    # block are consistent across both engines. _build_readme_markdown
    # reads instance state, so we instantiate just to render — never
    # call .run() on it.
    orch_for_render = ConsortiumOrchestrator(
        session_id=bundle.session_id, scope=scope,
    )
    orch_for_render.implementation = impl
    orch_for_render.verifications = verifications

    cb = ConsortiumBundle(
        session_id=bundle.session_id,
        scope=scope,
        research=None,
        thinking=None,
        implementation=impl,
        verifications=verifications,
        readme_markdown=orch_for_render._build_readme_markdown(),
        started_at=bundle.started_at,
        completed_at=bundle.completed_at,
    )
    ConsortiumOrchestrator._write_artifact_dir(cb, dst)


# ─── derive_title shim — ConsortiumOrchestrator._derive_title is a static method ─

# (the call in _write_artifact resolves at attribute access; nothing more
# needed here)


# ─── SSE stream ──────────────────────────────────────────────────────────────


@router.get("/{session_id}/events")
async def event_stream(
    session_id: str,
    request: Request,
    user: Optional[User] = Depends(get_optional_user),
):
    """SSE feed of quick-code events. Replays a snapshot first, then
    races the local in-memory queue against the Redis pub/sub channel
    so cross-replica subscribers see live events. ``event_id`` dedups
    across both delivery paths."""
    session = await _load(session_id, prefer_redis=True)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_owner(session, user)

    queue = _event_queue(session_id)
    channel = _QC_EVENT_CHANNEL.format(session_id=session_id)

    async def stream():
        snapshot = {
            "type": "quick_code_snapshot",
            "session_id": session_id,
            "status": session.get("status"),
            "request": session.get("request"),
            "phases": session.get("phases"),
            "current_phase": session.get("current_phase"),
            "gates": session.get("gates"),
            "completed_at": session.get("completed_at"),
        }
        yield f"data: {json.dumps(snapshot)}\n\n"

        seen_ids: set[str] = set()
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
                logger.debug("quick_code_subscribe_failed: %s", exc)

        sub_task = asyncio.create_task(_redis_pump())
        try:
            while True:
                if await request.is_disconnected():
                    break
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
                    for t in getters:
                        if not t.done():
                            t.cancel()
                if not done:
                    yield ": keep-alive\n\n"
                    continue
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
                if etype in {"quick_code_completed",
                             "quick_code_error",
                             "quick_code_cancelled"}:
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


# ─── status ──────────────────────────────────────────────────────────────────


def _public_snapshot(session: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": session.get("session_id"),
        "status": session.get("status"),
        "request": session.get("request"),
        "phases": session.get("phases"),
        "current_phase": session.get("current_phase"),
        "gates": session.get("gates"),
        "started_at": session.get("started_at"),
        "completed_at": session.get("completed_at"),
        "error": session.get("error"),
        "bundle": session.get("bundle"),
        "preferred_model": session.get("preferred_model"),
        "preferred_model_reason": session.get("preferred_model_reason"),
    }


@router.get("/{session_id}/status")
async def get_status(
    session_id: str,
    user: Optional[User] = Depends(get_optional_user),
):
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


# ─── cancel ──────────────────────────────────────────────────────────────────


@router.post("/{session_id}/cancel")
async def cancel_session(
    session_id: str,
    user: Optional[User] = Depends(get_optional_user),
):
    session = await _load(session_id, prefer_redis=True)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_owner(session, user)
    session["cancel_requested"] = True
    await _persist(session_id, session)
    task = _active_tasks.get(session_id)
    if task and not task.done():
        task.cancel()
    try:
        await cache_manager.publish_event(
            _QC_CANCEL_CHANNEL.format(session_id=session_id),
            {"type": "cancel", "session_id": session_id},
        )
    except Exception as exc:
        logger.debug("quick_code_cancel_publish_failed: %s", exc)
    return {"ok": True, "session_id": session_id}


# ─── artifact ────────────────────────────────────────────────────────────────


@router.get("/{session_id}/artifact")
async def download_artifact(
    session_id: str,
    user: Optional[User] = Depends(get_optional_user),
):
    """Stream a zip of the QuickCode artifact directory.

    The bundle writer dropped a runnable Python project layout
    (`src/`, `tests/`, `docs/`, `reports/`, `requirements.txt`,
    `run.sh`, `pyproject.toml`, `.gitignore`, plus top-level
    `README.md`, `scope.json`, `verifications.json`, `bundle.json`)."""
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
    filename = f"quick-code-{session_id[:8]}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
