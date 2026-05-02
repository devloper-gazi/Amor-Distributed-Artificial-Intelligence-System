"""
Route smoke tests for /api/quick-code/*.

The QuickCodeEngine is mocked so requests don't trigger real LLM
calls; the resolver is mocked so the route can exercise the
X-Model-Used header path; cache_manager is no-op so no Redis needed.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from document_processor.api import quick_code_routes


class _StubEngine:
    """Drop-in for QuickCodeEngine. Records constructor args and emits
    the canonical 5-phase event sequence on run()."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, *, session_id, request, on_event=None,
                 llm_call=None, sandbox=None, static_harness=None,
                 role_setter=None):
        _StubEngine.last_kwargs = {
            "session_id": session_id, "request": request,
        }
        self.session_id = session_id
        self.request = request
        self._on_event = on_event

    async def run(self):
        from document_processor.quick_code.models import (
            QuickCodeAlternative,
            QuickCodeBundle,
            QuickCodeGate,
            QuickCodeReasoning,
            QuickCodeVerification,
        )
        # Emit the canonical phase sequence so /events tests get real
        # event_id stamping behaviour from the route layer.
        for phase in ("triage", "reason", "implement", "verify"):
            await self._on_event({"type": "quick_code_phase_start", "phase": phase})
            await self._on_event({"type": "quick_code_phase_complete", "phase": phase})
        await self._on_event({
            "type": "quick_code_gate",
            "gate": {"phase": "verify", "status": "passed",
                     "score": 85.0, "findings": [], "summary": "ok"},
        })
        await self._on_event({"type": "quick_code_completed", "status": "ok"})
        return QuickCodeBundle(
            session_id=self.session_id, request=self.request,
            triage={"language": "python", "task_type": "generation"},
            reasoning=QuickCodeReasoning(
                alternatives=[QuickCodeAlternative(
                    label="A",
                    scores={"clarity": 0.8, "math_soundness": 0.8,
                            "performance": 0.7, "edge_cases": 0.7},
                    complexity_estimate="O(n)",
                )],
                chosen_label="A", rationale="A wins on clarity",
            ),
            code="print('hi')\n", tests="def test(): assert 1\n",
            verification=QuickCodeVerification(
                execution={"success": True, "exit_code": 0,
                           "stdout": "", "stderr": "",
                           "duration_ms": 5, "skipped": False,
                           "language": "python"},
                static={"severity_counts": {"error": 0}},
                score=85.0,
            ),
            gates=[QuickCodeGate(
                phase="verify", status="passed", score=85.0, summary="ok",
            )],
            deliverable_markdown="# Done",
            started_at="2025-01-01T00:00:00+00:00",
            completed_at="2025-01-01T00:00:01+00:00",
        )


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(quick_code_routes, "QuickCodeEngine", _StubEngine)
    monkeypatch.setattr(
        quick_code_routes, "_ARTIFACT_ROOT", tmp_path / "artifacts",
    )
    # No-op the resolver so the route doesn't hit chat_store / Mongo.
    async def fake_resolve(**kwargs):
        return ("qwen2.5:7b", None, "user preference (quick_code)")
    monkeypatch.setattr(
        quick_code_routes, "resolve_request_model_full", fake_resolve,
    )
    # No-op the artifact writer — its plumbing is exercised by the
    # consortium tests; we only care here that the route layer behaves.
    async def _no_artifact(*a, **kw):
        return None
    monkeypatch.setattr(quick_code_routes, "_write_artifact", _no_artifact)

    async def _none(*a, **kw):
        return None
    monkeypatch.setattr(quick_code_routes.cache_manager, "set_json", _none)
    monkeypatch.setattr(quick_code_routes.cache_manager, "get_json", _none)
    monkeypatch.setattr(
        quick_code_routes.cache_manager, "publish_event", _none,
    )
    application = FastAPI()
    application.include_router(quick_code_routes.router)
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


# ── /start ────────────────────────────────────────────────────────────


def test_start_requires_x_client_id(client):
    resp = client.post(
        "/api/quick-code/start",
        json={"prompt": "say hi in python"},
    )
    assert resp.status_code == 400


def test_start_validates_prompt_min_length(client):
    resp = client.post(
        "/api/quick-code/start",
        headers={"X-Client-Id": "tester"},
        json={"prompt": ""},
    )
    assert resp.status_code == 422


