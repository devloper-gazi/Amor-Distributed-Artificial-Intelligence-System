"""Unit tests for ``local_ai.llm_backend`` — Phase 16 Commit A.

Covers the ABC contract, ``normalize_messages`` coercion, the
``StubBackend`` round-trip, ``OllamaBackend`` constructor + options
packing, and the singleton/factory wiring.
"""

from __future__ import annotations

import asyncio

import pytest

from local_ai.llm_backend import (
    ChatMessage,
    ChatOptions,
    ChatResponse,
    LLMBackend,
    OllamaBackend,
    StubBackend,
    _reset_backend_cache,
    _set_backend,
    get_backend,
    make_backend,
    normalize_messages,
)


def _run(coro):
    return asyncio.run(coro)


# ─── ABC contract ──────────────────────────────────────────────────


def test_llmbackend_is_abstract():
    with pytest.raises(TypeError):
        LLMBackend()  # type: ignore[abstract]


def test_stub_implements_full_contract():
    s = StubBackend()
    # Sanity: every abstract method is concrete.
    assert callable(s.chat)
    assert callable(s.stream_chat)
    assert callable(s.complete)
    assert callable(s.health_check)
    assert callable(s.list_models)
    assert s.name == "stub"


# ─── normalize_messages ────────────────────────────────────────────


def test_normalize_messages_dicts_passthrough():
    msgs = [{"role": "user", "content": "hi"}]
    out = normalize_messages(msgs)
    assert out == msgs
    # Defensive copy — mutation must not bleed back.
    out[0]["content"] = "mutated"
    assert msgs[0]["content"] == "hi"


def test_normalize_messages_dataclass():
    msgs = [ChatMessage(role="user", content="hi")]
    out = normalize_messages(msgs)
    assert out == [{"role": "user", "content": "hi"}]


def test_normalize_messages_keeps_optional_fields():
    msgs = [ChatMessage(
        role="tool", content="42", name="calc", tool_call_id="t1",
    )]
    out = normalize_messages(msgs)
    assert out[0]["name"] == "calc"
    assert out[0]["tool_call_id"] == "t1"


def test_normalize_messages_rejects_unknown_type():
    with pytest.raises(TypeError):
        normalize_messages([42])  # type: ignore[list-item]


def test_normalize_messages_rejects_dict_missing_keys():
    with pytest.raises(ValueError):
        normalize_messages([{"role": "user"}])


# ─── StubBackend ───────────────────────────────────────────────────


def test_stub_chat_returns_canned_response_round_robin():
    s = StubBackend(responses=["alpha", "beta"])
    r1 = _run(s.chat([{"role": "user", "content": "hi"}], model="m"))
    r2 = _run(s.chat([{"role": "user", "content": "hi"}], model="m"))
    r3 = _run(s.chat([{"role": "user", "content": "hi"}], model="m"))
    assert (r1.content, r2.content, r3.content) == ("alpha", "beta", "alpha")


def test_stub_chat_records_calls():
    s = StubBackend(responses=["x"])
    _run(s.chat(
        [{"role": "user", "content": "hi"}],
        model="m",
        options=ChatOptions(temperature=0.3, max_tokens=64),
    ))
    assert len(s.calls) == 1
    call = s.calls[0]
    assert call["kind"] == "chat"
    assert call["model"] == "m"
    assert call["options"].temperature == 0.3


def test_stub_complete_returns_text():
    s = StubBackend(responses=["completion-text"])
    out = _run(s.complete("prompt", model="m", system="sys"))
    assert out == "completion-text"


def test_stub_stream_yields_chars():
    s = StubBackend(responses=["abc"])

    async def collect():
        out: list[str] = []
        async for chunk in s.stream_chat(
            [{"role": "user", "content": "hi"}], model="m",
        ):
            out.append(chunk)
        return out

    assert _run(collect()) == ["a", "b", "c"]


def test_stub_health_and_list_models():
    s = StubBackend(models=["m1", "m2"])
    assert _run(s.health_check()) is True
    assert _run(s.list_models()) == ["m1", "m2"]


def test_stub_queue_and_reset():
    s = StubBackend(responses=["x"])
    _run(s.chat([{"role": "user", "content": "hi"}], model="m"))
    s.queue_response("y")
    r = _run(s.chat([{"role": "user", "content": "hi"}], model="m"))
    assert r.content == "y"
    s.reset()
    assert s.calls == []
    # After reset() the round-robin index is back at 0.
    r = _run(s.chat([{"role": "user", "content": "hi"}], model="m"))
    assert r.content == "x"


# ─── OllamaBackend ────────────────────────────────────────────────


def test_ollama_backend_default_url():
    b = OllamaBackend()
    assert b.base_url == "http://localhost:11434"
    assert b.name == "ollama"


def test_ollama_backend_strips_trailing_slash():
    b = OllamaBackend(base_url="http://localhost:11434/")
    assert b.base_url == "http://localhost:11434"


def test_ollama_backend_options_pack_correctly():
    b = OllamaBackend()
    opts = ChatOptions(
        temperature=0.5,
        max_tokens=42,
        seed=7,
        stop=["\n"],
        extra={"top_k": 50, "num_ctx": 8192},
    )
    packed = b._build_options(opts)
    assert packed["temperature"] == 0.5
    assert packed["num_predict"] == 42
    assert packed["seed"] == 7
    assert packed["stop"] == ["\n"]
    assert packed["top_k"] == 50
    assert packed["num_ctx"] == 8192


def test_ollama_backend_options_minimal_defaults():
    b = OllamaBackend()
    packed = b._build_options(ChatOptions())
    assert "num_predict" not in packed   # max_tokens=None → unset
    assert "seed" not in packed
    assert "stop" not in packed
    assert packed["temperature"] == pytest.approx(0.7)


# ─── factory + singleton ──────────────────────────────────────────


def test_make_backend_ollama_returns_ollama_instance():
    b = make_backend("ollama", url="http://localhost:11434")
    assert isinstance(b, OllamaBackend)
    assert b.name == "ollama"


def test_make_backend_stub():
    b = make_backend("stub")
    assert isinstance(b, StubBackend)


def test_make_backend_unknown_kind_raises():
    with pytest.raises(ValueError):
        make_backend("magic-engine")


def test_make_backend_normalises_kind_case():
    b = make_backend("OLLAMA")
    assert isinstance(b, OllamaBackend)


def test_get_backend_returns_singleton(monkeypatch):
    monkeypatch.setenv("AMOR_LLM_BACKEND", "stub")
    _reset_backend_cache()
    a = get_backend()
    b = get_backend()
    assert a is b
    _reset_backend_cache()


def test_get_backend_explicit_kind_overrides_env(monkeypatch):
    monkeypatch.setenv("AMOR_LLM_BACKEND", "ollama")
    _reset_backend_cache()
    b = get_backend("stub")
    assert isinstance(b, StubBackend)
    _reset_backend_cache()


def test_set_backend_overrides_cache():
    _reset_backend_cache()
    sentinel = StubBackend(responses=["sentinel"])
    _set_backend("ollama", sentinel)
    b = get_backend("ollama")
    assert b is sentinel
    _reset_backend_cache()


def test_chat_response_dataclass_defaults():
    r = ChatResponse(content="x", model="m")
    assert r.finish_reason == "stop"
    assert r.usage.prompt_tokens == 0
    assert r.raw == {}
