"""Tests for v17 PR #3 — sticky cookie + SSE failover hardening.

Verifies:
1. ``POST /api/code/start`` emits ``Set-Cookie: amor_session=<sid>``
   so nginx's ``hash $cookie_amor_session consistent;`` upstream
   binds subsequent requests to the same replica.
2. ``POST /api/code/start`` emits ``X-AMOR-Replica`` so multi-
   replica failover smoke tests can verify the binding works.
3. The Redis Pub/Sub fan-out (Phase 17 Commit S) lets a subscriber
   on replica B receive events that were published from replica A
   — the mechanism that makes SSE survive a replica swap when
   the client reconnects.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def fake_user():
    from document_processor.auth.models import User
    return User(
        id="test-user-failover",
        username="tester",
        email="t@example.com",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        is_active=True,
    )


def _build_app(monkeypatch, fake_user):
    """Construct a FastAPI app with the code router + auth bypass +
    every external dep stubbed.  Returns (app, routes_module)."""
    from document_processor.api import code_intelligence_routes as r

    # Resolver — return a fixed tag so the start path completes.
    async def _fake_resolve(**kw):
        return ("qwen2.5:7b", None, "fallback")

    monkeypatch.setattr(r, "resolve_request_model_full", _fake_resolve)
    # No-op the bg session runner — start returns the session_id
    # immediately without firing engine.run().
    monkeypatch.setattr(r, "_run_session", AsyncMock(return_value=None))
    # Cache manager — get_json returns None (no prior session),
    # set_json + publish_event are no-ops.
    monkeypatch.setattr(r.cache_manager, "get_json", AsyncMock(return_value=None))
    monkeypatch.setattr(r.cache_manager, "set_json", AsyncMock(return_value=None))
    monkeypatch.setattr(
        r.cache_manager, "publish_event",
        AsyncMock(return_value=None),
    )

    app = FastAPI()
    app.include_router(r.router)
    from document_processor.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: fake_user
    return app, r


# ─── sticky cookie ───────────────────────────────────────────────


def test_start_sets_amor_session_cookie(monkeypatch, fake_user):
    """The /start response must carry a ``Set-Cookie:
    amor_session=<sid>`` so subsequent requests get pinned to the
    same nginx upstream."""
    app, r = _build_app(monkeypatch, fake_user)
    client = TestClient(app)
    resp = client.post(
        "/api/code/start",
        headers={"X-Client-Id": "ci-x"},
        json={"prompt": "hello", "effort": "basic"},
    )
    assert resp.status_code == 200, resp.text
    set_cookie = resp.headers.get("set-cookie", "")
    assert "amor_session=" in set_cookie, set_cookie


def test_start_cookie_matches_session_id(monkeypatch, fake_user):
    """The cookie's value MUST equal the response body's
    session_id — otherwise the sticky hash binds clients to the
    wrong replica."""
    app, _ = _build_app(monkeypatch, fake_user)
    client = TestClient(app)
    resp = client.post(
        "/api/code/start",
        headers={"X-Client-Id": "ci-x"},
        json={"prompt": "hello", "effort": "basic"},
    )
    assert resp.status_code == 200
    body_sid = resp.json()["session_id"]
    cookie = resp.cookies.get("amor_session")
    assert cookie == body_sid


def test_start_cookie_is_httponly_and_lax(monkeypatch, fake_user):
    """The cookie must be ``HttpOnly`` (no JS access) and
    ``SameSite=Lax`` (first-party only).  Defends against XSS
    leak + CSRF on cross-site POSTs."""
    app, _ = _build_app(monkeypatch, fake_user)
    client = TestClient(app)
    resp = client.post(
        "/api/code/start",
        headers={"X-Client-Id": "ci-x"},
        json={"prompt": "hello", "effort": "basic"},
    )
    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie


# ─── X-AMOR-Replica header ───────────────────────────────────────


def test_start_emits_x_amor_replica_header(monkeypatch, fake_user):
    """The replica header surfaces which replica served the
    request — failover smoke tests use this to verify sticky
    binding actually pins."""
    app, _ = _build_app(monkeypatch, fake_user)
    client = TestClient(app)
    resp = client.post(
        "/api/code/start",
        headers={"X-Client-Id": "ci-x"},
        json={"prompt": "hello", "effort": "basic"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("X-AMOR-Replica"), \
        "X-AMOR-Replica header must be set"


# ─── Redis Pub/Sub cross-replica fan-out ──────────────────────────


def test_publish_event_uses_session_specific_channel(monkeypatch):
    """``_publish`` must call ``cache_manager.publish_event`` on
    the session-specific channel so a subscriber tied to that
    session_id receives the event regardless of which replica
    it lives on."""
    from document_processor.api import code_intelligence_routes as r

    captured: list[tuple[str, dict]] = []

    async def _fake_publish_event(channel, event):
        captured.append((channel, dict(event)))

    monkeypatch.setattr(
        r.cache_manager, "publish_event", _fake_publish_event,
    )

    # The empty in-memory cache + Redis stub combo causes _publish
    # to skip the queue path (no event_queue exists yet) but
    # always tries the Redis fan-out.
    async def _empty_get(_key):
        return None

    async def _persist(*a, **kw):
        return None

    monkeypatch.setattr(r.cache_manager, "get_json", _empty_get)
    monkeypatch.setattr(r.cache_manager, "set_json", _persist)

    # Stub the adversarial reviewer to a permissive no-op.
    class _Reviewer:
        def inspect_event(self, _sid, _ev):
            return True, None

    monkeypatch.setattr(r, "get_adversarial_reviewer", lambda: _Reviewer())

    asyncio.run(r._publish("sid-fanout", {"type": "phase_start"}))

    # At least one publish call landed on the session-specific channel.
    assert any(
        "sid-fanout" in ch for ch, _ in captured
    ), f"no session-keyed channel in: {[c for c, _ in captured]}"


def test_load_falls_through_to_redis_when_in_memory_misses(monkeypatch):
    """The cross-replica scenario: Replica A starts the session,
    Replica B receives a follow-up request.  ``_load`` must hit
    Redis when the in-memory cache is empty (this is the
    foundation that sticky cookie OPTIMIZES; when sticky breaks,
    the fallback still works)."""
    from document_processor.api import code_intelligence_routes as r

    r._sessions.clear()  # Simulate fresh replica.

    fake_session = {
        "session_id": "sid-cross",
        "user_id": "u",
        "status": "in_progress",
        "started_at": "2026-05-02T00:00:00+00:00",
        "started_at_ts": 1000.0,
        "phases": [],
    }

    async def _fake_get_json(key):
        if "sid-cross" in key:
            return dict(fake_session)
        return None

    monkeypatch.setattr(r.cache_manager, "get_json", _fake_get_json)

    loaded = asyncio.run(r._load("sid-cross"))
    assert loaded is not None
    assert loaded["session_id"] == "sid-cross"
    # Repopulated the local cache for subsequent calls.
    assert "sid-cross" in r._sessions

    r._sessions.clear()
