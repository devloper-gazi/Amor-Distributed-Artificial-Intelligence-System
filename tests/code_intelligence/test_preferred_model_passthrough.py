"""Tests for the preferred_model override pipe (More settings → AI Model).

The override flows:
  Frontend  →  request body `preferred_model`
            →  session payload `preferred_model`
            →  ContextVar `_ACTIVE_MODEL`
            →  call_ollama_with → cache key + Ollama API request

These tests exercise the contextvar + cache-key + resolver paths
without needing a real Ollama. The HTTP path itself is covered by
the existing test_capability_discoverer / test_e2e_pipeline files.
"""

from __future__ import annotations

import pytest

from document_processor.api.local_ai_routes_simple import (
    _ACTIVE_MODEL,
    OLLAMA_MODEL,
    _llm_cache_key,
    _resolve_model,
    set_active_model,
)

# ── _resolve_model ────────────────────────────────────────────────────


def test_resolve_model_none_falls_back_to_default():
    # Ensure the contextvar is empty for this test.
    token = _ACTIVE_MODEL.set(None)
    try:
        assert _resolve_model(None) == OLLAMA_MODEL
        assert _resolve_model("") == OLLAMA_MODEL
        assert _resolve_model("   ") == OLLAMA_MODEL
    finally:
        _ACTIVE_MODEL.reset(token)


def test_resolve_model_explicit_wins():
    token = _ACTIVE_MODEL.set(None)
    try:
        assert _resolve_model("qwen2.5-coder:7b") == "qwen2.5-coder:7b"
        # Strips whitespace
        assert _resolve_model("  qwen2.5-coder:7b  ") == "qwen2.5-coder:7b"
    finally:
        _ACTIVE_MODEL.reset(token)


def test_resolve_model_contextvar_used_when_no_explicit():
    token = _ACTIVE_MODEL.set("qwen2.5-coder:32b")
    try:
        assert _resolve_model(None) == "qwen2.5-coder:32b"
    finally:
        _ACTIVE_MODEL.reset(token)


def test_resolve_model_explicit_overrides_contextvar():
    """Explicit param to call_ollama_with beats the ambient ContextVar."""
    token = _ACTIVE_MODEL.set("qwen2.5-coder:32b")
    try:
        assert _resolve_model("devstral:24b") == "devstral:24b"
    finally:
        _ACTIVE_MODEL.reset(token)


# ── set_active_model ──────────────────────────────────────────────────


def test_set_active_model_returns_token_and_updates_contextvar():
    initial = _ACTIVE_MODEL.set(None)
    try:
        assert _ACTIVE_MODEL.get() is None
        token = set_active_model("qwen2.5-coder:7b")
        assert _ACTIVE_MODEL.get() == "qwen2.5-coder:7b"
        _ACTIVE_MODEL.reset(token)
        assert _ACTIVE_MODEL.get() is None
    finally:
        _ACTIVE_MODEL.reset(initial)


def test_set_active_model_empty_clears_override():
    initial = _ACTIVE_MODEL.set(None)
    try:
        set_active_model("qwen2.5-coder:7b")
        assert _ACTIVE_MODEL.get() == "qwen2.5-coder:7b"
        # Empty string → cleared (treated as None)
        set_active_model("")
        assert _ACTIVE_MODEL.get() is None
    finally:
        _ACTIVE_MODEL.reset(initial)


# ── Cache key — model-aware ───────────────────────────────────────────


def test_cache_key_differs_per_model():
    """Two different model tags must produce different cache keys —
    same prompt with different models can return different output, so
    the cache MUST not collide."""
    token = _ACTIVE_MODEL.set(None)
    try:
        k1 = _llm_cache_key("hello", None, 100, "qwen2.5:7b")
        k2 = _llm_cache_key("hello", None, 100, "qwen2.5-coder:7b")
        assert k1 != k2
    finally:
        _ACTIVE_MODEL.reset(token)


def test_cache_key_same_for_default_and_explicit_default():
    """Passing OLLAMA_MODEL explicitly must hash identically to passing
    None (which also resolves to OLLAMA_MODEL). Otherwise the
    backwards-compat call_ollama path would miss the cache after the
    refactor."""
    token = _ACTIVE_MODEL.set(None)
    try:
        k_none = _llm_cache_key("hello", None, 100, None)
        k_explicit = _llm_cache_key("hello", None, 100, OLLAMA_MODEL)
        assert k_none == k_explicit
    finally:
        _ACTIVE_MODEL.reset(token)


def test_cache_key_deterministic():
    token = _ACTIVE_MODEL.set(None)
    try:
        k1 = _llm_cache_key("hello world", "be helpful", 200, "devstral:24b")
        k2 = _llm_cache_key("hello world", "be helpful", 200, "devstral:24b")
        assert k1 == k2
    finally:
        _ACTIVE_MODEL.reset(token)


# ── Pydantic request models accept the new field ─────────────────────


def test_local_ai_research_request_accepts_preferred_model():
    from document_processor.api.local_ai_routes_simple import (
        LocalAIResearchRequest,
    )

    req = LocalAIResearchRequest(
        topic="x",
        preferred_model="qwen2.5-coder:7b",
    )
    assert req.preferred_model == "qwen2.5-coder:7b"
    # Field is optional — omitting it defaults to None
    req2 = LocalAIResearchRequest(topic="y")
    assert req2.preferred_model is None


def test_think_request_accepts_preferred_model():
    from document_processor.thinking.models import ThinkRequest

    req = ThinkRequest(
        prompt="explain async",
        preferred_model="devstral:24b",
    )
    assert req.preferred_model == "devstral:24b"
    req2 = ThinkRequest(prompt="explain async")
    assert req2.preferred_model is None


def test_preferred_model_max_length_enforced():
    from pydantic import ValidationError

    from document_processor.api.local_ai_routes_simple import (
        LocalAIResearchRequest,
    )

    long_tag = "x" * 200  # > 120 max_length
    with pytest.raises(ValidationError):
        LocalAIResearchRequest(topic="x", preferred_model=long_tag)
