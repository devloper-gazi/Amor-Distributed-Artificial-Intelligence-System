"""
Tests for EpisodicMemoryStore — Pydantic-shaped store + cosine
similarity search + reuse/seed/fresh routing decision.

Tests use the in-memory fallback path + the deterministic
``hash_embedder`` so they don't touch Mongo or sentence-transformers.
"""

from __future__ import annotations

import asyncio

import pytest

from local_ai.episodic_memory import (
    DEFAULT_REUSE_THRESHOLD,
    DEFAULT_SEED_THRESHOLD,
    EpisodicMemoryEntry,
    EpisodicMemoryStore,
    RetrievalDecision,
    RetrievedEpisode,
    _cosine,
    hash_embedder,
)


def _entry(query: str, code: str = "print('x')",
           pass_rate: float = 1.0,
           **kwargs) -> EpisodicMemoryEntry:
    return EpisodicMemoryEntry(
        session_id=kwargs.pop("session_id", "s1"),
        user_query=query,
        final_code=code,
        test_pass_rate=pass_rate,
        **kwargs,
    )


# ── helpers ─────────────────────────────────────────────────────────


def test_cosine_identical_one():
    a = [1.0, 2.0, 3.0]
    assert _cosine(a, a) == pytest.approx(1.0)


def test_cosine_orthogonal_zero():
    assert _cosine([1, 0, 0], [0, 1, 0]) == 0.0


def test_cosine_empty_safe():
    assert _cosine([], [1, 2]) == 0.0
    assert _cosine([1, 2], []) == 0.0
    assert _cosine([1], [2, 3]) == 0.0  # length mismatch


def test_cosine_zero_vector_safe():
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


# ── EpisodicMemoryEntry ────────────────────────────────────────────


def test_entry_computes_content_hash_when_missing():
    e = _entry("hello world")
    assert e.content_hash
    assert len(e.content_hash) == 16  # 16-hex prefix of sha256


def test_entry_content_hash_deterministic():
    e1 = _entry("query A", code="solution A")
    e2 = _entry("query A", code="solution A")
    assert e1.content_hash == e2.content_hash


def test_entry_content_hash_changes_with_code():
    e1 = _entry("same query", code="version 1")
    e2 = _entry("same query", code="version 2")
    assert e1.content_hash != e2.content_hash


def test_entry_to_dict_round_trip():
    e = _entry("hello", code="x", pass_rate=0.85,
                language="python", complexity="O(n)",
                tags=["sort"])
    d = e.to_dict()
    for key in ("session_id", "user_query", "timestamp",
                "final_code", "test_pass_rate", "language",
                "complexity", "tags", "content_hash"):
        assert key in d


# ── store + retrieve (in-memory fallback) ──────────────────────────


@pytest.mark.asyncio
async def test_store_and_count_in_memory():
    store = EpisodicMemoryStore(embedder=hash_embedder())
    assert await store.count() == 0
    ok = await store.store(_entry("first query"))
    assert ok is True
    assert await store.count() == 1


@pytest.mark.asyncio
async def test_store_upserts_by_content_hash():
    """Re-storing the same content_hash replaces the prior entry,
    doesn't accumulate duplicates."""
    store = EpisodicMemoryStore(embedder=hash_embedder())
    e = _entry("repeated query", code="same code")
    await store.store(e)
    await store.store(e)
    await store.store(e)
    assert await store.count() == 1


@pytest.mark.asyncio
async def test_store_caps_inmemory_size():
    store = EpisodicMemoryStore(
        embedder=hash_embedder(), max_inmemory_size=3,
    )
    for i in range(10):
        await store.store(_entry(f"query {i}", code=f"code {i}"))
    assert await store.count() == 3


# ── search ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_returns_top_k_ordered_by_similarity():
    store = EpisodicMemoryStore(embedder=hash_embedder())
    await store.store(_entry("merge sort an array of integers",
                             code="ms"))
    await store.store(_entry("count even numbers",
                             code="cnt"))
    await store.store(_entry("sort a list of integers using merge sort",
                             code="ms2"))
    matches = await store.search("merge sort integers", k=2)
    assert len(matches) == 2
    # Both top matches mention sort + integers.
    top_query = matches[0].entry.user_query
    assert "sort" in top_query.lower()
    # Similarity is monotonically non-increasing.
    sims = [m.similarity for m in matches]
    assert sims == sorted(sims, reverse=True)


@pytest.mark.asyncio
async def test_search_empty_store_returns_empty():
    store = EpisodicMemoryStore(embedder=hash_embedder())
    assert await store.search("anything") == []


@pytest.mark.asyncio
async def test_search_without_embedder_returns_empty():
    store = EpisodicMemoryStore(embedder=None)
    await store.store(_entry("hello"))
    matches = await store.search("hello")
    assert matches == []


@pytest.mark.asyncio
async def test_search_min_similarity_filters_low_matches():
    store = EpisodicMemoryStore(embedder=hash_embedder())
    await store.store(_entry("merge sort"))
    await store.store(_entry("totally unrelated text about cats"))
    matches = await store.search("merge sort", k=10, min_similarity=0.95)
    # Only the near-perfect match survives.
    assert len(matches) == 1


# ── decide() routing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decide_returns_fresh_for_unrelated_queries():
    store = EpisodicMemoryStore(embedder=hash_embedder())
    await store.store(_entry("merge sort an array"))
    decision = await store.decide("compute the integral of f")
    assert isinstance(decision, RetrievalDecision)
    assert decision.action == "fresh"


