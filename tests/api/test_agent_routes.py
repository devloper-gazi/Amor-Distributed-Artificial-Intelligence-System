"""
Cycle C Sprint 8 Day 4 — agent route tests.

Drives ``/api/agent`` end-to-end against the in-process app + a
scripted LLM stub.  No real backend, no docker — the route's
``LLM_CALLER`` module attribute is monkeypatched.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def fake_user():
    from document_processor.auth.models import User
    return User(
        id="11111111-1111-1111-1111-111111111111",
        username="agent-tester",
        email="a@example.com",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        is_active=True,
    )


def _build_app(monkeypatch, fake_user, *, completions: List[str]):
    """Construct the FastAPI app with ``LLM_CALLER`` patched to a
    scripted sequence and a tool registry stub that returns echoes."""
    from document_processor.api import agent_routes as r

    queue = list(completions)

    async def stub_llm(prompt: str) -> str:
        if not queue:
            return ""  # exhausted — agent will eventually fall through
        return queue.pop(0)

    monkeypatch.setattr(r, "LLM_CALLER", stub_llm)

    # Replace the tool catalogue + dispatcher so the test doesn't pull
    # the real DEFAULT_REGISTRY (which depends on extra adapters).
    monkeypatch.setattr(
        r,
        "_tool_catalogue",
        lambda: [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "echoes",
                    "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
                },
            },
        ],
    )

    async def stub_dispatcher(name, args):
        return {
            "name": name,
            "ok": True,
            "output": {"echoed": dict(args or {})},
            "error": None,
            "elapsed_ms": 0.5,
        }

    # Patch the import target for the agent's default_tool_dispatcher.
    import local_ai.agentic.agent as agent_mod
    monkeypatch.setattr(agent_mod, "default_tool_dispatcher", stub_dispatcher)

    app = FastAPI()
    app.include_router(r.router)
    from document_processor.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: fake_user
    # Reset the in-process session map so tests don't bleed.
    r._SESSIONS.clear()
    return app


def test_start_returns_session_id(monkeypatch, fake_user):
    """A clean ``finish`` run completes synchronously — by the time
    snapshot() returns, the conversation is ``finished`` with the
    answer the LLM emitted."""
    completions = [
        '<thought>I know.</thought><action>{"tool":"finish","arguments":{"answer":"42"}}</action>',
    ]
    app = _build_app(monkeypatch, fake_user, completions=completions)
    with TestClient(app) as c:
        r = c.post("/api/agent/start", json={"task": "what's 6×7?"})
        assert r.status_code == 201, r.text
        body = r.json()
        sid = body["session_id"]
        assert sid

        # Drain — the bg task is async; wait briefly via snapshot poll.
        for _ in range(20):
            snap = c.get(f"/api/agent/sessions/{sid}").json()
            if snap.get("finished"):
                break
        assert snap["finished"] is True
        assert snap["finish_reason"] == "finish"
        roles = [e["kind"] for e in snap["events"]]
        assert "message" in roles
        assert "thought" in roles
        assert "action" in roles


def test_start_validates_input(monkeypatch, fake_user):
    """Empty task body must 422 — the agent loop has nothing to
    drive."""
    app = _build_app(monkeypatch, fake_user, completions=[])
    with TestClient(app) as c:
        r = c.post("/api/agent/start", json={"task": ""})
        assert r.status_code == 422


def test_snapshot_404s_unknown_sid(monkeypatch, fake_user):
    app = _build_app(monkeypatch, fake_user, completions=[])
    with TestClient(app) as c:
        r = c.get("/api/agent/sessions/does-not-exist")
        assert r.status_code == 404


def test_cancel_marks_session(monkeypatch, fake_user):
    """``cancel`` always 200s for a known session, even when the
    underlying task has already finished — operator UX should never
    see a 4xx for a stop-button click."""
    completions = [
        '<thought>quick.</thought><action>{"tool":"finish","arguments":{"answer":"ok"}}</action>',
    ]
    app = _build_app(monkeypatch, fake_user, completions=completions)
    with TestClient(app) as c:
        sid = c.post("/api/agent/start", json={"task": "x"}).json()["session_id"]
        # Drain to finished (so the task isn't running).
        for _ in range(20):
            snap = c.get(f"/api/agent/sessions/{sid}").json()
            if snap.get("finished"):
                break
        r = c.post(f"/api/agent/sessions/{sid}/cancel")
        assert r.status_code == 200
        assert r.json()["cancelled"] is True


def test_event_stream_carries_sse_ids_for_resume(monkeypatch, fake_user):
    """Sprint 9 Day 2 — every live event must ship an ``id:`` line so
    EventSource can resume on reconnect.  The snapshot envelope is
    deliberately id-less (it's idempotent on every connect)."""
    completions = [
        '<thought>plan</thought><action>{"tool":"echo","arguments":{"x":1}}</action>',
        '<thought>done</thought><action>{"tool":"finish","arguments":{"answer":"yay"}}</action>',
    ]
    app = _build_app(monkeypatch, fake_user, completions=completions)
    with TestClient(app) as c:
        sid = c.post("/api/agent/start", json={"task": "echo"}).json()["session_id"]
        for _ in range(40):
            snap = c.get(f"/api/agent/sessions/{sid}").json()
            if snap.get("finished"):
                break

        ids: list[str] = []
        with c.stream("GET", f"/api/agent/sessions/{sid}/events") as r:
            assert r.status_code == 200
            current_id: str | None = None
            for line in r.iter_lines():
                if not line:
                    continue
                if line.startswith("id:"):
                    current_id = line[3:].strip()
                    continue
                if line.startswith("data:"):
                    if current_id:
                        ids.append(current_id)
                        current_id = None
                    payload = json.loads(line[5:].strip())
                    if payload.get("type") in {"agent.done", "agent.cancelled", "agent.error"}:
                        break
        # At least 3 distinct ids (one per: thought, action, observation;
        # plus more for the second iteration + agent.done).
        assert len(ids) >= 3
        # IDs should sort lexicographically — Redis stream id format
        # ``<ms>-<seq>`` is naturally orderable.
        assert ids == sorted(ids)


def test_event_stream_replays_after_last_event_id(monkeypatch, fake_user):
    """A reconnect with ``Last-Event-ID: <id>`` must drop everything
    up to and including that id and pick up after."""
    completions = [
        '<thought>plan</thought><action>{"tool":"echo","arguments":{"x":1}}</action>',
        '<thought>done</thought><action>{"tool":"finish","arguments":{"answer":"yay"}}</action>',
    ]
    app = _build_app(monkeypatch, fake_user, completions=completions)
    with TestClient(app) as c:
        sid = c.post("/api/agent/start", json={"task": "echo"}).json()["session_id"]
        for _ in range(40):
            snap = c.get(f"/api/agent/sessions/{sid}").json()
            if snap.get("finished"):
                break

        # First connect — collect every id.
        ids_first: list[str] = []
        with c.stream("GET", f"/api/agent/sessions/{sid}/events") as r:
            current_id: str | None = None
            for line in r.iter_lines():
                if not line:
                    continue
                if line.startswith("id:"):
                    current_id = line[3:].strip()
                    continue
                if line.startswith("data:"):
                    if current_id:
                        ids_first.append(current_id)
                        current_id = None
                    payload = json.loads(line[5:].strip())
                    if payload.get("type") in {"agent.done", "agent.cancelled", "agent.error"}:
                        break
        assert len(ids_first) >= 3

        # Pick a checkpoint mid-stream and resume.
        checkpoint = ids_first[len(ids_first) // 2]

        ids_second: list[str] = []
        with c.stream(
            "GET",
            f"/api/agent/sessions/{sid}/events",
            headers={"Last-Event-ID": checkpoint},
        ) as r:
            current_id = None
            for line in r.iter_lines():
                if not line:
                    continue
                if line.startswith("id:"):
                    current_id = line[3:].strip()
                    continue
                if line.startswith("data:"):
                    if current_id:
                        ids_second.append(current_id)
                        current_id = None
                    payload = json.loads(line[5:].strip())
                    if payload.get("type") in {"agent.done", "agent.cancelled", "agent.error"}:
                        break

        # Resume must NOT re-deliver the checkpoint id itself OR any
        # earlier id; the first id seen on the second stream has to be
        # strictly later than the checkpoint.
        assert all(i > checkpoint for i in ids_second), (
            f"resume leaked old ids: checkpoint={checkpoint}, replayed={ids_second}"
        )


def test_event_stream_serves_unknown_sid_via_redis(monkeypatch, fake_user):
    """Sprint 9 Day 3 — when a reconnect lands on a replica that
    doesn't know the sid in-memory (cross-replica failover) the
    events handler MUST still serve the SSE stream by opening Redis
    directly.  We simulate this by starting a session, draining the
    in-memory state, then connecting after popping the _SESSIONS
    entry."""
    completions = [
        '<thought>plan</thought><action>{"tool":"echo","arguments":{"x":1}}</action>',
        '<thought>done</thought><action>{"tool":"finish","arguments":{"answer":"yay"}}</action>',
    ]
    app = _build_app(monkeypatch, fake_user, completions=completions)
    from document_processor.api import agent_routes as r

    with TestClient(app) as c:
        sid = c.post("/api/agent/start", json={"task": "echo"}).json()["session_id"]
        for _ in range(40):
            snap = c.get(f"/api/agent/sessions/{sid}").json()
            if snap.get("finished"):
                break

        # Simulate cross-replica scenario: the OTHER replica doesn't
        # have this sid in _SESSIONS but Redis still has the stream.
        r._SESSIONS.pop(sid, None)

        # Snapshot endpoint 404s (in-memory state is gone).
        assert c.get(f"/api/agent/sessions/{sid}").status_code == 404

        # Events endpoint MUST still work — straight off Redis.
        ids: list[str] = []
        with c.stream("GET", f"/api/agent/sessions/{sid}/events") as resp:
            assert resp.status_code == 200
            current_id: str | None = None
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("id:"):
                    current_id = line[3:].strip()
                    continue
                if line.startswith("data:"):
                    payload = json.loads(line[5:].strip())
                    if current_id:
                        ids.append(current_id)
                        current_id = None
                    if payload.get("type") in {"agent.done", "agent.cancelled", "agent.error"}:
                        break
                    if payload.get("type") == "agent.snapshot":
                        # Snapshot is empty + flagged cross_replica.
                        assert payload["events"] == []
                        assert payload.get("cross_replica") is True
        assert len(ids) >= 3, "cross-replica events stream returned too few events"


def test_event_stream_emits_snapshot_and_done(monkeypatch, fake_user):
    """SSE stream opens with ``agent.snapshot`` (carries the prior
    events) and ends with ``agent.done`` carrying the final answer."""
    completions = [
        '<thought>plan</thought><action>{"tool":"echo","arguments":{"x":1}}</action>',
        '<thought>done</thought><action>{"tool":"finish","arguments":{"answer":"yay"}}</action>',
    ]
    app = _build_app(monkeypatch, fake_user, completions=completions)
    with TestClient(app) as c:
        sid = c.post("/api/agent/start", json={"task": "echo"}).json()["session_id"]
        # Drain the run before subscribing — the queue still has the
        # buffered events for the late subscriber to replay.  The
        # snapshot envelope is unconditional; the .done envelope was
        # also queued by ``_runner``.
        for _ in range(40):
            snap = c.get(f"/api/agent/sessions/{sid}").json()
            if snap.get("finished"):
                break

        envelopes = []
        with c.stream("GET", f"/api/agent/sessions/{sid}/events") as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                try:
                    payload = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                envelopes.append(payload)
                if payload.get("type") in {"agent.done", "agent.cancelled", "agent.error"}:
                    break
        types = [e.get("type") for e in envelopes]
        assert types[0] == "agent.snapshot"
        assert any(t == "agent.done" for t in types)
        done = next(e for e in envelopes if e.get("type") == "agent.done")
        assert done["reason"] == "finish"
        assert done["answer"] == "yay"
