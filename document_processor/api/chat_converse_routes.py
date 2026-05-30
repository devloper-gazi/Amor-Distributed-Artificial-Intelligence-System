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

Design note (v2.9 fix): a chat turn is a SINGLE-SHOT reply, so the LLM
call runs INLINE inside the ``/events`` stream generator rather than in
a fire-and-forget background task fanned out over a per-session queue +
Redis pub/sub.  The queue/pub-sub pattern (cloned from thinking_routes)
raced a fast reply: pub/sub does not buffer, so if the producer emitted
before the ``/events`` subscriber attached (or the subscriber landed on
a different worker), the events were lost and the client hung at
"(starting…)".  Doing the work in the stream means the producer IS the
streamer — no queue, no fanout, no race.  Wire format reuses the
cross-cutting events the reducer already handles: ``text_chunk``
(appended) then ``done`` (terminal); see
lib/chat/event_registry.ts:isTextChunk.
"""

from __future__ import annotations

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


# ─── Session store (Redis-backed, mirrors thinking_routes) ─────────

_SESSION_PREFIX = "chat_converse_session:"
_SESSION_TTL_S = 7200

try:  # bounded hot cache; Redis is the durable cross-worker store
    from cachetools import TTLCache

    _sessions: Dict[str, Dict[str, Any]] = TTLCache(maxsize=512, ttl=7800)
except ImportError:  # pragma: no cover
    _sessions = {}


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


def _require_owner(session: Dict[str, Any], user: Any) -> None:
    owner = session.get("user_id")
    if owner and str(owner) != str(getattr(user, "id", "")):
        raise HTTPException(status_code=404, detail="Session not found")


def _sse(event: Dict[str, Any]) -> str:
    """Frame an event as an SSE ``data:`` line, stamping an event_id for
    the frontend's dedup (sse.ts)."""
    if "event_id" not in event:
        event = {**event, "event_id": uuid4().hex}
    return f"data: {json.dumps(event)}\n\n"


# ─── Routes ────────────────────────────────────────────────────────


@router.post("/converse", response_model=ConverseResponse)
async def start_converse(
    payload: ConverseRequest,
    user: Any = Depends(get_current_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> ConverseResponse:
    """Create a chat turn.  Returns immediately; the reply is generated
    + streamed by ``/converse/{sid}/events`` (single-shot, inline)."""
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
        # 1. Opening snapshot (lets the UI show the turn shell).
        snap = await _load(session_id) or snapshot
        yield _sse(
            {
                "type": "snapshot",
                "session_id": session_id,
                "status": snap.get("status"),
                "mode": "chat",
                "prompt": snap.get("prompt"),
                "reply": snap.get("reply"),
            }
        )

        status = snap.get("status")
        # 2a. Reconnect to an already-finished turn → replay + terminate.
        if status == "completed" and snap.get("reply"):
            yield _sse({"type": "text_chunk", "content": snap["reply"]})
            yield _sse({"type": "done", "session_id": session_id})
            return
        if status == "cancelled":
            yield _sse({"type": "cancelled", "session_id": session_id})
            return
        if status == "failed":
            yield _sse(
                {"type": "error", "message": "Sohbet yanıtı üretilemedi.", "recoverable": True}
            )
            return

        # 2b. Live turn — do the single persona-driven LLM call INLINE and
        #     stream the reply.  No background task / queue / pub-sub, so
        #     there is no producer/subscriber race for a fast reply.
        session = dict(snap)
        try:
            from .local_ai_routes_simple import call_ollama  # noqa: PLC0415

            reply = await call_ollama(
                session.get("prompt") or "", AMOR_PERSONA_SYSTEM, max_tokens=_MAX_TOKENS
            )
            reply = (reply or "").strip()

            # Cancellation may have landed during the call.
            latest = await _load(session_id)
            if latest and latest.get("status") == "cancelled":
                yield _sse({"type": "cancelled", "session_id": session_id})
                return

            if not reply:
                reply = (
                    "Merhaba! Ben Amor, kişisel asistanın. Şu an yanıt üretemedim — "
                    "tekrar dener misin?"
                )
            session["status"] = "completed"
            session["reply"] = reply
            session["completed_at"] = _now()
            await _persist(session_id, session)

            yield _sse({"type": "text_chunk", "content": reply})
            yield _sse({"type": "done", "session_id": session_id})
        except Exception as exc:  # noqa: BLE001 — surface to client
            # NB: asyncio.CancelledError (client disconnect) is a
            # BaseException in 3.11+, so it is NOT caught here — it
            # propagates and the stream simply ends, leaving the session
            # "started" so a reconnect re-runs the turn.
            logger.warning("chat_converse stream failed session=%s err=%s", session_id, exc)
            session["status"] = "failed"
            session["error"] = str(exc)[:500]
            await _persist(session_id, session)
            yield _sse(
                {"type": "error", "message": "Sohbet yanıtı üretilemedi.", "recoverable": True}
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
