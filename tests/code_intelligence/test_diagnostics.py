"""Tests for the Phase 17 Commit R diagnostics module + endpoint."""

from __future__ import annotations

import asyncio

import pytest

from document_processor.code_intelligence import diagnostics as diag


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_diag_state():
    diag.reset_cache()
    diag.reset_sandbox_timings()
    diag.reset_failures()
    yield
    diag.reset_cache()
    diag.reset_sandbox_timings()
    diag.reset_failures()


# ─── _percentile ───────────────────────────────────────────────────


def test_percentile_empty_returns_none():
    assert diag._percentile([], 50) is None


def test_percentile_p50_p95_monotonic():
    samples = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    p50 = diag._percentile(samples, 50)
    p95 = diag._percentile(samples, 95)
    assert p50 is not None
    assert p95 is not None
    assert p50 <= p95


def test_percentile_clamps_at_bounds():
    samples = [1.0, 2.0, 3.0]
    assert diag._percentile(samples, 0) == 1.0
    assert diag._percentile(samples, 100) == 3.0


# ─── sandbox timings ring buffer ───────────────────────────────────


def test_record_sandbox_run_appends_and_caps():
    for ms in range(1, 250):
        diag.record_sandbox_run_ms(float(ms))
    # Capped at _SANDBOX_TIMINGS_MAX (200).
    assert len(diag._SANDBOX_TIMINGS) == 200
    # Oldest 49 entries dropped → first surviving is 50.0.
    assert diag._SANDBOX_TIMINGS[0] == 50.0


def test_record_sandbox_ignores_zero_or_negative():
    diag.record_sandbox_run_ms(0)
    diag.record_sandbox_run_ms(-5.0)
    assert diag._SANDBOX_TIMINGS == []


# ─── failure ring buffer ───────────────────────────────────────────


def test_record_failure_round_trip():
    diag.record_failure("sandbox.image_pull", "image not found",
                        image="python:3.11-slim")
    out = [f.to_dict() for f in diag._FAILURES]
    assert len(out) == 1
    assert out[0]["where"] == "sandbox.image_pull"
    assert "image not found" in out[0]["detail"]
    assert out[0]["payload"]["image"] == "python:3.11-slim"


def test_failure_ring_buffer_caps():
    for i in range(45):
        diag.record_failure(f"src.{i}", f"detail {i}")
    assert len(diag._FAILURES) == 30


# ─── TTL cache behaviour ───────────────────────────────────────────


def test_cached_returns_same_payload_within_ttl():
    calls = {"n": 0}

    def fetcher():
        calls["n"] += 1
        return {"v": calls["n"]}

    a = diag._cached("x", ttl_s=60, fetcher=fetcher)
    b = diag._cached("x", ttl_s=60, fetcher=fetcher)
    assert a == b
    assert calls["n"] == 1


def test_cached_handles_fetcher_exception():
    def boom():
        raise RuntimeError("boom")

    payload = diag._cached("err", ttl_s=60, fetcher=boom)
    assert "error" in payload
    assert "boom" in payload["error"]


# ─── recent sessions collector ─────────────────────────────────────


def test_collect_recent_sessions_sorts_newest_first():
    sessions = {
        "a": {"session_id": "a", "started_at_ts": 100, "status": "completed",
              "phases": [{"name": "triage", "status": "completed"}]},
        "b": {"session_id": "b", "started_at_ts": 200, "status": "in_progress",
              "phases": [{"name": "triage", "status": "in_progress"}]},
        "c": {"session_id": "c", "started_at_ts": 50,  "status": "failed",
              "phases": [{"name": "implement", "status": "failed"}]},
    }
    out = diag.collect_recent_sessions(sessions)
    assert [r["sid"] for r in out] == ["b", "a", "c"]
    failed = next(r for r in out if r["sid"] == "c")
    assert failed["phases_failed"] == ["implement"]


def test_collect_recent_sessions_handles_invalid_input():
    assert diag.collect_recent_sessions(None) == []
    assert diag.collect_recent_sessions("not-a-dict") == []
    assert diag.collect_recent_sessions({}) == []


def test_collect_recent_sessions_caps_at_five():
    sessions = {
        f"s{i}": {"session_id": f"s{i}", "started_at_ts": i,
                  "status": "ok"}
        for i in range(20)
    }
    out = diag.collect_recent_sessions(sessions)
    assert len(out) == 5


# ─── pure-data section collectors ──────────────────────────────────


def test_collect_backend_returns_kind_and_class():
    out = diag.collect_backend()
    assert "error" not in out
    assert out["kind"] in ("ollama", "stub", "llama-swap", "llama-cpp",
                          "openai-compat")
    assert "class" in out


def test_collect_rag_has_expected_keys():
    out = diag.collect_rag()
    assert "embedder" in out
    assert "hybrid_enabled" in out
    assert isinstance(out["rrf_k"], int)


def test_collect_phase16_facade_has_flags():
    out = diag.collect_phase16_facade()
    assert "openai_compat_enabled" in out
    assert "mcp_server_enabled" in out
    assert "llm_backend" in out


def test_collect_ledger_runs_without_crashing():
    out = diag.collect_ledger()
    # Either an "intact" key (success) or "error" (degraded), never neither.
    assert "intact" in out or "error" in out


# ─── build_diagnostics integration ─────────────────────────────────


def test_build_diagnostics_assembly():
    sessions = {
        "x": {"session_id": "x", "started_at_ts": 1, "status": "completed"},
    }
    out = _run(diag.build_diagnostics(
        sessions_map=sessions, probe_sandbox=False,
    ))
    assert "ts" in out
    for key in ("backend", "backend_health", "models", "sandbox",
                "rag", "ledger", "phase16_facade",
                "recent_sessions", "recent_failures"):
        assert key in out, f"missing key: {key}"
    assert out["recent_sessions"][0]["sid"] == "x"


def test_build_diagnostics_skips_probe_when_requested():
    out = _run(diag.build_diagnostics(probe_sandbox=False))
    assert out["sandbox"]["probe"] is None
