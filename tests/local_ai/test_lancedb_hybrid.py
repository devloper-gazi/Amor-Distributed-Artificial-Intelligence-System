"""Unit tests for the Phase 16 Commit D1 RAG upgrade.

Targets:
* The Phase 16 helpers on ``LanceDBVectorStore`` — BM25 scoring,
  RRF math, settings-driven hybrid + reranker switches.
* No real LanceDB / sentence-transformers dependency for the
  numeric tests — they exercise the helpers directly so the suite
  stays fast and works without GPU.
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest


def _run(coro):
    return asyncio.run(coro)


# ─── BM25 helper (uses the real implementation from rag_engine) ────


def test_bm25_helper_returns_per_doc_scores():
    """The static BM25 helper mounted on LanceDBVectorStore should
    fit on the candidate texts and score them against the query."""
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore

    # Bypass __init__ — we only need the bound method.
    store = LanceDBVectorStore.__new__(LanceDBVectorStore)
    texts = [
        "the quick brown fox jumps over the lazy dog",
        "a brown fox is fast and lazy when the sun sets",
        "this document is unrelated to foxes or dogs",
    ]
    scores = store._bm25_scores("brown fox", texts)
    assert len(scores) == 3
    # First two contain "brown fox" → must outscore the third.
    assert scores[0] > scores[2]
    assert scores[1] > scores[2]


def test_bm25_helper_handles_empty_texts():
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore

    store = LanceDBVectorStore.__new__(LanceDBVectorStore)
    assert store._bm25_scores("anything", []) == []


# ─── settings-driven gates ────────────────────────────────────────


def test_hybrid_enabled_defaults_true(monkeypatch):
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore
    from document_processor.config.settings import settings

    store = LanceDBVectorStore.__new__(LanceDBVectorStore)
    monkeypatch.setattr(settings, "rag_hybrid_search_enabled", True)
    assert store._hybrid_enabled() is True
    monkeypatch.setattr(settings, "rag_hybrid_search_enabled", False)
    assert store._hybrid_enabled() is False


def test_rerank_override_wins_over_setting(monkeypatch):
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore
    from document_processor.config.settings import settings

    store = LanceDBVectorStore.__new__(LanceDBVectorStore)
    monkeypatch.setattr(settings, "rag_reranker_enabled", True)
    assert store._should_rerank(False) is False  # explicit override
    assert store._should_rerank(None) is True
    monkeypatch.setattr(settings, "rag_reranker_enabled", False)
    assert store._should_rerank(True) is True
    assert store._should_rerank(None) is False


def test_rrf_k_default_and_override(monkeypatch):
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore
    from document_processor.config.settings import settings

    store = LanceDBVectorStore.__new__(LanceDBVectorStore)
    # Override wins.
    assert store._rrf_k(10) == 10
    # Default falls through to settings.
    monkeypatch.setattr(settings, "rag_rrf_k", 99)
    assert store._rrf_k(None) == 99


def test_reranker_top_k_default_and_override(monkeypatch):
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore
    from document_processor.config.settings import settings

    store = LanceDBVectorStore.__new__(LanceDBVectorStore)
    assert store._reranker_top_k(50) == 50
    monkeypatch.setattr(settings, "rag_reranker_top_k", 33)
    assert store._reranker_top_k(None) == 33


# ─── hybrid_search end-to-end with a faked vector backend ─────────


class _FakeStore:
    """Subset of LanceDBVectorStore behaviour for hybrid_search math."""

    def __init__(self, vector_results):
        self._vector_results = vector_results
        self._reranker = None
        self.device = "cpu"

    # Reuse the real helpers verbatim.
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore as _L
    _settings_value = staticmethod(_L._settings_value)
    _hybrid_enabled = _L._hybrid_enabled
    _should_rerank = _L._should_rerank
    _reranker_top_k = _L._reranker_top_k
    _rrf_k = _L._rrf_k
    _bm25_scores = _L._bm25_scores
    _apply_reranker = _L._apply_reranker
    hybrid_search = _L.hybrid_search

    async def search(
        self, query, limit=5, min_score=0.0, filter_expr=None,
        rerank=None, rerank_top_k=None,
    ):  # noqa: ARG002
        return [dict(r) for r in self._vector_results[:limit]]


def test_hybrid_search_falls_back_to_dense_when_disabled(monkeypatch):
    from document_processor.config.settings import settings

    monkeypatch.setattr(settings, "rag_hybrid_search_enabled", False)
    monkeypatch.setattr(settings, "rag_reranker_enabled", False)

    fake_results = [
        {"id": "a", "text": "alpha", "score": 0.9},
        {"id": "b", "text": "beta", "score": 0.7},
    ]
    store = _FakeStore(fake_results)
    out = _run(store.hybrid_search("alpha", limit=2))
    # With hybrid off and no reranker, output equals dense search.
    assert [r["id"] for r in out] == ["a", "b"]


def test_hybrid_search_fuses_dense_and_bm25(monkeypatch):
    """Doc whose BM25 rank is high but dense rank is mediocre should
    bubble up via RRF fusion."""
    from document_processor.config.settings import settings

    monkeypatch.setattr(settings, "rag_hybrid_search_enabled", True)
    monkeypatch.setattr(settings, "rag_reranker_enabled", False)
    monkeypatch.setattr(settings, "rag_rrf_k", 1)  # tight k → ranks dominate

    # Dense ranks: a > b > c
    # BM25 query: "fox" — only c matches keyword.
    fake_results = [
        {"id": "a", "text": "alpha document", "score": 0.95},
        {"id": "b", "text": "beta paragraph", "score": 0.80},
        {"id": "c", "text": "the swift fox in autumn", "score": 0.60},
    ]
    store = _FakeStore(fake_results)
    out = _run(store.hybrid_search("fox", limit=3))
    # ``c`` benefits from BM25 rank=0 → strong RRF; should be in top 2.
    ids = [r["id"] for r in out]
    assert "c" in ids[:2]
    # Each result carries the breakdown.
    for r in out:
        assert "vector_score" in r
        assert "bm25_score" in r
        assert "vector_rank" in r
        assert "bm25_rank" in r


def test_hybrid_search_returns_empty_on_no_candidates(monkeypatch):
    from document_processor.config.settings import settings

    monkeypatch.setattr(settings, "rag_hybrid_search_enabled", True)
    monkeypatch.setattr(settings, "rag_reranker_enabled", False)

    store = _FakeStore([])
    out = _run(store.hybrid_search("anything", limit=5))
    assert out == []


# ─── _apply_reranker fail-soft path ───────────────────────────────


def test_apply_reranker_returns_input_when_dependency_missing(monkeypatch):
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore

    store = LanceDBVectorStore.__new__(LanceDBVectorStore)
    store._reranker = None
    store.device = "cpu"

    # If sentence-transformers isn't installed, rerank() inside
    # CrossEncoderReranker returns ``[(doc, 1/(i+1))]`` — i.e. it
    # gracefully falls through to original order.  We assert that
    # at minimum the same set of items comes out (not a crash).
    inputs = [
        {"id": "a", "text": "alpha", "score": 0.9},
        {"id": "b", "text": "beta", "score": 0.7},
    ]
    out = _run(store._apply_reranker("query", list(inputs)))
    assert {r["id"] for r in out} == {"a", "b"}


def test_apply_reranker_handles_empty_results():
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore

    store = LanceDBVectorStore.__new__(LanceDBVectorStore)
    store._reranker = None
    out = _run(store._apply_reranker("q", []))
    assert out == []
