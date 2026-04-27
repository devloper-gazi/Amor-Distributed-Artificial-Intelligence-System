"""
Code Intelligence Mode HTTP routes.

Endpoints
---------
POST  /api/code/triage                       — fast classification (no session)
POST  /api/code/start                        — start full pipeline session
GET   /api/code/{sid}/events                 — SSE stream
GET   /api/code/{sid}/status                 — session snapshot
GET   /api/code/{sid}                        — alias for /status
POST  /api/code/{sid}/cancel                 — cancel running session
GET   /api/code/models                       — list catalogue + install status
POST  /api/code/models/{tag}/pull            — SSE pull progress
GET   /api/code/sandbox/health               — Docker / image availability

Mirrors `thinking_routes.py`: per-user scoping, in-memory TTL-bounded
session cache, Redis durable backing, asyncio.Queue with sliding-window
drop, Redis pub/sub fan-out across replicas, lifespan sweeper hook.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional
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

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..services.model_resolution import (
    resolve_request_model,
    resolve_request_model_full,
)
from ..code_intelligence import (
    CODE_PHASES,
    AdversarialReviewer,
    CapabilityDiscoverer,
    CodeIntelligenceEngine,
    CodeModelRegistry,
    ExecutionSandbox,
    StaticAnalysisHarness,
)
from ..code_intelligence.agents import run_triage
from ..config.settings import settings
from ..infrastructure.cache import cache_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/code", tags=["code-intelligence"])


# ─── session storage ──────────────────────────────────────────────────────────


SESSION_CACHE_PREFIX = "code_session:"

try:
    from cachetools import TTLCache
    _sessions: Dict[str, Dict[str, Any]] = TTLCache(maxsize=256, ttl=7800)
except ImportError:  # pragma: no cover
    _sessions = {}

try:
    from cachetools import TTLCache as _TTLCache  # noqa: WPS433
    _event_queues: Dict[str, asyncio.Queue] = _TTLCache(
        maxsize=256, ttl=7800,
    )
except ImportError:  # pragma: no cover
    _event_queues = {}

_EVENT_QUEUE_MAXSIZE = 500
_CODE_EVENT_CHANNEL = "amor:code:events:{session_id}"

SESSION_CACHE_TTL_SECONDS = settings.code_session_ttl_seconds


# ─── shared singletons ───────────────────────────────────────────────────────


_model_registry: Optional[CodeModelRegistry] = None
_sandbox: Optional[ExecutionSandbox] = None
_static_harness: Optional[StaticAnalysisHarness] = None
_adversarial_reviewer: Optional[AdversarialReviewer] = None
_capability_discoverer: Optional[CapabilityDiscoverer] = None


def get_model_registry() -> CodeModelRegistry:
    global _model_registry
    if _model_registry is None:
        _model_registry = CodeModelRegistry(settings.code_ollama_base_url)
    return _model_registry


def get_sandbox() -> Optional[ExecutionSandbox]:
    global _sandbox
    if not settings.code_sandbox_enabled:
        return None
    if _sandbox is None:
        _sandbox = ExecutionSandbox(
            default_timeout=settings.code_sandbox_timeout,
            memory_limit=settings.code_sandbox_memory,
        )
    return _sandbox


def get_static_harness() -> StaticAnalysisHarness:
    global _static_harness
    if _static_harness is None:
        _static_harness = StaticAnalysisHarness()
    return _static_harness


def get_adversarial_reviewer() -> AdversarialReviewer:
    """Lazy singleton — one rule-pack-loaded reviewer per process."""
    global _adversarial_reviewer
    if _adversarial_reviewer is None:
        _adversarial_reviewer = AdversarialReviewer(block_on_critical=True)
    return _adversarial_reviewer


def get_capability_discoverer() -> CapabilityDiscoverer:
    """Lazy singleton (one Discoverer per process)."""
    global _capability_discoverer
    if _capability_discoverer is None:
        _capability_discoverer = CapabilityDiscoverer(
            interval_s=settings.code_capability_discovery_interval_seconds,
            max_per_cycle=settings.code_capability_discovery_max_per_cycle,
        )
    return _capability_discoverer


# ─── helpers (mirror thinking_routes.py) ─────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_key(session_id: str) -> str:
    return f"{SESSION_CACHE_PREFIX}{session_id}"


async def _persist(session_id: str, session: Dict[str, Any]) -> None:
    try:
        await cache_manager.set_json(
            _cache_key(session_id),
            session,
            ttl=SESSION_CACHE_TTL_SECONDS,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("code: persist failed: %s", exc)


async def _load(session_id: str) -> Optional[Dict[str, Any]]:
    session = _sessions.get(session_id)
    if session:
        return session
    try:
        cached = await cache_manager.get_json(_cache_key(session_id))
        if isinstance(cached, dict):
            _sessions[session_id] = cached
            return cached
    except Exception as exc:  # pragma: no cover
        logger.debug("code: load failed: %s", exc)
    return None


def _event_queue(session_id: str) -> asyncio.Queue:
    q = _event_queues.get(session_id)
    if q is None:
        q = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)
        _event_queues[session_id] = q
    return q


async def _publish(session_id: str, event: Dict[str, Any]) -> None:
    """
    Fan an event to local SSE subscribers + cross-replica Redis.

    Every event passes through the AdversarialReviewer first. On a
    critical match the original event is suppressed and an
    `adversarial_alert` is published in its place; the running
    session is also flagged for cancellation. On non-critical
    matches the alert event is published alongside the original.
    """
    if "event_id" not in event:
        event = {**event, "event_id": uuid4().hex}

    reviewer = get_adversarial_reviewer()
    allow, alert = reviewer.inspect_event(session_id, event)
    if alert is not None:
        # Stamp the alert with its own event_id so SSE dedupe works.
        if "event_id" not in alert:
            alert = {**alert, "event_id": uuid4().hex}
        # Critical hit → mark session for cancellation so the engine
        # halts at the next phase boundary.
        if alert.get("severity") == "critical":
            session = _sessions.get(session_id)
            if session is not None:
                session["cancel_requested"] = True
                session["adversarial_alert"] = alert
                await _persist(session_id, session)

    events_to_emit: List[Dict[str, Any]] = []
    if allow:
        events_to_emit.append(event)
    if alert is not None:
        events_to_emit.append(alert)

    queue = _event_queue(session_id)
    for ev in events_to_emit:
        try:
            queue.put_nowait(ev)
        except asyncio.QueueFull:
            # Sliding window drop — preserve newest.
            try:
                queue.get_nowait()
                queue.put_nowait(ev)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
        try:
            await cache_manager.publish_event(
                _CODE_EVENT_CHANNEL.format(session_id=session_id),
                ev,
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("code _publish redis fanout failed: %s", exc)


async def sweep_stale_event_queues() -> int:
    """Phase D4 sweeper hook for the lifespan task."""
    if not _event_queues:
        return 0
    dropped = 0
    for sid in list(_event_queues.keys()):
        snap = await _load(sid)
        if snap is None:
            _event_queues.pop(sid, None)
            dropped += 1
            continue
        if snap.get("status") in {"completed", "failed", "cancelled"}:
            _event_queues.pop(sid, None)
            dropped += 1
    if dropped:
        logger.warning("code_sse_queues_swept dropped=%d", dropped)
    return dropped


def _require_owner(session: Dict[str, Any], user: User) -> None:
    owner = session.get("user_id")
    if owner and str(owner) != str(user.id):
        raise HTTPException(status_code=404, detail="Session not found")


# ─── LLM plumbing — local Ollama only. NEVER calls anthropic/openai. ─────────


async def _llm_call_local(
    prompt: str,
    system: Optional[str],
    max_tokens: int,
) -> str:
    """
    Single bridge to Ollama. The CodeIntelligenceEngine is LLM-agnostic
    but the routes layer always wires it to local-only inference.
    """
    from .local_ai_routes_simple import call_ollama
    return await call_ollama(prompt, system, max_tokens=max_tokens)


# ─── request / response models ───────────────────────────────────────────────


class TriageRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=32000)
    code_context: Optional[str] = Field(None, max_length=50000)


class TriageResponse(BaseModel):
    task_type: str
    language: str
    complexity: str
    needs_execution: bool
    needs_tests: bool
    estimated_phases: List[str] = Field(default_factory=list)


class CodeStartRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=32000)
    code_context: Optional[str] = Field(None, max_length=50000)
    language: Optional[str] = None
    effort: str = Field("medium", pattern="^(basic|medium|deep|expert|ultra)$")
    provider: str = Field("local")  # always "local"
    enable_execution: bool = True
    enable_static_analysis: bool = True
    enable_testing: bool = True
    max_debug_iterations: Optional[int] = Field(None, ge=0, le=5)
    preferred_model: Optional[str] = Field(None, max_length=120)
    # Phase C linkage from the previous overhaul.
    chat_session_id: Optional[str] = Field(None, max_length=64)
    query_record_id: Optional[str] = Field(None, max_length=64)
    user_message_idempotency_key: Optional[str] = Field(None, max_length=64)
    assistant_message_idempotency_key: Optional[str] = Field(None, max_length=64)


class CodeStartResponse(BaseModel):
    success: bool
    session_id: str
    message: str


# ─── routes ──────────────────────────────────────────────────────────────────


@router.post("/triage", response_model=TriageResponse)
async def triage(
    payload: TriageRequest,
    user: User = Depends(get_current_user),
) -> TriageResponse:
    """Fast classification — no session, no SSE, just a model call."""
    try:
        data = await run_triage(
            _llm_call_local,
            payload.prompt,
            payload.code_context,
        )
    except Exception as exc:
        logger.warning("code_triage_failed: %s", exc)
        data = {
            "task_type": "generation",
            "language": "python",
            "complexity": "moderate",
            "needs_execution": True,
            "needs_tests": True,
            "estimated_phases": [],
        }
    return TriageResponse(**data)


@router.post("/start", response_model=CodeStartResponse)
async def start_code_session(
    payload: CodeStartRequest,
    background: BackgroundTasks,
    http_request: Request,
    user: User = Depends(get_current_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> CodeStartResponse:
    """
    Kick off a full Code Intelligence session. Returns immediately with
    a session_id; the pipeline runs in the background and streams over
    /events.
    """
    session_id = str(uuid4())
    effort = payload.effort or "medium"
    max_debug = (
        payload.max_debug_iterations
        if payload.max_debug_iterations is not None
        else settings.code_max_debug_iterations
    )

    # Server-side model resolution. The Code Intelligence engine *also*
    # picks per-role on top of this — but the resolved tag here becomes
    # the global default (planner/coder/critic all see it unless they
    # override). When the user picked a tag in the picker, we honour it
    # everywhere; otherwise we let the registry's per-role auto-pick
    # logic take over by leaving preferred_model = None.
    client_id_hdr = (x_client_id or "").strip() or session_id
    resolved_profile: Optional[Dict[str, Any]] = None
    try:
        resolved_model, resolved_profile, model_reason = (
            await resolve_request_model_full(
                request=http_request,
                requested_model=payload.preferred_model,
                user_id=str(user.id),
                client_id=client_id_hdr,
                mode="code",
                effort=effort,
            )
        )
        # Code Intelligence has a richer per-role auto-select than the
        # generic ModelManager — only honour resolution if it came from
        # an explicit user choice, not a generic auto pick.
        if model_reason in {"request override", "user preference (code)",
                             "user preference (coding)", "user preference (__all__)"}:
            effective_model = resolved_model
        else:
            effective_model = None  # Let CodeModelRegistry pick per role
            resolved_profile = None  # without an explicit pref the profile is moot
    except Exception as exc:  # pragma: no cover
        logger.warning("code_resolve_request_model_failed: %s", exc)
        effective_model, model_reason = (payload.preferred_model, "fallback")

    session: Dict[str, Any] = {
        "session_id": session_id,
        "user_id": str(user.id),
        "status": "started",
        "progress": 0,
        "prompt": payload.prompt,
        "code_context": payload.code_context,
        "language": payload.language,
        "effort": effort,
        "provider": "local",
        "enable_execution": bool(payload.enable_execution
                                 and settings.code_sandbox_enabled),
        "enable_static_analysis": bool(payload.enable_static_analysis),
        "enable_testing": bool(payload.enable_testing),
        "max_debug_iterations": int(max_debug),
        "preferred_model": effective_model,
        "preferred_model_requested": payload.preferred_model,
        "preferred_model_reason": model_reason,
        "preferred_model_profile": resolved_profile,
        # Phase scaffold matching CODE_PHASES order.
        "phases": [
            {"name": n, "label": l, "status": "pending", "detail": {}}
            for n, l in CODE_PHASES
        ],
        "current_phase": None,
        "current_task": "Warming up",
        # Pipeline outputs filled in as phases complete.
        "models_used": {},
        "triage": None,
        "plan": None,
        "code": None,
        "tests": None,
        "execution_results": [],
        "static_analysis": None,
        "review": None,
        "deliverable_markdown": None,
        "debug_iterations": 0,
        # Persistence linkage (Phase C of fancy-swinging-karp.md).
        "chat_session_id": payload.chat_session_id,
        "query_record_id": payload.query_record_id,
        "user_message_idempotency_key": payload.user_message_idempotency_key,
        "assistant_message_idempotency_key":
            payload.assistant_message_idempotency_key,
        "cancel_requested": False,
        "started_at": _now(),
        "completed_at": None,
        "error": None,
    }
    _sessions[session_id] = session
    await _persist(session_id, session)

    background.add_task(_run_session, session_id)
    return CodeStartResponse(
        success=True,
        session_id=session_id,
        message="Code intelligence session started",
    )


async def _run_session(session_id: str) -> None:
    """Background driver — owns the engine for one session lifetime."""
    session = _sessions.get(session_id)
    if session is None:
        return

    # v3 — propagate the resolved model + advanced profile (temperature,
    # num_gpu, system_prompt, …) into the local-AI ContextVars so every
    # nested call_ollama() inside the engine picks them up.
    if session.get("preferred_model") or session.get("preferred_model_profile"):
        try:
            from .local_ai_routes_simple import (
                set_active_model,
                set_active_profile,
            )
            if session.get("preferred_model"):
                set_active_model(session["preferred_model"])
            if session.get("preferred_model_profile"):
                set_active_profile(session["preferred_model_profile"])
        except Exception as _exc:  # noqa: BLE001
            logger.debug("code_preferred_model_contextvar_set_failed: %s", _exc)

    registry = get_model_registry()
    sandbox = get_sandbox() if session["enable_execution"] else None
    harness = get_static_harness() if session["enable_static_analysis"] else None

    # ── on_event: persist + fan out ──────────────────────────────────────

    async def on_event(event: Dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "phase_start":
            phase_name = event.get("phase")
            session["current_phase"] = phase_name
            session["current_task"] = event.get("label") or phase_name
            for p in session["phases"]:
                if p["name"] == phase_name:
                    p["status"] = "in_progress"
                    p["started_at"] = _now()
        elif etype == "phase_complete":
            phase_name = event.get("phase")
            detail = event.get("detail", {}) or {}
            for p in session["phases"]:
                if p["name"] == phase_name:
                    p["status"] = "completed"
                    p["completed_at"] = _now()
                    p["detail"] = detail
            _merge_phase_result(session, phase_name, detail)
            session["progress"] = max(
                session["progress"],
                CodeIntelligenceEngine.PHASE_PROGRESS.get(
                    phase_name, session["progress"]
                ),
            )
        elif etype == "phase_failed":
            phase_name = event.get("phase")
            for p in session["phases"]:
                if p["name"] == phase_name:
                    p["status"] = "failed"
                    p["completed_at"] = _now()
                    p["detail"] = {"error": event.get("error")}
        elif etype == "code_ready":
            session["code"] = event.get("code")
            session["language"] = (
                event.get("language") or session.get("language")
            )
        elif etype == "test_ready":
            session["tests"] = event.get("code")
        elif etype == "execution_result":
            r = event.get("result") or {}
            results = list(session.get("execution_results") or [])
            results.append(r)
            session["execution_results"] = results
        elif etype == "static_analysis_result":
            session["static_analysis"] = event.get("result")
        elif etype == "review_ready":
            session["review"] = event.get("review")
        elif etype == "deliverable_ready":
            session["deliverable_markdown"] = event.get("markdown")
        elif etype == "debug_iteration_start":
            session["debug_iterations"] = int(event.get("iteration") or 0)
        await _persist(session_id, session)
        await _publish(session_id, event)

    # ── prepare_models hook — auto-pull with progress events ─────────────

    async def prepare_models() -> Dict[str, str]:
        models_used: Dict[str, str] = {}
        await registry.probe()
        # If the user pinned a specific tag, use it for every role.
        if session.get("preferred_model"):
            tag = session["preferred_model"]
            if not registry._tag_installed(tag):  # noqa: SLF001
                if not settings.code_auto_pull_models:
                    raise RuntimeError(
                        f"Preferred model {tag!r} not installed and "
                        f"auto-pull disabled."
                    )
                await on_event({
                    "type": "model_download_start",
                    "model": tag,
                })
                ok = await registry.pull_model(
                    tag,
                    on_progress=_make_pull_progress(on_event, tag),
                )
                await on_event({
                    "type": "model_download_complete" if ok
                            else "model_download_failed",
                    "model": tag,
                })
                if not ok:
                    raise RuntimeError(
                        f"Failed to pull preferred model {tag!r}"
                    )
            for role in ("planner", "coder", "tester",
                         "debugger", "critic"):
                models_used[role] = tag
            return models_used

        # Otherwise auto-select per role and pull on first miss.
        for role in ("planner", "coder", "tester", "debugger", "critic"):
            spec = await registry.ensure_model(
                role=role,
                effort=session["effort"],
                on_download_start=(
                    lambda s: on_event({
                        "type": "model_download_start",
                        "model": s.ollama_tag,
                        "size_gb": s.vram_gb,
                        "display_name": s.display_name,
                    })
                    if settings.code_auto_pull_models else None
                ),
                on_progress=_make_pull_progress_for_spec(on_event),
                on_download_complete=(
                    lambda s: on_event({
                        "type": "model_download_complete",
                        "model": s.ollama_tag,
                    })
                ),
            )
            models_used[role] = spec.ollama_tag
        return models_used

    # ── engine wiring ────────────────────────────────────────────────────

    engine = CodeIntelligenceEngine(
        prompt=session["prompt"],
        code_context=session.get("code_context"),
        language=session.get("language"),
        effort=session["effort"],
        provider="local",
        llm_call=_llm_call_local,
        sandbox=sandbox,
        static_harness=harness,
        enable_execution=session["enable_execution"],
        enable_static_analysis=session["enable_static_analysis"],
        enable_testing=session["enable_testing"],
        max_debug_iterations=session["max_debug_iterations"],
        on_event=on_event,
        prepare_models=prepare_models,
    )

    session["status"] = "in_progress"
    session["progress"] = 5
    await _persist(session_id, session)

    # Effort-tiered hard ceiling (mirrors thinking_routes.py).
    EFFORT_TIMEOUT = {
        "basic": 600, "medium": 1800, "deep": 3600,
        "expert": 5400, "ultra": 7200,
    }
    timeout_seconds = EFFORT_TIMEOUT.get(session["effort"], 1800)

    try:
        result = await asyncio.wait_for(
            engine.run(), timeout=timeout_seconds,
        )
        # Mirror engine result onto session payload.
        session["models_used"] = result.get("models_used") or {}
        session["plan"] = result.get("plan")
        session["code"] = result.get("code")
        session["tests"] = result.get("tests")
        session["language"] = result.get("language") or session.get("language")
        session["execution_results"] = result.get("execution_results") or []
        session["static_analysis"] = result.get("static_analysis")
        session["review"] = result.get("review") or {}
        session["deliverable_markdown"] = (
            result.get("deliverable_markdown")
            or session.get("deliverable_markdown")
        )
        session["debug_iterations"] = result.get("debug_iterations") or 0
        session["title"] = result.get("title")
        session["task_type"] = result.get("task_type")
        session["status"] = "completed"
        session["progress"] = 100
        session["completed_at"] = _now()
        await _persist(session_id, session)
        await _publish(session_id, {"type": "done", "session_id": session_id})

        # ── server-side persistence (Phase C carry-over) ─────────────
        try:
            from ._query_persistence import (
                persist_user_message,
                persist_assistant_message,
                mark_query_completed,
            )
            await persist_user_message(
                chat_session_id=session.get("chat_session_id"),
                user_id=session.get("user_id"),
                client_id=None,
                prompt=session.get("prompt") or "",
                idempotency_key=session.get(
                    "user_message_idempotency_key"
                ),
            )
            extras_code = {
                "prompt": session.get("prompt") or "",
                "language": session.get("language") or "python",
                "title": session.get("title") or "",
                "task_type": session.get("task_type") or "generation",
                "effort": session.get("effort", "medium"),
                "models_used": session.get("models_used") or {},
                "plan": session.get("plan"),
                "code": session.get("code"),
                "tests": session.get("tests"),
                "execution_results":
                    session.get("execution_results") or [],
                "static_analysis": session.get("static_analysis"),
                "review": session.get("review"),
                "debug_iterations":
                    int(session.get("debug_iterations") or 0),
                "phases": session.get("phases") or [],
                "session_id": session_id,
                "state": "done",
            }
            await persist_assistant_message(
                chat_session_id=session.get("chat_session_id"),
                user_id=session.get("user_id"),
                client_id=None,
                content=session.get("deliverable_markdown") or "",
                ai_type="local-code",
                format="code",
                extras={"code": extras_code},
                idempotency_key=session.get(
                    "assistant_message_idempotency_key"
                ),
            )
            await mark_query_completed(
                query_record_id=session.get("query_record_id"),
                result_markdown=session.get("deliverable_markdown") or "",
            )
        except Exception as exc:
            logger.warning(
                "code_persist_failed session=%s error=%s",
                session_id, exc,
            )
    except asyncio.TimeoutError:
        msg = (
            f"Code intelligence exceeded the {session['effort']} effort "
            f"time budget ({timeout_seconds // 60} min). Pipeline stopped."
        )
        session["status"] = "failed"
        session["error"] = msg
        session["completed_at"] = _now()
        await _persist(session_id, session)
        await _publish(session_id, {"type": "error", "message": msg})
        try:
            from ._query_persistence import mark_query_failed
            await mark_query_failed(
                query_record_id=session.get("query_record_id"),
                error=msg,
            )
        except Exception:
            pass
    except asyncio.CancelledError:
        session["status"] = "cancelled"
        session["error"] = "Cancelled by user."
        session["completed_at"] = _now()
        await _persist(session_id, session)
        await _publish(session_id, {
            "type": "cancelled",
            "session_id": session_id,
        })
        try:
            from ._query_persistence import mark_query_cancelled
            await mark_query_cancelled(
                query_record_id=session.get("query_record_id"),
                reason="Cancelled by user.",
            )
        except Exception:
            pass
        raise
    except Exception as exc:
        logger.exception("code.run_session failed session=%s", session_id)
        session["status"] = "failed"
        session["error"] = str(exc)
        session["completed_at"] = _now()
        await _persist(session_id, session)
        await _publish(session_id, {
            "type": "error", "message": str(exc),
        })
        try:
            from ._query_persistence import mark_query_failed
            await mark_query_failed(
                query_record_id=session.get("query_record_id"),
                error=str(exc),
            )
        except Exception:
            pass


# ─── pull progress relays ────────────────────────────────────────────────────


def _make_pull_progress(
    on_event: Callable[[Dict[str, Any]], Awaitable[None]],
    tag: str,
) -> Callable[[int, int, str], Awaitable[None]]:
    """Closure that adapts (done, total, status) → SSE event."""
    async def relay(done: int, total: int, status: str) -> None:
        pct = int(done / total * 100) if total else 0
        await on_event({
            "type": "model_download_progress",
            "model": tag,
            "bytes_done": done,
            "bytes_total": total,
            "pct": pct,
            "status": status,
        })
    return relay


def _make_pull_progress_for_spec(
    on_event: Callable[[Dict[str, Any]], Awaitable[None]],
) -> Callable[[int, int, str], Awaitable[None]]:
    """Same as above but emits without a fixed tag — set inside the closure."""
    async def relay(done: int, total: int, status: str) -> None:
        pct = int(done / total * 100) if total else 0
        await on_event({
            "type": "model_download_progress",
            "bytes_done": done,
            "bytes_total": total,
            "pct": pct,
            "status": status,
        })
    return relay


# ─── phase result merging ────────────────────────────────────────────────────


def _merge_phase_result(
    session: Dict[str, Any],
    phase: str,
    detail: Dict[str, Any],
) -> None:
    if phase == "triage":
        session["triage"] = detail
        if detail.get("language"):
            session["language"] = (
                session.get("language") or detail["language"]
            )
    elif phase == "model_prep":
        if isinstance(detail.get("models_used"), dict):
            session["models_used"] = detail["models_used"]
    elif phase == "plan":
        session["plan"] = {
            k: v for k, v in detail.items() if k != "error"
        } or None
    elif phase == "implement":
        session["language"] = (
            detail.get("language") or session.get("language")
        )
    elif phase == "execute":
        # Already captured via `execution_result` event; merge no-op here.
        pass
    elif phase == "analyze":
        session["static_analysis"] = detail
    elif phase == "test":
        # Tester output captured via `test_ready`; metadata only here.
        pass
    elif phase == "debug":
        session["debug_iterations"] = int(detail.get("iterations") or 0)
    elif phase == "review":
        session["review"] = detail


# ─── /cancel ─────────────────────────────────────────────────────────────────


@router.post("/{session_id}/cancel")
async def cancel_code_session(
    session_id: str,
    user: User = Depends(get_current_user),
):
    session = await _load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_owner(session, user)

    if session.get("status") in {"completed", "failed", "cancelled"}:
        return {"cancelled": False, "reason": "already terminal"}

    session["status"] = "cancelled"
    session["cancel_requested"] = True
    session["error"] = "Cancelled by user."
    session["completed_at"] = _now()
    await _persist(session_id, session)
    await _publish(session_id, {
        "type": "cancelled", "session_id": session_id,
    })

    try:
        from ._query_persistence import mark_query_cancelled
        await mark_query_cancelled(
            query_record_id=session.get("query_record_id"),
            reason="Cancelled by user.",
        )
    except Exception:
        pass

    return {"cancelled": True, "session_id": session_id}


# ─── /events (SSE) ───────────────────────────────────────────────────────────


@router.get("/{session_id}/events")
async def stream_events(
    session_id: str,
    request: Request,
    user: User = Depends(get_current_user),
):
    snapshot = await _load(session_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_owner(snapshot, user)

    async def event_stream():
        from collections import deque
        queue = _event_queue(session_id)
        seen_ids: deque = deque(maxlen=200)
        channel = _CODE_EVENT_CHANNEL.format(session_id=session_id)
        sub_iter = cache_manager.subscribe_events(channel).__aiter__()

        async def _redis_pump():
            try:
                async for event in sub_iter:
                    await queue.put(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        pump_task = asyncio.create_task(_redis_pump())

        try:
            snap = await _load(session_id)
            if snap:
                payload = json.dumps({
                    "type": "snapshot",
                    **_public_snapshot(snap),
                })
                yield f"data: {payload}\n\n"
                if snap.get("status") in {"completed", "failed", "cancelled"}:
                    return

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=15.0,
                    )
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                eid = event.get("event_id")
                if eid:
                    if eid in seen_ids:
                        continue
                    seen_ids.append(eid)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in {"done", "error", "cancelled"}:
                    break
        finally:
            pump_task.cancel()
            try:
                await pump_task
            except (asyncio.CancelledError, Exception):
                pass
            _event_queues.pop(session_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── /status + /{sid} ────────────────────────────────────────────────────────


def _public_snapshot(session: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": session["session_id"],
        "status": session["status"],
        "progress": session.get("progress", 0),
        "prompt": session.get("prompt"),
        "code_context": session.get("code_context"),
        "language": session.get("language"),
        "effort": session.get("effort"),
        "provider": session.get("provider", "local"),
        "current_phase": session.get("current_phase"),
        "current_task": session.get("current_task"),
        "phases": session.get("phases", []),
        "models_used": session.get("models_used", {}),
        "triage": session.get("triage"),
        "plan": session.get("plan"),
        "code": session.get("code"),
        "tests": session.get("tests"),
        "execution_results": session.get("execution_results", []),
        "static_analysis": session.get("static_analysis"),
        "review": session.get("review"),
        "deliverable_markdown": session.get("deliverable_markdown"),
        "debug_iterations": session.get("debug_iterations", 0),
        "title": session.get("title"),
        "task_type": session.get("task_type"),
        "started_at": session.get("started_at"),
        "completed_at": session.get("completed_at"),
        "error": session.get("error"),
    }


@router.get("/{session_id}/status")
async def get_status(
    session_id: str,
    user: User = Depends(get_current_user),
):
    session = await _load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_owner(session, user)
    return _public_snapshot(session)


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    user: User = Depends(get_current_user),
):
    return await get_status(session_id, user)


# ─── /models ─────────────────────────────────────────────────────────────────


@router.get("/models")
async def list_models(user: User = Depends(get_current_user)):
    """List the curated catalogue with installed-status for each tag."""
    registry = get_model_registry()
    await registry.probe()
    return {
        "installed": registry.available,
        "catalogue": registry.catalogue_with_status(),
    }


@router.post("/models/{tag:path}/pull")
async def pull_model(
    tag: str,
    user: User = Depends(get_current_user),
):
    """Stream Ollama pull progress as SSE."""
    registry = get_model_registry()

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)

        async def on_progress(done: int, total: int, status: str):
            pct = int(done / total * 100) if total else 0
            try:
                queue.put_nowait({
                    "type": "pull_progress",
                    "tag": tag,
                    "status": status,
                    "bytes_done": done,
                    "bytes_total": total,
                    "pct": pct,
                })
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait({
                        "type": "pull_progress",
                        "tag": tag,
                        "pct": pct,
                    })
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

        pull_task = asyncio.create_task(
            registry.pull_model(tag, on_progress=on_progress),
        )

        try:
            yield f"data: {json.dumps({'type': 'pull_start', 'tag': tag})}\n\n"
            while not pull_task.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
            success = await pull_task
            yield (
                f"data: {json.dumps({'type': 'pull_complete' if success else 'pull_error', 'tag': tag})}\n\n"
            )
        finally:
            if not pull_task.done():
                pull_task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── /sandbox/health ─────────────────────────────────────────────────────────


@router.get("/sandbox/health")
async def sandbox_health(user: User = Depends(get_current_user)):
    """Surface whether the Docker sandbox is reachable + which images are warm."""
    if not settings.code_sandbox_enabled:
        return {
            "enabled": False,
            "docker_available": False,
            "images": {},
            "memory_limit": settings.code_sandbox_memory,
            "timeout_seconds": settings.code_sandbox_timeout,
        }
    sandbox = get_sandbox()
    if sandbox is None:
        return {"enabled": False, "docker_available": False, "images": {}}
    available = await sandbox.docker_available()
    images = await sandbox.image_status() if available else {}
    return {
        "enabled": True,
        "docker_available": available,
        "images": images,
        "memory_limit": settings.code_sandbox_memory,
        "timeout_seconds": settings.code_sandbox_timeout,
    }


# ─── /capabilities (v2 — autonomous extension) ───────────────────────────────


@router.get("/capabilities")
async def list_capabilities(user: User = Depends(get_current_user)):
    """List capabilities the discoverer has registered."""
    discoverer = get_capability_discoverer()
    items = await discoverer.registry.list_all()
    return {
        "count": len(items),
        "items": items,
        "discoverer": {
            "cycle_count": discoverer.cycle_count,
            "last_cycle_iso": discoverer.last_cycle_iso,
            "interval_seconds":
                settings.code_capability_discovery_interval_seconds,
            "max_per_cycle":
                settings.code_capability_discovery_max_per_cycle,
            "enabled": settings.code_capability_discovery_enabled,
        },
    }


@router.post("/capabilities/discover")
async def trigger_discovery(
    user: User = Depends(get_current_user),
):
    """
    Run an on-demand discovery cycle. Returns the report directly so
    the user can see exactly what was harvested + accepted + rejected.
    Independent of the long-lived loop schedule.
    """
    if not settings.code_capability_discovery_enabled:
        raise HTTPException(
            status_code=503,
            detail="Capability discovery is disabled "
                   "(CODE_CAPABILITY_DISCOVERY_ENABLED=false).",
        )
    discoverer = get_capability_discoverer()
    report = await discoverer.run_once()
    return report
