"""
Unit tests for `services/model_resolution.resolve_request_model`.

The helper is a single funnel through which the start-routes route
their model lookup. Resolution order:

  1. requested_model truthy           → ("<tag>", "request override")
  2. chat_store user/client mode pref → ("<tag>", "user preference (mode)")
  3. ModelManager.auto_select         → ("<tag>", "auto-selected — …")
  4. fall through (manager None)      → (None,   "fallback to OLLAMA_MODEL")
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from document_processor.services import model_resolution as mr


@pytest.fixture
def fake_request():
    """Stand-in for fastapi.Request — only `app.state` is touched."""
    state = SimpleNamespace(model_manager=None)
    app = SimpleNamespace(state=state)
    return SimpleNamespace(app=app)


@pytest.mark.asyncio
async def test_request_override_wins_first(monkeypatch, fake_request):
    # Sentinel so we know chat_store wasn't even queried.
    called = {}

    async def boom(**kw):
        called["pref"] = True
        return "should-not-happen"

    monkeypatch.setattr(mr.chat_store, "get_model_preference", boom)
    tag, reason = await mr.resolve_request_model(
        request=fake_request,
        requested_model="user-typed:1",
        user_id="u1",
        client_id="c1",
        mode="research",
    )
    assert tag == "user-typed:1"
    assert reason == "request override"
    assert "pref" not in called


@pytest.mark.asyncio
async def test_pref_used_when_no_request_override(monkeypatch, fake_request):
    monkeypatch.setattr(
        mr.chat_store, "get_model_preference",
        AsyncMock(return_value="from-mongo:7b"),
    )
    tag, reason = await mr.resolve_request_model(
        request=fake_request,
        requested_model=None,
        user_id="u1",
        client_id="c1",
        mode="thinking",
    )
    assert tag == "from-mongo:7b"
    assert "user preference" in reason
    assert "thinking" in reason


@pytest.mark.asyncio
async def test_falls_through_to_auto_select(monkeypatch, fake_request):
    monkeypatch.setattr(
        mr.chat_store, "get_model_preference",
        AsyncMock(return_value=None),
    )

    class _FakeMgr:
        async def auto_select(self, mode, effort):
            return ("auto:tag", f"auto-selected — best fit for {mode}/{effort}")

    fake_request.app.state.model_manager = _FakeMgr()

    tag, reason = await mr.resolve_request_model(
        request=fake_request,
        requested_model=None,
        user_id="u1",
        client_id="c1",
        mode="code",
        effort="deep",
    )
    assert tag == "auto:tag"
    assert "auto-selected" in reason
    assert "code/deep" in reason


@pytest.mark.asyncio
async def test_creates_manager_on_demand_if_missing(monkeypatch, fake_request):
    """If app.state.model_manager isn't set, the resolver instantiates
    one rather than 500-ing — important for the test harness path.

    `model_resolution` imports ``ModelManager`` directly into its
    namespace, so the monkeypatch must target ``mr.ModelManager`` (the
    bound name in the consuming module), not the original class object."""
    monkeypatch.setattr(
        mr.chat_store, "get_model_preference",
        AsyncMock(return_value=None),
    )

    class _NoopMgr:
        async def auto_select(self, mode, effort):
            return ("noop:tag", "auto-selected — noop")

    monkeypatch.setattr(mr, "ModelManager", lambda: _NoopMgr())

    fake_request.app.state.model_manager = None
    tag, reason = await mr.resolve_request_model(
        request=fake_request,
        requested_model=None,
        user_id=None,
        client_id="c1",
        mode="research",
    )
    assert tag == "noop:tag"
    assert fake_request.app.state.model_manager is not None
