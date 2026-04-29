"""
Tests for PerformanceBenchmarker — script assembly, BENCH_RESULT
parsing, power-law fit, claim-vs-measured comparator, fail-soft paths.
The sandbox is mocked so tests don't need Docker.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from document_processor.code_intelligence.reactor.benchmarker import (
    BenchmarkFit,
    BenchmarkRecord,
    BenchmarkResult,
    PerformanceBenchmarker,
    _BENCH_HARNESS,
    _exponent_to_label,
)


# ── label mapping ──────────────────────────────────────────────────


def test_exponent_to_label_constant():
    assert _exponent_to_label(0.0) == "O(1)"


def test_exponent_to_label_linear():
    assert _exponent_to_label(1.0) == "O(n)"


def test_exponent_to_label_n_log_n():
    assert _exponent_to_label(1.5) == "O(n log n)"


def test_exponent_to_label_quadratic():
    assert _exponent_to_label(2.0) == "O(n^2)"


def test_exponent_to_label_cubic():
    assert _exponent_to_label(3.0) == "O(n^3)"


def test_exponent_to_label_exponential():
    assert _exponent_to_label(8.0) == "O(2^n)"


# ── script assembly ────────────────────────────────────────────────


class _FakeSandbox:
    def __init__(self, stdout: str = "", skipped: bool = False):
        self.last_code: str | None = None
        self.stdout = stdout
        self.skipped = skipped

    async def execute(self, code, language="python", timeout=30):
        self.last_code = code

        class _R:
            def __init__(s):
                s.stdout = self.stdout
                s.stderr = ""
                s.exit_code = 0
                s.skipped = self.skipped
                s.success = not self.skipped
        return _R()


@pytest.mark.asyncio
async def test_script_includes_user_code_and_harness():
    sb = _FakeSandbox()
    bm = PerformanceBenchmarker(sb, scales=[10, 100])
    await bm.run("def f(xs): return sum(xs)\n")
    assert sb.last_code is not None
    assert "def f(xs):" in sb.last_code
    # The harness's distinctive pick-target helper must be present.
    assert "_amor_pick_target" in sb.last_code
    # Scales propagated.
    assert "10,100" in sb.last_code


@pytest.mark.asyncio
async def test_run_empty_code_fails_softly():
    sb = _FakeSandbox()
    bm = PerformanceBenchmarker(sb, scales=[10])
    res = await bm.run("")
    assert res.failed
    assert "no code" in res.failure_reason.lower()


@pytest.mark.asyncio
async def test_run_non_python_language_fails_softly():
    sb = _FakeSandbox()
    bm = PerformanceBenchmarker(sb, scales=[10])
    res = await bm.run("package main", language="go")
    assert res.failed
    assert "python" in res.failure_reason.lower()


@pytest.mark.asyncio
async def test_run_skipped_sandbox_fails_softly():
    sb = _FakeSandbox(skipped=True)
    bm = PerformanceBenchmarker(sb, scales=[10])
    res = await bm.run("def f(xs): return xs\n")
    assert res.failed
    assert "skipped" in res.failure_reason.lower()


# ── parsing ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_records_extracts_bench_result_lines():
    stdout = (
        "BENCH_TARGET=f\n"
        + json_line(scale=10,    ms=0.5,   peak_kb=12)
        + json_line(scale=100,   ms=5.0,   peak_kb=120)
        + json_line(scale=1000,  ms=50.0,  peak_kb=1200)
        + "BENCH_AUTO_SHRINK=1000\n"
    )
    sb = _FakeSandbox(stdout=stdout)
    bm = PerformanceBenchmarker(sb, scales=[10, 100, 1000, 10_000])
    res = await bm.run("def f(xs): return xs\n")
    assert not res.failed
    assert len(res.records) == 3
    assert [r.scale for r in res.records] == [10, 100, 1_000]
    # Power-law fit: 10×x growth per 10×n → b ≈ 1.0.
    assert 0.85 <= res.fit.exponent <= 1.15
    assert res.fit.measured_label == "O(n)"


def json_line(*, scale: int, ms: float, peak_kb: int = 0, error: str | None = None):
    payload = {"scale": scale, "ms": ms, "peak_kb": peak_kb}
    if error:
        payload["error"] = error
    return f"BENCH_RESULT={json.dumps(payload)}\n"


@pytest.mark.asyncio
async def test_quadratic_curve_detected_as_n_squared():
    """100×ms per 10×n → exponent 2 → O(n^2)."""
    stdout = (
        json_line(scale=10,    ms=1.0)
        + json_line(scale=100,   ms=100.0)
        + json_line(scale=1000,  ms=10_000.0)
    )
    sb = _FakeSandbox(stdout=stdout)
    bm = PerformanceBenchmarker(sb, scales=[10, 100, 1000])
    res = await bm.run("def f(xs): return xs\n")
    assert res.fit.measured_label == "O(n^2)"
    assert 1.85 <= res.fit.exponent <= 2.15


@pytest.mark.asyncio
async def test_per_scale_error_skips_that_record_only():
    stdout = (
        json_line(scale=10,    ms=0.5)
        + 'BENCH_RESULT={"scale":100,"ms":0.0,"peak_kb":0,"error":"ValueError: x"}\n'
        + json_line(scale=1000,  ms=50.0)
    )
    sb = _FakeSandbox(stdout=stdout)
    bm = PerformanceBenchmarker(sb, scales=[10, 100, 1000])
    res = await bm.run("def f(xs): return xs\n")
    assert len(res.records) == 3
    # Only the two successful records contribute to the fit.
    assert res.fit.samples_used == 2


@pytest.mark.asyncio
async def test_no_bench_result_lines_yields_failed_result():
    sb = _FakeSandbox(stdout="BENCH_ERROR: no callable found\n")
    bm = PerformanceBenchmarker(sb, scales=[10])
    res = await bm.run("x = 5\n")
    assert res.failed
    assert "no callable" in res.failure_reason or "no BENCH_RESULT" in res.failure_reason


@pytest.mark.asyncio
async def test_single_record_yields_no_fit_method_none():
    """Need ≥2 points for a power-law fit."""
    sb = _FakeSandbox(stdout=json_line(scale=10, ms=1.0))
    bm = PerformanceBenchmarker(sb, scales=[10])
    res = await bm.run("def f(xs): return xs\n")
    assert res.fit.method == "none"
    assert res.fit.measured_label == "O(?)"


# ── claim vs measured comparator ─────────────────────────────────


@pytest.mark.asyncio
async def test_claim_vs_measured_underclaim_returns_minus_one():
    """LLM claims O(n) but we measured O(n^2) → -1."""
    stdout = (
        json_line(scale=10,    ms=1.0)
        + json_line(scale=100,   ms=100.0)
        + json_line(scale=1000,  ms=10_000.0)
    )
    sb = _FakeSandbox(stdout=stdout)
    bm = PerformanceBenchmarker(sb, scales=[10, 100, 1000])
    res = await bm.run("def f(xs): return xs\n", claimed_label="O(n)")
    assert res.claim_vs_measured == -1
    assert res.fit.measured_label == "O(n^2)"


@pytest.mark.asyncio
async def test_claim_vs_measured_match_returns_zero():
    stdout = (
        json_line(scale=10,    ms=0.5)
        + json_line(scale=100,   ms=5.0)
        + json_line(scale=1000,  ms=50.0)
    )
    sb = _FakeSandbox(stdout=stdout)
    bm = PerformanceBenchmarker(sb, scales=[10, 100, 1000])
    res = await bm.run("def f(xs): return xs\n", claimed_label="O(n)")
    assert res.claim_vs_measured == 0


# ── to_dict round trip ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_to_dict_carries_all_fields():
    stdout = (
        json_line(scale=10, ms=0.5, peak_kb=10)
        + json_line(scale=100, ms=5.0, peak_kb=100)
    )
    sb = _FakeSandbox(stdout=stdout)
    bm = PerformanceBenchmarker(sb, scales=[10, 100])
    res = await bm.run("def f(xs): return xs\n", claimed_label="O(n)")
    d = res.to_dict()
    assert d["fit"]["measured_label"] == "O(n)"
    assert d["claimed_label"] == "O(n)"
    assert len(d["records"]) == 2
    assert d["records"][0]["runtime_ms"] == 0.5


# ── harness sanity ──────────────────────────────────────────────


def test_harness_template_contains_required_markers():
    """Quick sanity — the harness must keep its three landmark
    markers so the parser regexes keep matching after any future edit."""
    rendered = _BENCH_HARNESS.format(scales="10", timeout_per_scale=8)
    assert "BENCH_TARGET=" in rendered
    assert "BENCH_RESULT=" in rendered
    assert "BENCH_AUTO_SHRINK=" in rendered
    assert "_amor_pick_target" in rendered
