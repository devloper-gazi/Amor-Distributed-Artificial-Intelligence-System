"""
Tests for speculative_run — race a cache lookup against a live mesh
call, cancel cleanly when the cache wins.
"""

from __future__ import annotations

import asyncio

import pytest

from document_processor.code_intelligence.reactor.speculative import (
    speculative_run,
)


# ── cache wins ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_returns_immediately_and_cancels_live():
    live_started = asyncio.Event()
    live_completed = asyncio.Event()

    async def live():
        live_started.set()
        try:
            await asyncio.sleep(2.0)
            live_completed.set()
            return "live result"
        except asyncio.CancelledError:
            return "live result"  # caller never sees this

    async def cache():
        return "cached"

    result, was_hit = await speculative_run(
        cache_lookup=cache(), live_call=live(),
    )
    assert result == "cached"
    assert was_hit is True
    # Give the cancelled task a moment to land.
    await asyncio.sleep(0.05)
    # Live completion never happens because we cancelled it.
    assert not live_completed.is_set()


# ── cache misses → live wins ─────────────────────────────────


@pytest.mark.asyncio
async def test_cache_miss_yields_live_result():
    async def live():
        await asyncio.sleep(0.01)
        return "live"

    async def cache():
        await asyncio.sleep(0.01)
        return None

    result, was_hit = await speculative_run(
        cache_lookup=cache(), live_call=live(),
    )
    assert result == "live"
    assert was_hit is False


@pytest.mark.asyncio
async def test_cache_timeout_falls_back_to_live():
    async def slow_cache():
        await asyncio.sleep(2.0)  # > timeout
        return "slow cache"

    async def live():
        return "live"

    result, was_hit = await speculative_run(
        cache_lookup=slow_cache(),
        live_call=live(),
        cache_timeout_s=0.05,
    )
    assert result == "live"
    assert was_hit is False


# ── cache exception → live wins ──────────────────────────────


@pytest.mark.asyncio
async def test_cache_exception_does_not_block_live():
    async def cache():
        raise RuntimeError("cache fault")

    async def live():
        return "live"

    result, was_hit = await speculative_run(
        cache_lookup=cache(), live_call=live(),
    )
    assert result == "live"
    assert was_hit is False


# ── live exception with no cache → propagates ───────────────


@pytest.mark.asyncio
async def test_live_exception_with_no_cache_value_propagates():
    async def cache():
        return None

    async def live():
        raise RuntimeError("live fault")

    with pytest.raises(RuntimeError, match="live fault"):
        await speculative_run(
            cache_lookup=cache(), live_call=live(),
        )


# ── live exception but cache eventually returns → cache wins ──


@pytest.mark.asyncio
async def test_live_exception_then_cache_returns_value_uses_cache():
    async def cache():
        await asyncio.sleep(0.05)  # arrives just after live errors
        return "cached"

    async def live():
        raise RuntimeError("live fault")

    result, was_hit = await speculative_run(
        cache_lookup=cache(),
        live_call=live(),
        cache_timeout_s=0.01,  # timeout fires first → cache returns None initially
    )
    # The fallback path inside speculative_run gives the cache one
    # last short window. cached() resolves within that window so the
    # cache wins.
    assert result == "cached"
    assert was_hit is True
