"""
Tests for SemanticLLMCache — embedding-keyed Redis cache wrapping
any async ``llm_call``. Uses an in-memory cache shim to avoid Redis.
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence.reactor.llm_cache import (
    SemanticLLMCache,
    _cosine,
    _hash_system_prompt,
    _quantise_embedding,
    wrap_llm_call,
)


class _MemoryCache:
    """Minimal in-memory shim matching CacheManager's get_json/set_json."""

    def __init__(self):
        self._store: dict[str, object] = {}

    async def get_json(self, key: str):
        return self._store.get(key)

    async def set_json(self, key: str, value, ttl: int = 0):
        self._store[key] = value


def _embed_simple(s: str):
    """Tiny deterministic embedder — cosine similarity reflects how
    much two strings overlap on chars."""
    bag = [0.0] * 26
    for ch in s.lower():
        if "a" <= ch <= "z":
            bag[ord(ch) - ord("a")] += 1.0
    return bag


# ── helpers ──────────────────────────────────────────────────────


def test_cosine_identical_vectors_equals_one():
    a = [1.0, 2.0, 3.0]
    assert _cosine(a, a) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_equals_zero():
    assert _cosine([1, 0, 0], [0, 1, 0]) == 0.0


def test_cosine_empty_vectors_safe():
    assert _cosine([], [1, 2]) == 0.0


def test_quantise_rounds_to_decimals():
    assert _quantise_embedding([0.123456], decimals=2) == [0.12]


def test_hash_system_prompt_is_stable():
    assert _hash_system_prompt("x") == _hash_system_prompt("x")
    assert _hash_system_prompt("x") != _hash_system_prompt("y")


# ── lookup + store ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_misses_when_empty():
    cache = SemanticLLMCache(
        cache=_MemoryCache(), embedder=_embed_simple,
    )
    res = await cache.lookup(
        role="coder", system_prompt="sys",
        user_prompt="implement merge sort", max_tokens=1500,
    )
    assert res is None
    assert cache.misses == 1


@pytest.mark.asyncio
async def test_store_then_lookup_hits():
    cache = SemanticLLMCache(
        cache=_MemoryCache(), embedder=_embed_simple,
        cosine_threshold=0.99,  # identical-only
    )
    await cache.store(
        role="coder", system_prompt="sys",
        user_prompt="hello world", max_tokens=1500,
        response="cached response",
    )
    hit = await cache.lookup(
        role="coder", system_prompt="sys",
        user_prompt="hello world", max_tokens=1500,
    )
    assert hit == "cached response"
    assert cache.hits == 1


@pytest.mark.asyncio
async def test_lookup_misses_below_cosine_threshold():
    cache = SemanticLLMCache(
        cache=_MemoryCache(), embedder=_embed_simple,
        cosine_threshold=0.98,
    )
    await cache.store(
        role="coder", system_prompt="sys",
        user_prompt="aaa", max_tokens=1500,
        response="for aaa",
    )
    # "zzz" has zero overlap with "aaa" → cosine 0 → miss.
    res = await cache.lookup(
        role="coder", system_prompt="sys",
        user_prompt="zzz", max_tokens=1500,
    )
    assert res is None


@pytest.mark.asyncio
async def test_different_role_uses_different_bucket():
    cache = SemanticLLMCache(
        cache=_MemoryCache(), embedder=_embed_simple,
        cosine_threshold=0.99,
    )
    await cache.store(role="coder", system_prompt="s", user_prompt="x",
                       max_tokens=1500, response="A")
    res = await cache.lookup(role="planner", system_prompt="s",
                              user_prompt="x", max_tokens=1500)
    # Different role → different cache key → miss.
    assert res is None


@pytest.mark.asyncio
async def test_corpus_version_bump_invalidates_everything():
    mem = _MemoryCache()
    cache_v1 = SemanticLLMCache(
        cache=mem, embedder=_embed_simple,
        corpus_version=1, cosine_threshold=0.99,
    )
    await cache_v1.store(role="r", system_prompt="s", user_prompt="x",
                          max_tokens=1500, response="v1")
    cache_v2 = SemanticLLMCache(
        cache=mem, embedder=_embed_simple,
        corpus_version=2, cosine_threshold=0.99,
    )
    res = await cache_v2.lookup(role="r", system_prompt="s",
                                  user_prompt="x", max_tokens=1500)
    assert res is None  # v2 reads a different key


# ── wrap_llm_call ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrap_llm_call_caches_after_first_call():
    calls: list[str] = []

    async def real_llm(prompt, system, max_tokens):
        calls.append(prompt)
        return f"response for {prompt}"

    cache = SemanticLLMCache(
        cache=_MemoryCache(), embedder=_embed_simple,
        cosine_threshold=0.99,
    )
    wrapped = wrap_llm_call(real_llm, cache, role_getter=lambda: "coder")

    # First call hits real LLM + stores result.
    r1 = await wrapped("hello", "sys", 1500)
    # Second call with same prompt is served from cache.
    r2 = await wrapped("hello", "sys", 1500)
    assert r1 == r2 == "response for hello"
    assert len(calls) == 1


# ── failure-soft ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_returns_none_when_embed_throws():
    def bad_embed(s):
        raise RuntimeError("embed down")

    cache = SemanticLLMCache(
        cache=_MemoryCache(), embedder=bad_embed,
    )
    res = await cache.lookup(
        role="x", system_prompt="x", user_prompt="x", max_tokens=1500,
    )
    assert res is None
    assert cache.misses == 1


@pytest.mark.asyncio
async def test_store_swallows_failure_silently():
    class _BadCache:
        async def get_json(self, key): return None
        async def set_json(self, key, val, ttl=0):
            raise RuntimeError("redis down")

    cache = SemanticLLMCache(
        cache=_BadCache(), embedder=_embed_simple,
    )
    # Should not raise.
    await cache.store(role="x", system_prompt="x", user_prompt="x",
                       max_tokens=1500, response="x")
