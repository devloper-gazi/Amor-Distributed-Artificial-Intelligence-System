"""Unit tests for ``local_ai.memory`` — Phase 16 Commit F.

3-tier memory hierarchy plus ledger audit + Sentinel ``_BaseAgent``
DI integration.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from local_ai.memory import (
    ArchivalEntry,
    ArchivalMemoryBackend,
    CoreMemoryBackend,
    MemoryStats,
    MemoryStore,
    RecallEntry,
    RecallMemoryBackend,
    all_memory_tools,
    make_no_op_store,
)


def _run(coro):
    return asyncio.run(coro)


# ─── Core tier ──────────────────────────────────────────────────────


def test_core_read_empty_returns_empty_dict(tmp_path: Path):
    core = CoreMemoryBackend(tmp_path / "core.sqlite")
    assert core.read() == {}


def test_core_write_and_read_roundtrip(tmp_path: Path):
    core = CoreMemoryBackend(tmp_path / "core.sqlite")
    ok, size = core.write({"persona": "Sentinel", "task": "audit"})
    assert ok is True
    assert size > 0
    assert core.read() == {"persona": "Sentinel", "task": "audit"}


def test_core_byte_cap_blocks_oversized_writes(tmp_path: Path):
    core = CoreMemoryBackend(tmp_path / "core.sqlite", max_bytes=64)
    big = {"k": "x" * 1000}
    ok, size = core.write(big)
    assert ok is False
    assert size > 64
    # Existing row stays empty.
    assert core.read() == {}


def test_core_patch_merges(tmp_path: Path):
    core = CoreMemoryBackend(tmp_path / "core.sqlite")
    core.write({"a": 1, "b": 2})
    ok, _ = core.patch({"b": 3, "c": 4})
    assert ok is True
    assert core.read() == {"a": 1, "b": 3, "c": 4}


def test_core_separate_scopes(tmp_path: Path):
    core = CoreMemoryBackend(tmp_path / "core.sqlite")
    core.write({"v": 1}, scope="alice")
    core.write({"v": 2}, scope="bob")
    assert core.read(scope="alice") == {"v": 1}
    assert core.read(scope="bob") == {"v": 2}


# ─── Recall tier ───────────────────────────────────────────────────


def test_recall_append_returns_entry(tmp_path: Path):
    recall = RecallMemoryBackend(tmp_path / "recall.sqlite")
    e = recall.append("user", "hi", metadata={"src": "test"})
    assert isinstance(e, RecallEntry)
    assert e.role == "user"
    assert e.content == "hi"
    assert e.metadata == {"src": "test"}


def test_recall_latest_chronological_order(tmp_path: Path):
    recall = RecallMemoryBackend(tmp_path / "recall.sqlite")
    for i in range(5):
        recall.append("user", f"msg-{i}")
        time.sleep(0.001)  # ensure inserted_at differentiation
    rows = recall.latest()
    assert [r.content for r in rows] == [f"msg-{i}" for i in range(5)]


def test_recall_ring_buffer_evicts_oldest(tmp_path: Path):
    recall = RecallMemoryBackend(tmp_path / "recall.sqlite", window_size=3)
    for i in range(6):
        recall.append("user", f"msg-{i}")
    rows = recall.latest()
    # Only the last 3 survive.
    assert [r.content for r in rows] == ["msg-3", "msg-4", "msg-5"]
    assert recall.count() == 3


def test_recall_search_substring(tmp_path: Path):
    recall = RecallMemoryBackend(tmp_path / "recall.sqlite")
    recall.append("user", "hello world")
    recall.append("user", "goodbye")
    recall.append("assistant", "hello there")
    rows = recall.search("hello")
    assert len(rows) == 2
    assert all("hello" in r.content for r in rows)


# ─── Archival tier ─────────────────────────────────────────────────


def test_archival_archive_and_substring_search(tmp_path: Path):
    arch = ArchivalMemoryBackend(tmp_path / "arch.sqlite")
    _run(arch.archive("the quick brown fox", metadata={"id": "f1"}))
    _run(arch.archive("a different document"))
    rows = _run(arch.search("brown fox"))
    assert len(rows) >= 1
    assert any("brown fox" in r.text for r in rows)


def test_archival_search_with_embedder(tmp_path: Path):
    """When an embedder is wired, search uses cosine similarity."""
    docs_to_vec = {
        "fox": [1.0, 0.0, 0.0],
        "dog": [0.0, 1.0, 0.0],
        "cat": [0.0, 0.0, 1.0],
    }

    async def fake_embedder(text):
        if isinstance(text, list):
            return [docs_to_vec[t] for t in text]
        return [docs_to_vec[text]]

    arch = ArchivalMemoryBackend(
        tmp_path / "arch.sqlite", embedder=fake_embedder,
    )
    _run(arch.archive("fox"))
    _run(arch.archive("dog"))
    _run(arch.archive("cat"))
    # Query "fox" should rank fox > dog/cat (cosine = 1.0 vs 0.0).
    rows = _run(arch.search("fox", limit=3))
    assert rows[0].text == "fox"
    assert rows[0].score == pytest.approx(1.0)


def test_archival_count_and_clear(tmp_path: Path):
    arch = ArchivalMemoryBackend(tmp_path / "arch.sqlite")
    _run(arch.archive("a"))
    _run(arch.archive("b"))
    assert arch.count() == 2
    arch.clear()
    assert arch.count() == 0


# ─── MemoryStore orchestrator ──────────────────────────────────────


def test_memory_store_three_tiers_independent(tmp_path: Path):
    store = MemoryStore(root=tmp_path)
    # Core
    _run(store.write_core({"name": "Auditor"}))
    assert store.read_core() == {"name": "Auditor"}
    # Recall
    _run(store.append_recall("user", "hi"))
    assert len(store.latest_recall()) == 1
    # Archival
    _run(store.archive("first long-term note"))
    rows = _run(store.search_archival("long-term"))
    assert len(rows) >= 1


def test_memory_store_stats(tmp_path: Path):
    store = MemoryStore(root=tmp_path)
    _run(store.write_core({"a": 1}))
    _run(store.append_recall("u", "x"))
    _run(store.archive("y"))
    stats = store.stats()
    assert isinstance(stats, MemoryStats)
    assert stats.recall_count == 1
    assert stats.archival_count == 1


def test_memory_store_clear_all(tmp_path: Path):
    store = MemoryStore(root=tmp_path)
    _run(store.write_core({"a": 1}))
    _run(store.append_recall("u", "x"))
    _run(store.archive("y"))
    store.clear_all()
    assert store.read_core() == {}
    assert store.latest_recall() == []
    rows = _run(store.search_archival("y"))
    assert rows == []


def test_memory_store_ledger_audit_hook(tmp_path: Path):
    """When a ledger hook is supplied, every write fires it."""
    audited: list[tuple[str, dict]] = []

    def hook(kind: str, payload: dict) -> None:
        audited.append((kind, dict(payload)))

    store = MemoryStore(
        root=tmp_path, ledger_hook=hook, audit_enabled=True,
    )
    _run(store.write_core({"a": 1}))
    _run(store.append_recall("user", "hi"))
    _run(store.archive("note"))

    kinds = [k for k, _ in audited]
    assert "memory_core_written" in kinds
    assert "memory_recall_appended" in kinds
    assert "memory_archival_written" in kinds


def test_memory_store_audit_can_be_disabled(tmp_path: Path):
    audited: list[str] = []

    def hook(kind, payload):  # noqa: ARG001
        audited.append(kind)

    store = MemoryStore(
        root=tmp_path, ledger_hook=hook, audit_enabled=False,
    )
    _run(store.write_core({"a": 1}))
    assert audited == []


# ─── make_no_op_store fallback ────────────────────────────────────


def test_make_no_op_store_is_functional():
    store = make_no_op_store()
    _run(store.write_core({"x": 1}))
    assert store.read_core() == {"x": 1}


# ─── memory tools (Tool ABC subclasses) ───────────────────────────


def test_all_memory_tools_returns_seven(tmp_path: Path):
    store = MemoryStore(root=tmp_path)
    tools = all_memory_tools(store)
    names = {t.name for t in tools}
    expected = {
        "memory_core_read",
        "memory_core_write",
        "memory_core_patch",
        "memory_recall_append",
        "memory_recall_search",
        "memory_archive",
        "memory_archival_search",
    }
    assert names == expected


def test_memory_core_write_tool_round_trip(tmp_path: Path):
    from local_ai.memory.tools import CoreReadTool, CoreWriteTool

    store = MemoryStore(root=tmp_path)
    write = CoreWriteTool(store)
    read = CoreReadTool(store)
    args = write.validate({"payload": {"persona": "Auditor"}})
    res = _run(write.execute(args))
    assert res.ok
    res2 = read.execute(read.validate({}))
    assert res2.output == {"persona": "Auditor"}


def test_memory_recall_append_tool(tmp_path: Path):
    from local_ai.memory.tools import RecallAppendTool, RecallSearchTool

    store = MemoryStore(root=tmp_path)
    appender = RecallAppendTool(store)
    res = _run(appender.execute(appender.validate({
        "role": "user", "content": "hello world",
    })))
    assert res.ok
    searcher = RecallSearchTool(store)
    found = searcher.execute(searcher.validate({"query": "hello"}))
    assert found.ok
    assert any(r["content"] == "hello world" for r in found.output)


# ─── Sentinel agent DI integration ────────────────────────────────


def test_sentinel_base_agent_accepts_memory(tmp_path: Path):
    from document_processor.sentinel.agents import _BaseAgent

    store = MemoryStore(root=tmp_path)
    agent = _BaseAgent(memory=store)
    assert agent.memory is store


def test_sentinel_base_agent_default_memory_is_lazy(tmp_path: Path, monkeypatch):
    """A bare ``_BaseAgent()`` doesn't construct a memory store
    until ``_default_memory()`` is called."""
    from document_processor.sentinel.agents import _BaseAgent

    agent = _BaseAgent()
    assert agent.memory is None
    mem = _run(agent._default_memory())
    assert mem is not None
    # Cached on the instance.
    mem2 = _run(agent._default_memory())
    assert mem is mem2


def test_sentinel_concrete_agents_still_construct(tmp_path: Path):
    """The new ``memory`` field doesn't break existing agent
    construction."""
    from document_processor.sentinel.agents import (
        AuditorAgent,
        JudgeAgent,
        PatcherAgent,
        ReasonerAgent,
        RedTeamAgent,
    )

    for cls in (
        AuditorAgent, ReasonerAgent, RedTeamAgent,
        PatcherAgent, JudgeAgent,
    ):
        agent = cls()
        assert agent.role
        assert agent.memory is None
