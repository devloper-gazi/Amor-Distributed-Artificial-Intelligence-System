"""
Cycle C Sprint 7 Day 2 — admin Memory route tests.

Hits every endpoint in both regimes:
  * adapter unavailable (default) — status 200 with empty results,
    explicit ``available: false`` in the payload, add/delete return
    503 to make the operator's setup state obvious.
  * adapter available (stubbed Mem0) — search / get_all / add /
    delete propagate to the fake client and the responses contain
    real data.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def fake_user():
    from document_processor.auth.models import User
    return User(
        id="11111111-1111-1111-1111-111111111111",
        username="memuser",
        email="m@example.com",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        is_active=True,
    )


def _build_app(fake_user):
    from document_processor.api import admin_memory_routes as r
    app = FastAPI()
    app.include_router(r.router)
    from document_processor.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: fake_user
    return app


# ─── degraded regime (no mem0) ────────────────────────────────────


def test_status_returns_native_when_mem0_disabled(monkeypatch, fake_user):
    monkeypatch.delenv("AMOR_MEMORY_BACKEND", raising=False)
    # Make sure no leftover stub from a prior test still registered.
    monkeypatch.delitem(sys.modules, "mem0", raising=False)
    import local_ai.memory.mem0_adapter as adapter_mod
    adapter_mod._MEM0_INSTALLED = None

    app = _build_app(fake_user)
    with TestClient(app) as c:
        r = c.get("/api/admin/memory/status")
        assert r.status_code == 200
        body = r.json()
        assert body["backend"] == "native"
        assert body["available"] is False
        assert body["user_namespace"] == fake_user.id


def test_search_returns_empty_when_disabled(monkeypatch, fake_user):
    monkeypatch.delenv("AMOR_MEMORY_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "mem0", raising=False)
    import local_ai.memory.mem0_adapter as adapter_mod
    adapter_mod._MEM0_INSTALLED = None

    app = _build_app(fake_user)
    with TestClient(app) as c:
        r = c.get("/api/admin/memory/search?q=anything&limit=3")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["count"] == 0
        assert body["items"] == []


def test_add_returns_503_when_disabled(monkeypatch, fake_user):
    monkeypatch.delenv("AMOR_MEMORY_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "mem0", raising=False)
    import local_ai.memory.mem0_adapter as adapter_mod
    adapter_mod._MEM0_INSTALLED = None

    app = _build_app(fake_user)
    with TestClient(app) as c:
        r = c.post("/api/admin/memory/add", json={"text": "user likes mango"})
        assert r.status_code == 503


def test_delete_returns_503_when_disabled(monkeypatch, fake_user):
    monkeypatch.delenv("AMOR_MEMORY_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "mem0", raising=False)
    import local_ai.memory.mem0_adapter as adapter_mod
    adapter_mod._MEM0_INSTALLED = None

    app = _build_app(fake_user)
    with TestClient(app) as c:
        r = c.delete("/api/admin/memory/abc-123")
        assert r.status_code == 503


# ─── enabled regime (stubbed mem0) ────────────────────────────────


class _FakeMemoryClient:
    def __init__(self):
        self.added = []
        self.deleted = []

    def add(self, messages, user_id, metadata):
        self.added.append((messages, user_id, metadata))
        return {"results": [{"id": "m-1", "memory": "fact A", "user_id": user_id}]}

    def search(self, query, user_id, limit):
        return [{"id": "m-2", "memory": f"hit:{query}", "score": 0.9, "user_id": user_id}]

    def get_all(self, user_id, limit):
        return [{"id": "m-3", "memory": "all-1", "user_id": user_id}]

    def delete(self, memory_id):
        self.deleted.append(memory_id)


def _install_fake_mem0(monkeypatch):
    fake_client = _FakeMemoryClient()
    fake_mem0 = ModuleType("mem0")
    fake_mem0.Memory = SimpleNamespace(  # type: ignore[attr-defined]
        from_config=lambda cfg: fake_client,
    )
    monkeypatch.setitem(sys.modules, "mem0", fake_mem0)
    import local_ai.memory.mem0_adapter as adapter_mod
    adapter_mod._MEM0_INSTALLED = None
    monkeypatch.setenv("AMOR_MEMORY_BACKEND", "mem0")
    return fake_client


def test_status_reports_mem0_when_enabled(monkeypatch, fake_user):
    _install_fake_mem0(monkeypatch)
    app = _build_app(fake_user)
    with TestClient(app) as c:
        r = c.get("/api/admin/memory/status")
        assert r.status_code == 200
        body = r.json()
        assert body["backend"] == "mem0"
        assert body["available"] is True
        assert body["vector_store"] == "lancedb"


def test_search_returns_hits_when_enabled(monkeypatch, fake_user):
    _install_fake_mem0(monkeypatch)
    app = _build_app(fake_user)
    with TestClient(app) as c:
        r = c.get("/api/admin/memory/search?q=mango")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["count"] == 1
        assert body["items"][0]["text"] == "hit:mango"
        assert body["items"][0]["user_id"] == fake_user.id


def test_add_persists_when_enabled(monkeypatch, fake_user):
    fake = _install_fake_mem0(monkeypatch)
    app = _build_app(fake_user)
    with TestClient(app) as c:
        r = c.post(
            "/api/admin/memory/add",
            json={"text": "user prefers Turkish UI", "metadata": {"src": "test"}},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["count"] == 1
        assert body["items"][0]["text"] == "fact A"
    assert fake.added[0][1] == fake_user.id
    assert fake.added[0][2] == {"src": "test"}


def test_delete_drops_when_enabled(monkeypatch, fake_user):
    fake = _install_fake_mem0(monkeypatch)
    app = _build_app(fake_user)
    with TestClient(app) as c:
        r = c.delete("/api/admin/memory/m-3")
        assert r.status_code == 200
        body = r.json()
        assert body["deleted"] is True
        assert body["id"] == "m-3"
    assert fake.deleted == ["m-3"]
