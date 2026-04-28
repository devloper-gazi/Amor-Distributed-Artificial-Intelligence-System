"""
Route smoke tests for `/api/models/*`.

These tests only exercise the *route shape* — auth, header
requirements, body validation, and the expected 4xx/5xx mappings.
The actual model_manager + chat_store side-effects are stubbed so
nothing hits Mongo, Redis, or Ollama.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from document_processor.api import model_routes


# ── helpers ──────────────────────────────────────────────────────────


class _StubManager:
    """Drop-in replacement for ModelManager — every public coroutine
    returns a deterministic, deserialisable payload."""

    async def list_installed(self, force_refresh: bool = False):
        return []

    async def auto_select(self, mode: str = "__all__", effort: str = "medium"):
        return ("qwen2.5:7b", f"auto-selected — best fit for {mode}/{effort}")

    async def pull_model_stream(self, tag: str):
        yield {"type": "pull_start", "tag": tag}
        yield {"type": "pull_progress", "tag": tag, "pct": 50}
        yield {"type": "pull_complete", "tag": tag}

    async def import_gguf(self, **kwargs):
        return {"tag": "custom/test:abc12345", "display_name": "Test"}

    async def delete_custom_model(self, **kwargs):
        return None

    # v4 — these methods are called from the same routes the v2 tests
    # exercise, so the stub has to grow to match the real surface.
    async def detect_hardware(self):
        return {
            "gpu_available": False, "gpu_name": None, "gpu_count": 0,
            "vram_total_gb": None, "vram_free_gb": None,
            "cpu_threads": 4, "ollama_version": None, "platform": None,
        }

    async def warmup_model(self, tag: str):
        return True

    async def search_models(self, query, *, source="all", limit=20):
        return []

    async def test_generate(self, *, model, prompt, profile=None, max_tokens=128):
        yield {"type": "test_start", "model": model}
        yield {"type": "test_done", "elapsed_ms": 1, "tokens": 0,
               "tokens_per_second": 0.0}

    async def recommend_for_prompt(self, prompt, *, mode="__all__",
                                    usage=None, hardware=None):
        return {"tag": "qwen2.5:7b", "reason": "stub", "score": 0,
                "candidates": [], "detected_strengths": []}


@pytest.fixture
def app(monkeypatch):
    """Minimal FastAPI app with only the model_router mounted."""
    app = FastAPI()
    app.state.model_manager = _StubManager()
    # Force the routes to skip auth (get_optional_user → None).
    from document_processor.auth import dependencies as auth_deps
    monkeypatch.setattr(auth_deps, "get_optional_user", lambda: None)
    # Stub chat_store.* methods used by the routes.
    monkeypatch.setattr(
        model_routes.chat_store, "get_model_preference",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        model_routes.chat_store, "get_all_model_preferences",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        model_routes.chat_store, "set_model_preference",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        model_routes.chat_store, "delete_model_preference",
        AsyncMock(return_value=True),
    )
    app.include_router(model_routes.router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ── GET /api/models ──────────────────────────────────────────────────


def test_list_models_requires_x_client_id(client):
    resp = client.get("/api/models")
    assert resp.status_code == 400
    assert "X-Client-Id" in resp.text


def test_list_models_returns_envelope(client):
    resp = client.get(
        "/api/models?mode=research&effort=medium",
        headers={"X-Client-Id": "tester-1"},
    )
    assert resp.status_code == 200
    data = resp.json()
    for key in {"installed", "catalogue", "ollama_available",
                "default_tag", "active_preference", "auto_select"}:
        assert key in data
    assert data["auto_select"]["mode"] == "research"


def test_list_models_rejects_invalid_mode(client):
    resp = client.get(
        "/api/models?mode=bogus",
        headers={"X-Client-Id": "tester-1"},
    )
    assert resp.status_code == 400


# ── GET /api/models/auto-select ──────────────────────────────────────


def test_auto_select_preview_returns_pick(client):
    resp = client.get(
        "/api/models/auto-select?mode=thinking&effort=deep",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "thinking"
    assert body["effort"] == "deep"
    assert body["tag"]


def test_auto_select_preview_rejects_invalid_effort(client):
    resp = client.get("/api/models/auto-select?effort=zoom")
    assert resp.status_code == 400


# ── PUT /api/models/preference ───────────────────────────────────────


def test_set_preference_round_trip(client):
    resp = client.put(
        "/api/models/preference",
        headers={"X-Client-Id": "tester-1"},
        json={"mode": "research", "model_tag": "qwen2.5-coder:7b"},
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "research"


def test_set_preference_rejects_invalid_mode(client):
    resp = client.put(
        "/api/models/preference",
        headers={"X-Client-Id": "tester-1"},
        json={"mode": "bogus", "model_tag": "x:1"},
    )
    assert resp.status_code == 400


def test_set_preference_rejects_missing_tag(client):
    resp = client.put(
        "/api/models/preference",
        headers={"X-Client-Id": "tester-1"},
        json={"mode": "research"},  # no model_tag
    )
    assert resp.status_code == 422


# ── DELETE /api/models/preference/{mode} ─────────────────────────────


def test_delete_preference_returns_ok(client):
    resp = client.delete(
        "/api/models/preference/research",
        headers={"X-Client-Id": "tester-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "research"


# ── POST /api/models/upload ──────────────────────────────────────────


def test_upload_rejects_non_gguf_filename(client):
    resp = client.post(
        "/api/models/upload",
        headers={"X-Client-Id": "tester-1"},
        files={"file": ("not-a-model.txt", b"hello world",
                        "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert ".gguf" in resp.text


def test_upload_rejects_empty_file(client):
    resp = client.post(
        "/api/models/upload",
        headers={"X-Client-Id": "tester-1"},
        files={"file": ("empty.gguf", b"", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_upload_passes_to_manager_for_valid_gguf(client, app, monkeypatch):
    """A valid GGUF magic-byte file routes into manager.import_gguf."""
    captured = {}

    async def fake_import(**kw):
        captured.update(kw)
        return {"tag": "custom/blob:abcd0001", "display_name": "Blob"}

    app.state.model_manager.import_gguf = fake_import  # type: ignore

    resp = client.post(
        "/api/models/upload",
        headers={"X-Client-Id": "tester-1"},
        files={"file": ("blob.gguf", b"GGUF" + b"\0" * 32,
                        "application/octet-stream")},
        data={"display_name": "Blob"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tag"] == "custom/blob:abcd0001"
    assert captured["filename"] == "blob.gguf"
    assert captured["display_name"] == "Blob"


# ── DELETE /api/models/custom/{tag:path} ─────────────────────────────


def test_delete_custom_rejects_non_custom_namespace(client):
    """The route refuses to delete an official Ollama tag."""
    resp = client.delete(
        "/api/models/custom/qwen2.5:7b",
        headers={"X-Client-Id": "tester-1"},
    )
    assert resp.status_code == 400
    assert "custom/" in resp.text


def test_delete_custom_returns_ok_for_owner(client):
    resp = client.delete(
        "/api/models/custom/custom/blob:abcd0001",
        headers={"X-Client-Id": "tester-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["tag"] == "custom/blob:abcd0001"


def test_delete_custom_403_when_manager_raises_permission(
    client, app, monkeypatch,
):
    async def boom(**kw):
        raise PermissionError("not yours")
    app.state.model_manager.delete_custom_model = boom  # type: ignore

    resp = client.delete(
        "/api/models/custom/custom/blob:abcd0001",
        headers={"X-Client-Id": "tester-1"},
    )
    assert resp.status_code == 403
