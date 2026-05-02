"""Tests for v17 PR #5 — sandbox observability foundations.

This PR ships the telemetry + settings + (passive) socket-proxy
service that the future warm-pool will build on.  The actual
``SandboxPool`` class is staged for a follow-up commit; the goal
of this PR is to land the observability surface so operators can
measure sandbox cold-start BEFORE deciding whether the pool is
worth flipping on.

Verifies:
1. ``record_sandbox_run_ms`` is exported from diagnostics + the
   sliding-window stat is updated when called.
2. ``record_failure`` is exported + adds entries to the failure
   ring buffer.
3. New pool settings exist with the right defaults
   (``code_sandbox_pool_size = 0`` keeps the pool OFF until
   explicitly enabled).
4. The diagnostics ``build_diagnostics`` payload surfaces
   ``cold_start_p50_ms`` after a few recorded runs.
5. ``ExecutionSandbox.execute`` calls ``record_sandbox_run_ms``
   on a successful run (the new wire-up in sandbox.py).
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence import diagnostics


# ─── record_sandbox_run_ms — sliding window ───────────────────────


def test_record_sandbox_run_ms_appends_to_window():
    diagnostics.reset_sandbox_timings()
    diagnostics.record_sandbox_run_ms(150.0)
    diagnostics.record_sandbox_run_ms(220.0)
    diagnostics.record_sandbox_run_ms(180.0)
    assert list(diagnostics._SANDBOX_TIMINGS) == [150.0, 220.0, 180.0]  # noqa: SLF001


def test_record_sandbox_run_ms_caps_window_at_200():
    diagnostics.reset_sandbox_timings()
    for i in range(1, 251):  # 1..250 (non-zero so the helper records)
        diagnostics.record_sandbox_run_ms(float(i))
    window = list(diagnostics._SANDBOX_TIMINGS)  # noqa: SLF001
    assert len(window) == 200
    # Last entry is 250; the first 50 (1..50) were dropped.
    assert window[0] == 51.0
    assert window[-1] == 250.0


def test_record_sandbox_run_ms_drops_zero_or_negative():
    """Zero / negative durations are useless noise — the helper
    silently ignores them so a pathological 0ms run can't poison
    the percentile."""
    diagnostics.reset_sandbox_timings()
    diagnostics.record_sandbox_run_ms(0.0)
    diagnostics.record_sandbox_run_ms(-50.0)
    diagnostics.record_sandbox_run_ms(120.5)
    assert list(diagnostics._SANDBOX_TIMINGS) == [120.5]  # noqa: SLF001


# ─── record_failure — ring buffer ─────────────────────────────────


def test_record_failure_appends_with_payload():
    diagnostics.reset_failures()
    diagnostics.record_failure(
        "sandbox.execute",
        "image pull failed",
        language="python",
        image="python:3.11-slim",
    )
    entries = list(diagnostics._FAILURES)  # noqa: SLF001
    assert len(entries) == 1
    e = entries[0].to_dict()
    assert e["where"] == "sandbox.execute"
    assert e["detail"] == "image pull failed"
    assert e["payload"]["language"] == "python"
    assert e["payload"]["image"] == "python:3.11-slim"


def test_record_failure_ring_buffer_caps_at_30():
    diagnostics.reset_failures()
    for i in range(40):
        diagnostics.record_failure(f"loc.{i}", f"detail {i}")
    entries = list(diagnostics._FAILURES)  # noqa: SLF001
    assert len(entries) == 30
    # The first 10 were dropped; the oldest surviving entry is loc.10.
    assert entries[0].where == "loc.10"


# ─── pool settings ────────────────────────────────────────────────


def test_pool_settings_default_off():
    """``code_sandbox_pool_size = 0`` is the DEFAULT — pool is off
    until operator explicitly opts in.  Important: the pool's
    real implementation is staged for a follow-up; landing
    settings ahead of code lets the runbook reference them."""
    from document_processor.config.settings import settings
    assert settings.code_sandbox_pool_size == 0
    assert settings.code_sandbox_pool_languages == "python,javascript"
    assert settings.code_sandbox_lease_timeout_s == 5.0
    assert settings.code_sandbox_max_lease_count == 50


# ─── diagnostics payload — sandbox.cold_start ──────────────────


@pytest.mark.asyncio
async def test_build_diagnostics_includes_cold_start_p50_after_runs():
    """After recording a handful of runs, the diagnostics payload's
    ``sandbox`` section reports a non-null ``cold_start_p50_ms``."""
    diagnostics.reset_sandbox_timings()
    diagnostics.reset_cache()
    for ms in (50.0, 100.0, 150.0, 200.0, 250.0):
        diagnostics.record_sandbox_run_ms(ms)
    payload = await diagnostics.build_diagnostics(
        sessions_map={},
        probe_sandbox=False,
    )
    sandbox = payload.get("sandbox", {})
    p50 = sandbox.get("cold_start_p50_ms")
    p95 = sandbox.get("cold_start_p95_ms")
    assert p50 is not None
    assert isinstance(p50, (int, float))
    # p50 of [50, 100, 150, 200, 250] = 150.
    assert p50 == 150.0
    # p95 lands at 250 with the nearest-rank algorithm on n=5.
    assert p95 is not None
    assert p95 >= 200.0


# ─── sandbox wires the telemetry on each run ──────────────────────


@pytest.mark.asyncio
async def test_sandbox_execute_records_run_ms_on_success(monkeypatch, tmp_path):
    """The new wire-up: ``ExecutionSandbox.execute`` must call
    ``record_sandbox_run_ms`` after a clean run so the diagnostics
    sliding-window is populated."""
    from document_processor.code_intelligence.sandbox import (
        ExecutionSandbox,
    )

    diagnostics.reset_sandbox_timings()

    sb = ExecutionSandbox()

    # Stub the docker probe so the real CLI is never invoked.
    async def _docker_ok(*a, **kw):
        return True
    monkeypatch.setattr(sb, "docker_available", _docker_ok)

    # Stub _ensure_image to no-op (we don't want to hit the daemon).
    async def _noop_image(*a, **kw):
        return None
    monkeypatch.setattr(sb, "_ensure_image", _noop_image)

    # Stub the subprocess that ``docker run`` would spawn — return a
    # process whose communicate() yields the expected (stdout, stderr)
    # tuple and whose ``returncode`` is 0.
    class _FakeProc:
        returncode = 0

        async def communicate(self, input=None):  # noqa: ARG002
            return (b"hello\n", b"")

        def kill(self):
            return None

    async def _create_subprocess_exec(*args, **kwargs):  # noqa: ARG001
        return _FakeProc()

    import asyncio as _asyncio
    monkeypatch.setattr(_asyncio, "create_subprocess_exec",
                        _create_subprocess_exec)

    # Force the workdir into our tmp_path so we don't pollute the
    # named-volume / system tempdir.
    monkeypatch.setattr(sb, "_workdir_root", str(tmp_path))

    result = await sb.execute(
        code="print('hi')", language="python",
    )
    assert result.exit_code == 0
    # The sliding-window picked up at least one entry.
    assert len(diagnostics._SANDBOX_TIMINGS) >= 1  # noqa: SLF001
