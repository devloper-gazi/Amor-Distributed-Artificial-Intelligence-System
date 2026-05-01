"""
speculative_run — race a cache-lookup against a live mesh call.

If the cache wins (returns non-None first), cancel the live task,
return the cached value. The cancelled coroutine is allowed to run
to completion in the background (asyncio.shield-style); its result
is discarded. This matches the existing sandbox cancellation semantics
and avoids needing HTTP-level abort against Ollama.

If the live call wins (cache returns None or raises), use that
result; the cache lookup is awaited briefly to surface its result
for cache-write side-effects but never blocks the engine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


async def speculative_run(
    *,
    cache_lookup: Awaitable[_T | None],
    live_call: Awaitable[_T],
    cache_timeout_s: float = 0.5,
) -> tuple[_T, bool]:
    """Race the cache against the live call.

    Returns ``(result, was_cache_hit)``. Cache wins iff it resolves to
    a non-None value before the live call completes (or before the
    short ``cache_timeout_s`` budget if the cache itself is slow).
    """
    cache_task = asyncio.create_task(_swallow(cache_lookup))
    live_task = asyncio.create_task(_swallow(live_call, default=None))

    # Give the cache up to `cache_timeout_s` before we fall back to live.
    try:
        cache_value = await asyncio.wait_for(asyncio.shield(cache_task),
                                              timeout=cache_timeout_s)
    except asyncio.TimeoutError:
        cache_value = None
    except Exception as exc:
        logger.debug("speculative_cache_lookup_threw: %s", exc)
        cache_value = None

    if cache_value is not None:
        # Cache hit — cancel the live task, return cached.
        if not live_task.done():
            live_task.cancel()
        return cache_value, True

    # No cache hit — wait for the live call.
    try:
        live_value = await live_task
    except asyncio.CancelledError:
        raise
    except Exception:
        # If live raised AND cache is still pending, give it one last
        # short window — it might still help.
        if not cache_task.done():
            try:
                cache_value = await asyncio.wait_for(cache_task, timeout=0.2)
            except (asyncio.TimeoutError, Exception):
                cache_value = None
        if cache_value is not None:
            return cache_value, True
        raise
    if live_value is None:
        raise RuntimeError("live call returned None")
    return live_value, False


async def _swallow(
    coro: Awaitable[_T], *, default: _T | None = None,
) -> _T | None:
    """Wrap any awaitable so a cancellation doesn't propagate as an
    error. Returns the awaited value, or ``default`` on cancellation."""
    try:
        return await coro
    except asyncio.CancelledError:
        return default  # type: ignore[return-value]
    except Exception:
        # Re-raise — the caller catches.
        raise
