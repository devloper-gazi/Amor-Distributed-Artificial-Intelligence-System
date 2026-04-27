"""
Spec validation point #1 — every AI start endpoint must surface the
resolved Ollama tag in the response via ``X-Model-Used: <tag>``.

Three start endpoints are covered:
  · POST /api/local-ai/research      → research mode
  · POST /api/thinking/think          → thinking mode
  · POST /api/code/start              → code intelligence mode

Each endpoint goes through ``resolve_request_model_full`` and stamps
the response. Code Intelligence is special: when the user has no
explicit pref, the engine picks a different tag per agent role at
runtime, so the header lands on a sentinel ``auto:per-role``.

These tests stub at the import boundary — no Ollama, no Mongo, no
Redis, no real auth. The point is to verify the wire contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def fake_user():
    """Bypass auth — the start endpoints require a User but we don't
    care which one."""
    from document_processor.auth.models import User
    return User(
        id="test-user-1",
        username="tester",
        email="t@example.com",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        is_active=True,
    )


# ─── /api/local-ai/research ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_research_start_emits_x_model_used(monkeypatch, fake_user):
    """The header carries the tag returned by resolve_request_model_full."""
    from document_processor.api import local_ai_routes_simple as la

    # No-op the Ollama readiness probe and the bg persist call.
    async def _noop(*a, **kw): return {"model_installed": True}
    monkeypatch.setattr(la, "_ensure_ollama_ready", _noop)
    monkeypatch.setattr(la, "_persist_session", AsyncMock(return_value=None))

    # Resolver returns a known tag. Three-tuple (tag, profile, reason).
    async def fake_resolve(**kwargs):
        return ("qwen2.5-coder:7b", None, "user preference (research)")
    monkeypatch.setattr(la, "resolve_request_model_full", fake_resolve)

    # Replace the auth dependency so the test client doesn't need a JWT.
    app = FastAPI()
    app.include_router(la.router)
    from document_processor.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: fake_user

    # The route adds a background task — the TestClient still runs it,
    # so we patch the worker to a no-op too.
    monkeypatch.setattr(la, "execute_advanced_research", AsyncMock(return_value=None))

    client = TestClient(app)
    resp = client.post(
        "/api/local-ai/research",
        headers={"X-Client-Id": "test-cli"},
        json={"topic": "smoke test the header", "depth": "basic"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("X-Model-Used") == "qwen2.5-coder:7b"


@pytest.mark.asyncio
async def test_research_start_falls_back_to_default(monkeypatch, fake_user):
    """When the resolver throws, the header falls back to OLLAMA_MODEL."""
    from document_processor.api import local_ai_routes_simple as la

    async def _ok(*a, **kw): return {"model_installed": True}
    monkeypatch.setattr(la, "_ensure_ollama_ready", _ok)
    monkeypatch.setattr(la, "_persist_session", AsyncMock(return_value=None))
    monkeypatch.setattr(la, "execute_advanced_research", AsyncMock(return_value=None))

    async def boom(**kwargs):
        raise RuntimeError("resolver down")
    monkeypatch.setattr(la, "resolve_request_model_full", boom)

    app = FastAPI()
    app.include_router(la.router)
    from document_processor.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: fake_user

    client = TestClient(app)
    resp = client.post(
        "/api/local-ai/research",
        headers={"X-Client-Id": "test-cli"},
        json={"topic": "fallback path", "depth": "medium",
              "preferred_model": None},
    )
    assert resp.status_code == 200
    # When the resolver fails we set resolved_model = request.preferred_model
    # which is None here, so the header lands on OLLAMA_MODEL.
    assert resp.headers.get("X-Model-Used") == la.OLLAMA_MODEL


# ─── /api/thinking/think ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_thinking_start_emits_x_model_used(monkeypatch, fake_user):
    from document_processor.api import thinking_routes as th

    async def fake_resolve(**kwargs):
        return ("qwen2.5:7b", None, "user preference (thinking)")
    monkeypatch.setattr(th, "resolve_request_model_full", fake_resolve)

    # The route also calls _persist + background _run_session — stub.
    monkeypatch.setattr(th, "_persist", AsyncMock(return_value=None))
    monkeypatch.setattr(th, "_run_session", AsyncMock(return_value=None))

    app = FastAPI()
    app.include_router(th.router)
    from document_processor.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: fake_user

    client = TestClient(app)
    resp = client.post(
        "/api/thinking/think",
        headers={"X-Client-Id": "test-cli"},
        json={
            "prompt": "should I use postgres or mongo for this?",
            "effort": "medium",
            "provider": "local",
            "detected_deliverable": "explanation",
            "clarifications": {},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("X-Model-Used") == "qwen2.5:7b"


# ─── /api/code/start ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_code_start_emits_x_model_used_for_user_pref(
    monkeypatch, fake_user,
):
    """When the user has set an explicit pref, the header carries the tag."""
    from document_processor.api import code_intelligence_routes as ci

    async def fake_resolve(**kwargs):
        # Reason contains "user preference" so the route honours it.
        return ("qwen2.5-coder:32b", None, "user preference (code)")
    monkeypatch.setattr(ci, "resolve_request_model_full", fake_resolve)

    monkeypatch.setattr(ci, "_persist", AsyncMock(return_value=None))
    monkeypatch.setattr(ci, "_run_session", AsyncMock(return_value=None))

    app = FastAPI()
    app.include_router(ci.router)
    from document_processor.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: fake_user

    client = TestClient(app)
    resp = client.post(
        "/api/code/start",
        headers={"X-Client-Id": "test-cli"},
        json={"prompt": "build a tiny CLI calculator", "effort": "basic"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("X-Model-Used") == "qwen2.5-coder:32b"


@pytest.mark.asyncio
async def test_code_start_emits_per_role_sentinel_when_no_pref(
    monkeypatch, fake_user,
):
    """When the resolver returns a generic auto-select, Code Intelligence
    demotes effective_model to None and the header lands on
    ``auto:per-role`` so clients know the engine will pick per-role at
    runtime."""
    from document_processor.api import code_intelligence_routes as ci

    async def fake_resolve(**kwargs):
        # Reason is *not* "request override" / "user preference" → the
        # route falls through to per-role auto-pick.
        return ("qwen2.5:7b", None, "auto-selected — best fit for code/basic")
    monkeypatch.setattr(ci, "resolve_request_model_full", fake_resolve)

    monkeypatch.setattr(ci, "_persist", AsyncMock(return_value=None))
    monkeypatch.setattr(ci, "_run_session", AsyncMock(return_value=None))

    app = FastAPI()
    app.include_router(ci.router)
    from document_processor.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: fake_user

    client = TestClient(app)
    resp = client.post(
        "/api/code/start",
        headers={"X-Client-Id": "test-cli"},
        json={"prompt": "implement a sieve of eratosthenes",
              "effort": "medium"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("X-Model-Used") == "auto:per-role"
