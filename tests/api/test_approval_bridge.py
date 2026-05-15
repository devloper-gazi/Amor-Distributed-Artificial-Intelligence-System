"""Cycle F Sprint 5 — tests for document_processor/api/approval/bridge.py.

Coverage:
  * `request_user_approval` → future resolution path
  * Timeout path
  * `resolve_approval` for an unknown id is a no-op
  * `_PENDING` registry hygiene (entries dropped after resolution)
  * `to_event()` shape matches the SSE wire contract
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from document_processor.api.approval.bridge import (
    AwaitingApproval,
    _PENDING,
    pending_count,
    request_user_approval,
    resolve_approval,
)


# ─── AwaitingApproval shape ─────────────────────────────────────────


def test_awaiting_approval_to_event_shape():
    req = AwaitingApproval(
        request_id="abc123",
        session_id="sess-1",
        tool_name="rm_rf",
        category="delete",
        arguments={"path": "/tmp/x"},
        actor_role="coder",
    )
    ev = req.to_event()
    assert ev["type"] == "approval_required"
    assert ev["request_id"] == "abc123"
    assert ev["tool_name"] == "rm_rf"
    assert ev["category"] == "delete"
    assert ev["arguments"] == {"path": "/tmp/x"}
    assert ev["actor_role"] == "coder"
    assert "timeout_s" in ev


# ─── request_user_approval — approval path ──────────────────────────


@pytest.mark.asyncio
async def test_request_user_approval_resolves_to_true():
    """Operator approves: future resolves True; registry cleaned."""

    published: list[dict] = []

    async def fake_publish(session_id, event):
        published.append({"session_id": session_id, "event": event})

    # Run the await + resolution concurrently.
    async def approve_after_publish():
        # Wait long enough for request_user_approval to add to _PENDING.
        for _ in range(50):
            await asyncio.sleep(0.001)
            if pending_count() > 0:
                req_id = next(iter(_PENDING.keys()))
                resolve_approval(req_id, approved=True)
                return
        raise RuntimeError("request never landed in _PENDING")

    async with asyncio.TaskGroup() as tg:
        approver = tg.create_task(approve_after_publish())
        approved = await request_user_approval(
            session_id="s1",
            tool_name="rm_rf",
            category="delete",
            timeout_s=5.0,
            publish_fn=fake_publish,
        )
        await approver

    assert approved is True
    # Cleaned up after resolution.
    assert pending_count() == 0
    # SSE event was published.
    assert len(published) == 1
    assert published[0]["event"]["type"] == "approval_required"


# ─── request_user_approval — denial path ────────────────────────────


@pytest.mark.asyncio
async def test_request_user_approval_resolves_to_false():
    """Operator denies: future resolves False; registry cleaned."""

    async def fake_publish(session_id, event):
        pass

    async def deny_after_publish():
        for _ in range(50):
            await asyncio.sleep(0.001)
            if pending_count() > 0:
                req_id = next(iter(_PENDING.keys()))
                resolve_approval(req_id, approved=False)
                return
        raise RuntimeError("request never landed")

    async with asyncio.TaskGroup() as tg:
        tg.create_task(deny_after_publish())
        approved = await request_user_approval(
            session_id="s2",
            tool_name="docker_run",
            timeout_s=5.0,
            publish_fn=fake_publish,
        )

    assert approved is False
    assert pending_count() == 0


# ─── request_user_approval — timeout path ───────────────────────────


@pytest.mark.asyncio
async def test_request_user_approval_times_out():
    """No resolution within timeout → returns False."""

    published: list[dict] = []

    async def fake_publish(session_id, event):
        published.append(event)

    approved = await request_user_approval(
        session_id="s3",
        tool_name="docker_run",
        timeout_s=0.05,  # ms-scale for the test
        publish_fn=fake_publish,
    )
    assert approved is False
    # Both `approval_required` + `approval_resolved` events emitted.
    types = [ev["type"] for ev in published]
    assert "approval_required" in types
    assert "approval_resolved" in types
    # Registry cleaned up after timeout.
    assert pending_count() == 0


# ─── resolve_approval — unknown id ──────────────────────────────────


def test_resolve_approval_unknown_id_returns_false():
    """Resolving an id with no waiting future is a no-op."""

    fake_id = uuid4().hex
    assert resolve_approval(fake_id, approved=True) is False


# ─── Registry hygiene ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_count_tracks_registry():
    initial = pending_count()

    async def fake_publish(s, e):
        pass

    async def resolve_quickly():
        for _ in range(50):
            await asyncio.sleep(0.001)
            if pending_count() > initial:
                req_id = next(iter(_PENDING.keys()))
                resolve_approval(req_id, approved=True)
                return

    async with asyncio.TaskGroup() as tg:
        tg.create_task(resolve_quickly())
        await request_user_approval(
            session_id="s",
            tool_name="x",
            timeout_s=5.0,
            publish_fn=fake_publish,
        )

    assert pending_count() == initial
