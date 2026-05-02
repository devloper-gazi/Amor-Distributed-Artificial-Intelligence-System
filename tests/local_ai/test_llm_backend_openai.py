"""Unit tests for the OpenAI-shape backends — Phase 16 Commit B.

Covers ``OpenAICompatibleBackend``, ``LlamaSwapBackend``, and
``LlamaCppBackend``.  All three speak the same wire shape (chat
completions on ``/v1/chat/completions``); the subclasses differ in
default port + identifier only.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from local_ai.llm_backend import (
    ChatMessage,
    ChatOptions,
    ChatResponse,
    make_backend,
)
from local_ai.llm_backend.llama_cpp import LlamaCppBackend
from local_ai.llm_backend.llama_swap import LlamaSwapBackend
from local_ai.llm_backend.openai_compat import OpenAICompatibleBackend


def _run(coro):
    return asyncio.run(coro)


# ─── identity + URL normalisation ──────────────────────────────────


def test_openai_compat_normalises_base_url_without_v1():
    b = OpenAICompatibleBackend(base_url="http://localhost:8080")
    assert b.base_url == "http://localhost:8080/v1"


def test_openai_compat_keeps_v1_suffix():
    b = OpenAICompatibleBackend(base_url="http://localhost:8080/v1")
    assert b.base_url == "http://localhost:8080/v1"


def test_openai_compat_strips_trailing_slash():
    b = OpenAICompatibleBackend(base_url="http://localhost:8080/v1/")
    assert b.base_url == "http://localhost:8080/v1"


def test_openai_compat_name_is_openai_compat():
    b = OpenAICompatibleBackend(base_url="http://localhost:8080")
    assert b.name == "openai-compat"


def test_llama_swap_default_port_and_name():
    b = LlamaSwapBackend()
    assert b.base_url == "http://localhost:11435/v1"
    assert b.name == "llama-swap"


def test_llama_cpp_default_port_and_name():
    b = LlamaCppBackend()
    assert b.base_url == "http://localhost:8080/v1"
    assert b.name == "llama-cpp"


def test_factory_constructs_each_subclass():
    assert isinstance(
        make_backend("llama-swap", url="http://localhost:11435"),
        LlamaSwapBackend,
    )
    assert isinstance(
        make_backend("llama-cpp", url="http://localhost:8080"),
        LlamaCppBackend,
    )
    assert isinstance(
        make_backend("openai-compat", url="http://localhost:9000"),
        OpenAICompatibleBackend,
    )


def test_factory_accepts_alias_kinds():
    # underscore + dot variants accepted.
    assert make_backend("llama_swap").name == "llama-swap"
    assert make_backend("llama_cpp").name == "llama-cpp"
    assert make_backend("llama.cpp").name == "llama-cpp"
    assert make_backend("openai_compat").name == "openai-compat"


# ─── headers + auth ────────────────────────────────────────────────


def test_headers_include_bearer_when_api_key_set():
    b = OpenAICompatibleBackend(
        base_url="http://localhost:8080", api_key="secret-token",
    )
    h = b._headers()
    assert h["Authorization"] == "Bearer secret-token"
    assert h["Content-Type"] == "application/json"


def test_headers_omit_bearer_without_key():
    b = OpenAICompatibleBackend(base_url="http://localhost:8080")
    h = b._headers()
    assert "Authorization" not in h


# ─── body packing ──────────────────────────────────────────────────


def test_build_body_basic():
    b = OpenAICompatibleBackend(base_url="http://localhost:8080")
    body = b._build_body(
        [{"role": "user", "content": "hi"}],
        model="m",
        options=ChatOptions(temperature=0.5, max_tokens=64),
        stream=False,
    )
    assert body["model"] == "m"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["temperature"] == 0.5
    assert body["max_tokens"] == 64
    assert body["stream"] is False


def test_build_body_extra_keys_propagate():
    b = OpenAICompatibleBackend(base_url="http://localhost:8080")
    body = b._build_body(
        [{"role": "user", "content": "hi"}],
        model="m",
        options=ChatOptions(extra={"presence_penalty": 0.7}),
        stream=True,
    )
    assert body["presence_penalty"] == 0.7
    assert body["stream"] is True


def test_build_body_seed_and_stop():
    b = OpenAICompatibleBackend(base_url="http://localhost:8080")
    body = b._build_body(
        [ChatMessage(role="user", content="hi")],
        model="m",
        options=ChatOptions(seed=42, stop=["\n\n", "###"]),
        stream=False,
    )
    assert body["seed"] == 42
    assert body["stop"] == ["\n\n", "###"]


# ─── chat() against a mocked HTTP transport ────────────────────────


def _mock_transport(*, status: int = 200, payload: dict | None = None):
    """Return an httpx MockTransport that replies once with the
    configured payload regardless of request."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload or {})
    return httpx.MockTransport(_handler)


def test_chat_parses_openai_response(monkeypatch):
    payload = {
        "model": "m",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "hello world"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 11,
            "total_tokens": 16,
        },
    }

    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(_handler)

    # Patch httpx.AsyncClient to use our transport for this test.
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    b = OpenAICompatibleBackend(base_url="http://localhost:8080")
    resp = _run(b.chat(
        [{"role": "user", "content": "hi"}],
        model="m",
        options=ChatOptions(temperature=0.3, max_tokens=32),
    ))
    assert isinstance(resp, ChatResponse)
    assert resp.content == "hello world"
    assert resp.usage.completion_tokens == 11
    assert resp.usage.total_tokens == 16
    assert resp.finish_reason == "stop"
    assert len(captured) == 1
    sent_body = captured[0].read()
    assert b"\"model\": \"m\"" in sent_body or b'"model":"m"' in sent_body


def test_chat_handles_empty_choices(monkeypatch):
    transport = _mock_transport(status=200, payload={"choices": []})
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    b = OpenAICompatibleBackend(base_url="http://localhost:8080")
    resp = _run(b.chat(
        [{"role": "user", "content": "hi"}], model="m",
    ))
    assert resp.content == ""


# ─── complete() routes through chat() ─────────────────────────────


def test_complete_uses_chat_internally(monkeypatch):
    payload = {
        "model": "m",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "completion"},
            "finish_reason": "stop",
        }],
    }
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured.append(_json.loads(request.content))
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(_handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    b = OpenAICompatibleBackend(base_url="http://localhost:8080")
    out = _run(b.complete("prompt-text", model="m", system="sys-text"))
    assert out == "completion"
    # complete() converts to a system+user chat exchange.
    assert captured[0]["messages"] == [
        {"role": "system", "content": "sys-text"},
        {"role": "user", "content": "prompt-text"},
    ]
