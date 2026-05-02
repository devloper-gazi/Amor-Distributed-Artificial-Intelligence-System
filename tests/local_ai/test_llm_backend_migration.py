"""Migration smoke tests — Phase 16 Commit B.

Verifies that the inference call sites that previously issued
bespoke ``httpx.post(/api/generate)`` now delegate to the pluggable
backend when ``settings.llm_backend != "ollama"``.

The Ollama hot path is *not* touched by these tests — they specifically
swap in a ``StubBackend`` and confirm the non-Ollama branch fires.
"""

from __future__ import annotations

import asyncio

import pytest

from local_ai.llm_backend import (
    StubBackend,
    _reset_backend_cache,
    _set_backend,
)


def _run(coro):
    return asyncio.run(coro)


# ─── _call_ollama_uncached_with — local_ai_routes_simple.py ────────


def test_call_ollama_uncached_routes_to_stub_when_backend_is_not_ollama():
    """When the backend resolver returns a non-Ollama backend, the
    routing shim delegates ``backend.complete()``."""
    from document_processor.api.local_ai_routes_simple import (
        _call_ollama_uncached_with,
    )

    _reset_backend_cache()
    stub = StubBackend(responses=["routed-via-abstraction"])
    _set_backend("stub", stub)

    # Force the resolver to pick "stub" by setting llm_backend on
    # settings; we monkeypatch via direct attribute assignment.
    from document_processor.config.settings import settings
    original = getattr(settings, "llm_backend", None)
    try:
        settings.llm_backend = "stub"  # type: ignore[attr-defined]
        out = _run(_call_ollama_uncached_with(
            "qwen2.5:7b", "hello", system="be helpful", max_tokens=64,
        ))
        assert out == "routed-via-abstraction"
        # And the stub recorded the call.
        assert len(stub.calls) == 1
        assert stub.calls[0]["kind"] == "complete"
        assert stub.calls[0]["model"] == "qwen2.5:7b"
        assert stub.calls[0]["system"] == "be helpful"
    finally:
        if original is None:
            try:
                delattr(settings, "llm_backend")
            except AttributeError:
                pass
        else:
            settings.llm_backend = original  # type: ignore[attr-defined]
        _reset_backend_cache()


def test_call_ollama_uncached_falls_through_when_backend_is_ollama(monkeypatch):
    """When ``llm_backend`` is ``ollama`` (or unset) the routing
    shim must NOT touch ``backend.complete``; the original
    ``httpx.post(/api/generate)`` path runs."""
    from document_processor.api.local_ai_routes_simple import (
        _call_ollama_uncached_with,
    )

    _reset_backend_cache()
    # Inject a stub that would CRASH if called — proves it isn't.
    class CrashingStub(StubBackend):
        async def complete(self, *a, **k):
            raise AssertionError("ollama path must not delegate to abstraction")

    crashing = CrashingStub()
    _set_backend("ollama", crashing)

    # Patch _ensure_ollama_ready to avoid network + force the inner
    # httpx.post to a known surface.  We don't actually want the
    # call to succeed — we just want to prove the abstraction wasn't
    # invoked.  We catch HTTPException from the downstream Ollama
    # path.
    from document_processor.api import local_ai_routes_simple as mod

    async def _noop_ready():
        return {"ollama_available": True, "model_installed": True}

    monkeypatch.setattr(mod, "_ensure_ollama_ready", _noop_ready)

    # Block any outbound HTTP — we expect HTTPException from the
    # network failure, NOT the AssertionError from CrashingStub.
    import httpx as _httpx_mod

    class _BlockedClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            raise _httpx_mod.ConnectError("blocked-for-test")

    monkeypatch.setattr(mod, "httpx", type(
        "x", (), {
            "AsyncClient": _BlockedClient,
            "TimeoutException": _httpx_mod.TimeoutException,
            "ConnectError": _httpx_mod.ConnectError,
        },
    ))

    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _run(_call_ollama_uncached_with(
            "qwen2.5:7b", "hello", system=None, max_tokens=64,
        ))
    # CrashingStub.complete was never called — assertion didn't fire.
    _reset_backend_cache()


# ─── rag_engine synthesis routing ──────────────────────────────────


def test_rag_synthesis_routes_to_stub_when_backend_is_not_ollama():
    """The RAG synthesis call delegates to ``backend.complete()``
    when the backend isn't Ollama."""
    from document_processor.rag.rag_engine import (
        RAGConfig,
        RAGEngine,
        SearchResult,
    )

    _reset_backend_cache()
    stub = StubBackend(responses=["synthesis-via-abstraction"])
    _set_backend("stub", stub)

    from document_processor.config.settings import settings
    original = getattr(settings, "llm_backend", None)
    try:
        settings.llm_backend = "stub"  # type: ignore[attr-defined]

        cfg = RAGConfig()
        engine = RAGEngine(cfg)
        sources = [
            SearchResult(
                id="s1", text="The sky is blue.", score=0.9,
                title="Atlas", source_url="x",
            ),
        ]
        answer, score = _run(
            engine._synthesize_answer("What colour is the sky?", sources),
        )
        assert answer == "synthesis-via-abstraction"
        assert score == pytest.approx(0.9)
        assert len(stub.calls) == 1
        assert stub.calls[0]["kind"] == "complete"
    finally:
        if original is None:
            try:
                delattr(settings, "llm_backend")
            except AttributeError:
                pass
        else:
            settings.llm_backend = original  # type: ignore[attr-defined]
        _reset_backend_cache()


# ─── Singleton respects settings.llm_backend ──────────────────────


def test_get_backend_resolves_settings_value():
    """A user setting ``settings.llm_backend = 'stub'`` flips the
    singleton even when ``$AMOR_LLM_BACKEND`` is unset."""
    from document_processor.config.settings import settings
    from local_ai.llm_backend import get_backend

    _reset_backend_cache()
    original = getattr(settings, "llm_backend", None)
    try:
        settings.llm_backend = "stub"  # type: ignore[attr-defined]
        b = get_backend()
        assert b.name == "stub"
    finally:
        if original is None:
            try:
                delattr(settings, "llm_backend")
            except AttributeError:
                pass
        else:
            settings.llm_backend = original  # type: ignore[attr-defined]
        _reset_backend_cache()
