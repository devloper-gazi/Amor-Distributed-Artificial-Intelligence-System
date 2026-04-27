"""Tests for PhaseHooks (Charter §6 Mandate 3) + VersionedModel
(Charter §6 Mandate 6)."""

from __future__ import annotations

from typing import Any

import pytest

from document_processor.code_intelligence.hooks import (
    ChainedHooks,
    NoopHooks,
    PhaseHooks,
    TelemetryHooks,
)
from document_processor.code_intelligence.schema import (
    CURRENT_SCHEMA_VERSIONS,
    VersionedModel,
    ensure_schema_version,
    schema_version_of,
)


# ── PhaseHooks protocol ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_hooks_does_nothing():
    h = NoopHooks()
    assert isinstance(h, PhaseHooks)
    assert await h.before_phase("plan", {}) is None
    assert await h.after_phase("plan", {}, {"x": 1}) is None


class _Recorder:
    """Test hook that records every call."""

    def __init__(self) -> None:
        self.before: list[tuple[str, dict[str, Any]]] = []
        self.after: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []

    async def before_phase(self, name: str, state: dict[str, Any]) -> None:
        self.before.append((name, dict(state)))

    async def after_phase(
        self,
        name: str,
        state: dict[str, Any],
        result: dict[str, Any] | None,
    ) -> None:
        self.after.append((name, dict(state), result))


@pytest.mark.asyncio
async def test_chained_hooks_calls_all_in_forward_order():
    a = _Recorder()
    b = _Recorder()
    c = _Recorder()
    chain = ChainedHooks(a, b, c)
    assert chain.count == 3

    await chain.before_phase("plan", {"step": 1})
    # Forward order: a, b, c
    assert [r.before[-1][0] for r in (a, b, c)] == ["plan", "plan", "plan"]


@pytest.mark.asyncio
async def test_chained_hooks_after_phase_runs_reverse():
    a = _Recorder()
    b = _Recorder()
    chain = ChainedHooks(a, b)
    await chain.after_phase("plan", {"x": 1}, {"y": 2})
    # Reverse: b first, then a
    assert b.after[-1][0] == "plan"
    assert a.after[-1][0] == "plan"


@pytest.mark.asyncio
async def test_chained_hooks_swallows_per_hook_exception():
    class _Bad:
        async def before_phase(self, name: str, state: dict[str, Any]) -> None:
            raise RuntimeError("intentional")

        async def after_phase(self, name, state, result) -> None:
            return None

    good = _Recorder()
    chain = ChainedHooks(_Bad(), good)
    # Exception in _Bad must NOT prevent `good` from running.
    await chain.before_phase("plan", {})
    assert good.before  # got called


@pytest.mark.asyncio
async def test_chained_hooks_add_appends():
    chain = ChainedHooks()
    assert chain.count == 0
    chain.add(_Recorder())
    chain.add(_Recorder())
    assert chain.count == 2


@pytest.mark.asyncio
async def test_telemetry_hooks_emits_jsonl_spans(tmp_path, monkeypatch):
    """TelemetryHooks should produce free-standing observability events."""
    from document_processor.code_intelligence import observability

    monkeypatch.setattr(observability, "_TRACE_DIR", tmp_path)
    monkeypatch.setattr(observability, "_LANGFUSE_CLIENT", None)
    monkeypatch.setattr(observability, "_LANGFUSE_TRIED", True)

    h = TelemetryHooks()
    await h.before_phase("plan", {})
    await h.after_phase("plan", {}, {"ok": True})
    await h.after_phase("execute", {}, None)  # failure path

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    import json

    spans = [
        json.loads(line)
        for line in files[0].read_text().splitlines()
        if line.strip()
    ]
    names = [s["name"] for s in spans]
    assert names == ["phase_start", "phase_complete", "phase_failed"]


# ── VersionedModel + helpers ───────────────────────────────────────────


def test_current_schema_versions_has_required_kinds():
    for kind in ("code_session", "capability_record", "adversarial_event",
                 "trace_span", "query_record"):
        assert kind in CURRENT_SCHEMA_VERSIONS
        assert CURRENT_SCHEMA_VERSIONS[kind] >= 1


def test_versioned_model_default_version_is_one():
    class Demo(VersionedModel):
        x: int = 0

    d = Demo(x=42)
    assert d.schema_version == 1
    assert d.x == 42


def test_versioned_model_subclass_picks_up_kind_default():
    class CodeSessionDemo(VersionedModel):
        __schema_kind__ = "code_session"
        sid: str = "x"

    d = CodeSessionDemo()
    # Default reflects CURRENT_SCHEMA_VERSIONS["code_session"]
    assert d.schema_version == CURRENT_SCHEMA_VERSIONS["code_session"]


def test_versioned_model_explicit_version_wins():
    class Demo(VersionedModel):
        x: int = 0

    d = Demo(x=1, schema_version=42)
    assert d.schema_version == 42


def test_ensure_schema_version_known_kind():
    payload = {"foo": "bar"}
    out = ensure_schema_version(payload, "code_session")
    assert out is payload  # mutated in place
    assert out["schema_version"] == CURRENT_SCHEMA_VERSIONS["code_session"]


def test_ensure_schema_version_unknown_kind_defaults_to_1(caplog):
    payload: dict[str, Any] = {}
    out = ensure_schema_version(payload, "definitely-not-a-real-kind")
    assert out["schema_version"] == 1


def test_ensure_schema_version_idempotent():
    payload = {"schema_version": 99, "foo": "bar"}
    out = ensure_schema_version(payload, "code_session")
    # Existing value is preserved (setdefault semantics) — migrations
    # are explicit, not implicit on every write.
    assert out["schema_version"] == 99


def test_schema_version_of_reads_correctly():
    assert schema_version_of({"schema_version": 7}) == 7
    assert schema_version_of({}) == 1
    assert schema_version_of({"schema_version": "invalid"}) == 1
    assert schema_version_of({"schema_version": "5"}) == 5  # str → int OK


def test_ensure_schema_version_handles_non_dict():
    assert ensure_schema_version("not a dict", "code_session") == "not a dict"  # type: ignore[arg-type]
