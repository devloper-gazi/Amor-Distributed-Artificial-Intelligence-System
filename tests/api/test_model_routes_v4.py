"""
v4 route tests — /warmup, /usage, /recommend, plus the v4 fields on /api/models.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from document_processor.api import model_routes
from document_processor.services.model_manager import InstalledModel


class _StubManager:
    async def list_installed(self, force_refresh: bool = False):
        from document_processor.code_intelligence.model_registry import (
            CODE_MODEL_CATALOGUE,
        )
        spec = CODE_MODEL_CATALOGUE[0]
        return [InstalledModel(
            tag=spec.ollama_tag, size_bytes=4_400_000_000,
            modified_at="", spec=spec,
        )]

    async def auto_select(self, mode="__all__", effort="medium"):
        return ("qwen2.5:7b", "auto-selected")

    async def detect_hardware(self):
        return {
            "gpu_available": True, "gpu_name": "RTX 4090",
            "gpu_count": 1, "vram_total_gb": 24.0,
            "vram_free_gb": 20.0, "cpu_threads": 24,
            "ollama_version": "0.5.0", "platform": "linux",
        }

    async def warmup_model(self, tag: str):
        return True

    async def recommend_for_prompt(self, prompt, *, mode="__all__",
                                    usage=None, hardware=None):
        return {
            "tag": "qwen2.5-coder:7b",
            "display_name": "Qwen2.5-Coder 7B",
            "score": 78.5,
            "reason": "matches code generation, debugging · fits comfortably",
            "candidates": [],
            "detected_strengths": ["code generation", "debugging"],
        }


@pytest.fixture
def app(monkeypatch):
    app = FastAPI()
    app.state.model_manager = _StubManager()
    from document_processor.auth import dependencies as auth_deps
    monkeypatch.setattr(auth_deps, "get_optional_user", lambda: None)
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
    monkeypatch.setattr(
        model_routes.chat_store, "get_model_routing",
        AsyncMock(return_value={"strategy": "single", "single_tag": None,
                                "mode_routes": {}, "role_routes": {},
                                "fallback_chain": [], "hardware_pref": "auto",
                                "gpu_layers": None, "ensemble": {}}),
    )
    monkeypatch.setattr(
        model_routes.chat_store, "set_model_routing",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        model_routes.chat_store, "delete_model_routing",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        model_routes.chat_store, "increment_model_usage",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        model_routes.chat_store, "get_model_usage",
        AsyncMock(return_value={
            "qwen2.5:7b": {
                "count_total": 47,
                "by_mode": {"research": 30, "code": 17},
                "last_used_at": "2026-04-27T12:00:00Z",
            },
        }),
    )
    app.include_router(model_routes.router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ── /api/models surfaces v4 fields ────────────────────────────────────


def test_list_models_includes_hardware_envelope(client):
    resp = client.get(
        "/api/models?mode=research",
        headers={"X-Client-Id": "tester-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "hardware" in body
    assert body["hardware"]["gpu_name"] == "RTX 4090"


def test_list_models_includes_fit_and_usage(client, monkeypatch):
    # Override the usage mock so the count keys our installed tag.
    from document_processor.code_intelligence.model_registry import (
        CODE_MODEL_CATALOGUE,
    )
    spec = CODE_MODEL_CATALOGUE[0]
    monkeypatch.setattr(
        model_routes.chat_store, "get_model_usage",
        AsyncMock(return_value={
            spec.ollama_tag: {"count_total": 47, "by_mode": {"research": 47}},
        }),
    )
    resp = client.get(
        "/api/models?mode=research",
        headers={"X-Client-Id": "tester-1"},
    )
    body = resp.json()
    assert body["installed"]
    m = body["installed"][0]
    assert "fit" in m
    assert m["fit"] in {"fits", "tight", "too_big", "cpu", "unknown"}
    assert "usage" in m
    assert m["usage"]["count_total"] == 47


# ── /api/models/warmup ────────────────────────────────────────────────


def test_warmup_returns_ok(client):
    resp = client.post(
        "/api/models/warmup",
        headers={"X-Client-Id": "tester-1"},
        json={"tag": "qwen2.5:7b"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["tag"] == "qwen2.5:7b"


def test_warmup_rejects_empty_tag(client):
    resp = client.post(
        "/api/models/warmup",
        headers={"X-Client-Id": "tester-1"},
        json={"tag": ""},
    )
    assert resp.status_code == 422


# ── /api/models/usage ─────────────────────────────────────────────────


def test_usage_returns_per_tag_counts(client):
    resp = client.get(
        "/api/models/usage",
        headers={"X-Client-Id": "tester-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "usage" in body
    assert body["usage"]["qwen2.5:7b"]["count_total"] == 47
    assert body["usage"]["qwen2.5:7b"]["by_mode"]["research"] == 30


def test_usage_requires_x_client_id(client):
    resp = client.get("/api/models/usage")
    assert resp.status_code == 400


# ── /api/models/recommend ─────────────────────────────────────────────


def test_recommend_returns_suggestion(client):
    resp = client.post(
        "/api/models/recommend",
        headers={"X-Client-Id": "tester-1"},
        json={"prompt": "Fix this Python bug — it raises TypeError on line 42",
              "mode": "code"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tag"] == "qwen2.5-coder:7b"
    assert body["score"] > 0
    assert "code generation" in body["detected_strengths"]


def test_recommend_validates_mode(client):
    resp = client.post(
        "/api/models/recommend",
        headers={"X-Client-Id": "tester-1"},
        json={"prompt": "test", "mode": "bogus"},
    )
    assert resp.status_code == 400


def test_recommend_requires_prompt(client):
    resp = client.post(
        "/api/models/recommend",
        headers={"X-Client-Id": "tester-1"},
        json={"prompt": "", "mode": "code"},
    )
    assert resp.status_code == 422


# ── PUT /preference now also bumps usage counter ──────────────────────


def test_preference_save_increments_usage(client, monkeypatch):
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(model_routes.chat_store, "increment_model_usage", spy)
    resp = client.put(
        "/api/models/preference",
        headers={"X-Client-Id": "tester-1"},
        json={"mode": "research", "model_tag": "qwen2.5:7b"},
    )
    assert resp.status_code == 200
    spy.assert_awaited_once()
    args = spy.await_args.kwargs
    assert args["tag"] == "qwen2.5:7b"
    assert args["mode"] == "research"
    assert args["kind"] == "preference"
