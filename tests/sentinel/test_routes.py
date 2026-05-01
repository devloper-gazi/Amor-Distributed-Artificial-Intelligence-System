"""Smoke tests for ``/api/sentinel/*`` routes.

We use FastAPI TestClient with the real router but a fake engine
injection (the route's `engine` reference is stored in the session
dict, so we patch _SESSIONS post-start to swap in a pre-cooked
bundle).  This exercises the route surface without spinning up
Docker / Ollama.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from document_processor.api.sentinel_routes import _SESSIONS, router
from document_processor.sentinel.models import (
    Finding,
    SentinelBundle,
    SentinelRequest,
)


@pytest.fixture
def app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app_with_router: FastAPI) -> TestClient:
    return TestClient(app_with_router)


@pytest.fixture(autouse=True)
def _clear_sessions():
    _SESSIONS.clear()
    yield
    _SESSIONS.clear()


# ─── start endpoint ────────────────────────────────────────────────


def test_start_requires_paths_or_code(client: TestClient):
    resp = client.post(
        "/api/sentinel/start",
        json={"prompt": "audit", "scan_profile": "quick"},
        headers={"X-Client-Id": "smoke"},
    )
    assert resp.status_code == 400


def test_start_requires_x_client_id(client: TestClient):
    resp = client.post(
        "/api/sentinel/start",
        json={"prompt": "audit", "paths": ["x.py"], "scan_profile": "quick"},
    )
    assert resp.status_code == 400


def test_start_with_valid_payload_returns_session_id(
    client: TestClient, tmp_path: Path,
):
    p = tmp_path / "x.py"
    p.write_text("x = 1\n", encoding="utf-8")
    resp = client.post(
        "/api/sentinel/start",
        json={
            "prompt": "audit",
            "paths": [str(p)],
            "scan_profile": "quick",
        },
        headers={"X-Client-Id": "smoke-2"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["session_id"]
    assert data["scan_profile"] == "quick"


def test_start_rejects_unknown_profile(client: TestClient, tmp_path: Path):
    p = tmp_path / "x.py"
    p.write_text("x = 1", encoding="utf-8")
    resp = client.post(
        "/api/sentinel/start",
        json={
            "paths": [str(p)],
            "scan_profile": "ultra-deep-paranoid-mode",
        },
        headers={"X-Client-Id": "smoke-3"},
    )
    assert resp.status_code == 422  # pydantic validation


# ─── status endpoint ───────────────────────────────────────────────


def test_status_404_for_unknown_session(client: TestClient):
    resp = client.get(
        "/api/sentinel/does-not-exist/status",
        headers={"X-Client-Id": "smoke-4"},
    )
    assert resp.status_code == 404


def test_status_returns_envelope_after_scan_completes(
    client: TestClient, tmp_path: Path,
):
    """Inject a finished session directly to bypass the live engine."""
    bundle = SentinelBundle(
        session_id="s-known",
        request=SentinelRequest(scan_profile="quick"),
        findings=[Finding(tool="bandit", severity="medium", confidence=0.5)],
        repo_risk_score=2.5,
    )
    bundle.severity_histogram = {"medium": 1}
    _SESSIONS["s-known"] = {
        "session_id": "s-known",
        "user_id": None,
        "client_id": "smoke",
        "scan_profile": "quick",
        "request": bundle.request.to_dict(),
        "status": "ok",
        "started_at": "2026-05-01T12:00:00Z",
        "started_at_ts": 0,
        "completed_at": "2026-05-01T12:00:30Z",
        "queue": None,
        "engine": None,
        "bundle": bundle,
        "phases": [{"name": "report", "status": "completed"}],
        "current_phase": None,
        "events_seen": 7,
        "task": None,
    }
    resp = client.get(
        "/api/sentinel/s-known/status",
        headers={"X-Client-Id": "smoke"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["bundle"] is not None
    assert data["bundle"]["session_id"] == "s-known"


# ─── cancel endpoint ───────────────────────────────────────────────


def test_cancel_known_session_returns_ok(client: TestClient):
    class _FakeEngine:
        def __init__(self):
            self.cancelled = False
        def cancel(self):
            self.cancelled = True
    fake = _FakeEngine()
    _SESSIONS["c-1"] = {
        "session_id": "c-1",
        "engine": fake,
        "task": None,
        "started_at_ts": 0,
    }
    resp = client.post(
        "/api/sentinel/c-1/cancel",
        headers={"X-Client-Id": "smoke"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert fake.cancelled is True


def test_cancel_unknown_session_404(client: TestClient):
    resp = client.post(
        "/api/sentinel/none/cancel",
        headers={"X-Client-Id": "smoke"},
    )
    assert resp.status_code == 404


# ─── artifact endpoint ─────────────────────────────────────────────


def test_artifact_404_when_no_bundle(client: TestClient):
    _SESSIONS["a-1"] = {"session_id": "a-1", "started_at_ts": 0,
                        "bundle": None}
    resp = client.get(
        "/api/sentinel/a-1/artifact",
        headers={"X-Client-Id": "smoke"},
    )
    assert resp.status_code == 409  # bundle not ready yet


def test_artifact_sarif_format(client: TestClient):
    bundle = SentinelBundle(
        session_id="a-2",
        request=SentinelRequest(),
        sarif_report='{"$schema":"x","version":"2.1.0","runs":[]}',
    )
    _SESSIONS["a-2"] = {
        "session_id": "a-2", "started_at_ts": 0, "bundle": bundle,
    }
    resp = client.get(
        "/api/sentinel/a-2/artifact?format=sarif",
        headers={"X-Client-Id": "smoke"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/sarif+json")
    assert json.loads(resp.text)["version"] == "2.1.0"


def test_artifact_zip_includes_all_formats(client: TestClient):
    bundle = SentinelBundle(
        session_id="a-3",
        request=SentinelRequest(),
        sarif_report='{"version":"2.1.0"}',
        markdown_report="# Report",
        html_report="<html></html>",
    )
    _SESSIONS["a-3"] = {
        "session_id": "a-3", "started_at_ts": 0, "bundle": bundle,
    }
    resp = client.get(
        "/api/sentinel/a-3/artifact",
        headers={"X-Client-Id": "smoke"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    # Zip header magic
    assert resp.content[:2] == b"PK"
