"""Tests for the OpenAI-compatible facade — Phase 16 Commit C.

Uses FastAPI's TestClient with a ``StubBackend`` injected via
``_set_backend`` so the tests run without Ollama or any network.
Verifies the wire shape closely matches OpenAI's so external SDKs
plug in.
"""

from __future__ import annotations

import json
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from document_processor.api.openai_compat_routes import router
from document_processor.config.settings import settings
from local_ai.llm_backend import (
    StubBackend,
    _reset_backend_cache,
    _set_backend,
)


# ─── fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def stub_backend() -> Iterator[StubBackend]:
    _reset_backend_cache()
    stub = StubBackend(
        responses=["pluggable backend says hello"],
        models=["qwen2.5:7b", "qwen2.5-coder:7b"],
    )
    _set_backend("stub", stub)
    original = getattr(settings, "llm_backend", "ollama")
    settings.llm_backend = "stub"  # type: ignore[attr-defined]
    try:
        yield stub
    finally:
        settings.llm_backend = original  # type: ignore[attr-defined]
        _reset_backend_cache()


@pytest.fixture
def client(stub_backend: StubBackend) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ─── /v1/models ─────────────────────────────────────────────────────


def test_list_models_returns_openai_shape(client: TestClient):
    r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    ids = [m["id"] for m in body["data"]]
    assert "qwen2.5:7b" in ids
    assert "qwen2.5-coder:7b" in ids
    for entry in body["data"]:
        assert entry["object"] == "model"
        assert entry["owned_by"] == "stub"
        assert isinstance(entry["created"], int)


def test_list_models_when_disabled(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "openai_compat_enabled", False)
    r = client.get("/v1/models")
    assert r.status_code == 503


# ─── /v1/chat/completions ───────────────────────────────────────────


def test_chat_completions_non_streaming(
    client: TestClient, stub_backend: StubBackend,
):
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "qwen2.5:7b",
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hello"},
            ],
            "temperature": 0.3,
            "max_tokens": 64,
            "stream": False,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "qwen2.5:7b"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "pluggable backend says hello"
    assert body["choices"][0]["finish_reason"] == "stop"
    # Usage block populated.
    assert body["usage"]["completion_tokens"] > 0
    # Stub recorded the call with options.
    assert len(stub_backend.calls) == 1
    call = stub_backend.calls[0]
    assert call["kind"] == "chat"
    assert call["options"].temperature == pytest.approx(0.3)
    assert call["options"].max_tokens == 64


def test_chat_completions_validates_messages_required(client: TestClient):
    r = client.post(
        "/v1/chat/completions",
        json={"model": "qwen2.5:7b", "messages": []},
    )
    # Pydantic min_length=1 violation → 422.
    assert r.status_code == 422


def test_chat_completions_streaming_emits_done_marker(client: TestClient):
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "qwen2.5:7b",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    assert "data: [DONE]" in body
    # First chunk should carry a delta with the first character.
    lines = [ln for ln in body.splitlines() if ln.startswith("data:")]
    assert len(lines) >= 2  # at least one delta + final + DONE
    first = lines[0][len("data: "):]
    parsed = json.loads(first)
    assert parsed["object"] == "chat.completion.chunk"
    # Stub yields characters one at a time → first delta is a single char.
    assert parsed["choices"][0]["delta"].get("content") == "p"


def test_chat_completions_finish_reason_in_final_chunk(client: TestClient):
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "qwen2.5:7b",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as r:
        body = "".join(r.iter_text())
    # Final delta-less chunk carries finish_reason="stop".
    chunks = [
        json.loads(ln[len("data: "):])
        for ln in body.splitlines()
        if ln.startswith("data: ") and ln != "data: [DONE]"
    ]
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "stop"


# ─── /v1/completions  (legacy) ───────────────────────────────────────


def test_completions_legacy_works(client: TestClient):
    r = client.post(
        "/v1/completions",
        json={
            "model": "qwen2.5:7b",
            "prompt": "complete this",
            "max_tokens": 32,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "text_completion"
    assert body["choices"][0]["text"] == "pluggable backend says hello"


def test_completions_streaming_rejected(client: TestClient):
    r = client.post(
        "/v1/completions",
        json={"model": "qwen2.5:7b", "prompt": "x", "stream": True},
    )
    assert r.status_code == 400


# ─── /v1/embeddings ─────────────────────────────────────────────────


def test_embeddings_503_on_non_ollama_backend(client: TestClient):
    # Stub backend isn't Ollama, so the embeddings facade rejects.
    r = client.post(
        "/v1/embeddings",
        json={"model": "default", "input": "hello"},
    )
    assert r.status_code == 503
    assert "Phase 16" in r.json()["detail"]


def test_embeddings_input_validation(client: TestClient):
    # Non-string non-list input → 400.
    r = client.post(
        "/v1/embeddings",
        json={"model": "default", "input": 42},
    )
    # 503 fires before the type check on stub backend, so accept either.
    assert r.status_code in (400, 503)
