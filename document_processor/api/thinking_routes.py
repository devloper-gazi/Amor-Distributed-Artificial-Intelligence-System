"""
Thinking Mode HTTP routes.

Endpoints
---------
POST  /api/thinking/analyze           — decide if clarifying questions are needed
POST  /api/thinking/think             — start a thinking session (with answers)
GET   /api/thinking/{sid}/events      — SSE stream of phase events
GET   /api/thinking/{sid}/status      — session snapshot (for polling)
GET   /api/thinking/{sid}             — alias for /status, returns 404 if missing

Design mirrors the research endpoints: per-user scoping, in-memory sessions
backed by Redis for cross-replica reads, SSE with a per-session asyncio.Queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..infrastructure.cache import cache_manager
from ..thinking import ThinkingEngine
from ..thinking.engine import _extract_json
from ..thinking.models import (
    AnalyzeRequest,
    AnalyzeResponse,
    ClarifyingQuestion,
    ThinkingSessionSnapshot,
    ThinkRequest,
    ThinkResponse,
)
from ..thinking.prompts import SYSTEM_PROMPT, analyze_prompt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/thinking", tags=["thinking"])


# ─── session storage ──────────────────────────────────────────────────────────

SESSION_CACHE_PREFIX = "thinking_session:"

# P1.2: Bounded in-memory session cache (was an unbounded dict).
# Redis remains the durable store; this dict is only a hot cache.
try:
    from cachetools import TTLCache
    _sessions: Dict[str, Dict[str, Any]] = TTLCache(maxsize=512, ttl=7800)
except ImportError:  # pragma: no cover
    _sessions = {}

# Phase D4: bound the per-session event queue dict with a TTL cache —
# a stalled client used to leak its asyncio.Queue forever (the cleanup
# `pop` only fires when the SSE generator exits, which requires a
# connected client). TTL matches the session payload's TTL so a queue
# can never outlive its session by more than the cache slack.
try:
    from cachetools import TTLCache as _TTLCache  # noqa: WPS433
    _event_queues: Dict[str, asyncio.Queue] = _TTLCache(maxsize=512, ttl=7800)
except ImportError:  # pragma: no cover
    _event_queues = {}

# Per-queue maxsize so a stalled subscriber can't OOM the producer
# (sliding-window drop in _publish below).
_EVENT_QUEUE_MAXSIZE = 500

# P1.1: Pub/sub channel for cross-replica event fan-out.
_THINKING_EVENT_CHANNEL = "amor:thinking:events:{session_id}"
try:
    SESSION_CACHE_TTL_SECONDS = int(os.getenv("THINKING_SESSION_TTL_SECONDS", "7200"))
except ValueError:
    SESSION_CACHE_TTL_SECONDS = 7200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_key(session_id: str) -> str:
    return f"{SESSION_CACHE_PREFIX}{session_id}"


async def _persist(session_id: str, session: Dict[str, Any]) -> None:
    try:
        await cache_manager.set_json(
            _cache_key(session_id), session, ttl=SESSION_CACHE_TTL_SECONDS
        )
    except Exception as exc:  # pragma: no cover — best-effort
        logger.debug("thinking: persist failed: %s", exc)


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
        logger.debug("thinking: load failed: %s", exc)
    return None


def _event_queue(session_id: str) -> asyncio.Queue:
    q = _event_queues.get(session_id)
    if q is None:
        q = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)  # Phase D4 bound
        _event_queues[session_id] = q
    return q


async def _publish(session_id: str, event: Dict[str, Any]) -> None:
    """
    P1.1: Fan an event out to:
      1. The local in-process queue (for SSE clients on this replica).
      2. A Redis pub/sub channel (so SSE clients on the OTHER replica
         can receive it too).

    Phase D4: bounded queue + sliding-window drop. If a stalled
    subscriber lets the queue fill up, the OLDEST event is dropped to
    make room for the newest — keeps the event stream relevant
    instead of letting the producer OOM.
    """
    from uuid import uuid4 as _uuid4  # avoid shadowing module-level uuid4
    if "event_id" not in event:
        event = {**event, "event_id": _uuid4().hex}
    queue = _event_queue(session_id)
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        # Drop oldest, push newest — the SSE client will see a small
        # gap rather than indefinitely lag behind the producer.
        try:
            queue.get_nowait()
            queue.put_nowait(event)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass
    try:
        await cache_manager.publish_event(
            _THINKING_EVENT_CHANNEL.format(session_id=session_id),
            event,
        )
    except Exception as exc:  # pragma: no cover — pub/sub is best-effort
        logger.debug("thinking _publish redis fanout failed: %s", exc)


async def sweep_stale_event_queues() -> int:
    """
    Phase D4: drop event queues whose backing session is already in a
    terminal state. Called periodically by the lifespan task in
    main.py. Defensive — a disconnected SSE client used to leak its
    queue indefinitely; this guarantees cleanup within the sweep
    cadence (5 min) regardless of client liveness.
    """
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
        logger.warning("thinking_sse_queues_swept", dropped=dropped)
    return dropped


def _require_owner(session: Dict[str, Any], user: User) -> None:
    owner = session.get("user_id")
    if owner and str(owner) != str(user.id):
        raise HTTPException(status_code=404, detail="Session not found")


# ─── LLM plumbing (local + Claude) ───────────────────────────────────────────

async def _llm_call_local(prompt: str, system: Optional[str], max_tokens: int) -> str:
    # Lazy-import so this module boots even if local_ai isn't wired yet.
    from .local_ai_routes_simple import call_ollama

    return await call_ollama(prompt, system, max_tokens=max_tokens)


async def _llm_call_claude(prompt: str, system: Optional[str], max_tokens: int) -> str:
    from .chat_research_routes import anthropic_client

    if anthropic_client is None:
        raise HTTPException(
            status_code=503,
            detail="Claude API not configured — set ANTHROPIC_API_KEY on the server.",
        )
    response = await anthropic_client.messages.create(
        model="claude-3.5-sonnet-latest",
        max_tokens=max(256, max_tokens),
        temperature=0.3,
        system=system or SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = []
    for block in response.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts).strip()


def _pick_llm(provider: str):
    if provider == "claude":
        return _llm_call_claude
    return _llm_call_local


# ─── /analyze ────────────────────────────────────────────────────────────────


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(
    payload: AnalyzeRequest,
    user: User = Depends(get_current_user),
) -> AnalyzeResponse:
    """
    Decide whether the prompt needs clarifying questions before we start
    thinking. Always uses local AI for speed — this is a fast "triage" call.
    """
    try:
        raw = await _llm_call_local(
            analyze_prompt(payload.prompt, payload.deliverable),
            SYSTEM_PROMPT,
            max_tokens=900,
        )
        data = _extract_json(raw)
    except Exception as exc:
        logger.exception("thinking.analyze failed")
        # Graceful fallback: treat as moderate, no clarification needed,
        # so the user can still proceed.
        return AnalyzeResponse(
            needs_clarification=False,
            complexity="moderate",
            rationale=f"Analyzer unavailable — proceeding directly. ({exc})"[:140],
            detected_deliverable=payload.deliverable
            if payload.deliverable != "auto"
            else "explanation",
            questions=[],
        )

    complexity = _enum(data.get("complexity"), {"trivial", "moderate", "complex", "expert"}, "moderate")
    needs = bool(data.get("needs_clarification", False))
    rationale = str(data.get("rationale", ""))[:200] or "Analysis complete."
    detected = _enum(
        data.get("detected_deliverable"),
        {"plan", "architecture", "code", "analysis", "decision", "explanation"},
        payload.deliverable if payload.deliverable != "auto" else "explanation",
    )

    raw_questions = data.get("questions") if needs else []
    questions = []
    if isinstance(raw_questions, list):
        for q in raw_questions[:5]:
            if not isinstance(q, dict) or not q.get("question"):
                continue
            qid = str(q.get("id") or _slug(q["question"]))
            questions.append(
                ClarifyingQuestion(
                    id=qid,
                    question=str(q["question"])[:400],
                    why_it_matters=str(q.get("why_it_matters", ""))[:200],
                    suggestions=[str(s)[:40] for s in (q.get("suggestions") or [])][:5],
                    input_type=_enum(
                        q.get("input_type"),
                        {"text", "choice", "number", "multiline"},
                        "text",
                    ),
                    placeholder=(str(q["placeholder"])[:100] if q.get("placeholder") else None),
                    required=bool(q.get("required", False)),
                )
            )

    # If the model said "yes, clarify" but gave zero usable questions,
    # downgrade gracefully.
    if needs and not questions:
        needs = False
        rationale = "No high-leverage clarifying questions produced — proceeding directly."

    return AnalyzeResponse(
        needs_clarification=needs,
        complexity=complexity,
        rationale=rationale,
        detected_deliverable=detected,
        questions=questions,
    )


# ─── /think ──────────────────────────────────────────────────────────────────


@router.post("/think", response_model=ThinkResponse)
async def start_think(
    payload: ThinkRequest,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
) -> ThinkResponse:
    """
    Kick off a thinking session. Returns immediately with a session_id;
    the actual pipeline runs in the background and streams events over
    /events.
    """
    session_id = str(uuid4())
    deliverable = payload.detected_deliverable
    if deliverable == "auto":
        deliverable = "explanation"

    session: Dict[str, Any] = {
        "session_id": session_id,
        "user_id": str(user.id),
        "status": "started",
        "progress": 0,
        "prompt": payload.prompt,
        "deliverable": deliverable,
        "effort": payload.effort,
        "provider": payload.provider,
        "clarifications": payload.clarifications or {},
        "phases": [
            {"name": n, "label": l, "status": "pending", "detail": {}}
            for n, l in [
                ("understand", "Understanding"),
                ("decompose", "Decomposing"),
                ("explore", "Exploring alternatives"),
                ("evaluate", "Evaluating & deciding"),
                ("synthesize", "Synthesizing"),
                ("critique", "Self-critique"),
            ]
        ],
        "current_phase": None,
        "current_task": "Warming up",
        "understanding": None,
        "sub_questions": [],
        "alternatives": [],
        "decision": None,
        "deliverable_markdown": None,
        "critique": None,
        "started_at": _now(),
        "completed_at": None,
        "error": None,
        # Phase C linkage — chat_session_id + query_record_id ride
        # along the session payload so they survive replica-hops via
        # Redis and are available to the persistence helpers when the
        # background pipeline finishes.
        "chat_session_id": payload.chat_session_id,
        "query_record_id": payload.query_record_id,
        "user_message_idempotency_key": payload.user_message_idempotency_key,
        "assistant_message_idempotency_key": payload.assistant_message_idempotency_key,
        # Phase C2 cancellation flag — checked by _run_session between
        # phases so a /cancel call can stop the pipeline without having
        # to find the asyncio.Task object directly.
        "cancel_requested": False,
    }
    _sessions[session_id] = session
    await _persist(session_id, session)

    # Stamp the query record with our backend session id so the
    # frontend can reverse-lookup the SSE stream from a record id.
    if payload.query_record_id:
        try:
            from .._query_persistence_helpers import _link_thinking_record
            await _link_thinking_record(payload.query_record_id, session_id)
        except Exception:
            # Local helper not yet shipped — fall through to direct call.
            try:
                from ..infrastructure.chat_store import chat_store
                await chat_store.update_query_record(
                    payload.query_record_id,
                    thinking_session_id=session_id,
                    status="running",
                    progress=5,
                )
            except Exception as exc:
                logger.debug("link_thinking_record_failed: %s", exc)

    background.add_task(_run_session, session_id)
    return ThinkResponse(success=True, session_id=session_id, message="Thinking started")


async def _run_session(session_id: str) -> None:
    session = _sessions.get(session_id)
    if session is None:
        return
    llm = _pick_llm(session["provider"])

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
                ThinkingEngine.PHASE_PROGRESS.get(phase_name, session["progress"]),
            )
        elif etype == "phase_failed":
            phase_name = event.get("phase")
            for p in session["phases"]:
                if p["name"] == phase_name:
                    p["status"] = "failed"
                    p["completed_at"] = _now()
                    p["detail"] = {"error": event.get("error")}
        elif etype == "deliverable_ready":
            session["deliverable_markdown"] = event.get("markdown")
        await _persist(session_id, session)
        await _publish(session_id, event)

    engine = ThinkingEngine(
        prompt=session["prompt"],
        clarifications=session["clarifications"],
        deliverable=session["deliverable"],
        effort=session["effort"],
        provider=session["provider"],
        llm_call=llm,
        on_event=on_event,
    )

    session["status"] = "in_progress"
    session["progress"] = 5
    await _persist(session_id, session)

    # P1.3: Hard ceilings per effort tier so a hung Ollama can't wedge the
    # background task forever. Numbers map to roughly 4–6× the nominal time
    # budget so a healthy run never trips the limit.
    EFFORT_TIMEOUT = {
        "basic": 600,     # 10 min ceiling
        "medium": 1800,   # 30 min ceiling
        "deep": 3600,     # 1 h ceiling
        "expert": 5400,   # 1.5 h ceiling
        "ultra": 7200,    # 2 h ceiling
        # legacy aliases
        "quick": 600,
        "standard": 1800,
    }
    effort = session.get("effort") or "medium"
    timeout_seconds = EFFORT_TIMEOUT.get(effort, EFFORT_TIMEOUT["medium"])

    try:
        result = await asyncio.wait_for(engine.run(), timeout=timeout_seconds)
        # Mirror the engine result into the session (belt and braces).
        session["understanding"] = result.get("understanding")
        session["sub_questions"] = result.get("sub_questions", [])
        session["alternatives"] = result.get("alternatives", [])
        session["decision"] = result.get("decision")
        session["deliverable_markdown"] = result.get("deliverable_markdown") or session.get(
            "deliverable_markdown"
        )
        session["critique"] = result.get("critique")
        session["status"] = "completed"
        session["progress"] = 100
        session["completed_at"] = _now()
        await _persist(session_id, session)
        await _publish(session_id, {"type": "done", "session_id": session_id})

        # ── Phase C — server-side persistence + query_record update ──
        # Frontend also writes these messages with the same idempotency
        # keys; the unique sparse index dedupes the parallel writes.
        try:
            from ._query_persistence import (
                persist_user_message,
                persist_assistant_message,
                mark_query_completed,
            )
            await persist_user_message(
                chat_session_id=session.get("chat_session_id"),
                user_id=session.get("user_id"),
                client_id=None,  # thinking sessions are user-scoped, not client-scoped
                prompt=session.get("prompt") or "",
                idempotency_key=session.get("user_message_idempotency_key"),
            )
            # Persist the FULL snapshot (matching ThinkingView.fromSnapshot
            # shape) so reloading the chat re-mounts the rich timeline + the
            # deliverable card instead of showing raw markdown literals.
            # See the matching note in local_ai_routes_simple.py — this
            # backend write and the frontend write share an idempotency key,
            # so whichever lands first wins; both must be a complete
            # snapshot for the chat history to look right either way.
            provider_kind = session.get("provider") or "local"
            session_payload = {
                "session_id": session.get("session_id"),
                "phases": session.get("phases", []),
                "sub_questions": session.get("sub_questions", []),
                "alternatives": session.get("alternatives", []),
                "decision": session.get("decision"),
                "critique": session.get("critique"),
                "understanding": session.get("understanding"),
                "deliverable_markdown": session.get("deliverable_markdown") or "",
                "detected_deliverable": session.get("detected_deliverable", "auto"),
                "status": session.get("status", "completed"),
            }
            await persist_assistant_message(
                chat_session_id=session.get("chat_session_id"),
                user_id=session.get("user_id"),
                client_id=None,
                content=session.get("deliverable_markdown") or "",
                ai_type="local-thinking" if provider_kind == "local" else "claude-thinking",
                format="thinking",
                extras={"thinking": {
                    "prompt": session.get("prompt") or "",
                    "effort": session.get("effort", "medium"),
                    "provider": provider_kind,
                    "analysis": session.get("analysis"),
                    "session": session_payload,
                    "state": "done",
                }},
                idempotency_key=session.get("assistant_message_idempotency_key"),
            )
            await mark_query_completed(
                query_record_id=session.get("query_record_id"),
                result_markdown=session.get("deliverable_markdown") or "",
            )
        except Exception as exc:
            logger.warning("thinking_persist_failed session=%s error=%s", session_id, exc)
    except asyncio.TimeoutError:
        logger.error(
            "thinking.run_session exceeded effort timeout session=%s (effort=%s, limit=%ds)",
            session_id, effort, timeout_seconds,
        )
        msg = (
            f"Thinking exceeded the {effort} effort time budget "
            f"({timeout_seconds // 60} min). The pipeline was stopped."
        )
        session["status"] = "failed"
        session["error"] = msg
        session["completed_at"] = _now()
        await _persist(session_id, session)
        await _publish(session_id, {"type": "error", "message": msg})
        try:
            from ._query_persistence import mark_query_failed
            await mark_query_failed(
                query_record_id=session.get("query_record_id"), error=msg
            )
        except Exception:
            pass
    except asyncio.CancelledError:
        # Phase C2 — pipeline was cancelled via /cancel.
        session["status"] = "cancelled"
        session["error"] = "Cancelled by user."
        session["completed_at"] = _now()
        await _persist(session_id, session)
        await _publish(session_id, {"type": "cancelled", "session_id": session_id})
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
        logger.exception("thinking.run_session failed session=%s", session_id)
        session["status"] = "failed"
        session["error"] = str(exc)
        session["completed_at"] = _now()
        await _persist(session_id, session)
        await _publish(session_id, {"type": "error", "message": str(exc)})
        try:
            from ._query_persistence import mark_query_failed
            await mark_query_failed(
                query_record_id=session.get("query_record_id"), error=str(exc)
            )
        except Exception:
            pass


# ─── Phase C2 — cancellation ─────────────────────────────────────────


@router.post("/{session_id}/cancel")
async def cancel_thinking(
    session_id: str,
    user: User = Depends(get_current_user),
):
    """
    Cancel an in-flight thinking session.

    Marks the session ``status="cancelled"`` in Redis (so the SSE
    snapshot reflects it on next read), publishes a ``cancelled``
    event for any active SSE subscribers, and updates the matching
    query_record. The actual asyncio.Task is signalled via the
    ``cancel_requested`` flag in the session payload — the next
    ``await self._emit(...)`` in the engine raises an asyncio.
    CancelledError, which our `_run_session` exception path catches.
    """
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
    await _publish(session_id, {"type": "cancelled", "session_id": session_id})

    try:
        from ._query_persistence import mark_query_cancelled
        await mark_query_cancelled(
            query_record_id=session.get("query_record_id"),
            reason="Cancelled by user.",
        )
    except Exception:
        pass

    return {"cancelled": True, "session_id": session_id}


def _merge_phase_result(session: Dict[str, Any], phase: str, detail: Dict[str, Any]) -> None:
    if phase == "understand":
        session["understanding"] = detail
    elif phase == "decompose":
        session["sub_questions"] = detail.get("sub_questions", [])
    elif phase == "explore":
        session["alternatives"] = detail.get("alternatives", [])
    elif phase == "evaluate":
        session["decision"] = {k: v for k, v in detail.items() if k != "error"} or None
    elif phase == "synthesize":
        md = detail.get("markdown")
        if md:
            session["deliverable_markdown"] = md
    elif phase == "critique":
        session["critique"] = detail


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
        # P1.1: Race the local queue against a Redis pub/sub subscription
        # so cross-replica events still reach this SSE client.
        from collections import deque
        queue = _event_queue(session_id)
        seen_ids: deque = deque(maxlen=200)

        channel = _THINKING_EVENT_CHANNEL.format(session_id=session_id)
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
                payload = json.dumps({"type": "snapshot", **_public_snapshot(snap)})
                yield f"data: {payload}\n\n"
                if snap.get("status") in {"completed", "failed"}:
                    return

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                # Dedupe local + Redis paths.
                eid = event.get("event_id")
                if eid:
                    if eid in seen_ids:
                        continue
                    seen_ids.append(eid)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in {"done", "error"}:
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


# ─── /status + session fetch ─────────────────────────────────────────────────


def _public_snapshot(session: Dict[str, Any]) -> Dict[str, Any]:
    """Shape the session for client consumption (drop server-only fields)."""
    return {
        "session_id": session["session_id"],
        "status": session["status"],
        "progress": session.get("progress", 0),
        "prompt": session.get("prompt"),
        "deliverable": session.get("deliverable"),
        "effort": session.get("effort"),
        "provider": session.get("provider"),
        "current_phase": session.get("current_phase"),
        "current_task": session.get("current_task"),
        "phases": session.get("phases", []),
        "clarifications": session.get("clarifications", {}),
        "understanding": session.get("understanding"),
        "sub_questions": session.get("sub_questions", []),
        "alternatives": session.get("alternatives", []),
        "decision": session.get("decision"),
        "deliverable_markdown": session.get("deliverable_markdown"),
        "critique": session.get("critique"),
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


# ─── utilities ───────────────────────────────────────────────────────────────


def _enum(value: Any, allowed: set, default: str) -> str:
    v = str(value or "").lower()
    return v if v in allowed else default


def _slug(text: str) -> str:
    import re as _re

    text = _re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return text[:40] or "q"
