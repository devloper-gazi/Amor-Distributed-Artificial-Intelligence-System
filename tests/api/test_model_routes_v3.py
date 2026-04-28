"""
v3 route tests — /search, /hardware, /test, /routing GET/PUT/DELETE,
plus the extended /preference body that now accepts a profile dict.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from document_processor.api import model_routes


class _StubManager:
    async def list_installed(self, force_refresh: bool = False):
        return []

    async def auto_select(self, mode="__all__", effort="medium"):
        return ("qwen2.5:7b", "auto-selected")

    async def search_models(self, query, *, source="all", limit=20):
        return [
            {
                "tag": f"{query}/{source}:Q4",
                "display_name": query.upper(),
                "source": "ollama_curated",
                "description": "stub",
                "license": "MIT",
                "size_bytes": 4_400_000_000,
                "stars": 100,
                "downloads": 1000,
                "spec": None,
            },
        ]

    async def detect_hardware(self):
        return {
            "gpu_available": True,
            "gpu_name": "RTX 4090",
            "gpu_count": 1,
            "vram_total_gb": 24.0,
            "vram_free_gb": 18.5,
            "cpu_threads": 24,
            "ollama_version": "0.5.0",
            "platform": "linux",
        }

    async def test_generate(self, *, model, prompt, profile=None, max_tokens=128):
        yield {"type": "test_start", "model": model}
        yield {"type": "test_chunk", "delta": "ok"}
        yield {"type": "test_done", "elapsed_ms": 42, "tokens": 1}


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
        AsyncMock(return_value={
            "strategy": "single",
            "single_tag": None,
            "mode_routes": {},
            "role_routes": {},
            "fallback_chain": [],
            "hardware_pref": "auto",
            "gpu_layers": None,
            "ensemble": {},
        }),
    )
    monkeypatch.setattr(
        model_routes.chat_store, "set_model_routing",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        model_routes.chat_store, "delete_model_routing",
        AsyncMock(return_value=True),
    )
    app.include_router(model_routes.router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ── /api/models/search ───────────────────────────────────────────────


def test_search_requires_query(client):
    resp = client.get("/api/models/search")
    assert resp.status_code == 422  # FastAPI: missing required `q`


def test_search_returns_results(client):
    resp = client.get("/api/models/search?q=qwen")
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "qwen"
    assert body["source"] == "all"
    assert isinstance(body["results"], list)
    assert body["results"][0]["tag"].startswith("qwen/")


def test_search_validates_source(client):
    resp = client.get("/api/models/search?q=qwen&source=mars")
    assert resp.status_code == 400


def test_search_validates_limit(client):
    resp = client.get("/api/models/search?q=qwen&limit=999")
    assert resp.status_code == 400


# ── /api/models/hardware ─────────────────────────────────────────────


def test_hardware_returns_stub(client):
    resp = client.get("/api/models/hardware")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gpu_available"] is True
    assert body["gpu_name"] == "RTX 4090"
    assert body["cpu_threads"] == 24


# ── /api/models/preference with profile ─────────────────────────────


def test_preference_accepts_full_profile(client):
    resp = client.put(
        "/api/models/preference",
        headers={"X-Client-Id": "tester"},
        json={
            "mode": "research",
            "model_tag": "qwen2.5:7b",
            "profile": {
                "temperature": 0.7,
                "top_p": 0.9,
                "num_ctx": 4096,
                "num_gpu": 999,
                "system_prompt": "You are concise.",
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"]["temperature"] == 0.7
    assert body["profile"]["system_prompt"] == "You are concise."


def test_preference_rejects_out_of_range_profile(client):
    resp = client.put(
        "/api/models/preference",
        headers={"X-Client-Id": "tester"},
        json={
            "mode": "research",
            "model_tag": "qwen2.5:7b",
            "profile": {"temperature": 5.0},  # > 2.0
        },
    )
    assert resp.status_code == 422


# ── /api/models/routing ──────────────────────────────────────────────


def test_get_routing_returns_default(client):
    resp = client.get(
        "/api/models/routing",
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy"] == "single"
    assert body["hardware_pref"] == "auto"


def test_put_routing_writes(client):
    resp = client.put(
        "/api/models/routing",
        headers={"X-Client-Id": "tester"},
        json={
            "strategy": "per_role",
            "role_routes": {"planner": "qwen2.5:7b", "coder": "deepseek-coder:6.7b"},
            "hardware_pref": "gpu",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy"] == "per_role"
    assert body["hardware_pref"] == "gpu"


def test_delete_routing_returns_ok(client):
    resp = client.delete(
        "/api/models/routing",
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_routing_requires_x_client_id(client):
    resp = client.get("/api/models/routing")
    assert resp.status_code == 400
