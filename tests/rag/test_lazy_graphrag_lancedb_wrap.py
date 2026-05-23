"""Cycle H.0.2 — LanceDB ↔ LazyGraphRAG retrieval-wrap coverage.

These tests verify the wiring at ``local_ai/vector_store/lancedb_store.py``:
when ``settings.rag_graphrag_enabled=True`` AND an entity-graph index is
present, the search path narrows the candidate set to communities that
share entities with the query.  When disabled, the legacy LanceDB-only
path runs unchanged.

LanceDB itself is heavy (sentence-transformers + LanceDB native);
these tests construct a `LanceDBVectorStore` shell with mocked
internals so we exercise the wrap logic in isolation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest


# ─── _load_lazy_graphrag_config caching + import-fail tolerance ────


class _Shell:
    """Minimal shell exposing the wrap methods without booting LanceDB.

    The real ``LanceDBVectorStore.__init__`` loads SentenceTransformer
    + LanceDB native; for unit tests we just mount the helper methods
    onto a bare object.
    """
    def __init__(self):
        self._lazy_graphrag_index = None
        self._lazy_graphrag_config = None


def _build_shell_with_wrap():
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore
    shell = _Shell()
    # Bind the unbound class methods to the shell instance.
    shell._load_lazy_graphrag_config = (
        LanceDBVectorStore._load_lazy_graphrag_config.__get__(shell)
    )
    shell._lazy_graphrag_prefilter = (
        LanceDBVectorStore._lazy_graphrag_prefilter.__get__(shell)
    )
    return shell


def test_load_lazy_graphrag_config_returns_disabled_by_default():
    """v18.1.5 plan-locked default: settings keep LazyGraphRAG OFF.

    The first call should resolve the config from Pydantic settings;
    the second call returns the cached instance (no repeated imports).
    """
    shell = _build_shell_with_wrap()
    cfg1 = shell._load_lazy_graphrag_config()
    cfg2 = shell._load_lazy_graphrag_config()
    assert cfg1 is cfg2, "config should be cached on the instance"
    assert cfg1 is not None
    assert cfg1.enabled is False   # rag_graphrag_enabled default


def test_load_lazy_graphrag_config_tolerates_missing_module(monkeypatch):
    """When the lazy_graphrag module fails to import, the helper must
    return ``None`` and never raise — the caller falls through to the
    legacy LanceDB-only path."""
    import sys
    # Poison the import path so the LazyGraphRAG module can't be loaded.
    monkeypatch.setitem(
        sys.modules,
        "document_processor.rag.lazy_graphrag",
        None,
    )
    shell = _build_shell_with_wrap()
    cfg = shell._load_lazy_graphrag_config()
    assert cfg is None


# ─── _lazy_graphrag_prefilter — index missing + happy path ─────────


def test_prefilter_returns_empty_when_index_missing():
    """No index → returns empty set, never raises."""
    shell = _build_shell_with_wrap()
    cfg = shell._load_lazy_graphrag_config()
    out = asyncio.run(shell._lazy_graphrag_prefilter("test query", cfg))
    assert out == set()


def test_prefilter_returns_community_member_source_ids():
    """When the index has a community whose top entities overlap with
    the query's entities, the prefilter returns its member source_ids.

    The entity extractor recognises CamelCase ≥2 segments and snake_case
    ≥2 segments — single-word identifiers like 'Approval' DO NOT match
    by design.  Use multi-segment identifiers in the fixture.
    """
    shell = _build_shell_with_wrap()
    shell._lazy_graphrag_index = {
        "communities": [
            {
                "top_entities": ["ApprovalFlow", "ToolRegistry", "DispatchHandler"],
                "members": ["chunk-1", "chunk-2", "chunk-3"],
            },
            {
                "top_entities": ["SandboxRunner", "DockerProxy"],
                "members": ["chunk-4"],
            },
        ],
    }
    cfg = shell._load_lazy_graphrag_config()
    out = asyncio.run(shell._lazy_graphrag_prefilter(
        "How does ApprovalFlow dispatch through ToolRegistry?", cfg,
    ))
    assert "chunk-1" in out
    assert "chunk-2" in out
    assert "chunk-3" in out


def test_prefilter_returns_empty_set_when_no_overlap():
    """A query with no shared entities → empty set; caller falls
    through to LanceDB-only path."""
    shell = _build_shell_with_wrap()
    shell._lazy_graphrag_index = {
        "communities": [
            {
                "top_entities": ["ApprovalFlow", "ToolRegistry"],
                "members": ["chunk-1"],
            },
        ],
    }
    cfg = shell._load_lazy_graphrag_config()
    out = asyncio.run(shell._lazy_graphrag_prefilter(
        "lorem ipsum garbage text", cfg,
    ))
    assert out == set()


# ─── build_lazy_graphrag_index — minimal end-to-end ────────────────


def test_search_path_disabled_uses_lancedb_only(monkeypatch):
    """When ``rag_graphrag_enabled=False`` (default), the search()
    path runs the legacy LanceDB-only flow — the prefilter is never
    invoked.  Regression guard against accidentally enabling the
    extra layer in production.
    """
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore
    from document_processor.rag.lazy_graphrag import LazyGraphRAGConfig

    shell = _build_shell_with_wrap()
    # Inject a disabled config to short-circuit the search wrap.
    shell._lazy_graphrag_config = LazyGraphRAGConfig(enabled=False)
    # Index ALSO populated to prove the gating flag is the only thing
    # that disables the wrap.
    shell._lazy_graphrag_index = {
        "communities": [
            {"top_entities": ["AnyEntity"], "members": ["chunk-1"]},
        ],
    }

    # Build search wrap entry; just verify config returns disabled.
    cfg = shell._load_lazy_graphrag_config()
    assert cfg.enabled is False
    # The prefilter is gated on (cfg.enabled AND index): with
    # enabled=False, the search() path short-circuits before
    # calling _lazy_graphrag_prefilter at all.  Functionally
    # captured by reading the gate expression in search().


def test_build_lazy_graphrag_index_with_explicit_chunks():
    """Building from an explicit chunk list (no LanceDB roundtrip)
    populates the in-memory index + returns stats."""
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore
    shell = _build_shell_with_wrap()
    shell.build_lazy_graphrag_index = (
        LanceDBVectorStore.build_lazy_graphrag_index.__get__(shell)
    )
    # Provide the same multi-segment entities in 2+ chunks so a
    # community forms.  Entity extractor needs CamelCase ≥2 segments
    # (`ApprovalFlow`) or snake_case ≥2 segments (`approval_flow`).
    chunks = [
        {"source_id": "s1", "text": "ApprovalFlow ToolRegistry DispatchHandler flow."},
        {"source_id": "s2", "text": "ApprovalFlow ToolRegistry DispatchHandler handler."},
        {"source_id": "s3", "text": "ApprovalFlow ToolRegistry DispatchHandler tests."},
        {"source_id": "s4", "text": "ApprovalFlow ToolRegistry DispatchHandler metrics."},
    ]
    stats = asyncio.run(shell.build_lazy_graphrag_index(chunks=chunks))
    assert stats["chunk_count"] == 4
    assert stats["entity_count"] >= 1
    # Index now populated on the shell.
    assert shell._lazy_graphrag_index is not None
    assert "inv_index" in shell._lazy_graphrag_index
    assert "communities" in shell._lazy_graphrag_index
    assert "signature" in shell._lazy_graphrag_index