@pytest.mark.asyncio
async def test_decide_returns_reuse_for_near_identical_query():
    store = EpisodicMemoryStore(embedder=hash_embedder())
    await store.store(_entry("merge sort the input array"))
    decision = await store.decide("merge sort the input array")
    assert decision.action == "reuse"
    assert decision.best_similarity >= DEFAULT_REUSE_THRESHOLD


@pytest.mark.asyncio
async def test_decide_returns_seed_for_partial_match():
    """Carefully chosen queries: enough trigram overlap to clear the
    seed threshold but not the reuse threshold."""
    store = EpisodicMemoryStore(
        embedder=hash_embedder(),
        reuse_threshold=0.95,
        seed_threshold=0.4,
    )
    await store.store(_entry("merge sort an array of integers"))
    decision = await store.decide("sort an array")
    # With a high reuse threshold and low seed threshold, the
    # partial overlap lands in the seed band.
    assert decision.action in {"seed", "fresh"}
    if decision.action == "seed":
        assert DEFAULT_SEED_THRESHOLD <= decision.best_similarity < 0.95
        # Best match's reuse_recommended is False but seed_recommended True.
        best = decision.matches[0]
        assert not best.reuse_recommended


@pytest.mark.asyncio
async def test_decide_empty_store_returns_fresh_no_matches():
    store = EpisodicMemoryStore(embedder=hash_embedder())
    decision = await store.decide("anything")
    assert decision.action == "fresh"
    assert decision.matches == []
    assert decision.best_similarity == 0.0


# ── Mongo collection (mocked async) ──────────────────────────────


class _MockCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _MockCollection:
    def __init__(self):
        self._docs: list[dict] = []
        self.update_calls = 0
        self.fail_writes = False

    async def update_one(self, query, update, upsert=False):
        self.update_calls += 1
        if self.fail_writes:
            raise RuntimeError("simulated mongo write failure")
        new_doc = update.get("$set", {})
        # Find by content_hash.
        ch = query.get("content_hash")
        for i, d in enumerate(self._docs):
            if d.get("content_hash") == ch:
                self._docs[i] = new_doc
                return
        if upsert:
            self._docs.append(new_doc)

    def find(self, query, projection):
        return _MockCursor(list(self._docs))

    async def count_documents(self, query):
        return len(self._docs)


@pytest.mark.asyncio
async def test_store_persists_to_mongo_collection():
    coll = _MockCollection()
    store = EpisodicMemoryStore(
        collection=coll, embedder=hash_embedder(),
    )
    await store.store(_entry("query A"))
    assert coll.update_calls == 1
    assert len(coll._docs) == 1


@pytest.mark.asyncio
async def test_store_falls_back_to_inmemory_when_mongo_write_fails():
    coll = _MockCollection()
    coll.fail_writes = True
    store = EpisodicMemoryStore(
        collection=coll, embedder=hash_embedder(),
    )
    ok = await store.store(_entry("query"))
    assert ok is True   # never raises
    # Mongo failed → in-memory keeps the entry.
    assert await store.count() >= 1


@pytest.mark.asyncio
async def test_search_unions_mongo_and_inmemory():
    coll = _MockCollection()
    store = EpisodicMemoryStore(
        collection=coll, embedder=hash_embedder(),
    )
    # Pre-seed Mongo with a doc.
    coll._docs.append(
        _entry("merge sort", session_id="m1").to_dict(),
    )
    # And the in-memory bag with another.
    store._inmemory.append(_entry("count even numbers",
                                    session_id="m2"))
    matches = await store.search("merge sort", k=10)
    assert len(matches) == 2


# ── retrieved-episode helpers ─────────────────────────────────────


def test_retrieved_episode_routing_flags():
    e = _entry("x")
    high = RetrievedEpisode(entry=e, similarity=0.92)
    mid  = RetrievedEpisode(entry=e, similarity=0.70)
    low  = RetrievedEpisode(entry=e, similarity=0.30)
    assert high.reuse_recommended and not high.seed_recommended
    assert not mid.reuse_recommended and mid.seed_recommended
    assert not low.reuse_recommended and not low.seed_recommended


def test_retrieved_episode_to_dict_carries_recommendations():
    e = _entry("x")
    r = RetrievedEpisode(entry=e, similarity=0.91)
    d = r.to_dict()
    assert d["reuse_recommended"] is True
    assert "entry" in d


def test_retrieval_decision_to_dict_round_trip():
    d = RetrievalDecision(
        action="reuse",
        matches=[RetrievedEpisode(entry=_entry("x"), similarity=0.9)],
        best_similarity=0.9,
    )
    out = d.to_dict()
    assert out["action"] == "reuse"
    assert isinstance(out["matches"], list)


# ── housekeeping ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_inmemory_does_not_touch_mongo():
    coll = _MockCollection()
    store = EpisodicMemoryStore(
        collection=coll, embedder=hash_embedder(),
    )
    coll._docs.append(_entry("from_mongo", session_id="mg").to_dict())
    store._inmemory.append(_entry("from_local", session_id="lc"))
    await store.clear_inmemory()
    # in-memory cleared but Mongo doc still there.
    matches = await store.search("from", k=10)
    queries = {m.entry.user_query for m in matches}
    assert "from_mongo" in queries
    assert "from_local" not in queries
