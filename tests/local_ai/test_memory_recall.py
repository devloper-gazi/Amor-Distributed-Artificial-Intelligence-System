"""
Cycle C Sprint 7 Day 4 — engine memory-recall hook tests.

The hook lives in ``document_processor/services/memory_recall.py`` —
pure async helper.  Tests pin:

* The recall is a no-op when Mem0 is disabled.
* When Mem0 is enabled (stub regime), the helper returns a normalised
  ``RecallResult`` with the matched memories.
* ``format_recall_block`` produces a Markdown block with bullet-style
  snippets and ``None`` when there's nothing to render.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from document_processor.services.memory_recall import (
    RecallResult,
    format_recall_block,
    memory_recall_enabled_in_engine,
    recall_for_prompt,
)


@pytest.fixture(autouse=True)
def _reset_adapter():
    import local_ai.memory.mem0_adapter as adapter_mod
    adapter_mod._MEM0_INSTALLED = None
    adapter_mod._GLOBAL_ADAPTER = None
    yield
    adapter_mod._MEM0_INSTALLED = None
    adapter_mod._GLOBAL_ADAPTER = None


def test_memory_recall_disabled_when_env_unset(monkeypatch):
    monkeypatch.delenv("AMOR_MEMORY_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "mem0", raising=False)
    assert memory_recall_enabled_in_engine() is False


def test_memory_recall_enabled_when_env_and_lib_present(monkeypatch):
    fake_mem0 = ModuleType("mem0")
    fake_mem0.Memory = SimpleNamespace(from_config=lambda cfg: object())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mem0", fake_mem0)
    monkeypatch.setenv("AMOR_MEMORY_BACKEND", "mem0")
    assert memory_recall_enabled_in_engine() is True


def test_memory_recall_force_disabled_via_env(monkeypatch):
    """Operators can flip ``AMOR_MEMORY_RECALL_ENABLED=0`` to keep
    Mem0 ingestion live but stop the engine from injecting context."""
    fake_mem0 = ModuleType("mem0")
    fake_mem0.Memory = SimpleNamespace(from_config=lambda cfg: object())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mem0", fake_mem0)
    monkeypatch.setenv("AMOR_MEMORY_BACKEND", "mem0")
    monkeypatch.setenv("AMOR_MEMORY_RECALL_ENABLED", "0")
    assert memory_recall_enabled_in_engine() is False


@pytest.mark.asyncio
async def test_recall_for_empty_prompt_short_circuits(monkeypatch):
    monkeypatch.delenv("AMOR_MEMORY_BACKEND", raising=False)
    result = await recall_for_prompt("", user_id="u1", limit=5)
    assert result.count == 0
    assert result.available is False


@pytest.mark.asyncio
async def test_recall_disabled_returns_empty(monkeypatch):
    monkeypatch.delenv("AMOR_MEMORY_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "mem0", raising=False)
    result = await recall_for_prompt("hello", user_id="u1", limit=3)
    assert result.count == 0
    assert result.available is False
    assert result.backend == "native"


class _FakeMem0:
    def search(self, query, user_id, limit):
        return [
            {"id": "1", "memory": "User likes mango", "score": 0.9},
            {"id": "2", "memory": "User speaks Turkish", "score": 0.8},
        ]
    # mem0 adapter only calls .search in the recall path — stub the rest
    # to avoid attribute errors if anything else is touched.
    def add(self, *a, **k): return {"results": []}
    def get_all(self, *a, **k): return []
    def delete(self, *a, **k): return None


@pytest.mark.asyncio
async def test_recall_returns_normalised_records(monkeypatch):
    fake_mem0 = ModuleType("mem0")
    fake_mem0.Memory = SimpleNamespace(from_config=lambda cfg: _FakeMem0())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mem0", fake_mem0)
    monkeypatch.setenv("AMOR_MEMORY_BACKEND", "mem0")
    import local_ai.memory.mem0_adapter as adapter_mod
    adapter_mod._MEM0_INSTALLED = None

    result = await recall_for_prompt("what does the user like?", user_id="u1", limit=5)
    assert result.count == 2
    assert "mango" in result.snippets[0]
    assert result.backend == "mem0"
    assert result.available is True


def test_format_recall_block_renders_bullets():
    r = RecallResult(
        count=2,
        snippets=["User likes mango", "User speaks Turkish"],
        backend="mem0",
        available=True,
    )
    out = format_recall_block(r)
    assert out is not None
    assert "## Recalled memory" in out
    assert "- User likes mango" in out
    assert "- User speaks Turkish" in out


def test_format_recall_block_returns_none_for_empty():
    r = RecallResult(count=0, snippets=[], backend="native", available=False)
    assert format_recall_block(r) is None


@pytest.mark.asyncio
async def test_recall_swallows_search_errors(monkeypatch):
    """A misbehaving Mem0 client must not propagate — recall is
    advisory; downstream pipelines must not crash.  The adapter's
    own try/except converts the exception into an empty list, so the
    helper still reports ``backend='mem0'`` (the lib IS functional)
    but ``count=0`` so the engine treats it as no recall."""
    class _ExplodingClient:
        def search(self, *a, **k):
            raise RuntimeError("boom")

    fake_mem0 = ModuleType("mem0")
    fake_mem0.Memory = SimpleNamespace(  # type: ignore[attr-defined]
        from_config=lambda cfg: _ExplodingClient(),
    )
    monkeypatch.setitem(sys.modules, "mem0", fake_mem0)
    monkeypatch.setenv("AMOR_MEMORY_BACKEND", "mem0")
    import local_ai.memory.mem0_adapter as adapter_mod
    adapter_mod._MEM0_INSTALLED = None

    result = await recall_for_prompt("anything", user_id="u1", limit=3)
    # Hard guarantee: pipeline never sees a crash + never sees fake data.
    assert result.count == 0
    assert result.snippets == []
    # The pipeline's only behavioural switch is ``count > 0`` so the
    # backend label can either be "mem0" (adapter alive but search
    # failed) or "native" (adapter not constructed); we don't pin it.
