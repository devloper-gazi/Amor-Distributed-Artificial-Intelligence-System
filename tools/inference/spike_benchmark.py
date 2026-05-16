#!/usr/bin/env python3
"""
Cycle G G2 — multi-tenant inference engine spike benchmark.

Compares llama-swap (today's default) against SGLang under the same
concurrency level + prompt shape that AMOR's pipeline produces.

Kill criterion (Plan-agent locked, see plan file Cycle G section)
-----------------------------------------------------------------
If SGLang p95 throughput on 4 concurrent identical-prefix sessions
doesn't beat llama-swap by ≥1.5×, ABANDON the migration and delete
the spike branch.  Document the verdict at
`docs/inference_engine_decision.md`; re-evaluate when hardware
relaxes the 8 GB VRAM constraint.

Benchmark design
----------------
* 4 concurrent connections fire the SAME 1000-token prefix prompt.
  Mirrors AMOR's worst case (4 build sessions hitting the editor
  model with the same system + plan prefix).  The cache-reuse
  win shows up when the engine recognises the shared prefix and
  serves the 2nd-4th calls from cached KV.
* Each connection completes ``--max-tokens N`` tokens (default 64);
  small enough to measure prefill+decode overhead without warming
  the GPU to thermal-throttle territory.
* Repeat N rounds (default 3) and report p50/p95/p99 of the
  per-request wall-clock + total tokens/second.

Usage
-----

  # Bench llama-swap (today's default)
  python tools/inference/spike_benchmark.py \\
    --base-url http://amor-llama-swap:9100 \\
    --model amor-editor --concurrency 4 --rounds 3 --max-tokens 64

  # Bench SGLang (spike service, compose/sglang)
  python tools/inference/spike_benchmark.py \\
    --base-url http://amor-sglang:9101 \\
    --model qwen2.5-coder --concurrency 4 --rounds 3

  # Side-by-side comparison + verdict
  python tools/inference/spike_benchmark.py \\
    --compare http://amor-llama-swap:9100,http://amor-sglang:9101 \\
    --kill-ratio 1.5

Exit codes
----------
0  ran successfully (verdict may be 'keep' or 'migrate' but not failure)
1  one backend unreachable (benchmark incomplete)
2  fatal config error (no models, bad args)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional

import httpx

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        _r = getattr(_s, "reconfigure", None)
        if _r is not None:
            try:
                _r(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ─── Standard prefix prompt (~1000 tokens of shared system + plan) ──


SHARED_PREFIX = (
    "You are AMOR's editor agent.  Generate Python code that solves the "
    "given task.  Output ONLY the function body, no explanation, no "
    "test runner.  Follow these conventions strictly:\n"
    "  * snake_case for variables and functions\n"
    "  * type hints on every signature\n"
    "  * docstrings in Google style\n"
    "  * raise ValueError on invalid input\n"
    "  * never use bare except\n"
    "  * Python 3.11 syntax (PEP 695 generic types OK)\n"
)
# Bulk it up to ~1000 tokens with a synthetic 'plan' section that all
# 4 concurrent sessions share, so cache reuse has something meaningful
# to cache.
SHARED_PREFIX += "\n\n# PLAN\n" + ("# stage: " + ("x" * 80) + "\n") * 12
TASK = "\n\nTask: write a function `add(a: int, b: int) -> int` that returns a + b.\n"


@dataclass
class RequestSample:
    """Per-request measurement."""
    duration_s: float
    completion_tokens: int
    prompt_tokens: int
    error: str = ""

    @property
    def tokens_per_s(self) -> float:
        if self.duration_s <= 0:
            return 0.0
        return self.completion_tokens / self.duration_s


@dataclass
class BenchmarkResult:
    """Aggregated result for one backend.  Shipped to the verdict
    function + persisted to JSON for comparison."""
    base_url: str
    model: str
    concurrency: int
    rounds: int
    max_tokens: int
    samples: List[RequestSample] = field(default_factory=list)
    reachable: bool = True
    error: str = ""

    @property
    def latencies_s(self) -> List[float]:
        return [s.duration_s for s in self.samples if not s.error]

    @property
    def total_completion_tokens(self) -> int:
        return sum(s.completion_tokens for s in self.samples if not s.error)

    @property
    def total_wall_s(self) -> float:
        """Wall-clock of the BENCH itself, not sum of per-request."""
        return self._wall_clock_s

    _wall_clock_s: float = 0.0

    def percentile(self, pct: float) -> Optional[float]:
        values = self.latencies_s
        if not values:
            return None
        s = sorted(values)
        k = max(0, min(len(s) - 1, int(round(pct / 100.0 * (len(s) - 1)))))
        return s[k]

    @property
    def throughput_total_tokens_per_s(self) -> float:
        if self._wall_clock_s <= 0:
            return 0.0
        return self.total_completion_tokens / self._wall_clock_s

    def summary(self) -> dict:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "concurrency": self.concurrency,
            "rounds": self.rounds,
            "max_tokens": self.max_tokens,
            "reachable": self.reachable,
            "error": self.error,
            "samples_total": len(self.samples),
            "samples_failed": sum(1 for s in self.samples if s.error),
            "p50_s": self.percentile(50),
            "p95_s": self.percentile(95),
            "p99_s": self.percentile(99),
            "throughput_tokens_per_s": round(self.throughput_total_tokens_per_s, 2),
            "wall_clock_s": round(self._wall_clock_s, 3),
        }


# ─── Single request ────────────────────────────────────────────────


async def _fire_one(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    max_tokens: int,
    timeout_s: float = 120.0,
) -> RequestSample:
    started = time.perf_counter()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": SHARED_PREFIX + TASK}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        r = await client.post(
            f"{base_url}/v1/chat/completions",
            json=body, timeout=timeout_s,
        )
        r.raise_for_status()
        data = r.json()
        elapsed = time.perf_counter() - started
        usage = data.get("usage") or {}
        return RequestSample(
            duration_s=elapsed,
            completion_tokens=int(usage.get("completion_tokens", 0)),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
        )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        elapsed = time.perf_counter() - started
        return RequestSample(
            duration_s=elapsed,
            completion_tokens=0,
            prompt_tokens=0,
            error=str(exc)[:240],
        )


# ─── One full benchmark pass ───────────────────────────────────────


async def benchmark_backend(
    base_url: str,
    model: str,
    *,
    concurrency: int = 4,
    rounds: int = 3,
    max_tokens: int = 64,
    timeout_s: float = 120.0,
) -> BenchmarkResult:
    """Fire `concurrency × rounds` requests against the backend with
    SHARED_PREFIX + TASK as the prompt.  Returns aggregated result."""
    result = BenchmarkResult(
        base_url=base_url, model=model,
        concurrency=concurrency, rounds=rounds, max_tokens=max_tokens,
    )

    # Health probe first — if the backend is unreachable, mark and
    # return without firing the full benchmark.
    async with httpx.AsyncClient() as probe_client:
        try:
            r = await probe_client.get(f"{base_url}/v1/models", timeout=5.0)
            if r.status_code >= 500:
                result.reachable = False
                result.error = f"models endpoint returned {r.status_code}"
                return result
        except httpx.HTTPError as exc:
            result.reachable = False
            result.error = f"unreachable: {exc}"
            return result

    bench_start = time.perf_counter()
    async with httpx.AsyncClient() as client:
        for round_idx in range(rounds):
            tasks = [
                _fire_one(client, base_url, model, max_tokens, timeout_s)
                for _ in range(concurrency)
            ]
            samples = await asyncio.gather(*tasks)
            result.samples.extend(samples)
    result._wall_clock_s = time.perf_counter() - bench_start
    return result


# ─── Comparison + verdict ──────────────────────────────────────────


@dataclass
class ComparisonVerdict:
    """The output of side-by-side comparison."""
    incumbent_label: str
    challenger_label: str
    incumbent_throughput: float
    challenger_throughput: float
    ratio: float
    kill_ratio: float
    verdict: str   # "migrate" | "keep" | "incomplete"
    rationale: str


def compare_backends(
    incumbent: BenchmarkResult,
    challenger: BenchmarkResult,
    *,
    kill_ratio: float = 1.5,
    incumbent_label: str = "llama-swap",
    challenger_label: str = "sglang",
) -> ComparisonVerdict:
    """Apply the kill-ratio rule to decide migrate vs keep.

    The Plan-agent-locked rule: challenger must beat incumbent on
    p95 throughput by ≥ kill_ratio (default 1.5×) to justify the
    migration cost.  Otherwise the spike is abandoned.
    """
    if not incumbent.reachable or not challenger.reachable:
        return ComparisonVerdict(
            incumbent_label=incumbent_label,
            challenger_label=challenger_label,
            incumbent_throughput=incumbent.throughput_total_tokens_per_s,
            challenger_throughput=challenger.throughput_total_tokens_per_s,
            ratio=0.0,
            kill_ratio=kill_ratio,
            verdict="incomplete",
            rationale=(
                f"one or both backends unreachable: "
                f"incumbent_reachable={incumbent.reachable}, "
                f"challenger_reachable={challenger.reachable}"
            ),
        )
    incumbent_tp = incumbent.throughput_total_tokens_per_s
    challenger_tp = challenger.throughput_total_tokens_per_s
    if incumbent_tp <= 0:
        ratio = 0.0
        verdict = "incomplete"
        rationale = "incumbent throughput is 0 (no successful samples)"
    else:
        ratio = challenger_tp / incumbent_tp
        if ratio >= kill_ratio:
            verdict = "migrate"
            rationale = (
                f"{challenger_label} throughput {challenger_tp:.1f} tok/s "
                f"beats {incumbent_label} {incumbent_tp:.1f} tok/s by "
                f"{ratio:.2f}× ≥ {kill_ratio:.2f}× kill threshold"
            )
        else:
            verdict = "keep"
            rationale = (
                f"{challenger_label} throughput {challenger_tp:.1f} tok/s "
                f"only {ratio:.2f}× of {incumbent_label} {incumbent_tp:.1f} "
                f"tok/s — below {kill_ratio:.2f}× kill threshold; abandon spike"
            )
    return ComparisonVerdict(
        incumbent_label=incumbent_label,
        challenger_label=challenger_label,
        incumbent_throughput=incumbent_tp,
        challenger_throughput=challenger_tp,
        ratio=round(ratio, 3),
        kill_ratio=kill_ratio,
        verdict=verdict,
        rationale=rationale,
    )


# ─── CLI ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", help="single backend to bench")
    p.add_argument("--model", default="amor-editor",
                   help="model id to request (default amor-editor)")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--compare", default=None,
                   help="two URLs CSV (incumbent,challenger) — "
                        "kicks off side-by-side mode")
    p.add_argument("--kill-ratio", type=float, default=1.5,
                   help="challenger must beat incumbent throughput by "
                        "this factor (default 1.5×)")
    p.add_argument("--out", default=None,
                   help="write JSON summary to this file")
    return p


async def _run(args: argparse.Namespace) -> int:
    if args.compare:
        urls = [u.strip() for u in args.compare.split(",") if u.strip()]
        if len(urls) != 2:
            logger.error("--compare needs exactly two URLs separated by comma")
            return 2
        incumbent_url, challenger_url = urls
        logger.info("benching incumbent: %s", incumbent_url)
        incumbent = await benchmark_backend(
            incumbent_url, args.model,
            concurrency=args.concurrency, rounds=args.rounds,
            max_tokens=args.max_tokens,
        )
        logger.info("benching challenger: %s", challenger_url)
        challenger = await benchmark_backend(
            challenger_url, args.model,
            concurrency=args.concurrency, rounds=args.rounds,
            max_tokens=args.max_tokens,
        )
        verdict = compare_backends(
            incumbent, challenger, kill_ratio=args.kill_ratio,
        )
        report = {
            "incumbent": incumbent.summary(),
            "challenger": challenger.summary(),
            "verdict": asdict(verdict),
        }
        print(json.dumps(report, indent=2))
        if args.out:
            from pathlib import Path
            Path(args.out).write_text(
                json.dumps(report, indent=2), encoding="utf-8",
            )
        return 0 if verdict.verdict != "incomplete" else 1

    if not args.base_url:
        logger.error("either --base-url (single) or --compare (pair) is required")
        return 2

    logger.info("benching %s", args.base_url)
    result = await benchmark_backend(
        args.base_url, args.model,
        concurrency=args.concurrency, rounds=args.rounds,
        max_tokens=args.max_tokens,
    )
    print(json.dumps(result.summary(), indent=2))
    if args.out:
        from pathlib import Path
        Path(args.out).write_text(
            json.dumps(result.summary(), indent=2), encoding="utf-8",
        )
    return 0 if result.reachable else 1


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
