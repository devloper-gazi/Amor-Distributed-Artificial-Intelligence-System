"""
Unit tests for ``document_processor/quick_code/striatum.py``.

The tests run entirely in-memory (cache=None) so we never need a
real Redis instance.  The ``hash_embedder`` is deterministic, so two
identical prompts produce cosine ≈ 1.0 — perfect for the
fast-path-hit test.  We also exercise a custom embedder shim to
validate the cosine path.
"""

from __future__ import annotations

import asyncio

import pytest

from document_processor.quick_code.striatum import (
    Striatum,
    cosine,
    hash_embedder,
)


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


class _FakeCache:
    """A trivial async dict that mimics the cache_manager surface
    used by Striatum (``get`` / ``set`` / ``delete``).  TTL is
    accepted but not enforced — tests don't need wall-clock eviction."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        self.store[key] = value
        if ttl is not None:
            self.ttls[key] = int(ttl)
        return True

    async def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None


# ─────────────────────────────────────────────────────────────────────
# Embedder + cosine sanity
# ─────────────────────────────────────────────────────────────────────


def test_hash_embedder_is_deterministic():
    a = hash_embedder("reverse a string")
    b = hash_embedder("reverse a string")
    assert a == b
    assert len(a) == 64


def test_hash_embedder_handles_empty():
    assert hash_embedder("") == [0.0] * 64


def test_cosine_self_similarity_is_one():
    v = hash_embedder("merge two sorted lists")
    assert cosine(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal_zero():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert cosine(a, b) == pytest.approx(0.0)


def test_cosine_handles_zero_vectors():
    assert cosine([0, 0, 0], [1, 1, 1]) == 0.0
    assert cosine([], [1.0]) == 0.0


# ─────────────────────────────────────────────────────────────────────
# Lookup + store on in-memory storage
# ─────────────────────────────────────────────────────────────────────


def test_lookup_returns_none_on_empty_store():
    s = Striatum(cache=None)
    assert _run(s.lookup("anything")) is None


def test_store_then_exact_lookup_returns_bundle():
    s = Striatum(cache=None)
    bundle = {"code": "print(1)", "session_id": "abc"}
    _run(s.store("reverse a string", bundle))
    hit = _run(s.lookup("reverse a string"))
    assert hit is not None
    assert hit["code"] == "print(1)"
    assert hit["striatum_meta"]["score"] >= 0.95


def test_lookup_below_threshold_returns_none():
    # Build a deterministic embedder where two specific prompts map
    # to fully orthogonal one-hot vectors.  We can't rely on Python's
    # hash() (it's randomised per process), so we look up the prompt
    # by string identity.
    embeddings = {
        "foo": [1.0, 0.0, 0.0, 0.0],
        "totally different bar": [0.0, 1.0, 0.0, 0.0],
    }

    def orth_embed(text: str) -> list[float]:
        return embeddings.get(text, [0.0, 0.0, 1.0, 0.0])

    s = Striatum(cache=None, embedder=orth_embed, threshold=0.5)
    _run(s.store("foo", {"k": 1}))
    # With orthogonal vectors, cosine = 0 for "totally different bar".
    assert _run(s.lookup("totally different bar")) is None


def test_lookup_above_threshold_returns_best_match():
    s = Striatum(cache=None)
    _run(s.store("reverse a string", {"id": "A"}))
    _run(s.store("merge two dicts", {"id": "B"}))
    hit = _run(s.lookup("reverse a string"))
    assert hit is not None
    assert hit["id"] == "A"


# ─────────────────────────────────────────────────────────────────────
# Eviction + max entries
# ─────────────────────────────────────────────────────────────────────


def test_max_entries_eviction():
    s = Striatum(cache=None, max_entries=2)
    _run(s.store("a", {"id": 1}))
    _run(s.store("b", {"id": 2}))
    _run(s.store("c", {"id": 3}))
    stats = _run(s.stats())
    assert stats["size"] == 2
    # The oldest "a" should have been evicted.
    assert _run(s.lookup("a")) is None


def test_clear_drops_state():
    s = Striatum(cache=None)
    _run(s.store("x", {"id": "X"}))
    _run(s.clear())
    assert _run(s.stats())["size"] == 0


# ─────────────────────────────────────────────────────────────────────
# Redis-shim path
# ─────────────────────────────────────────────────────────────────────


def test_lookup_via_fake_redis_round_trip():
    cache = _FakeCache()
    s = Striatum(cache=cache, ttl_s=60)
    _run(s.store("hello world", {"id": "hw"}))
    # Construct a fresh Striatum on the same cache to confirm
    # persistence across instances.
    s2 = Striatum(cache=cache, ttl_s=60)
    hit = _run(s2.lookup("hello world"))
    assert hit is not None
    assert hit["id"] == "hw"


def test_salt_invalidates_entries():
    cache = _FakeCache()
    s = Striatum(cache=cache, salt=1)
    _run(s.store("hi", {"id": "v1"}))
    s_new_salt = Striatum(cache=cache, salt=2)
    # New salt → different scoped key → cold cache.
    assert _run(s_new_salt.lookup("hi")) is None
    # Old salt still has it.
    assert _run(s.lookup("hi")) is not None


def test_lookup_handles_redis_get_error():
    class Broken:
        async def get(self, key):
            raise RuntimeError("redis down")

        async def set(self, *a, **kw):
            return False

        async def delete(self, *a):
            return False

    s = Striatum(cache=Broken())
    # Should fall back to in-memory list (empty), returning None.
    assert _run(s.lookup("anything")) is None


# ─────────────────────────────────────────────────────────────────────
# Bundle isolation
# ─────────────────────────────────────────────────────────────────────


def test_returned_bundle_is_a_copy():
    s = Striatum(cache=None)
    bundle = {"code": "print(1)", "nested": {"a": 1}}
    _run(s.store("k", bundle))
    hit = _run(s.lookup("k"))
    assert hit is not None
    hit["code"] = "MUTATED"
    hit["nested"]["a"] = 999
    # Re-lookup must still return the original.
    again = _run(s.lookup("k"))
    assert again is not None
    assert again["code"] == "print(1)"
    assert again["nested"]["a"] == 1


# ─────────────────────────────────────────────────────────────────────
# No-filter prompt sanity (Striatum has no prompt — this checks the
# module docstring is clean of refusal language)
# ─────────────────────────────────────────────────────────────────────


def test_module_docstring_no_refusal_language():
    import document_processor.quick_code.striatum as mod

    text = (mod.__doc__ or "").lower()
    for token in ("i cannot help", "i won't", "as an ai", "consult a lawyer"):
        assert token not in text
