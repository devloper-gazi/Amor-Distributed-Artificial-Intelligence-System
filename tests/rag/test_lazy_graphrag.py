"""Cycle H Phase A.2 — LazyGraphRAG knowledge layer tests."""

from __future__ import annotations

import asyncio

import pytest

from document_processor.rag import lazy_graphrag as lg


# ─── Configuration ─────────────────────────────────────────────────


def test_config_defaults():
    cfg = lg.LazyGraphRAGConfig()
    assert cfg.enabled is False
    assert cfg.hierarchy_depth == 2
    assert cfg.relevance_budget == 500
    assert cfg.entity_min_length == 3
    assert cfg.community_min_size == 3


def test_load_config_from_settings_falls_back_when_settings_missing(monkeypatch):
    """Import-failure path should give the defaults, not crash.
    Use monkeypatch.setitem so pytest auto-restores after the test
    (avoiding sys.modules pollution that would break later tests
    importing `document_processor.config.settings`)."""
    import sys
    monkeypatch.setitem(sys.modules, "document_processor.config.settings", None)
    cfg = lg.load_config_from_settings()
    assert cfg.enabled is False


def test_load_config_reads_settings_overrides(monkeypatch):
    from document_processor.config.settings import settings
    monkeypatch.setattr(settings, "rag_graphrag_enabled", True, raising=False)
    monkeypatch.setattr(settings, "rag_graphrag_relevance_budget", 250, raising=False)
    cfg = lg.load_config_from_settings()
    assert cfg.enabled is True
    assert cfg.relevance_budget == 250


# ─── Entity extraction ─────────────────────────────────────────────


def test_extract_entities_snake_case():
    text = "def calculate_total(items_list): return sum_values(items_list)"
    entities = lg.extract_entities(text)
    assert "calculate_total" in entities
    assert "items_list" in entities
    assert "sum_values" in entities


def test_extract_entities_camel_case():
    text = "class UserAccount extends BaseModel { renderHtml() {} }"
    entities = lg.extract_entities(text)
    assert "UserAccount" in entities
    assert "BaseModel" in entities


def test_extract_entities_dotted_paths():
    text = "from document_processor.code_intelligence.engine import CodeIntelligenceEngine"
    entities = lg.extract_entities(text)
    # Snake_case parts get matched too
    assert "document_processor" in entities
    assert "CodeIntelligenceEngine" in entities


def test_extract_entities_filters_short():
    text = "x ab cd long_name short"
    entities = lg.extract_entities(text, min_length=4)
    assert "long_name" in entities
    # Single-segment "short" doesn't match snake_case ≥2 rule


def test_extract_entities_deduplicates_case_insensitive():
    text = "FooBar foo_bar fooBar FooBar"
    entities = lg.extract_entities(text)
    # Different identifier shapes but lowercased-deduped → 2 unique
    assert len(entities) == 2


def test_extract_entities_handles_empty():
    assert lg.extract_entities("") == []
    assert lg.extract_entities(None) == []  # type: ignore


# ─── Graph + community detection ───────────────────────────────────


def _make_chunks():
    """Three chunks with overlapping entity sets for community tests."""
    return [
        {
            "source_id": "src/auth.py",
            "text": "class UserAccount: def authenticate_user(): pass",
        },
        {
            "source_id": "src/login.py",
            "text": "from auth import UserAccount; authenticate_user()",
        },
        {
            "source_id": "src/database.py",
            "text": "class DatabaseConnection: def execute_query(): pass",
        },
        {
            "source_id": "src/models.py",
            "text": "from database import DatabaseConnection; execute_query()",
        },
    ]


def test_build_entity_graph_inverted_index():
    chunks = _make_chunks()
    inv = lg.build_entity_graph(chunks)
    # UserAccount appears in 2 sources
    assert sorted(inv.get("UserAccount") or []) == sorted(["src/auth.py", "src/login.py"])
    # DatabaseConnection appears in 2 sources
    assert sorted(inv.get("DatabaseConnection") or []) == sorted(["src/database.py", "src/models.py"])


