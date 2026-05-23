"""Cycle F Sprint 5 — tests for POST /api/approval/{request_id}.

Uses FastAPI TestClient against an isolated app to exercise the
HTTP shape without spinning up the real AMOR stack.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from document_processor.api.approval import (
    register_approval_routes,
    resolve_approval,
)
from document_processor.api.approval.bridge import (
    AwaitingApproval,
    _PENDING,
)


@pytest.fixture
def client():
    app = FastAPI()
    register_approval_routes(app)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_pending():
    """Each test starts with an empty registry."""

    _PENDING.clear()
    yield
    _PENDING.clear()


# ─── POST /api/approval/{request_id} ───────────────────────────────


def test_approve_resolves_local_future(client):
    """An approval landing on the same replica that registered the
    request resolves the local future."""

    # Pre-register a request (simulates an in-flight tool call).
    import asyncio
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    req = AwaitingApproval(
        request_id="req-1",
        session_id="s1",
        tool_name="rm_rf",
        category="delete",
        arguments={},
        actor_role="coder",
        future=fut,
    )
    _PENDING["req-1"] = req

    with patch(
        "document_processor.api.approval.routes._broadcast_decision",
        new=AsyncMock(return_value=None),
    ):
        resp = client.post(
            "/api/approval/req-1",
            json={"approved": True, "note": "looks safe"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] is True
    assert body["approved"] is True
    assert fut.done()
    assert fut.result() is True

    loop.close()


def test_deny_resolves_local_future(client):
    import asyncio
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    _PENDING["req-2"] = AwaitingApproval(
        request_id="req-2",
        session_id="s",
        tool_name="x",
        category="exec",
        arguments={},
        actor_role=None,
        future=fut,
    )

    with patch(
        "document_processor.api.approval.routes._broadcast_decision",
        new=AsyncMock(return_value=None),
    ):
        resp = client.post(
            "/api/approval/req-2",
            json={"approved": False},
        )
    assert resp.status_code == 200
    assert resp.json()["resolved"] is True
    assert fut.done()
    assert fut.result() is False

    loop.close()


def test_unknown_request_returns_redis_via_response(client):
    """No local future → 200 with `resolved: false, via: 'redis'` so
    the operator knows the decision was broadcast for cross-replica
    resolution."""

    with patch(
        "document_processor.api.approval.routes._broadcast_decision",
        new=AsyncMock(return_value=None),
    ):
        resp = client.post(
            "/api/approval/unknown-id-xyz",
            json={"approved": True},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] is False
    assert body.get("via") == "redis"


def test_invalid_request_id_returns_400(client):
    resp = client.post(
        "/api/approval/" + "x" * 200,  # > 64 chars
        json={"approved": True},
    )
    assert resp.status_code == 400


def test_missing_approved_field_returns_422(client):
    resp = client.post(
        "/api/approval/req-x",
        json={},  # missing `approved`
    )
    assert resp.status_code == 422


def test_pending_debug_endpoint(client):
    import asyncio
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    _PENDING["req-debug"] = AwaitingApproval(
        request_id="req-debug",
        session_id="sess-x",
        tool_name="git_push",
        category="git",
        arguments={"branch": "main"},
        actor_role="coder",
        future=fut,
    )

    resp = client.get("/api/approval/_pending")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    found = [p for p in body["pending"] if p["request_id"] == "req-debug"]
    assert len(found) == 1
    assert found[0]["tool_name"] == "git_push"
    assert found[0]["category"] == "git"

    loop.close()


def test_resolve_approval_idempotent_against_already_resolved(client):
    """Calling resolve twice on the same future doesn't crash."""

    import asyncio
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    _PENDING["req-twice"] = AwaitingApproval(
        request_id="req-twice",
        session_id="s",
        tool_name="x",
        category="exec",
        arguments={},
        actor_role=None,
        future=fut,
    )

    assert resolve_approval("req-twice", approved=True) is True
    # Future already done — second call returns False (no-op).
    assert resolve_approval("req-twice", approved=False) is False
    assert fut.result() is True  # first decision wins

    loop.close()
