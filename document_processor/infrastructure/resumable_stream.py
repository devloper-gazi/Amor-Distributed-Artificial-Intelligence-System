"""
Cycle C Sprint 9 Day 1 — resumable SSE stream backed by Redis Streams.

Implements the vercel/resumable-stream pattern:

* Every published event lands in a Redis Stream via ``XADD`` with a
  capped length (``MAXLEN ~10000``).  The auto-generated ``id``
  (``ms-seq``) becomes the SSE ``id:`` line so a client can resume
  after disconnect by sending it as ``Last-Event-ID``.
* On reconnect the server replays via ``XRANGE last-id + ... +`` and
  then transitions to a live tail via ``XREAD BLOCK 0 STREAMS … last-id``.
* When Redis is unreachable the stream falls back to a local
  ``asyncio.Queue`` — single-replica delivery still works, just
  without replay.

The class is intentionally NOT tied to the ``CacheManager`` already
in the codebase: that one uses Redis Pub/Sub (no durability), this
one needs Streams.  Both can coexist on the same Redis instance.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── stream key helpers ───────────────────────────────────────────


STREAM_KEY_PREFIX = "amor:stream"
ACTIVE_MSG_KEY_PREFIX = "amor:active_msg"
DEFAULT_MAX_LEN = 10_000
DEFAULT_BLOCK_MS = 30_000  # 30 s server-side block before keep-alive
SENTINEL_CLOSE = "__close__"


def stream_key(stream_id: str) -> str:
    return f"{STREAM_KEY_PREFIX}:{stream_id}"


def active_msg_key(chat_id: str) -> str:
    """Per-chat key holding the currently-active message id.  Sprint 9
    Day 3 wires this into agent_routes so two replicas don't claim the
    same chat at the same time."""
    return f"{ACTIVE_MSG_KEY_PREFIX}:{chat_id}"


# ─── data classes ────────────────────────────────────────────────


@dataclass(frozen=True)
class StreamEvent:
    """One event read off the stream.  ``id`` is the Redis stream id
    (``"<ms>-<seq>"``) — opaque to callers; they pass it back as
    ``Last-Event-ID`` to resume."""

    id: str
    data: Dict[str, Any]


# ─── resumable stream ────────────────────────────────────────────


class ResumableStream:
    """Per-stream-id helper around a Redis Stream key.

    Construct one instance per logical stream (e.g. one per agent
    session id).  ``publish`` writes; ``replay`` reads from a
    starting-id forward; ``tail`` blocks for new events.

    Redis is **optional**.  If ``redis_client`` is None, every
    operation falls back to an in-memory queue keyed on the stream
    id — the route layer then can't recover from a process restart
    but at least live delivery still works.
    """

    # Class-level fallback queues, keyed on stream id.  Different
    # ResumableStream instances pointing at the same id share the
    # same queue so a single-process app delivers events end-to-end
    # even with Redis down.
    _LOCAL_QUEUES: Dict[str, asyncio.Queue] = {}
    _LOCAL_LOCK = asyncio.Lock()

    def __init__(
        self,
        stream_id: str,
        *,
        redis_client: Any | None = None,
        max_len: int = DEFAULT_MAX_LEN,
    ) -> None:
        self.stream_id = stream_id
        self.key = stream_key(stream_id)
        self.redis = redis_client
        self.max_len = int(max_len)
        # In-memory replay buffer when Redis is absent (matches the
        # MAX_LEN cap so behaviour mirrors the Redis path).
        self._mem_log: List[StreamEvent] = []

    # ── publish ──────────────────────────────────────────────────

    async def publish(self, payload: Dict[str, Any]) -> StreamEvent:
        """Append one event to the stream.  Returns the event with
        its assigned id (the Redis stream id when Redis is present,
        a synthetic ``"<ms>-<seq>"`` otherwise)."""
        body = json.dumps(payload, default=str, ensure_ascii=False)
        if self.redis is not None:
            try:
                event_id = await self.redis.xadd(
                    self.key,
                    {"data": body},
                    maxlen=self.max_len,
                    approximate=True,
                )
                if isinstance(event_id, bytes):
                    event_id = event_id.decode()
                return StreamEvent(id=str(event_id), data=payload)
            except Exception as exc:
                logger.warning(
                    "ResumableStream.publish redis xadd failed (%s); "
                    "falling back to in-memory log",
                    exc,
                )
        # Fallback path.
        synthetic_id = self._next_synthetic_id()
        ev = StreamEvent(id=synthetic_id, data=payload)
        self._mem_log.append(ev)
        if len(self._mem_log) > self.max_len:
            del self._mem_log[: len(self._mem_log) - self.max_len]
        await self._fanout_local(payload)
        return ev

    async def publish_close(self) -> StreamEvent:
        """Append a sentinel event signalling the producer is done."""
        return await self.publish({"__sentinel__": SENTINEL_CLOSE})

    # ── replay ───────────────────────────────────────────────────

    async def replay(self, *, after_id: Optional[str] = None) -> List[StreamEvent]:
        """Return every event AFTER ``after_id`` (exclusive).  When
        ``after_id`` is None, returns the entire stream from the
        beginning."""
        # Redis path: ``XRANGE`` with the ``(<id>`` exclusive prefix
        # is the canonical "give me everything after this id" call.
        start = f"({after_id}" if after_id else "-"
        if self.redis is not None:
            try:
                rows = await self.redis.xrange(self.key, min=start, max="+")
                out: List[StreamEvent] = []
                for raw_id, fields in rows:
                    sid = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
                    data = self._decode_fields(fields)
                    out.append(StreamEvent(id=sid, data=data))
                return out
            except Exception as exc:
                logger.warning(
                    "ResumableStream.replay redis xrange failed (%s); "
                    "falling back to in-memory log",
                    exc,
                )
        # Fallback: filter the in-memory log.
        if after_id is None:
            return list(self._mem_log)
        try:
            cutoff = self._mem_log_index_after(after_id)
        except ValueError:
            # Unknown id → replay from the head (operator-friendly).
            return list(self._mem_log)
        return self._mem_log[cutoff:]

    # ── tail (live) ──────────────────────────────────────────────

    async def tail(
        self,
        *,
        from_id: str = "$",
        block_ms: int = DEFAULT_BLOCK_MS,
        idle_keep_alive: bool = True,
    ) -> AsyncIterator[Optional[StreamEvent]]:
        """Block-read new events.  Yields ``None`` once per
        ``block_ms`` window when there's nothing new — the route
        layer turns those into SSE keep-alive pings.  The async
        generator exits on a sentinel-close event."""
        last_id = from_id
        if self.redis is not None:
            while True:
                try:
                    rows = await self.redis.xread(
                        {self.key: last_id},
                        count=100,
                        block=block_ms,
                    )
                except Exception as exc:
                    logger.warning(
                        "ResumableStream.tail redis xread failed (%s); "
                        "falling back to in-memory queue",
                        exc,
                    )
                    break
                if not rows:
                    if idle_keep_alive:
                        yield None
                    continue
                for _key, entries in rows:
                    for raw_id, fields in entries:
                        sid = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
                        data = self._decode_fields(fields)
                        last_id = sid
                        ev = StreamEvent(id=sid, data=data)
                        if data.get("__sentinel__") == SENTINEL_CLOSE:
                            yield ev
                            return
                        yield ev
            # fall through to in-memory tail when Redis errored
        # Fallback path — read off the per-stream-id local queue.
        queue = await self._get_local_queue()
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=block_ms / 1000.0)
            except asyncio.TimeoutError:
                if idle_keep_alive:
                    yield None
                continue
            sid = self._next_synthetic_id()
            ev = StreamEvent(id=sid, data=payload)
            if isinstance(payload, dict) and payload.get("__sentinel__") == SENTINEL_CLOSE:
                yield ev
                return
            yield ev

    # ── lifecycle helpers ────────────────────────────────────────

    async def trim(self, *, max_len: Optional[int] = None) -> None:
        n = max_len if max_len is not None else self.max_len
        if self.redis is not None:
            try:
                await self.redis.xtrim(self.key, maxlen=n, approximate=True)
                return
            except Exception as exc:
                logger.warning("ResumableStream.trim failed: %s", exc)
        if len(self._mem_log) > n:
            del self._mem_log[: len(self._mem_log) - n]

    async def length(self) -> int:
        if self.redis is not None:
            try:
                return int(await self.redis.xlen(self.key))
            except Exception as exc:
                logger.warning("ResumableStream.length xlen failed: %s", exc)
        return len(self._mem_log)

    async def delete(self) -> None:
        if self.redis is not None:
            try:
                await self.redis.delete(self.key)
            except Exception as exc:
                logger.warning("ResumableStream.delete failed: %s", exc)
        self._mem_log.clear()
        async with self._LOCAL_LOCK:
            self._LOCAL_QUEUES.pop(self.stream_id, None)

    # ── internals ────────────────────────────────────────────────

    @staticmethod
    def _decode_fields(fields: Dict) -> Dict[str, Any]:
        # ``redis-py`` returns bytes when ``decode_responses=False``
        # and plain strings when it's True.  Handle both.
        out: Dict[str, Any] = {}
        for k, v in (fields or {}).items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            val = v.decode() if isinstance(v, bytes) else v
            out[key] = val
        # Our publish path stores everything under "data" as JSON; if
        # the caller stored a raw mapping we keep it as-is.
        if "data" in out and isinstance(out["data"], str):
            try:
                return json.loads(out["data"])
            except json.JSONDecodeError:
                pass
        return out

    def _mem_log_index_after(self, after_id: str) -> int:
        for i, ev in enumerate(self._mem_log):
            if ev.id == after_id:
                return i + 1
        raise ValueError(f"unknown id: {after_id}")

    _SYNTHETIC_SEQ = 0

    @classmethod
    def _next_synthetic_id(cls) -> str:
        import time as _t
        cls._SYNTHETIC_SEQ += 1
        return f"{int(_t.time() * 1000)}-{cls._SYNTHETIC_SEQ}"

    async def _get_local_queue(self) -> asyncio.Queue:
        async with self._LOCAL_LOCK:
            q = self._LOCAL_QUEUES.get(self.stream_id)
            if q is None:
                q = asyncio.Queue()
                self._LOCAL_QUEUES[self.stream_id] = q
            return q

    async def _fanout_local(self, payload: Dict[str, Any]) -> None:
        """Push the same payload onto every subscriber's queue.  The
        in-memory fallback only supports ONE consumer (the SSE route)
        so a single queue is enough."""
        async with self._LOCAL_LOCK:
            q = self._LOCAL_QUEUES.get(self.stream_id)
        if q is not None:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover — unbounded
                pass


# ─── connection helpers ─────────────────────────────────────────


@asynccontextmanager
async def get_redis_client(
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    db: Optional[int] = None,
    password: Optional[str] = None,
):
    """Async context manager that yields a connected redis client +
    closes it on exit.  Reads ``settings`` for defaults so the route
    layer doesn't have to know the connection details."""
    try:
        import redis.asyncio as aioredis  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        logger.warning("redis-py not installed: %s", exc)
        yield None
        return

    try:
        from ..config.settings import settings  # noqa: PLC0415
        h = host or settings.redis_host
        p = port or settings.redis_port
        d = db if db is not None else settings.redis_db
        pw = password or settings.redis_password
    except Exception:  # pragma: no cover — settings missing
        h = host or "redis"
        p = port or 6379
        d = db or 0
        pw = password

    url = f"redis://{h}:{p}/{d}" if not pw else f"redis://:{pw}@{h}:{p}/{d}"
    client = None
    try:
        client = aioredis.from_url(
            url,
            decode_responses=False,  # we handle the bytes ourselves
            socket_connect_timeout=5,
            socket_keepalive=True,
        )
        await client.ping()
        yield client
    except Exception as exc:
        logger.warning("redis connect failed (%s); resumable streams degraded", exc)
        yield None
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover
                pass