def test_detect_communities_finds_auth_cluster():
    """auth.py + login.py share UserAccount AND authenticate_user → community."""
    chunks = _make_chunks()
    inv = lg.build_entity_graph(chunks)
    communities = lg.detect_communities(inv, min_size=2)
    assert len(communities) >= 1
    # Find the community containing both auth and login files
    auth_community = next(
        (c for c in communities if "src/auth.py" in c["members"] and "src/login.py" in c["members"]),
        None,
    )
    assert auth_community is not None
    assert "UserAccount" in (auth_community.get("top_entities") or [])


def test_detect_communities_filters_below_min_size():
    """Singleton communities (< min_size) get dropped."""
    chunks = [
        {"source_id": "lone.py", "text": "class IsolatedClass: def alone_method(): pass"},
    ]
    inv = lg.build_entity_graph(chunks)
    communities = lg.detect_communities(inv, min_size=3)
    assert communities == []


def test_detect_communities_stable_ids_across_runs():
    """Hash-derived community ID must be deterministic across runs
    (so re-indexing same corpus produces same community IDs)."""
    chunks = _make_chunks()
    inv = lg.build_entity_graph(chunks)
    first_run = lg.detect_communities(inv, min_size=2)
    second_run = lg.detect_communities(inv, min_size=2)
    assert sorted(c["id"] for c in first_run) == sorted(c["id"] for c in second_run)


# ─── Relevance filter ──────────────────────────────────────────────


def test_relevance_score_jaccard_basic():
    """3 query entities; community has 2 of them → 2/3 = 0.66 jaccard."""
    score = lg._relevance_score(
        ["foo", "bar", "baz"], ["foo", "bar", "qux", "quux"],
    )
    # |{foo,bar}| / |{foo,bar,baz,qux,quux}| = 2/5 = 0.4
    assert abs(score - 0.4) < 0.001


def test_relevance_score_empty_returns_zero():
    assert lg._relevance_score([], ["a", "b"]) == 0.0
    assert lg._relevance_score(["a"], []) == 0.0


def test_relevance_score_perfect_match():
    score = lg._relevance_score(["a", "b"], ["a", "b"])
    assert score == 1.0


def test_filter_communities_by_relevance_ranks_correctly():
    """Higher-Jaccard community should come first in the ranked
    output; zero-Jaccard communities get dropped entirely."""
    communities = [
        {"id": "c1", "top_entities": ["UserAccount", "authenticate_user"]},
        {"id": "c2", "top_entities": ["unrelated", "stuff"]},
        {"id": "c3", "top_entities": ["UserAccount"]},
    ]
    ranked = lg.filter_communities_by_relevance(
        "how does UserAccount authenticate_user", communities, top_k=3,
    )
    ids = [c["id"] for c, _score in ranked]
    assert "c1" in ids
    assert "c3" in ids
    # c2 has no entity overlap → dropped
    assert "c2" not in ids
    # c1 has more entity overlap than c3
    assert ids.index("c1") < ids.index("c3")


def test_filter_communities_top_k_caps_result_count():
    communities = [
        {"id": f"c{i}", "top_entities": ["UserAccount"]}
        for i in range(20)
    ]
    ranked = lg.filter_communities_by_relevance(
        "UserAccount overview", communities, top_k=5,
    )
    assert len(ranked) == 5


# ─── End-to-end build_index + query ────────────────────────────────


def test_build_index_no_llm_calls():
    """Indexing is LLM-free.  Stats reflect the lazy posture.
    Use explicit config with community_min_size=2 because the
    4-file fixture produces 2-member auth/db clusters."""
    chunks = _make_chunks()
    cfg = lg.LazyGraphRAGConfig(community_min_size=2)
    inv_index, communities, stats = lg.build_index(chunks, config=cfg)
    assert stats.documents_indexed == 4
    assert stats.entities_extracted > 0
    assert stats.communities_detected >= 1
    assert stats.wall_clock_s >= 0


def test_query_without_summariser_returns_findings_with_no_snippets():
    """When `summariser` is None (or budget=0), findings come back
    with empty snippets but with entity overlap + scores."""
    chunks = _make_chunks()
    cfg = lg.LazyGraphRAGConfig(community_min_size=2)
    inv_index, communities, _stats = lg.build_index(chunks, config=cfg)

    async def driver():
        return await lg.query(
            "how does UserAccount authenticate",
            inv_index, communities,
            summariser=None,
            top_k=3,
        )

    findings = asyncio.run(driver())
    assert len(findings) >= 1
    # At least one finding mentions UserAccount or the auth files
    assert any(
        "UserAccount" in f.entities or "src/auth.py" in f.source_id
        for f in findings
    )
    # No LLM calls made → no snippets
    assert all(f.snippet == "" for f in findings)


