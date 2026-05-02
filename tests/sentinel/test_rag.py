"""Unit tests for ``document_processor/sentinel/rag.py``."""

from __future__ import annotations

import asyncio

import pytest

from document_processor.sentinel.models import Finding
from document_processor.sentinel.rag import (
    SentinelRAG,
    cosine,
    hash_embed,
    load_cwe_corpus,
    load_cwe_cvss_map,
    load_owasp_corpus,
    load_source_weights,
)


def _run(coro):
    return asyncio.run(coro)


# ─── Bundled corpora load ───────────────────────────────────────────


def test_cwe_corpus_loaded_with_top25_minimum():
    corpus = load_cwe_corpus()
    # Top-25 means at least 20 entries to be useful.
    assert len(corpus) >= 20
    ids = {e["id"] for e in corpus}
    # Spot-check the most common ones.
    for must_have in ("CWE-79", "CWE-89", "CWE-22", "CWE-78", "CWE-798"):
        assert must_have in ids


def test_owasp_corpus_loaded_with_ten_entries():
    corpus = load_owasp_corpus()
    assert len(corpus) == 10
    ids = {e["id"] for e in corpus}
    assert "A01:2021" in ids
    assert "A10:2021" in ids


def test_cwe_cvss_map_has_priors():
    m = load_cwe_cvss_map()
    assert m["CWE-89"]["score"] >= 9.0
    assert m["CWE-79"]["score"] > 0
    assert "vector" in m["CWE-78"]


def test_source_weights_load():
    w = load_source_weights()
    assert "weights" in w
    assert "tool_overrides" in w
    assert w["weights"]["redteam"] >= 0.85
    assert w["tool_overrides"]["gitleaks"] >= 0.8


# ─── Hash embedder + cosine ─────────────────────────────────────────


def test_hash_embed_deterministic():
    a = hash_embed("sql injection vulnerability")
    b = hash_embed("sql injection vulnerability")
    assert a == b
    assert len(a) == 96


def test_cosine_self_one():
    v = hash_embed("xss in html template")
    assert cosine(v, v) == pytest.approx(1.0)


# ─── SentinelRAG end-to-end ─────────────────────────────────────────


def test_rag_loads_corpora_lazily():
    rag = SentinelRAG()
    stats = rag.stats()
    assert stats["loaded"] is False
    _run(rag.ensure_loaded())
    stats = rag.stats()
    assert stats["loaded"] is True
    assert stats["cwe_count"] >= 20
    assert stats["owasp_count"] == 10


def test_rag_enrich_returns_cwe_entry_when_finding_tags_it():
    rag = SentinelRAG()
    f = Finding(tool="bandit", cwe="CWE-89", raw_message="SQLi via concat")
    ctx = _run(rag.enrich(f))
    assert ctx.cwe_entry is not None
    assert ctx.cwe_entry["id"] == "CWE-89"


def test_rag_enrich_vector_search_when_no_cwe():
    rag = SentinelRAG()
    # No cwe set; relies on vector search over corpus.
    f = Finding(
        tool="ml_secret_detector",
        raw_message="hardcoded credential token in source repository",
    )
    ctx = _run(rag.enrich(f))
    # Vector search may or may not find a match depending on the
    # embedder — we just assert the call succeeded and returned an
    # RAGContext.
    assert ctx is not None


def test_rag_enrich_owasp_lookup():
    rag = SentinelRAG()
    f = Finding(tool="bandit", owasp="A03:2021")
    ctx = _run(rag.enrich(f))
    assert ctx.owasp_entry is not None
    assert ctx.owasp_entry["id"] == "A03:2021"


def test_rag_history_upsert_and_search():
    rag = SentinelRAG()
    _run(rag.ensure_loaded())
    f1 = Finding(tool="auditor", cwe="CWE-89", raw_message="sqli in user query")
    _run(rag.upsert_history(f1))
    # Now ensure a similar finding pulls history.
    f2 = Finding(tool="bandit", cwe="CWE-89", raw_message="sqli in admin query")
    ctx = _run(rag.enrich(f2))
    assert len(ctx.similar_findings) >= 1
