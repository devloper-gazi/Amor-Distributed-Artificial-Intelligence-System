"""Cycle G G2 — coverage for the SGLang vs llama-swap spike benchmark
tool.  No live SGLang needed — every HTTP call is mocked.
"""

from __future__ import annotations

import asyncio

import pytest

from tools.inference.spike_benchmark import (
    BenchmarkResult,
    ComparisonVerdict,
    RequestSample,
    benchmark_backend,
    compare_backends,
)


# ─── RequestSample math ────────────────────────────────────────────


def test_request_sample_tokens_per_s_division():
    s = RequestSample(duration_s=2.0, completion_tokens=100, prompt_tokens=0)
    assert s.tokens_per_s == 50.0


def test_request_sample_zero_duration_returns_zero():
    s = RequestSample(duration_s=0.0, completion_tokens=100, prompt_tokens=0)
    assert s.tokens_per_s == 0.0


# ─── BenchmarkResult percentiles ────────────────────────────────────


def test_benchmark_result_percentile_basic():
    br = BenchmarkResult(
        base_url="http://x", model="m", concurrency=1,
        rounds=1, max_tokens=64,
    )
    br.samples = [
        RequestSample(duration_s=i, completion_tokens=10, prompt_tokens=0)
        for i in range(1, 11)
    ]
    assert br.percentile(50) == 5
    assert br.percentile(95) is not None
    assert br.percentile(99) == 10


def test_benchmark_result_percentile_empty_returns_none():
    br = BenchmarkResult(
        base_url="x", model="m", concurrency=1, rounds=1, max_tokens=64,
    )
    assert br.percentile(50) is None


def test_benchmark_result_skips_failed_samples_in_latency():
    br = BenchmarkResult(
        base_url="x", model="m", concurrency=2, rounds=1, max_tokens=64,
    )
    br.samples = [
        RequestSample(duration_s=1.0, completion_tokens=10, prompt_tokens=0),
        RequestSample(duration_s=5.0, completion_tokens=0, prompt_tokens=0, error="500"),
    ]
    # latency list skips failed
    assert br.latencies_s == [1.0]
    assert br.total_completion_tokens == 10


def test_benchmark_result_throughput_uses_wall_clock_not_sum():
    br = BenchmarkResult(
        base_url="x", model="m", concurrency=2, rounds=1, max_tokens=64,
    )
    br.samples = [
        RequestSample(duration_s=2.0, completion_tokens=50, prompt_tokens=0),
        RequestSample(duration_s=2.0, completion_tokens=50, prompt_tokens=0),
    ]
    br._wall_clock_s = 2.5  # both ran concurrently, took ~2.5s wall
    assert br.throughput_total_tokens_per_s == 100 / 2.5


def test_benchmark_result_summary_dict_shape():
    br = BenchmarkResult(
        base_url="http://x:9100", model="amor-editor",
        concurrency=4, rounds=3, max_tokens=64,
    )
    br.samples = [RequestSample(duration_s=1.0, completion_tokens=10, prompt_tokens=20)]
    br._wall_clock_s = 1.5
    summary = br.summary()
    assert summary["concurrency"] == 4
    assert summary["rounds"] == 3
    assert summary["p50_s"] is not None
    assert summary["throughput_tokens_per_s"] > 0
    assert summary["samples_total"] == 1
    assert summary["samples_failed"] == 0


# ─── compare_backends kill-ratio logic ─────────────────────────────


def _result(tp_tokens_per_s: float, reachable: bool = True) -> BenchmarkResult:
    """Synthesize a BenchmarkResult with the given throughput."""
    br = BenchmarkResult(
        base_url="http://x", model="m", concurrency=4, rounds=3, max_tokens=64,
        reachable=reachable,
    )
    br.samples = [RequestSample(duration_s=1.0, completion_tokens=int(tp_tokens_per_s), prompt_tokens=0)]
    # Set wall_clock to 1s so throughput = completion_tokens
    br._wall_clock_s = 1.0 if tp_tokens_per_s > 0 else 0.0
    return br


def test_compare_migrate_when_ratio_above_kill():
    incumbent = _result(50)
    challenger = _result(100)   # 2.0× ratio, beats 1.5× threshold
    v = compare_backends(incumbent, challenger, kill_ratio=1.5)
    assert v.verdict == "migrate"
    assert v.ratio == 2.0
    assert "≥ 1.50×" in v.rationale