def test_query_with_summariser_consumes_budget():
    """When `summariser` is supplied + budget > 0, each community
    survives the Jaccard filter consumes one LLM call."""
    chunks = _make_chunks()
    inv_index, communities, _stats = lg.build_index(chunks)

    llm_call_count = {"n": 0}

    async def stub_summariser(query, members):
        llm_call_count["n"] += 1
        return f"Summary of {','.join(members)} re: {query}"

    cfg = lg.LazyGraphRAGConfig(relevance_budget=5, community_min_size=2)
    # Re-detect with smaller min_size because the fixture has 2-member clusters
    communities = lg.detect_communities(inv_index, min_size=2)

    async def driver():
        return await lg.query(
            "UserAccount authentication flow",
            inv_index, communities,
            config=cfg, summariser=stub_summariser, top_k=3,
        )

    findings = asyncio.run(driver())
    assert llm_call_count["n"] > 0
    # At least one finding has a non-empty snippet
    assert any(f.snippet for f in findings)


def test_query_respects_budget_cap():
    """Budget cap prevents runaway LLM costs even when many
    communities pass the Jaccard filter."""
    chunks = _make_chunks()
    inv_index = lg.build_entity_graph(chunks)
    communities = lg.detect_communities(inv_index, min_size=2)

    llm_calls = {"n": 0}

    async def stub_summariser(query, members):
        llm_calls["n"] += 1
        return "summary"

    cfg = lg.LazyGraphRAGConfig(relevance_budget=1)

    async def driver():
        return await lg.query(
            "UserAccount DatabaseConnection",
            inv_index, communities,
            config=cfg, summariser=stub_summariser, top_k=10,
        )

    findings = asyncio.run(driver())
    # Budget is 1 → at most 1 LLM call
    assert llm_calls["n"] <= 1


# ─── Cache key + signature ─────────────────────────────────────────


def test_content_hash_stable():
    h1 = lg.content_hash("hello world")
    h2 = lg.content_hash("hello world")
    assert h1 == h2 and len(h1) == 16
    assert lg.content_hash("hello world") != lg.content_hash("hello world!")


def test_stable_index_signature_detects_corpus_change():
    """When the corpus changes (even one chunk), the signature must
    differ so the cache invalidation logic in LanceDB metadata table
    triggers a rebuild."""
    chunks_a = _make_chunks()
    chunks_b = [{**c} for c in chunks_a]
    chunks_b[0]["text"] = chunks_b[0]["text"] + "  # added comment"
    cfg = lg.LazyGraphRAGConfig()
    sig_a = lg.stable_index_signature(chunks_a, cfg)
    sig_b = lg.stable_index_signature(chunks_b, cfg)
    assert sig_a != sig_b


def test_stable_index_signature_detects_config_change():
    """Same corpus + different config (e.g. depth or min_length)
    must produce different signatures."""
    chunks = _make_chunks()
    sig_default = lg.stable_index_signature(
        chunks, lg.LazyGraphRAGConfig(),
    )
    sig_deeper = lg.stable_index_signature(
        chunks, lg.LazyGraphRAGConfig(hierarchy_depth=4),
    )
    assert sig_default != sig_deeper


def test_stable_index_signature_idempotent():
    chunks = _make_chunks()
    cfg = lg.LazyGraphRAGConfig()
    assert lg.stable_index_signature(chunks, cfg) == lg.stable_index_signature(chunks, cfg)


# ─── Finding serialization ─────────────────────────────────────────


def test_finding_to_dict_shape():
    f = lg.GraphRAGFinding(
        source_id="src/auth.py", community_id="abc123",
        score=0.85, snippet="x" * 800,    # over 500 — must be capped
        entities=["UserAccount", "auth"],
        metadata={"size": 3},
    )
    d = f.to_dict()
    assert d["source_id"] == "src/auth.py"
    assert d["community_id"] == "abc123"
    assert d["score"] == 0.85
    assert len(d["snippet"]) == 500   # capped
    assert d["entities"] == ["UserAccount", "auth"]
    assert d["metadata"] == {"size": 3}
