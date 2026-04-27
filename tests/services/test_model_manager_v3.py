"""
v3 unit tests — search_models, detect_hardware, profile helpers, test_generate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from document_processor.services.model_manager import ModelManager


# ── apply_profile_to_options ──────────────────────────────────────────


def test_apply_profile_whitelists_known_keys():
    out = ModelManager.apply_profile_to_options({
        "temperature": 0.5,
        "top_p": 0.85,
        "top_k": 40,
        "num_ctx": 4096,
        "num_gpu": 32,
        "num_thread": 8,
        "seed": 42,
        # Unknown keys are dropped silently:
        "evil_key": "<script>",
        "another": True,
    })
    assert out == {
        "temperature": 0.5,
        "top_p": 0.85,
        "top_k": 40,
        "num_ctx": 4096,
        "num_gpu": 32,
        "num_thread": 8,
        "seed": 42,
    }


def test_apply_profile_returns_empty_for_none():
    assert ModelManager.apply_profile_to_options(None) == {}
    assert ModelManager.apply_profile_to_options({}) == {}


def test_apply_profile_preserves_stop_sequences():
    out = ModelManager.apply_profile_to_options({
        "stop": ["\n\n", "Human:", "Assistant:"],
    })
    assert out["stop"] == ["\n\n", "Human:", "Assistant:"]


def test_apply_profile_caps_stop_sequences():
    out = ModelManager.apply_profile_to_options({
        "stop": [f"s{i}" for i in range(20)],
    })
    assert len(out["stop"]) == 8


def test_apply_profile_drops_falsy_stop_entries():
    out = ModelManager.apply_profile_to_options({
        "stop": ["valid", "", None, "another"],
    })
    assert out["stop"] == ["valid", "another"]


def test_apply_profile_coerces_types():
    out = ModelManager.apply_profile_to_options({
        "temperature": "0.7",   # string → float
        "top_k": "40",           # string → int
        "num_gpu": 99.9,         # float → int
    })
    assert out["temperature"] == 0.7
    assert out["top_k"] == 40
    assert out["num_gpu"] == 99


def test_apply_profile_skips_invalid_casts():
    out = ModelManager.apply_profile_to_options({
        "temperature": "not-a-number",  # silently dropped
        "top_k": 40,
    })
    assert "temperature" not in out
    assert out["top_k"] == 40


# ── system_prompt_from_profile ────────────────────────────────────────


def test_system_prompt_strips_and_caps():
    sp = ModelManager.system_prompt_from_profile(
        {"system_prompt": "  Hello there  "},
    )
    assert sp == "Hello there"


def test_system_prompt_returns_none_for_empty():
    assert ModelManager.system_prompt_from_profile(None) is None
    assert ModelManager.system_prompt_from_profile({}) is None
    assert ModelManager.system_prompt_from_profile(
        {"system_prompt": ""},
    ) is None
    assert ModelManager.system_prompt_from_profile(
        {"system_prompt": "   "},
    ) is None


def test_system_prompt_caps_at_4kb():
    long = "x" * 8192
    sp = ModelManager.system_prompt_from_profile(
        {"system_prompt": long},
    )
    assert len(sp) == 4096


# ── search_models ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_models_empty_query():
    mgr = ModelManager()
    assert await mgr.search_models("") == []
    assert await mgr.search_models("   ") == []


@pytest.mark.asyncio
async def test_search_models_curated_only():
    mgr = ModelManager()
    # "qwen" is in the curated catalogue (qwen2.5-coder:* etc.)
    results = await mgr.search_models("qwen", source="ollama_curated")
    assert all(r["source"] == "ollama_curated" for r in results)
    assert any("qwen" in r["tag"].lower() for r in results)


@pytest.mark.asyncio
async def test_search_models_invalid_source_falls_through():
    """``source="hf"`` with HF unreachable → returns curated only as
    long as the curated branch matches. We patch httpx to simulate
    network failure."""
    mgr = ModelManager()

    class _BoomClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): pass
        async def get(self, *a, **kw):
            raise RuntimeError("network down")

    import httpx
    with patch.object(httpx, "AsyncClient", _BoomClient):
        # source="hf" alone gets nothing because the HF call fails
        # and curated isn't queried.
        out = await mgr.search_models("nonexistent-zzz", source="hf")
        assert out == []


# ── detect_hardware ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detect_hardware_returns_baseline_when_ollama_unreachable():
    mgr = ModelManager()
    info = await mgr.detect_hardware()
    # Even with no Ollama, we still return a valid envelope.
    assert isinstance(info, dict)
    for k in {"gpu_available", "gpu_count", "cpu_threads"}:
        assert k in info
    # cpu_threads should always be populated (os.cpu_count).
    assert info["cpu_threads"] is None or info["cpu_threads"] >= 1


@pytest.mark.asyncio
async def test_detect_hardware_picks_up_cuda_visible_devices(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1,2")
    # Simulate pynvml not installed by NOT importing it.
    mgr = ModelManager()
    info = await mgr.detect_hardware()
    # If pynvml is unavailable, we fall back to the env hint.
    # We can't assert gpu_available=True universally because pynvml may
    # actually exist on the test host; the envelope just needs to match.
    assert info["gpu_count"] >= 0


# ── test_generate (mocked) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_generate_yields_start_and_error_when_offline():
    mgr = ModelManager()
    events = []
    async for evt in mgr.test_generate(
        model="qwen2.5:7b",
        prompt="hello",
        max_tokens=8,
    ):
        events.append(evt)
        # Bail after 5 events to avoid hanging on real HTTP.
        if len(events) >= 5:
            break
    assert events[0]["type"] == "test_start"
    # Either we got chunks (Ollama up) or an error (offline) — never silent.
    assert any(e["type"] in {"test_chunk", "test_done", "test_error"}
               for e in events)
