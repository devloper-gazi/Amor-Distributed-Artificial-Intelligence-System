"""
Cycle C Sprint 8 Day 4 — agentic loop API.

Wraps :class:`ReActAgent` in a FastAPI surface modelled on the Code
Intelligence routes:

* ``POST /api/agent/start``               start a new agent session
* ``GET  /api/agent/sessions/{sid}/events`` SSE stream of events
* ``GET  /api/agent/sessions/{sid}``       snapshot
* ``POST /api/agent/sessions/{sid}/cancel``cancel a running session

The route keeps an in-process ``Conversation`` per session id and a
shared ``asyncio.Queue`` per session for SSE fan-out.  Cross-replica
fan-out (Phase 17 PR #3 Redis Streams) is *out of scope* for Sprint
8 — when the sticky cookie pins the client to one replica, single-
process delivery is sufficient.

The LLM caller defaults to a small adapter that calls AMOR's existing
``LLMBackend`` via ``make_backend()``.  When the backend is missing
(test env) the route returns 503 cleanly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..i18n import get_locale, localized_http_exception, t
from ..infrastructure.resumable_stream import (
    ResumableStream,
    SENTINEL_CLOSE,
    get_redis_client,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])


# ─── per-session state ───────────────────────────────────────────


@dataclass
class _Session:
    sid: str
    conv: Any                        # local_ai.agentic.Conversation
    stream: ResumableStream           # Sprint 9 — Redis-backed event log
    redis_client: Any = None          # held so the bg task can close it
    task: Optional[asyncio.Task] = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    user_task: str = ""
    user_id: str = "local"


_SESSIONS: Dict[str, _Session] = {}
_SESSIONS_LOCK = asyncio.Lock()


def _get(sid: str, locale: str = "en") -> _Session:
    sess = _SESSIONS.get(sid)
    if sess is None:
        raise localized_http_exception(
            status_code=404,
            key="agent.session_not_found",
            locale=locale,
        )
    return sess


# ─── default LLM caller (real backend) ──────────────────────────


async def _default_llm_caller(prompt: str) -> str:
    """Adapter from the agent's ``LLMCaller`` shape to AMOR's
    ``LLMBackend.generate``.  Picked up at the route layer so tests
    can swap a stub via the module-level ``LLM_CALLER`` override."""
    try:
        from local_ai.llm_backend import make_backend  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        # Caller-side locale is unknown for this internal helper; the
        # ``en`` default still surfaces a sensible message because
        # this only fires when the backend itself is missing.
        raise localized_http_exception(
            status_code=503,
            key="agent.llm_unavailable",
            locale="en",
            params={"err": str(exc)},
        ) from exc
    backend = make_backend()
    return await backend.generate(prompt=prompt, max_tokens=1024)


# Module-level pointer so test harnesses can monkeypatch the LLM
# without touching the per-route construction code.
LLM_CALLER = _default_llm_caller


# ─── tool catalogue ─────────────────────────────────────────────


def _tool_catalogue() -> List[Dict[str, Any]]:
    """Snapshot the registry into the OpenAI ``functions`` shape the
    prompt template understands."""
    try:
        from local_ai.tools import DEFAULT_REGISTRY  # noqa: PLC0415
        return list(DEFAULT_REGISTRY.to_openai_format() or [])
    except Exception as exc:  # pragma: no cover
        logger.warning("tool registry unavailable: %s", exc)
        return []


# ─── start ───────────────────────────────────────────────────────


class StartIn(BaseModel):
    task: str = Field(..., min_length=1, max_length=4096)
    max_iterations: int = Field(10, ge=1, le=30)
    stuck_window: int = Field(3, ge=2, le=10)


class StartOut(BaseModel):
    session_id: str
    started_at: str


@router.post("/start", response_model=StartOut, status_code=status.HTTP_201_CREATED)
async def start_agent(
    body: StartIn,
    user: User = Depends(get_current_user),
) -> StartOut:
    from local_ai.agentic import (  # noqa: PLC0415
        AgentConfig,
        Conversation,
        ReActAgent,
        default_tool_dispatcher,
    )

    sid = uuid.uuid4().hex
    conv = Conversation(session_id=sid)

    # Sprint 9 Day 2 — every event lands in a Redis-backed stream so
    # a client that disconnects mid-run can resume via Last-Event-ID.
    # ``redis_client`` is None when Redis is unreachable; the
    # ResumableStream falls back to an in-memory log so single-replica
    # delivery still works.
    redis_cm = get_redis_client()
    redis_client = await redis_cm.__aenter__()
    stream = ResumableStream(sid, redis_client=redis_client)

    async def fan_out(event) -> None:
        # ``event`` is a Pydantic Event — build a dict envelope that
        # carries both the canonical Sprint-4 stream payloads AND
        # the raw event for the snapshot endpoint.
        payload = event.model_dump(mode="json")
        envelope = {
            "type": "agent.event",
            "event": payload,
            "tool_stream": event.to_tool_stream(),
        }
        await stream.publish(envelope)

    catalogue = _tool_catalogue()
    agent = ReActAgent(
        conversation=conv,
        llm_caller=LLM_CALLER,
        tools_catalogue=catalogue,
        config=AgentConfig(
            max_iterations=body.max_iterations,
            stuck_window=body.stuck_window,
        ),
        tool_dispatcher=default_tool_dispatcher,
        on_event=fan_out,
    )

    sess = _Session(
        sid=sid,
        conv=conv,
        stream=stream,
        redis_client=redis_client,
        user_task=body.task,
        user_id=user.id,
    )
    async with _SESSIONS_LOCK:
        _SESSIONS[sid] = sess

    async def _runner():
        try:
            result = await agent.run(user_task=body.task)
            await stream.publish({
                "type": "agent.done",
                "reason": result.reason,
                "answer": result.answer,
                "iterations": result.iterations,
            })
        except asyncio.CancelledError:
            await stream.publish({"type": "agent.cancelled"})
            raise
        except Exception as exc:
            logger.exception("agent run crashed for sid=%s", sid)
            await stream.publish({
                "type": "agent.error",
                "message": f"{type(exc).__name__}: {exc}",
            })
        finally:
            # Sentinel — every active subscriber's tail() exits when
            # it sees this, regardless of replica.
            await stream.publish_close()
            try:
                await redis_cm.__aexit__(None, None, None)
            except Exception:  # pragma: no cover
                pass

    sess.task = asyncio.create_task(_runner(), name=f"agent-{sid}")
    return StartOut(session_id=sid, started_at=sess.started_at.isoformat())


# ─── snapshot ────────────────────────────────────────────────────


@router.get("/sessions/{sid}")
async def session_snapshot(
    sid: str,
    _user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    sess = _get(sid, locale=locale)
    snap = sess.conv.snapshot()
    return {
        "sid": sess.sid,
        "started_at": sess.started_at.isoformat(),
        "user_task": sess.user_task,
        "iteration": snap.iteration,
        "finished": snap.finished,
        "finish_reason": snap.finish_reason,
        "events": [ev.model_dump(mode="json") for ev in snap.events],
    }


# ─── cancel ──────────────────────────────────────────────────────


@router.post("/sessions/{sid}/cancel")
async def cancel_session(
    sid: str,
    _user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    sess = _get(sid, locale=locale)
    if sess.task and not sess.task.done():
        sess.task.cancel()
    return {"sid": sid, "cancelled": True}


# ─── SSE ─────────────────────────────────────────────────────────


@router.get("/sessions/{sid}/events")
async def session_events(
    sid: str,
    request: Request,
    _user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> StreamingResponse:
    # Sprint 9 Day 3 — cross-replica resume.  The in-process
    # ``_SESSIONS`` entry only lives on the replica that *started*
    # the run.  When sticky-cookie routing breaks (replica restart,
    # tab in another browser, DNS reshuffle), a reconnect can land
    # on a replica that doesn't know the sid in-memory but Redis
    # absolutely does.  Degrade gracefully: pull the stream straight
    # from Redis and serve replay + tail without an in-process
    # session.  The snapshot envelope just becomes empty in that
    # case — the wire-log replay is still complete.
    sess = _SESSIONS.get(sid)

    last_event_id: Optional[str] = (
        request.headers.get("last-event-id")
        or request.query_params.get("last_event_id")
        or None
    )

    async def stream_iter() -> AsyncIterator[bytes]:
        # ── stream resolution ──────────────────────────────────
        if sess is not None:
            stream = sess.stream
            redis_cm = None
        else:
            # Cross-replica path — open our own Redis client + stream.
            redis_cm = get_redis_client()
            redis_client = await redis_cm.__aenter__()
            stream = ResumableStream(sid, redis_client=redis_client)
            # Refuse if Redis is down AND we have no local state —
            # there's literally nothing to serve.
            try:
                length = await stream.length()
            except Exception:
                length = 0
            if redis_client is None and length == 0:
                yield _sse({
                    "type": "agent.error",
                    "message": t("stream.cross_replica_no_redis", locale=locale),
                })
                if redis_cm is not None:
                    try:
                        await redis_cm.__aexit__(None, None, None)
                    except Exception:  # pragma: no cover
                        pass
                return

        # ── snapshot ───────────────────────────────────────────
        if sess is not None:
            snap = sess.conv.snapshot()
            snapshot_msg = {
                "type": "agent.snapshot",
                "iteration": snap.iteration,
                "finished": snap.finished,
                "finish_reason": snap.finish_reason,
                "events": [ev.model_dump(mode="json") for ev in snap.events],
            }
        else:
            # No in-memory Conversation; reconstruct iteration count
            # from the stream's known events for a coarse snapshot.
            snapshot_msg = {
                "type": "agent.snapshot",
                "iteration": 0,
                "finished": False,
                "finish_reason": None,
                "events": [],
                "cross_replica": True,
            }
        yield _sse(snapshot_msg)

        try:
            # ── replay ────────────────────────────────────────
            try:
                history = await stream.replay(after_id=last_event_id)
            except Exception as exc:  # pragma: no cover — safety net
                logger.warning("stream replay failed: %s", exc)
                history = []
            saw_close_in_history = False
            for ev in history:
                if await request.is_disconnected():
                    return
                yield _sse_with_id(ev.id, ev.data)
                if isinstance(ev.data, dict) and ev.data.get("__sentinel__") == SENTINEL_CLOSE:
                    saw_close_in_history = True
                    break
            if saw_close_in_history:
                return

            # ── live tail ─────────────────────────────────────
            from_id = history[-1].id if history else (last_event_id or "$")
            try:
                async for ev in stream.tail(
                    from_id=from_id,
                    block_ms=15_000,
                    idle_keep_alive=True,
                ):
                    if await request.is_disconnected():
                        break
                    if ev is None:
                        yield b": keep-alive\n\n"
                        continue
                    if isinstance(ev.data, dict) and ev.data.get("__sentinel__") == SENTINEL_CLOSE:
                        break
                    yield _sse_with_id(ev.id, ev.data)
            except Exception as exc:  # pragma: no cover
                logger.warning("stream tail crashed: %s", exc)
        finally:
            if redis_cm is not None:
                try:
                    await redis_cm.__aexit__(None, None, None)
                except Exception:  # pragma: no cover
                    pass

    return StreamingResponse(
        stream_iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx buffering off for SSE
        },
    )


def _sse(payload: Dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f"data: {body}\n\n".encode("utf-8")


def _sse_with_id(event_id: str, payload: Dict[str, Any]) -> bytes:
    """Same as :func:`_sse` but stamps the chunk with an SSE ``id:``
    line.  EventSource captures the most recent id and re-sends it
    via ``Last-Event-ID`` on auto-reconnect — that's how the resume
    chain is closed end-to-end."""
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f"id: {event_id}\ndata: {body}\n\n".encode("utf-8")
