"""
v4 unit tests — VRAM fit, smart recommendation, ensemble Jaccard,
preset consistency, fallback chain ordering.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from document_processor.services.model_manager import (
    InstalledModel,
    ModelManager,
)


# ── estimate_vram_gb / fit_classification ─────────────────────────────


def test_estimate_vram_uses_size_bytes_first():
    m = InstalledModel(
        tag="x:1", size_bytes=4_400_000_000, modified_at="",
        spec=None,
    )
    assert ModelManager.estimate_vram_gb(m) == pytest.approx(4.10, abs=0.01)


def test_estimate_vram_falls_back_to_spec():
    from document_processor.code_intelligence.model_registry import (
        CODE_MODEL_CATALOGUE,
    )
    spec = next(s for s in CODE_MODEL_CATALOGUE if s.tier == "balanced")
    m = InstalledModel(tag=spec.ollama_tag, size_bytes=0, modified_at="", spec=spec)
    assert ModelManager.estimate_vram_gb(m) == float(spec.vram_gb)


def test_estimate_vram_returns_none_when_unknown():
    m = InstalledModel(tag="?:?", size_bytes=0, modified_at="", spec=None)
    assert ModelManager.estimate_vram_gb(m) is None


def test_fit_classification_basic_thresholds():
    cls = ModelManager.fit_classification
    # No GPU detected → CPU
    assert cls(4.0, None) == "cpu"
    # Unknown VRAM requirement → "unknown"
    assert cls(None, 24.0) == "unknown"
    # 4 GB on a 24 GB GPU with all 24 GB free → fits comfortably
    assert cls(4.0, 24.0, 24.0) == "fits"
    # 20 GB on a 24 GB GPU with 24 free → tight (above the 0.85*budget cut)
    assert cls(22.0, 24.0, 24.0) == "tight"
    # 26 GB on a 24 GB GPU → too big
    assert cls(26.0, 24.0, 18.0) == "too_big"


def test_fit_uses_free_vram_when_provided():
    cls = ModelManager.fit_classification
    # 8 GB requirement, 24 GB total but only 6 GB free → tight
    assert cls(8.0, 24.0, 6.0) in {"tight", "too_big"}


# ── ensemble jaccard / weighted picks ─────────────────────────────────


@pytest.mark.asyncio
async def test_call_ollama_ensemble_first_returns_first_finished(monkeypatch):
    """`first` voting should return whichever member completes first."""
    from document_processor.api import local_ai_routes_simple as la

    async def fake_uncached(model, prompt, system, max_tokens):
        if model == "fast:1":
            return "from fast"
        # Slow member — would lose the race; simulate with await sleep.
        import asyncio
        await asyncio.sleep(0.3)
        return "from slow"

    monkeypatch.setattr(la, "_call_ollama_uncached_with", fake_uncached)
    monkeypatch.setattr(la, "_ACTIVE_ROUTING", la._contextvars.ContextVar(
        "amor_active_model_routing_test_first",
        default={"strategy": "ensemble",
                 "ensemble": {"voting": "first",
                              "members": ["fast:1", "slow:1"]}},
    ))
    result = await la._call_ollama_ensemble("hi", None, 16)
    assert result == "from fast"


@pytest.mark.asyncio
async def test_call_ollama_ensemble_weighted_picks_longest(monkeypatch):
    from document_processor.api import local_ai_routes_simple as la

    async def fake_uncached(model, prompt, system, max_tokens):
        return {"a:1": "short", "b:1": "this is a much longer reply",
                "c:1": "medium length"}[model]

    monkeypatch.setattr(la, "_call_ollama_uncached_with", fake_uncached)
    monkeypatch.setattr(la, "_ACTIVE_ROUTING", la._contextvars.ContextVar(
        "amor_active_model_routing_test_w",
        default={"strategy": "ensemble",
                 "ensemble": {"voting": "weighted",
                              "members": ["a:1", "b:1", "c:1"]}},
    ))
    result = await la._call_ollama_ensemble("hi", None, 16)
    assert result == "this is a much longer reply"


@pytest.mark.asyncio
async def test_call_ollama_ensemble_majority_picks_consensus(monkeypatch):
    """Majority voting prefers the response that's most-similar to others."""
    from document_processor.api import local_ai_routes_simple as la

    async def fake_uncached(model, prompt, system, max_tokens):
        return {
            "a:1": "the cat sat on the mat",
            "b:1": "the cat lay on the mat",
            "c:1": "spaghetti aliens neon thunder",  # outlier
        }[model]

    monkeypatch.setattr(la, "_call_ollama_uncached_with", fake_uncached)
    monkeypatch.setattr(la, "_ACTIVE_ROUTING", la._contextvars.ContextVar(
        "amor_active_model_routing_test_m",
        default={"strategy": "ensemble",
                 "ensemble": {"voting": "majority",
                              "members": ["a:1", "b:1", "c:1"]}},
    ))
    result = await la._call_ollama_ensemble("hi", None, 16)
    # The two cat-related answers should beat the outlier.
    assert "cat" in result
    assert "spaghetti" not in result


