"""Tests for Phase 17 Commit S — engine routing_setter callback +
``_publish`` cross-replica Redis fallback.
"""

from __future__ import annotations

import asyncio

import pytest

from document_processor.code_intelligence.engine import (
    CodeIntelligenceEngine,
)


def _run(coro):
    return asyncio.run(coro)


# ─── routing_setter (engine→routes layer-violation invert) ─────────


def _make_engine(*, routing_setter=None, prepare_models=None):
    """Build a minimal engine that ONLY exercises the model-prep
    phase.  Other phases are not invoked here."""
    async def _stub_llm(prompt, system, max_tokens):
        return ""

    return CodeIntelligenceEngine(
        prompt="", code_context=None, language=None,
        effort="medium", provider="local",
        llm_call=_stub_llm,
        sandbox=None, static_harness=None,
        enable_execution=False,
        enable_static_analysis=False,
        enable_testing=False,
        max_debug_iterations=0,
        prepare_models=prepare_models,
        routing_setter=routing_setter,
    )


def test_routing_setter_default_is_no_op():
    """Engine constructed without ``routing_setter`` doesn't import
    anything from the routes layer.  Default is a silent lambda."""
    eng = _make_engine()
    # Calling it must not raise.
    eng._routing_setter({"strategy": "per_role", "role_routes": {}})


def test_routing_setter_fires_with_diverse_models():
    """When ``models_used`` has ≥2 distinct tags, the engine pushes
    a per-role routing doc into the injected setter."""
    captured: list[dict] = []

    def _setter(doc: dict) -> None:
        captured.append(dict(doc))

    async def _prepare() -> dict:
        return {
            "planner": "qwen3:8b",
            "coder": "qwen2.5-coder:7b",
            "tester": "qwen2.5-coder:7b",
            "debugger": "qwen3:8b",
            "critic": "qwen2.5:7b",
        }

    eng = _make_engine(routing_setter=_setter, prepare_models=_prepare)
    out = _run(eng._phase_model_prep())

    assert out["models_used"]["planner"] == "qwen3:8b"
    assert len(captured) == 1
    assert captured[0]["strategy"] == "per_role"
    assert captured[0]["role_routes"]["planner"] == "qwen3:8b"
    assert captured[0]["role_routes"]["critic"] == "qwen2.5:7b"


def test_routing_setter_skipped_when_single_model():
    """Single distinct model = no spread, no routing setter fires."""
    captured: list[dict] = []

    def _setter(doc: dict) -> None:
        captured.append(doc)

    async def _prepare() -> dict:
        return {role: "qwen2.5-coder:7b"
                for role in ["planner", "coder", "tester",
                             "debugger", "critic"]}

    eng = _make_engine(routing_setter=_setter, prepare_models=_prepare)
    _run(eng._phase_model_prep())
    assert captured == []


def test_routing_setter_exception_is_swallowed():
    """A misbehaving setter must not break the model_prep phase."""
    def _bad(_doc: dict) -> None:
        raise RuntimeError("boom")

    async def _prepare() -> dict:
        return {"a": "x:7b", "b": "y:7b"}

    eng = _make_engine(routing_setter=_bad, prepare_models=_prepare)
    out = _run(eng._phase_model_prep())
    # Phase still completes; the exception was logged.
    assert "models_used" in out


# ─── _publish cross-replica Redis fallback ────────────────────────


def test_publish_critical_alert_falls_through_to_redis(monkeypatch):
    """When the in-memory ``_sessions`` dict on this replica is empty
    (because the start landed elsewhere), the critical alert must
    still set ``cancel_requested`` via the Redis cache.
    """
    from document_processor.api import code_intelligence_routes as r

    # Empty the in-memory cache (simulate a fresh replica).
    r._sessions.clear()

    # Stub the cache_manager so _load returns a session.
    fake_session = {
        "session_id": "sid-xxx",
        "user_id": None,
        "status": "in_progress",
        "started_at": "2026-05-02T09:00:00+00:00",
        "started_at_ts": 1000.0,
        "phases": [],
    }

    persisted: list[dict] = []

    async def _fake_get_json(key):
        if "sid-xxx" in key:
            return dict(fake_session)
        return None

    async def _fake_set_json(key, value, ttl=None):  # noqa: ARG001
        persisted.append(dict(value))

    monkeypatch.setattr(r.cache_manager, "get_json", _fake_get_json)
    monkeypatch.setattr(r.cache_manager, "set_json", _fake_set_json)
    monkeypatch.setattr(
        r.cache_manager, "publish_event",
        lambda *a, **k: asyncio.sleep(0),
    )

    # Stub out adversarial reviewer to emit a critical alert.
    class _StubReviewer:
        def inspect_event(self, sid, event):  # noqa: ARG002
            return True, {
                "type": "adversarial_alert",
                "severity": "critical",
                "detail": "test",
            }

    monkeypatch.setattr(r, "get_adversarial_reviewer",
                        lambda: _StubReviewer())

    _run(r._publish("sid-xxx", {"type": "test"}))

    # Session should have been loaded from Redis, marked
    # cancel_requested, and re-persisted.
    assert "sid-xxx" in r._sessions
    assert r._sessions["sid-xxx"]["cancel_requested"] is True
    # _persist was called → set_json fired.
    assert persisted, "session was not re-persisted via Redis"
    assert persisted[-1]["cancel_requested"] is True

    r._sessions.clear()  # cleanup
