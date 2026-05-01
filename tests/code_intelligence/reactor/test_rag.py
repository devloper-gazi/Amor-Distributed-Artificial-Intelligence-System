"""
Tests for CodeCorpusRAG — retrieval + similarity floor + prompt
formatting. Vector store is a list-shim so no LanceDB required.
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence.reactor.rag import (
    CodeCorpusRAG,
    CorpusPattern,
    RetrievalResult,
)


class _ListStore:
    """Minimal vector store shim — assigns a fixed score per row."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.searches: list[tuple[list[float], int]] = []

    def search(self, vec, k):
        self.searches.append((list(vec), k))
        return list(self._rows[:k])


def _embed(s: str):
    return [1.0, 0.0, 0.0]  # constant — store decides the scores


# ── retrieve happy path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_retrieve_returns_only_above_floor():
    store = _ListStore([
        {"pattern": "merge_sort", "code": "def f(): pass",
         "complexity_claim": "O(n log n)", "summary": "ms",
         "score": 0.9},
        {"pattern": "bubble_sort", "code": "def f(): pass",
         "complexity_claim": "O(n^2)", "summary": "bs",
         "score": 0.4},  # below 0.55 floor
    ])
    rag = CodeCorpusRAG(
        vector_store=store, embedder=_embed,
        top_k=3, similarity_floor=0.55,
    )
    res = await rag.retrieve("sort a list")
    assert isinstance(res, RetrievalResult)
    assert len(res.patterns) == 1
    assert res.patterns[0].pattern == "merge_sort"


@pytest.mark.asyncio
async def test_retrieve_passes_top_k_to_store():
    store = _ListStore([])
    rag = CodeCorpusRAG(
        vector_store=store, embedder=_embed, top_k=5, similarity_floor=0.0,
    )
    await rag.retrieve("x")
    assert store.searches[0][1] == 5


@pytest.mark.asyncio
async def test_retrieve_empty_prompt_fails_softly():
    rag = CodeCorpusRAG(vector_store=_ListStore([]), embedder=_embed)
    res = await rag.retrieve("")
    assert res.failed
    assert "empty" in res.failure_reason.lower()


@pytest.mark.asyncio
async def test_retrieve_handles_embed_failure():
    def bad_embed(s):
        raise RuntimeError("embed down")

    rag = CodeCorpusRAG(vector_store=_ListStore([]), embedder=bad_embed)
    res = await rag.retrieve("x")
    assert res.failed
    assert "embed" in res.failure_reason.lower()


@pytest.mark.asyncio
async def test_retrieve_handles_search_failure():
    class _BadStore:
        def search(self, *a, **kw):
            raise RuntimeError("lance down")

    rag = CodeCorpusRAG(vector_store=_BadStore(), embedder=_embed)
    res = await rag.retrieve("x")
    assert res.failed
    assert "search" in res.failure_reason.lower()


@pytest.mark.asyncio
async def test_retrieve_skips_non_dict_rows():
    store = _ListStore([
        {"pattern": "ok", "score": 0.9, "code": "x", "summary": "y",
         "complexity_claim": "O(1)"},
        "not a dict",  # type: ignore[list-item]
    ])
    rag = CodeCorpusRAG(vector_store=store, embedder=_embed,
                         similarity_floor=0.0)
    res = await rag.retrieve("x")
    assert len(res.patterns) == 1


# ── format_for_prompt ───────────────────────────────────────────


def test_format_returns_empty_string_when_no_patterns():
    assert CodeCorpusRAG.format_for_prompt([]) == ""


def test_format_includes_anti_overfit_framing():
    p = CorpusPattern(
        pattern="merge_sort", code="def ms(): pass",
        complexity_claim="O(n log n)", summary="merge halves",
    )
    rendered = CodeCorpusRAG.format_for_prompt([p])
    assert "RIFF" in rendered
    assert "DON'T COPY" in rendered
    assert "merge_sort" in rendered
    assert "O(n log n)" in rendered


def test_format_includes_code_block_when_present():
    p = CorpusPattern(pattern="x", code="def f(): pass")
    rendered = CodeCorpusRAG.format_for_prompt([p])
    assert "```python" in rendered
    assert "def f(): pass" in rendered


# ── from_dict / to_dict round trip ──────────────────────────────


def test_corpus_pattern_round_trip():
    src = {
        "pattern": "x", "code": "y", "complexity_claim": "O(1)",
        "summary": "s", "score": 0.7, "topic": "t", "language": "python",
    }
    p = CorpusPattern.from_dict(src)
    d = p.to_dict()
    for k in ("pattern", "code", "complexity_claim", "summary",
              "topic", "language"):
        assert d[k] == src[k]
    assert d["score"] == pytest.approx(0.7)
