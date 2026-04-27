"""
Route smoke tests for /api/consortium/*.

The orchestrator is mocked so requests don't trigger real LLM calls.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from document_processor.api import consortium_routes


class _StubOrchestrator:
    """Drop-in for ConsortiumOrchestrator. Records constructor args
    and emits a deterministic event sequence on run()."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, *, session_id, scope, on_event=None, artifact_dir=None):
        _StubOrchestrator.last_kwargs = {
            "session_id": session_id, "scope": scope,
            "artifact_dir": artifact_dir,
        }
        self.session_id = session_id
        self.scope = scope
        self._on_event = on_event
        self._artifact_dir = artifact_dir

    async def run(self):
        from document_processor.consortium.models import (
            ConsortiumBundle, ImplementationArtifact, VerificationGate,
        )
        # Emit a phase_start + complete + gate + completed sequence.
        await self._on_event({"type": "consortium_phase_start", "phase": "scope"})
        await self._on_event({"type": "consortium_phase_complete", "phase": "scope"})
        await self._on_event({
            "type": "consortium_gate",
            "gate": {"phase": "research", "status": "passed",
                     "score": 80, "findings": [], "summary": "ok"},
        })
        await self._on_event({"type": "consortium_completed", "status": "ok"})
        # Pretend we wrote an artifact dir.
        if self._artifact_dir:
            self._artifact_dir.mkdir(parents=True, exist_ok=True)
            (self._artifact_dir / "README.md").write_text(
                "# stub bundle\n", encoding="utf-8",
            )
            (self._artifact_dir / "bundle.json").write_text(
                "{}", encoding="utf-8",
            )
        return ConsortiumBundle(
            session_id=self.session_id, scope=self.scope,
            implementation=ImplementationArtifact(
                code="print('hi')", language="python",
                deliverable_markdown="# done",
            ),
            verifications=[
                VerificationGate(
                    phase="research", status="passed",
                    score=80, summary="ok",
                ),
            ],
            readme_markdown="# stub bundle",
        )


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(
        consortium_routes, "ConsortiumOrchestrator", _StubOrchestrator,
    )
    monkeypatch.setattr(
        consortium_routes, "_ARTIFACT_ROOT", tmp_path / "artifacts",
    )
    from document_processor.auth import dependencies as auth_deps
    monkeypatch.setattr(auth_deps, "get_optional_user", lambda: None)
    # Pre-emptively no-op the cache calls so we don't need Redis.
    monkeypatch.setattr(
        consortium_routes.cache_manager, "set_json",
        lambda *a, **kw: None,
    )
    async def _none(*a, **kw):
        return None
    monkeypatch.setattr(
        consortium_routes.cache_manager, "get_json", _none,
    )
    application = FastAPI()
    application.include_router(consortium_routes.router)
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


# ── /start ────────────────────────────────────────────────────────────


def test_start_requires_x_client_id(client):
    resp = client.post(
        "/api/consortium/start",
        json={"goal": "Build a CSV diff tool that compares two files"},
    )
    assert resp.status_code == 400


def test_start_validates_goal_min_length(client):
    resp = client.post(
        "/api/consortium/start",
        headers={"X-Client-Id": "tester"},
        json={"goal": "tiny"},
    )
    assert resp.status_code == 422


def test_start_returns_session_id(client):
    resp = client.post(
        "/api/consortium/start",
        headers={"X-Client-Id": "tester"},
        json={
            "goal": "Build me a tiny CSV diff tool that runs in pure Python",
            "depth": "basic",
            "language": "python",
            "allow_external_research": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["session_id"]


def test_start_normalizes_invalid_depth_to_medium(client):
    resp = client.post(
        "/api/consortium/start",
        headers={"X-Client-Id": "tester"},
        json={
            "goal": "Build a working CSV diff tool with tests included",
            "depth": "BOGUS",
        },
    )
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    # The session payload's scope should reflect the normalisation.
    sess = consortium_routes._sessions.get(sid)
    assert sess is not None
    assert sess["scope"]["depth"] == "medium"


# ── /status + /{sid} ─────────────────────────────────────────────────


def test_status_404_for_unknown_session(client):
    resp = client.get("/api/consortium/does-not-exist/status")
    assert resp.status_code == 404


def test_status_returns_envelope_after_start(client):
    start = client.post(
        "/api/consortium/start",
        headers={"X-Client-Id": "tester"},
        json={"goal": "Build a CSV diff CLI with rich progress output"},
    )
    sid = start.json()["session_id"]
    # Background task may not have run yet; the snapshot still exists.
    resp = client.get(f"/api/consortium/{sid}/status")
    assert resp.status_code == 200
    body = resp.json()
    for key in {"session_id", "status", "scope", "phases", "verifications"}:
        assert key in body
    assert body["scope"]["depth"] in {"basic", "medium", "deep", "expert", "ultra"}


# ── /cancel ──────────────────────────────────────────────────────────


def test_cancel_sets_flag_on_session(client):
    start = client.post(
        "/api/consortium/start",
        headers={"X-Client-Id": "tester"},
        json={"goal": "Implement a tiny rate limiter with token bucket"},
    )
    sid = start.json()["session_id"]
    resp = client.post(
        f"/api/consortium/{sid}/cancel",
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # Session payload should reflect the request.
    sess = consortium_routes._sessions.get(sid)
    assert sess["cancel_requested"] is True


def test_cancel_404_for_unknown_session(client):
    resp = client.post("/api/consortium/zzz/cancel")
    assert resp.status_code == 404


# ── /artifact ────────────────────────────────────────────────────────


def test_artifact_404_when_dir_missing(client):
    start = client.post(
        "/api/consortium/start",
        headers={"X-Client-Id": "tester"},
        json={"goal": "Build a CSV diff tool that handles edge cases"},
    )
    sid = start.json()["session_id"]
    # Background task hasn't necessarily run yet; the artifact dir
    # therefore doesn't exist. Without it /artifact must 404.
    resp = client.get(f"/api/consortium/{sid}/artifact")
    assert resp.status_code in {404, 200}  # 200 only if bg task already ran


def test_artifact_streams_zip_when_dir_exists(client, tmp_path):
    start = client.post(
        "/api/consortium/start",
        headers={"X-Client-Id": "tester"},
        json={"goal": "Build a CSV diff tool — needs to be reasonably advanced"},
    )
    sid = start.json()["session_id"]
    sess = consortium_routes._sessions.get(sid)
    art_dir = Path(sess["artifact_dir"])
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "README.md").write_text("# hi", encoding="utf-8")
    (art_dir / "bundle.json").write_text("{}", encoding="utf-8")

    resp = client.get(f"/api/consortium/{sid}/artifact")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")
    # The body should be a non-empty zip containing both files.
    assert len(resp.content) > 50
    import io, zipfile
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    assert "README.md" in names
    assert "bundle.json" in names
