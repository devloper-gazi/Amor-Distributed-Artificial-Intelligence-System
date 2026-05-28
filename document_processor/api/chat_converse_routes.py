"""Cycle UI v2.9 — fast conversational ("chat") mode.

The 6 reasoning modes (build / research / thinking / consortium /
sentinel / quickcode) are all heavyweight multi-phase pipelines.  A
plain greeting ("merhaba") or a quick conversational turn does not
need any of them — routing it to Thinking spins up the 6-phase
reasoning engine (100-540s).  This endpoint is the fast lane: a single
persona-driven LLM call that streams a short reply in ~1-5s.

The auto-classifier's rule heuristic routes greetings / chitchat /
identity questions to the ``chat`` mode, whose ``MODE_ADAPTERS`` entry
in the frontend points here.

Endpoints (mirrors the thinking/research SSE trio so the existing
``openEventStream`` + UNIFIED_REDUCER pipeline works unchanged):

    POST  /api/chat/converse                 -> {session_id, mode}
    GET   /api/chat/converse/{sid}/events     -> SSE stream
    POST  /api/chat/converse/{sid}/cancel     -> stop a run

Wire format reuses the cross-cutting events the reducer already
handles: ``text_chunk`` (appended) then ``done`` (terminal).  No new
event types are needed (see lib/chat/event_registry.ts:isTextChunk).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth.dependencies import get_current_user
from ..infrastructure.cache import cache_manager


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat-converse"])


# ─── Amor persona ──────────────────────────────────────────────────
# Tunable in one place.  Turkish-first because the operator is Turkish,
# but the model is told to mirror the user's language.

AMOR_PERSONA_SYSTEM = (
    "Sen Amor'sun — yerel çalışan, gizliliğe önem veren kişisel bir yapay "
    "zekâ asistanısın. Kullanıcının kendi cihazında, hiçbir veriyi buluta "
    "göndermeden çalışırsın. Üslubun sıcak, samimi, kısa ve net; gereksiz "
    "uzatmazsın.\n"
    "- Kullanıcının yazdığı dilde yanıt ver (Türkçe yazdıysa Türkçe, "
    "İngilizce yazdıysa İngilizce).\n"
    "- Selamlaşmalara kısa ve kişilikli karşılık ver. Örneğin "
    "\"Merhaba! Ben Amor, kişisel asistanın. Bugün sana nasıl yardımcı "
    "olabilirim?\"\n"
    "- Kimliğin sorulursa kendini Amor olarak tanıt: yerel, gizlilik-odaklı, "
    "çok-modlu bir asistan.\n"
    "- Konu kod yazmak, derin araştırma ya da çok adımlı analiz gerektiriyorsa "
    "bunu kısaca yapabileceğini söyle; ama basit sohbeti uzun pipeline'a "
    "sokmadan, doğrudan ve hızlı yanıtla."
)

# A single short conversational turn — keep token budget small so the
# reply lands fast (greetings are tiny; this caps the worst case).
_MAX_TOKENS = 512


# ─── Request / response models ─────────────────────────────────────


class ConverseRequest(BaseModel):
    """Body for ``POST /api/chat/converse``.  ``prompt`` is the user's
    message; ``history`` is optional prior turns for light context."""

    prompt: str = Field(..., min_length=1, max_length=8000)
    history: list[dict] = Field(default_factory=list)


class ConverseResponse(BaseModel):
    session_id: str
    mode: str = "chat"


# ─── Session + event-queue store (mirrors thinking_routes) ─────────

_SESSION_PREFIX = "chat_converse_session:"
_SESSION_TTL_S = 7200
_EVENT_QUEUE_MAXSIZE = 200
_EVENT_CHANNEL = "amor:chat_converse:events:{session_id}"

try:  # bounded hot cache; Redis is the durable store
    from cachetools import TTLCache

    _sessions: Dict[str, Dict[str, Any]] = TTLCache(maxsize=512, ttl=7800)
    _event_queues: Dict[str, asyncio.Queue] = TTLCache(maxsize=512, ttl=7800)
except ImportError:  # pragma: no cover
    _sessions = {}
    _event_queues = {}

# Hold references to background tasks so they aren't GC'd mid-run.
_tasks: Dict[str, asyncio.Task] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_key(session_id: str) -> str:
    return f"{_SESSION_PREFIX}{session_id}"


async def _persist(session_id: str, session: Dict[str, Any]) -> None:
    _sessions[session_id] = session
    try:
        await cache_manager.set_json(_cache_key(session_id), session, ttl=_SESSION_TTL_S)
    except Exception as exc:  # pragma: no cover — best-effort
        logger.debug("chat_converse persist failed: %s", exc)


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
        logger.debug("chat_converse load failed: %s", exc)
    return None


def _event_queue(session_id: str) -> asyncio.Queue:
    q = _event_queues.get(session_id)
    if q is None:
        q = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)
        _event_queues[session_id] = q
    return q


async def _publish(session_id: str, event: Dict[str, Any]) -> None:
    """Push an event to the local SSE queue (sliding-window drop on a
    stalled subscriber) + fan out over Redis for cross-replica clients.
    Auto-stamps an ``event_id`` for the frontend dedup logic."""
    if "event_id" not in event:
        event = {**event, "event_id": uuid4().hex}
    queue = _event_queue(session_id)
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
            queue.put_nowait(event)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass
    try:
        await cache_manager.publish_event(
            _EVENT_CHANNEL.format(session_id=session_id), event
        )
    except Exception as exc:  # pragma: no cover — pub/sub best-effort
        logger.debug("chat_converse redis fanout failed: %s", exc)


def _require_owner(session: Dict[str, Any], user: Any) -> None:
    owner = session.get("user_id")
    if owner and str(owner) != str(getattr(user, "id", "")):
        raise HTTPException(status_code=404, detail="Session not found")


# ─── Background run ────────────────────────────────────────────────


async def _run_session(session_id: str) -> None:
    """Single persona-driven LLM call → stream the reply + terminate."""
    session = await _load(session_id)
    if session is None:
        return
    if session.get("status") == "cancelled":
        return

    session["status"] = "running"
    await _persist(session_id, session)

    prompt = session.get("prompt") or ""
    try:
        # Lazy import keeps this module importable even if local_ai
        # isn't wired in a given deployment.
        from .local_ai_routes_simple import call_ollama  # noqa: PLC0415

        reply = await call_ollama(prompt, AMOR_PERSONA_SYSTEM, max_tokens=_MAX_TOKENS)
        reply = (reply or "").strip()

        # Re-check cancellation that may have landed during the call.
        latest = await _load(session_id)
        if latest and latest.get("status") == "cancelled":
            return

        if not reply:
            reply = (
                "Merhaba! Ben Amor, kişisel asistanın. Şu an yanıt üretemedim — "
                "tekrar dener misin?"
            )
        await _publish(session_id, {"type": "text_chunk", "content": reply})
        session = await _load(session_id) or session
        session["status"] = "completed"
        session["reply"] = reply
        session["completed_at"] = _now()
        await _persist(session_id, session)
        await _publish(session_id, {"type": "done", "session_id": session_id})
    except Exception as exc:  # pragma: no cover — surface to client
        logger.warning("chat_converse run failed session=%s err=%s", session_id, exc)
        session = await _load(session_id) or session
        session["status"] = "failed"
        session["error"] = str(exc)[:500]
        await _persist(session_id, session)
        await _publish(
            session_id,
            {"type": "error", "message": "Sohbet yanıtı üretilemedi.", "recoverable": True},
        )
    finally:
        _tasks.pop(session_id, None)


# ─── Routes ────────────────────────────────────────────────────────


@router.post("/converse", response_model=ConverseResponse)
async def start_converse(
    payload: ConverseRequest,
    user: Any = Depends(get_current_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> ConverseResponse:
    """Start a fast chat turn.  Returns immediately; the reply streams
    over ``/converse/{sid}/events``."""
    session_id = str(uuid4())
    session: Dict[str, Any] = {
        "session_id": session_id,
        "user_id": str(getattr(user, "id", "")),
        "client_id": (x_client_id or "").strip() or session_id,
        "status": "started",
        "mode": "chat",
        "prompt": payload.prompt,
        "started_at": _now(),
    }
    await _persist(session_id, session)
    _tasks[session_id] = asyncio.create_task(_run_session(session_id))
    return ConverseResponse(session_id=session_id, mode="chat")


@router.post("/converse/{session_id}/cancel")
async def cancel_converse(
    session_id: str,
    user: Any = Depends(get_current_user),
) -> Dict[str, Any]:
    session = await _load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_owner(session, user)
    session["status"] = "cancelled"
    await _persist(session_id, session)
    task = _tasks.get(session_id)
    if task is not None and not task.done():
        task.cancel()
    await _publish(session_id, {"type": "cancelled", "session_id": session_id})
    return {"session_id": session_id, "status": "cancelled"}


@router.get("/converse/{session_id}/events")
async def stream_converse_events(
    session_id: str,
    request: Request,
    user: Any = Depends(get_current_user),
):
    snapshot = await _load(session_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_owner(snapshot, user)

    async def event_stream():
        from collections import deque

        queue = _event_queue(session_id)
        seen_ids: deque = deque(maxlen=200)

        channel = _EVENT_CHANNEL.format(session_id=session_id)
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
                payload = json.dumps(
                    {
                        "type": "snapshot",
                        "session_id": session_id,
                        "status": snap.get("status"),
                        "mode": "chat",
                        "prompt": snap.get("prompt"),
                        "reply": snap.get("reply"),
                    }
                )
                yield f"data: {payload}\n\n"
                # If the run already finished before the client connected,
                # replay the reply + terminate so we don't hang.
                if snap.get("status") == "completed" and snap.get("reply"):
                    yield (
                        "data: "
                        + json.dumps({"type": "text_chunk", "content": snap["reply"]})
                        + "\n\n"
                    )
                    yield "data: " + json.dumps({"type": "done", "session_id": session_id}) + "\n\n"
                    return
                if snap.get("status") in {"failed", "cancelled"}:
                    return

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
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
