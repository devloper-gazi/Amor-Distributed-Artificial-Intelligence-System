"""Tests for v17 PR #2 — persistent default-memory factory.

The Phase-16 ``make_no_op_store`` used ``tempfile.mkdtemp`` whose
path is silently GC'd by Windows ``%TEMP%`` cleanup / Docker
overlay GC mid-session.  Agents that lazily constructed a default
memory store lost their writes between turns when the OS swept
the temp dir.

PR #2 replaces the temp-dir path with a persistent root under
``settings.memory_root / default-<pid>/``.  These tests verify:

1. ``make_persistent_default_store`` writes survive across calls.
2. The path is stable within a process (same pid → same dir).
3. ``_BaseAgent._default_memory()`` uses the persistent factory.
4. ``make_in_memory_store`` is opt-in for tests + still functional.
5. The deprecated ``make_no_op_store`` alias still works.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from local_ai.memory import (
    MemoryStore,
    make_in_memory_store,
    make_no_op_store,
    make_persistent_default_store,
)


def _run(coro):
    return asyncio.run(coro)


# ─── make_persistent_default_store ────────────────────────────────


def test_persistent_store_uses_pid_subdir(tmp_path: Path):
    """The store path includes the current pid so concurrent
    workers don't trample each other's WAL files."""
    store = make_persistent_default_store(root=tmp_path)
    expected = tmp_path / f"default-{os.getpid()}"
    assert store.root == expected
    assert store.root.exists()


def test_persistent_store_is_stable_across_calls(tmp_path: Path):
    """Two calls within the same process return the SAME root —
    the process-local agent reads its own previous writes."""
    a = make_persistent_default_store(root=tmp_path)
    b = make_persistent_default_store(root=tmp_path)
    assert a.root == b.root


def test_persistent_store_writes_survive_reload(tmp_path: Path):
    """Write via one ``MemoryStore`` instance, drop the reference,
    construct a new one rooted at the same path — the SQLite file
    on disk surfaces the previous write.  Mirrors how an agent
    re-entering ``_default_memory()`` sees prior turns."""
    a = make_persistent_default_store(root=tmp_path)
    _run(a.write_core({"persona": "Auditor", "n": 7}))
    # Drop the in-memory backend.
    del a
    b = make_persistent_default_store(root=tmp_path)
    assert b.read_core() == {"persona": "Auditor", "n": 7}


def test_persistent_store_default_root_falls_back_to_settings():
    """Calling without explicit root uses ``settings.memory_root``
    by default — the production code path the agent actually uses."""
    store = make_persistent_default_store()
    # The store root must be under SOMETHING that exists.
    assert store.root.exists()
    # The directory name encodes the pid.
    assert store.root.name == f"default-{os.getpid()}"


def test_persistent_store_no_audit_by_default(tmp_path: Path):
    """The default factory disables ledger audit — agent default
    memory shouldn't pollute the immutable ledger with its own
    writes (which the legacy fallback also avoided)."""
    store = make_persistent_default_store(root=tmp_path)
    assert store.audit_enabled is False


# ─── make_in_memory_store (opt-in tests) ──────────────────────────


def test_in_memory_store_is_functional():
    """The in-memory tempfile path still allows test code to
    spin up a throwaway store without the persistence path."""
    store = make_in_memory_store()
    _run(store.write_core({"k": "v"}))
    assert store.read_core() == {"k": "v"}


# ─── make_no_op_store (deprecated alias) ──────────────────────────


def test_no_op_store_alias_routes_to_persistent():
    """Existing callers (``_BaseAgent._default_memory`` pre-v17,
    legacy tests) calling ``make_no_op_store()`` get the new
    persistent path transparently."""
    store = make_no_op_store()
    # Path mirrors the persistent factory's pid-subdir convention.
    assert store.root.name == f"default-{os.getpid()}"


# ─── _BaseAgent._default_memory wires the new path ────────────────


def test_sentinel_agent_default_memory_uses_persistent_factory():
    """The agent's lazy fallback must construct a persistent
    store (not a temp-dir store) so writes survive past Windows
    temp-dir GC."""
    from document_processor.sentinel.agents import _BaseAgent

    agent = _BaseAgent()
    mem = _run(agent._default_memory())
    assert mem is not None
    assert isinstance(mem, MemoryStore)
    # Persistent path → pid-suffix subdir.
    assert mem.root.name == f"default-{os.getpid()}"


def test_sentinel_agent_default_memory_round_trip(tmp_path, monkeypatch):
    """Write via the agent's default memory, drop the agent
    reference, construct a new agent — it sees the prior write
    when both share the same memory_root."""
    from document_processor.config import settings as settings_mod
    from document_processor.sentinel.agents import _BaseAgent

    monkeypatch.setattr(settings_mod.settings, "memory_root", str(tmp_path))

    a = _BaseAgent()
    mem_a = _run(a._default_memory())
    _run(mem_a.write_core({"turn": "first"}))

    b = _BaseAgent()
    mem_b = _run(b._default_memory())
    # Same persistent root → same on-disk SQLite file.
    assert mem_a.root == mem_b.root
    assert mem_b.read_core() == {"turn": "first"}