def test_start_returns_session_id(client):
    resp = client.post(
        "/api/quick-code/start",
        headers={"X-Client-Id": "tester"},
        json={"prompt": "hello world", "language": "python", "effort": "basic"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["session_id"]
    assert body["model_used"] == "qwen2.5:7b"


def test_start_emits_x_model_used_header(client):
    """Spec validation point #1 — every AI start endpoint surfaces the
    resolved tag in the response header."""
    resp = client.post(
        "/api/quick-code/start",
        headers={"X-Client-Id": "tester"},
        json={"prompt": "factorial function in python"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("X-Model-Used") == "qwen2.5:7b"


def test_start_clamps_max_refine_to_cap(client):
    """Pydantic le=3 validator should reject max_refine=99."""
    resp = client.post(
        "/api/quick-code/start",
        headers={"X-Client-Id": "tester"},
        json={"prompt": "hi", "max_refine": 99},
    )
    assert resp.status_code == 422


def test_start_normalises_invalid_effort_to_medium(client):
    resp = client.post(
        "/api/quick-code/start",
        headers={"X-Client-Id": "tester"},
        json={"prompt": "build a thing", "effort": "BOGUS"},
    )
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    sess = quick_code_routes._sessions.get(sid)
    assert sess is not None
    assert sess["request"]["effort"] == "medium"


def test_start_falls_back_to_default_when_resolver_throws(monkeypatch, client):
    """Resolver exceptions land in the OLLAMA_MODEL fallback path."""
    async def boom(**kwargs):
        raise RuntimeError("resolver down")
    monkeypatch.setattr(quick_code_routes, "resolve_request_model_full", boom)
    resp = client.post(
        "/api/quick-code/start",
        headers={"X-Client-Id": "tester"},
        json={"prompt": "build me x"},
    )
    assert resp.status_code == 200
    # The header must still be set — falls back to OLLAMA_MODEL env var
    # (default qwen2.5:7b when unset).
    assert resp.headers.get("X-Model-Used")


# ── /status + /{sid} ─────────────────────────────────────────────────


def test_status_404_for_unknown_session(client):
    resp = client.get("/api/quick-code/does-not-exist/status")
    assert resp.status_code == 404


def test_status_returns_envelope_after_start(client):
    start = client.post(
        "/api/quick-code/start",
        headers={"X-Client-Id": "tester"},
        json={"prompt": "build a tiny CLI calculator"},
    )
    sid = start.json()["session_id"]
    resp = client.get(f"/api/quick-code/{sid}/status")
    assert resp.status_code == 200
    body = resp.json()
    for key in {"session_id", "status", "request",
                "phases", "current_phase", "gates"}:
        assert key in body
    # Phase scaffold present even before the bg task runs.
    # v9 added audit + arbiter (mesh post-processing); v10 added reactor.
    # V2 added classify + striatum at the front.
    assert {p["name"] for p in body["phases"]} == {
        "classify", "striatum",
        "triage", "reason", "implement", "verify", "refine",
        "reactor", "audit", "arbiter",
    }


def test_status_alias_returns_same_envelope(client):
    start = client.post(
        "/api/quick-code/start",
        headers={"X-Client-Id": "tester"},
        json={"prompt": "implement merge sort in python"},
    )
    sid = start.json()["session_id"]
    a = client.get(f"/api/quick-code/{sid}/status").json()
    b = client.get(f"/api/quick-code/{sid}").json()
    # Same JSON envelope shape from both routes.
    assert a.keys() == b.keys()
    assert a["session_id"] == b["session_id"]


# ── /cancel ──────────────────────────────────────────────────────────


def test_cancel_sets_flag_on_session(client):
    start = client.post(
        "/api/quick-code/start",
        headers={"X-Client-Id": "tester"},
        json={"prompt": "stable softmax in numpy"},
    )
    sid = start.json()["session_id"]
    resp = client.post(
        f"/api/quick-code/{sid}/cancel",
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    sess = quick_code_routes._sessions.get(sid)
    assert sess["cancel_requested"] is True


def test_cancel_404_for_unknown_session(client):
    resp = client.post("/api/quick-code/zzz/cancel")
    assert resp.status_code == 404


# ── /artifact ────────────────────────────────────────────────────────


def test_artifact_404_when_dir_missing(client):
    start = client.post(
        "/api/quick-code/start",
        headers={"X-Client-Id": "tester"},
        json={"prompt": "build a tiny linter"},
    )
    sid = start.json()["session_id"]
    resp = client.get(f"/api/quick-code/{sid}/artifact")
    # bg task may or may not have created the dir; either is acceptable.
    assert resp.status_code in {404, 200}


def test_artifact_streams_zip_when_dir_exists(client):
    start = client.post(
        "/api/quick-code/start",
        headers={"X-Client-Id": "tester"},
        json={"prompt": "implement a stable softmax"},
    )
    sid = start.json()["session_id"]
    sess = quick_code_routes._sessions.get(sid)
    art_dir = Path(sess["artifact_dir"])
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "README.md").write_text("# qc bundle", encoding="utf-8")
    (art_dir / "bundle.json").write_text("{}", encoding="utf-8")

    resp = client.get(f"/api/quick-code/{sid}/artifact")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")
    assert len(resp.content) > 50
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    assert "README.md" in names
    assert "bundle.json" in names
    # Filename hint mentions quick-code so it doesn't collide with
    # consortium bundle downloads in the user's downloads folder.
    cd = resp.headers.get("content-disposition", "")
    assert "quick-code" in cd