def test_compare_keep_when_ratio_below_kill():
    incumbent = _result(50)
    challenger = _result(60)   # 1.2× ratio, below 1.5×
    v = compare_backends(incumbent, challenger, kill_ratio=1.5)
    assert v.verdict == "keep"
    assert v.ratio == 1.2
    assert "abandon spike" in v.rationale


def test_compare_keep_when_ratio_exactly_at_kill():
    """Boundary: exactly at threshold should MIGRATE (≥, not >)."""
    incumbent = _result(50)
    challenger = _result(75)   # 1.5× ratio exactly
    v = compare_backends(incumbent, challenger, kill_ratio=1.5)
    assert v.verdict == "migrate"


def test_compare_incomplete_when_incumbent_unreachable():
    incumbent = _result(0, reachable=False)
    challenger = _result(100)
    v = compare_backends(incumbent, challenger, kill_ratio=1.5)
    assert v.verdict == "incomplete"
    assert "incumbent_reachable=False" in v.rationale


def test_compare_incomplete_when_challenger_unreachable():
    """The HIGH-risk-flagged case: SGLang OOMs on 8 GB VRAM and
    can't even respond to the bench.  Verdict must be 'incomplete',
    not 'migrate' or 'keep'."""
    incumbent = _result(50)
    challenger = _result(0, reachable=False)
    v = compare_backends(incumbent, challenger, kill_ratio=1.5)
    assert v.verdict == "incomplete"
    assert "challenger_reachable=False" in v.rationale


def test_compare_incomplete_when_incumbent_throughput_zero():
    """Edge: incumbent reachable but produced 0 throughput (e.g.
    all requests timed out).  Avoid ZeroDivisionError."""
    incumbent = _result(0)
    challenger = _result(100)
    v = compare_backends(incumbent, challenger, kill_ratio=1.5)
    assert v.verdict == "incomplete"
    assert "throughput is 0" in v.rationale


# ─── benchmark_backend with mocked httpx ───────────────────────────


def test_benchmark_unreachable_short_circuits(monkeypatch):
    """When the /v1/models probe fails, benchmark_backend exits
    immediately with reachable=False — no need to fire the actual
    concurrent load against a dead endpoint."""
    import tools.inference.spike_benchmark as bench
    import httpx as _httpx

    class FailingClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, *a, **k):
            raise _httpx.ConnectError("connection refused")

    monkeypatch.setattr(bench.httpx, "AsyncClient", FailingClient)
    result = asyncio.run(benchmark_backend(
        "http://nonexistent:9999", "m",
        concurrency=4, rounds=3,
    ))
    assert result.reachable is False
    assert "unreachable" in result.error
    assert result.samples == []


def test_benchmark_runs_concurrency_x_rounds_requests(monkeypatch):
    """Verify the runner fires concurrency × rounds = N requests."""
    import tools.inference.spike_benchmark as bench

    call_count = {"n": 0}

    class ProbeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"data": [{"id": "m"}]}

    class ChatResp:
        def raise_for_status(self): pass
        def json(self):
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 10},
            }

    class StubClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, *a, **k): return ProbeResp()
        async def post(self, *a, **k):
            call_count["n"] += 1
            return ChatResp()

    monkeypatch.setattr(bench.httpx, "AsyncClient", StubClient)
    result = asyncio.run(benchmark_backend(
        "http://x", "m", concurrency=4, rounds=3,
    ))
    assert call_count["n"] == 12
    assert len(result.samples) == 12
    assert result.reachable is True
    assert result.total_completion_tokens == 120


def test_benchmark_records_per_sample_error(monkeypatch):
    """When chat completion fails (HTTP 500), the sample carries the
    error string — aggregation excludes it from latency/tokens."""
    import tools.inference.spike_benchmark as bench
    import httpx as _httpx

    class ProbeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"data": []}

    class StubClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, *a, **k): return ProbeResp()
        async def post(self, *a, **k):
            raise _httpx.HTTPStatusError(
                "500", request=None, response=None,
            )

    monkeypatch.setattr(bench.httpx, "AsyncClient", StubClient)
    result = asyncio.run(benchmark_backend(
        "http://x", "m", concurrency=2, rounds=1,
    ))
    assert result.reachable is True
    assert len(result.samples) == 2
    assert all(s.error for s in result.samples)
    assert result.latencies_s == []   # all failed → no latencies counted
