"""
Cycle C Sprint 9 Day 1 — ResumableStream tests.

Two regimes — same contract, both paths must satisfy it:

* **In-memory fallback** (no ``redis_client``) — verifies the
  single-replica delivery + replay-from-cap behaviour the live app
  falls back to when Redis is unreachable.
* **Mocked redis-py async client** — verifies XADD / XRANGE / XREAD
  call sites + the bytes-vs-str decoding logic.

The stream key + active-msg key naming is also pinned so a future
refactor doesn't accidentally break the wire contract Sprint 9 Day 3
relies on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from document_processor.infrastructure.resumable_stream import (
    ACTIVE_MSG_KEY_PREFIX,
    DEFAULT_MAX_LEN,
    SENTINEL_CLOSE,
    STREAM_KEY_PREFIX,
    ResumableStream,
    StreamEvent,
    active_msg_key,
    stream_key,
)


# ─── key helpers ────────────────────────────────────────────────


def test_stream_key_format():
    assert stream_key("abc") == f"{STREAM_KEY_PREFIX}:abc"


def test_active_msg_key_format():
    assert active_msg_key("chat-1") == f"{ACTIVE_MSG_KEY_PREFIX}:chat-1"


# ─── in-memory regime ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_inmem_publish_then_replay_full():
    s = ResumableStream("sid-imem-1")
    a = await s.publish({"type": "thought", "text": "first"})
    b = await s.publish({"type": "action", "tool": "echo"})
    out = await s.replay()
    ids = [e.id for e in out]
    assert ids == [a.id, b.id]
    assert out[0].data["type"] == "thought"
    assert out[1].data["tool"] == "echo"


@pytest.mark.asyncio
async def test_inmem_replay_after_id_excludes_starting_event():
    s = ResumableStream("sid-imem-2")
    a = await s.publish({"i": 1})
    await s.publish({"i": 2})
    await s.publish({"i": 3})
    tail = await s.replay(after_id=a.id)
    assert [e.data["i"] for e in tail] == [2, 3]


@pytest.mark.asyncio
async def test_inmem_replay_unknown_id_returns_full_log():
    """Operator-friendly: a stale Last-Event-ID falls back to a full
    replay rather than an error."""
    s = ResumableStream("sid-imem-3")
    await s.publish({"i": 1})
    out = await s.replay(after_id="9999-9999")
    assert len(out) == 1


@pytest.mark.asyncio
async def test_inmem_max_len_caps_log():
    s = ResumableStream("sid-imem-cap", max_len=3)
    for i in range(7):
        await s.publish({"i": i})
    out = await s.replay()
    assert len(out) == 3
    assert [e.data["i"] for e in out] == [4, 5, 6]


@pytest.mark.asyncio
async def test_inmem_length_reflects_buffer():
    s = ResumableStream("sid-imem-len")
    assert await s.length() == 0
    await s.publish({"a": 1})
    await s.publish({"a": 2})
    assert await s.length() == 2


@pytest.mark.asyncio
async def test_inmem_delete_clears():
    s = ResumableStream("sid-imem-del")
    await s.publish({"x": 1})
    await s.delete()
    assert await s.length() == 0
    assert await s.replay() == []


# ─── tail (live) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inmem_tail_yields_published_events():
    s = ResumableStream("sid-tail-1")
    received: list = []

    async def consume():
        async for ev in s.tail(block_ms=200, idle_keep_alive=False):
            received.append(ev)
            if ev and ev.data.get("__sentinel__") == SENTINEL_CLOSE:
                return

    import asyncio
    consumer = asyncio.create_task(consume())
    # Give the consumer a moment to start awaiting the queue.
    await asyncio.sleep(0.05)
    await s.publish({"i": 1})
    await s.publish({"i": 2})
    await s.publish_close()
    await asyncio.wait_for(consumer, timeout=2.0)

    payloads = [ev.data for ev in received if ev is not None]
    assert {"i": 1} in payloads
    assert {"i": 2} in payloads
    assert any(p.get("__sentinel__") == SENTINEL_CLOSE for p in payloads)


@pytest.mark.asyncio
async def test_inmem_tail_emits_keepalive_on_idle():
    """When ``idle_keep_alive=True`` (default), the tail yields
    ``None`` once per block window so the route layer can ping the
    SSE client."""
    import asyncio
    s = ResumableStream("sid-tail-keep")
    gen = s.tail(block_ms=50, idle_keep_alive=True)
    first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    assert first is None  # keep-alive
    await gen.aclose()


# ─── redis-mocked regime ────────────────────────────────────────


@pytest.mark.asyncio
async def test_redis_publish_calls_xadd_with_maxlen():
    redis = MagicMock()
    redis.xadd = AsyncMock(return_value=b"1700000000000-0")
    s = ResumableStream("sid-r-1", redis_client=redis)

    ev = await s.publish({"k": "v"})

    redis.xadd.assert_awaited_once()
    args, kwargs = redis.xadd.call_args
    assert args[0] == stream_key("sid-r-1")
    assert isinstance(args[1], dict) and "data" in args[1]
    assert kwargs["maxlen"] == DEFAULT_MAX_LEN
    assert kwargs["approximate"] is True
    assert ev.id == "1700000000000-0"


@pytest.mark.asyncio
async def test_redis_publish_falls_back_when_xadd_raises():
    redis = MagicMock()
    redis.xadd = AsyncMock(side_effect=RuntimeError("redis unhappy"))
    s = ResumableStream("sid-r-fallback", redis_client=redis)
    ev = await s.publish({"k": "v"})
    # Synthetic id used when Redis fails.
    assert "-" in ev.id
    out = await s.replay()
    assert len(out) == 1


@pytest.mark.asyncio
async def test_redis_replay_uses_exclusive_min_when_after_id():
    redis = MagicMock()
    redis.xrange = AsyncMock(return_value=[])
    s = ResumableStream("sid-r-replay", redis_client=redis)
    await s.replay(after_id="42-1")
    redis.xrange.assert_awaited_once()
    _, kwargs = redis.xrange.call_args
    assert kwargs["min"] == "(42-1"
    assert kwargs["max"] == "+"


@pytest.mark.asyncio
async def test_redis_replay_decodes_bytes_payloads():
    redis = MagicMock()
    redis.xrange = AsyncMock(return_value=[
        (b"1-0", {b"data": b'{"i": 1}'}),
        (b"2-0", {b"data": b'{"i": 2}'}),
    ])
    s = ResumableStream("sid-r-decode", redis_client=redis)
    out = await s.replay()
    assert [e.id for e in out] == ["1-0", "2-0"]
    assert [e.data["i"] for e in out] == [1, 2]


@pytest.mark.asyncio
async def test_redis_replay_returns_full_log_when_no_after_id():
    redis = MagicMock()
    redis.xrange = AsyncMock(return_value=[
        (b"5-0", {b"data": b'{"x": "y"}'}),
    ])
    s = ResumableStream("sid-r-full", redis_client=redis)
    out = await s.replay()
    redis.xrange.assert_awaited_once()
    _, kwargs = redis.xrange.call_args
    assert kwargs["min"] == "-"
    assert out[0].data == {"x": "y"}


@pytest.mark.asyncio
async def test_redis_tail_iterates_xread_results():
    """First xread returns one event, second returns empty (no
    block hit), third returns the close sentinel — generator exits."""
    redis = MagicMock()
    redis.xread = AsyncMock(side_effect=[
        [(b"amor:stream:sid-r-tail", [(b"1-0", {b"data": b'{"i": 1}'})])],
        [],  # idle
        [(b"amor:stream:sid-r-tail", [(b"2-0", {b"data": b'{"__sentinel__": "__close__"}'})])],
    ])
    s = ResumableStream("sid-r-tail", redis_client=redis)

    out: list = []
    async for ev in s.tail(block_ms=50, idle_keep_alive=True):
        out.append(ev)

    # First yield: the live event.
    assert out[0].data["i"] == 1
    # Second yield: keep-alive None.
    assert out[1] is None
    # Third yield: the sentinel — generator exits after.
    assert out[2].data.get("__sentinel__") == SENTINEL_CLOSE
    assert len(out) == 3


@pytest.mark.asyncio
async def test_redis_length_calls_xlen():
    redis = MagicMock()
    redis.xlen = AsyncMock(return_value=42)
    s = ResumableStream("sid-r-len", redis_client=redis)
    assert await s.length() == 42


@pytest.mark.asyncio
async def test_redis_trim_calls_xtrim():
    redis = MagicMock()
    redis.xtrim = AsyncMock()
    s = ResumableStream("sid-r-trim", redis_client=redis, max_len=99)
    await s.trim()
    redis.xtrim.assert_awaited_once()
    args, kwargs = redis.xtrim.call_args
    assert args[0] == stream_key("sid-r-trim")
    assert kwargs["maxlen"] == 99
    assert kwargs["approximate"] is True


@pytest.mark.asyncio
async def test_redis_delete_calls_redis_delete():
    redis = MagicMock()
    redis.delete = AsyncMock()
    s = ResumableStream("sid-r-del", redis_client=redis)
    await s.delete()
    redis.delete.assert_awaited_once_with(stream_key("sid-r-del"))
