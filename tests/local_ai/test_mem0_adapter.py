"""
Cycle C Sprint 7 Day 1 — Mem0 adapter tests.

Two regimes:

* **No-op regime (default)** — exercises the safety guarantees the
  rest of the codebase relies on: ``add`` / ``search`` / ``get_all``
  / ``delete`` never raise even when mem0 is absent / disabled.
* **Stubbed-client regime** — installs a fake ``mem0.Memory`` and
  asserts the adapter normalises Mem0's return shapes correctly.

Real mem0 isn't installed in CI; the stub regime stands in for it.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from local_ai.memory.mem0_adapter import (
    Mem0Adapter,
    MemoryRecord,
    _normalise,
    get_default_adapter,
    mem0_available,
    mem0_enabled,
    reset_default_adapter,
)


@pytest.fixture(autouse=True)
def _reset_adapter():
    reset_default_adapter()
    yield
    reset_default_adapter()


# ─── feature-probe / no-op regime ────────────────────────────────


def test_mem0_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AMOR_MEMORY_BACKEND", raising=False)
    assert mem0_enabled() is False


def test_mem0_disabled_when_env_set_but_lib_absent(monkeypatch):
    """``AMOR_MEMORY_BACKEND=mem0`` alone isn't enough — the adapter
    refuses to claim availability when ``import mem0`` would fail."""
    monkeypatch.setenv("AMOR_MEMORY_BACKEND", "mem0")
    # If the test host happens to have mem0 installed we skip — this
    # test pins the negative path.
    if mem0_available():
        pytest.skip("real mem0 is installed; this test exercises the absent path")
    assert mem0_enabled() is False


def test_adapter_constructible_without_mem0(monkeypatch):
    monkeypatch.delenv("AMOR_MEMORY_BACKEND", raising=False)
    adapter = Mem0Adapter(user_id="alice")
    assert adapter._client is None
    s = adapter.status()
    assert s.backend == "native"
    assert s.available is False
    assert s.user_namespace == "alice"


def test_adapter_no_op_methods_never_raise():
    """The whole point of the wrapper — every method is safe to
    call even when the adapter is degraded."""
    adapter = Mem0Adapter(user_id="alice")
    assert adapter.add("hello") == []
    assert adapter.add([{"role": "user", "content": "hi"}], metadata={"a": 1}) == []
    assert adapter.search("hello") == []
    assert adapter.get_all(limit=10) == []
    assert adapter.delete("any-id") is False


def test_default_adapter_is_singleton():
    a = get_default_adapter()
    b = get_default_adapter()
    assert a is b


def test_reset_default_adapter_rebuilds():
    a = get_default_adapter()
    reset_default_adapter()
    b = get_default_adapter()
    assert a is not b


# ─── normaliser ─────────────────────────────────────────────────


def test_normalise_handles_results_envelope():
    raw = {
        "results": [
            {"id": "1", "memory": "user likes mango", "score": 0.92, "metadata": {"k": "v"}},
            {"id": "2", "text": "second memory", "score": 0.81},
        ],
    }
    out = _normalise(raw, fallback_user="alice")
    assert len(out) == 2
    assert out[0].text == "user likes mango"
    assert out[0].score == pytest.approx(0.92)
    assert out[0].metadata == {"k": "v"}
    assert out[1].text == "second memory"
    assert out[1].user_id == "alice"  # no user_id in entry → fallback


def test_normalise_handles_bare_list():
    raw = [{"memory_id": "abc", "memory": "hello", "user_id": "alice"}]
    out = _normalise(raw, fallback_user="bob")
    assert len(out) == 1
    assert out[0].id == "abc"
    assert out[0].user_id == "alice"  # explicit beats fallback


def test_normalise_skips_non_dict_entries():
    raw = [{"id": "1", "memory": "ok"}, "garbage", None, 42]
    out = _normalise(raw, fallback_user="x")
    assert len(out) == 1
    assert out[0].id == "1"


def test_normalise_handles_missing_score_and_dates():
    raw = [{"id": "1", "memory": "no score"}]
    out = _normalise(raw, fallback_user="x")
    assert out[0].score is None
    assert out[0].created_at is None
    assert out[0].updated_at is None


# ─── stubbed-client regime ──────────────────────────────────────


class _FakeMemoryClient:
    """Stub matching the slice of ``mem0.Memory`` we touch."""

    def __init__(self):
        self.added: list[Any] = []
        self.searched: list[tuple[str, str, int]] = []
        self.deleted: list[str] = []

    def add(self, messages, user_id, metadata):
        self.added.append((messages, user_id, metadata))
        return {
            "results": [
                {"id": "stub-1", "memory": "extracted fact", "user_id": user_id},
            ],
        }

    def search(self, query, user_id, limit):
        self.searched.append((query, user_id, limit))
        return [
            {"id": "stub-2", "memory": f"hit:{query}", "score": 0.7, "user_id": user_id},
        ]

    def get_all(self, user_id, limit):
        return [{"id": "stub-3", "memory": "long-term", "user_id": user_id}]

    def delete(self, memory_id):
        self.deleted.append(memory_id)


def _install_fake_mem0(monkeypatch, fake_client: _FakeMemoryClient) -> None:
    """Inject a fake ``mem0`` module so the adapter's lazy import
    path resolves to our stub."""
    fake_mem0 = ModuleType("mem0")
    fake_mem0.Memory = SimpleNamespace(from_config=lambda cfg: fake_client)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mem0", fake_mem0)
    # Reset the cached probe.
    import local_ai.memory.mem0_adapter as adapter_mod
    adapter_mod._MEM0_INSTALLED = None
    monkeypatch.setenv("AMOR_MEMORY_BACKEND", "mem0")


def test_adapter_uses_client_when_enabled(monkeypatch, tmp_path):
    fake = _FakeMemoryClient()
    _install_fake_mem0(monkeypatch, fake)
    adapter = Mem0Adapter(user_id="alice", root=tmp_path / "mem")
    assert adapter._client is fake
    assert adapter.status().backend == "mem0"

    out = adapter.add("user prefers mango", metadata={"src": "test"})
    assert len(out) == 1
    assert out[0].text == "extracted fact"
    assert fake.added[0][1] == "alice"

    hits = adapter.search("mango", limit=4)
    assert len(hits) == 1
    assert hits[0].text == "hit:mango"
    assert fake.searched[0] == ("mango", "alice", 4)

    everything = adapter.get_all(limit=2)
    assert len(everything) == 1
    assert everything[0].user_id == "alice"

    assert adapter.delete("stub-3") is True
    assert fake.deleted == ["stub-3"]


def test_adapter_swallows_client_errors(monkeypatch, tmp_path):
    """A misbehaving Mem0 client must NOT propagate — memory is
    advisory; an exception there should not crash the calling
    pipeline.  Adapter logs + returns the no-op result."""
    class _ExplodingClient:
        def add(self, *_, **__): raise RuntimeError("boom")
        def search(self, *_, **__): raise RuntimeError("boom")
        def get_all(self, *_, **__): raise RuntimeError("boom")
        def delete(self, *_, **__): raise RuntimeError("boom")

    fake = _ExplodingClient()
    fake_mem0 = ModuleType("mem0")
    fake_mem0.Memory = SimpleNamespace(from_config=lambda cfg: fake)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mem0", fake_mem0)
    import local_ai.memory.mem0_adapter as adapter_mod
    adapter_mod._MEM0_INSTALLED = None
    monkeypatch.setenv("AMOR_MEMORY_BACKEND", "mem0")

    adapter = Mem0Adapter(user_id="alice", root=tmp_path / "mem")
    assert adapter.add("hi") == []
    assert adapter.search("anything") == []
    assert adapter.get_all() == []
    assert adapter.delete("x") is False