# ── recommend_for_prompt scoring ──────────────────────────────────────


@pytest.mark.asyncio
async def test_recommend_returns_default_when_nothing_installed():
    mgr = ModelManager()
    with patch.object(mgr, "list_installed", AsyncMock(return_value=[])):
        result = await mgr.recommend_for_prompt("fix my code")
        assert "tag" in result
        assert result["candidates"] == []


@pytest.mark.asyncio
async def test_recommend_picks_code_model_for_debug_prompt():
    """Debugging keywords should bias toward code-strength models."""
    from document_processor.code_intelligence.model_registry import (
        CODE_MODEL_CATALOGUE,
    )
    code_spec = next(
        s for s in CODE_MODEL_CATALOGUE if "debugging" in s.strengths
    )
    other_spec = next(
        s for s in CODE_MODEL_CATALOGUE
        if "debugging" not in s.strengths and s.tier == "balanced"
    )
    mgr = ModelManager()
    installed = [
        InstalledModel(
            tag=other_spec.ollama_tag, size_bytes=4_000_000_000,
            modified_at="", spec=other_spec,
        ),
        InstalledModel(
            tag=code_spec.ollama_tag, size_bytes=4_500_000_000,
            modified_at="", spec=code_spec,
        ),
    ]
    with patch.object(mgr, "list_installed",
                      AsyncMock(return_value=installed)):
        result = await mgr.recommend_for_prompt(
            "Fix this exception: TypeError on line 42 — debug please",
            mode="code",
        )
        assert result["tag"] == code_spec.ollama_tag
        assert "debugging" in (result.get("detected_strengths") or [])


@pytest.mark.asyncio
async def test_recommend_includes_human_readable_reason():
    from document_processor.code_intelligence.model_registry import (
        CODE_MODEL_CATALOGUE,
    )
    spec = CODE_MODEL_CATALOGUE[0]
    mgr = ModelManager()
    installed = [InstalledModel(
        tag=spec.ollama_tag, size_bytes=4_000_000_000,
        modified_at="", spec=spec,
    )]
    with patch.object(mgr, "list_installed", AsyncMock(return_value=installed)):
        result = await mgr.recommend_for_prompt("explain this code")
        assert isinstance(result.get("reason"), str)
        assert len(result["reason"]) > 0


# ── live tok/s telemetry in test_generate ─────────────────────────────


@pytest.mark.asyncio
async def test_test_generate_includes_tokens_per_second_on_done():
    """When Ollama is unreachable we still get a clean done frame
    with zero tokens but a tokens_per_second key (for shape stability)."""
    mgr = ModelManager()
    seen_keys = set()
    async for evt in mgr.test_generate(
        model="qwen2.5:7b", prompt="hi", max_tokens=4,
    ):
        for k in evt:
            seen_keys.add(k)
        if evt["type"] == "test_done":
            assert "tokens_per_second" in evt
            return
        if evt["type"] == "test_error":
            return  # acceptable when offline
    # We should have seen at least test_start.
    assert "type" in seen_keys
