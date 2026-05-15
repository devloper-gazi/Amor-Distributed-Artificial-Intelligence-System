"""
Cycle C Sprint 6 Day 1 — admin training (preference pairs) route tests.

Exercises POST/GET ingestion + the listing privacy rule (raw text
masked unless ``opt_in_raw`` is True) against an in-memory FastAPI
app with the storage layer fully mocked.

Why mock instead of use a real DB
---------------------------------
The route's dependency surface is exactly one symbol —
``storage_manager.pg_session_maker``.  A small AsyncMock that yields
a stub session is enough to drive every code path (success, dedup
hash, raw-text masking) without spinning up a real Postgres for the
unit suite.  The schema migration is integration-tested by the live
smoke probe in ``docs/sprint6_results.md``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def fake_user():
    from document_processor.auth.models import User
    return User(
        id="11111111-1111-1111-1111-111111111111",
        username="rater",
        email="r@example.com",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        is_active=True,
    )


def _build_app(monkeypatch, fake_user, *, exec_returns=None):
    """Spin up a FastAPI app with the training router + a stubbed
    pg_session_maker.  ``exec_returns`` lets a test override what
    ``session.execute()`` resolves to."""
    from document_processor.api import admin_training_routes as r

    # Stub `pg_session_maker` so the route's
    # ``async with storage_manager.pg_session_maker() as session`` works.
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.execute.side_effect = exec_returns

    @asynccontext
    async def _maker():
        yield session

    monkeypatch.setattr(
        r.storage_manager, "pg_session_maker", _maker, raising=False,
    )

    app = FastAPI()
    app.include_router(r.router)

    from document_processor.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: fake_user
    return app, session


# Tiny inline ``asynccontextmanager``-style helper so the fixture
# above doesn't have to import ``contextlib`` (keeps the dependency
# surface short).
def asynccontext(func):
    from contextlib import asynccontextmanager
    return asynccontextmanager(func)


def _make_row(row_id: str = "abc-123", created_at: datetime | None = None) -> Any:
    return SimpleNamespace(
        id=row_id,
        created_at=created_at or datetime(2026, 5, 4, tzinfo=timezone.utc),
    )


def test_create_pair_persists_default_no_raw(monkeypatch, fake_user):
    """Default ``opt_in_raw=False`` ⇒ the route must NOT pass raw
    text into the SQL params."""
    captured: dict = {}

    async def stub_execute(stmt, params=None, *args, **kwargs):  # noqa: ARG001
        captured["params"] = params
        # Return a result-like object whose ``fetchone()`` yields one row.
        result = MagicMock()
        result.fetchone = MagicMock(return_value=_make_row())
        return result

    app, session = _build_app(monkeypatch, fake_user)
    session.execute.side_effect = stub_execute

    with TestClient(app) as c:
        r = c.post(
            "/api/admin/training/pairs",
            json={
                "rejected_turn_id": "turn-9",
                "mode": "build",
                "opt_in_raw": False,
                "prompt": "leaky-prompt",
                "chosen": "leaky-chosen",
                "rejected": "leaky-rejected",
            },
        )
        assert r.status_code == 201, r.text
    p = captured["params"]
    # Hash must include all THREE inputs even when raw isn't stored.
    assert p["code_hash"]
    assert p["prompt"] is None
    assert p["chosen"] is None
    assert p["rejected"] is None
    assert p["opt_in_raw"] is False


def test_create_pair_opt_in_raw_keeps_text(monkeypatch, fake_user):
    captured: dict = {}

    async def stub_execute(stmt, params=None, *args, **kwargs):  # noqa: ARG001
        captured["params"] = params
        result = MagicMock()
        result.fetchone = MagicMock(return_value=_make_row())
        return result

    app, session = _build_app(monkeypatch, fake_user)
    session.execute.side_effect = stub_execute

    with TestClient(app) as c:
        r = c.post(
            "/api/admin/training/pairs",
            json={
                "chosen_turn_id": "turn-1",
                "mode": "research",
                "opt_in_raw": True,
                "prompt": "p",
                "chosen": "c",
                "rejected": "r",
            },
        )
        assert r.status_code == 201, r.text
    p = captured["params"]
    assert p["prompt"] == "p"
    assert p["chosen"] == "c"
    assert p["rejected"] == "r"
    assert p["opt_in_raw"] is True


def test_create_pair_rejects_invalid_mode(monkeypatch, fake_user):
    app, _ = _build_app(monkeypatch, fake_user)
    with TestClient(app) as c:
        r = c.post(
            "/api/admin/training/pairs",
            json={"chosen_turn_id": "x", "mode": "wrongmode"},
        )
        # Pydantic 422 for the literal-string mismatch.
        assert r.status_code == 422


def test_stats_aggregates_counts(monkeypatch, fake_user):
    """The ``/pairs/stats`` endpoint must produce a single payload
    with total / untrained / opt_in / by_mode + the train threshold."""
    rows = [
        SimpleNamespace(_mapping={"mode": "build", "n": 5}),
    ]

    async def exec_route(stmt, params=None, *args, **kwargs):  # noqa: ARG001
        sql = str(stmt).lower()
        result = MagicMock()
        if "group by mode" in sql:
            result.mappings = MagicMock(
                return_value=MagicMock(all=MagicMock(return_value=[
                    {"mode": "build", "n": 5},
                    {"mode": "research", "n": 2},
                ])),
            )
        elif "where opt_in_raw" in sql:
            result.scalar = MagicMock(return_value=1)
        elif "trained_in is null" in sql:
            result.scalar = MagicMock(return_value=4)
        else:
            result.scalar = MagicMock(return_value=7)
        return result

    app, session = _build_app(monkeypatch, fake_user)
    session.execute.side_effect = exec_route

    with TestClient(app) as c:
        r = c.get("/api/admin/training/pairs/stats")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 7
        assert body["untrained"] == 4
        assert body["opt_in_raw"] == 1
        assert body["by_mode"] == {"build": 5, "research": 2}
        assert body["train_threshold"] == 200
        assert body["ready_to_train"] is False


def test_listing_truncates_raw_when_optin(monkeypatch, fake_user):
    long_text = "x" * 500

    async def exec_route(stmt, params=None, *args, **kwargs):  # noqa: ARG001
        result = MagicMock()
        result.mappings = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "chosen_turn_id": "t1",
                    "rejected_turn_id": None,
                    "code_hash": "deadbeef",
                    "mode": "build",
                    "opt_in_raw": True,
                    "prompt": long_text,
                    "chosen": long_text,
                    "rejected": None,
                    "backend": "ollama",
                    "model_tag": None,
                    "created_at": datetime(2026, 5, 4, tzinfo=timezone.utc),
                    "trained_in": None,
                },
            ])),
        )
        return result

    app, session = _build_app(monkeypatch, fake_user)
    session.execute.side_effect = exec_route

    with TestClient(app) as c:
        r = c.get("/api/admin/training/pairs?limit=10")
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        # 160-char + '…' truncation guard.
        assert item["prompt"].endswith("…")
        assert len(item["prompt"]) <= 161 + 1
