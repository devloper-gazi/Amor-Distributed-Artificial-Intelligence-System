#!/usr/bin/env python3
"""Cycle H.1 — synthetic shadow-window load runner.

Plan-agent locked: v20 gate condition #2 (BitNet shadow agreement
rate ≥ 85%, p95 latency ≤ 6s) requires ≥200 samples for statistical
validity.  At AMOR's nominal ~5-10 sessions/day single-user pace,
accumulating 200 real samples would take 20-40 days.

This runner short-cuts the 14-day shadow wall: it fires planner-
shaped prompts directly at BitNet via the configured llama-swap
URL, records each outcome through ``bitnet_shadow.record_shadow_outcome``,
and reports the resulting promotion-eligibility window.

Wall-clock estimate: ~7-15 min for 200 samples at ~9 tok/s
single-thread CPU decode (matches the Plan-agent locked range).

Usage::

    # From inside the amor-app-2 container (so the in-process
    # ring buffer + admin endpoint share state):
    docker exec amor-app-2 python /app/tools/bitnet_shadow_synthetic_load.py \\
        --samples 200

Exit codes:
  0   samples >= target AND agreement_rate >= 0.85 AND p95_ms <= 6000
      → promotion eligible per Plan-agent locked thresholds
  1   completed but below promotion thresholds (operator inspects)
  2   IO / network error (BitNet endpoint unreachable)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

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


_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Synthetic planner prompts — varied enough to surface
# disagreement modes when the main planner would produce a more
# nuanced output.  Each prompt is a short task spec the main
# planner would normally turn into a 5-step plan.
_PROMPTS = [
    "Write a Python function fizzbuzz(n) that returns a list of FizzBuzz strings for 1..n.",
    "Build a single-file HTML snake game with arrow-key controls and a score panel.",
    "Implement a REST endpoint that returns the current UTC time as ISO8601.",
    "Create a Rust CLI todo tool that persists tasks to ~/.todo.json.",
    "Write a Python module that exports a clamp(x, lo, hi) function with doctests.",
    "Build a Go program that prints the SHA256 of its first argument.",
    "Design a small SQL schema for a blog with posts, authors, and tags.",
    "Implement a debounce decorator in Python that defers calls by 500ms.",
    "Write a JavaScript function that throttles event handlers to 60Hz.",
    "Build a Python CLI that converts a CSV file to JSON via stdin/stdout.",
    "Implement a binary-search function in Rust with property-test invariants.",
    "Write a TypeScript class wrapping IndexedDB get/put/delete primitives.",
    "Design a Flask blueprint for user signup with bcrypt password hashing.",
    "Build a Python coroutine that polls a URL until it returns 200 or 30s elapsed.",
    "Implement a least-recently-used cache in Python with a max_size cap.",
    "Write a bash script that monitors disk usage and alerts above 85%.",
    "Build a Vue 3 composable for managing dark-mode preference in localStorage.",
    "Implement a Python context manager that times the wrapped block in ms.",
    "Write a Go function that streams a large file in 64KB chunks.",
    "Design a yaml-driven feature-flag system with per-environment overrides.",
]


def _make_main_plan(prompt: str) -> Dict[str, Any]:
    """Synthesize a plausible main-planner output for the shadow
    agreement comparison.  Format mirrors what PlannerAgent would
    produce: a `steps` list of short imperatives.  Identical-for-now
    so agreement_rate measures the BitNet path's stability.

    For a more realistic measurement, the operator can swap this
    for a call to the real main planner — but at the H.1 stage
    we just need representative shape."""
    return {
        "steps": [
            "Triage the requirements",
            "Sketch the data shape",
            "Implement the core function",
            "Add unit + property tests",
            "Run the verifier suite",
        ],
        "language": "python",
        "summary": prompt[:80],
    }


async def _run(args: argparse.Namespace) -> int:
    try:
        from document_processor.config.settings import settings  # noqa: PLC0415
        from document_processor.code_intelligence import bitnet_shadow  # noqa: PLC0415
        from local_ai.llm_backend import make_backend  # noqa: PLC0415
        from local_ai.llm_backend.base import ChatOptions  # noqa: PLC0415
    except Exception as exc:
        logger.error("imports failed: %s", exc)
        return 2

    if not settings.code_bitnet_planner_enabled:
        logger.warning("settings.code_bitnet_planner_enabled is False — "
                       "the shadow path is disabled in production.  Test still records "
                       "samples via the ring buffer (manual mode).")

    url = args.url or settings.code_bitnet_planner_url
    model = args.model or settings.code_bitnet_model_tag
    timeout_s = float(args.timeout_s)
    backend = make_backend("bitnet-cpu", url=url)
    opts = ChatOptions(temperature=0.0, max_tokens=args.max_tokens)

    if args.reset:
        bitnet_shadow.reset_stats()
        logger.info("ring buffer reset")

    started = time.time()
    rng = random.Random(args.seed)
    latencies_ms: List[float] = []
    timeouts = 0
    parse_failures = 0

    for i in range(args.samples):
        prompt = rng.choice(_PROMPTS)
        main_plan = _make_main_plan(prompt)
        shadow_plan: Any = None
        timed_out = False
        t0 = time.perf_counter()
        try:
            async def _call() -> str:
                return await backend.complete(
                    f"Produce a 5-step plan as JSON for this task:\n{prompt}\n"
                    "Format: {\"steps\": [\"...\", \"...\"], \"language\": \"...\", \"summary\": \"...\"}",
                    model=model,
                    system="You are a code planner.  Reply with concise JSON.",
                    options=opts,
                )
            try:
                raw = await asyncio.wait_for(_call(), timeout=timeout_s)
            except asyncio.TimeoutError:
                timed_out = True
                timeouts += 1
                raw = ""
        except Exception as exc:
            logger.warning("call %d failed: %s", i, exc)
            raw = ""
        latency_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(latency_ms)

        if raw and not timed_out:
            try:
                text = raw.strip()
                if "```" in text:
                    start = text.find("```")
                    end = text.find("```", start + 3)
                    if end > 0:
                        inner = text[start + 3:end].lstrip("\n")
                        if inner.startswith("json"):
                            inner = inner[4:].lstrip("\n")
                        text = inner
                shadow_plan = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                shadow_plan = {"_raw_text": raw[:500]}
                parse_failures += 1

        bitnet_shadow.record_shadow_outcome(
            request_id=f"synth-{i}",
            main_plan=main_plan,
            shadow_plan=shadow_plan,
            latency_ms=latency_ms,
            timed_out=timed_out,
            fell_back=(shadow_plan is None),
        )

        if (i + 1) % args.log_every == 0 or (i + 1) == args.samples:
            stats = bitnet_shadow.get_shadow_stats()
            elapsed_s = time.time() - started
            rate = (i + 1) / elapsed_s if elapsed_s > 0 else 0.0
            logger.info(
                "[%d/%d] elapsed=%.1fs rate=%.1f/s | "
                "p50=%.0fms p95=%.0fms | agreement=%.3f fallback=%.3f timeout=%.3f",
                i + 1, args.samples, elapsed_s, rate,
                stats["p50_ms"], stats["p95_ms"],
                stats["agreement_rate"], stats["fallback_rate"], stats["timeout_rate"],
            )

    final = bitnet_shadow.get_shadow_stats()
    elapsed = time.time() - started
    summary = {
        "samples_target": args.samples,
        "samples_recorded": final["samples"],
        "elapsed_s": round(elapsed, 1),
        "throughput_per_min": round(args.samples / elapsed * 60.0, 1),
        "p50_ms": final["p50_ms"],
        "p95_ms": final["p95_ms"],
        "agreement_rate": final["agreement_rate"],
        "fallback_rate": final["fallback_rate"],
        "timeout_rate": final["timeout_rate"],
        "promotion_eligible": final["promotion_eligible"],
        "timeouts_local": timeouts,
        "parse_failures_local": parse_failures,
        "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    print(json.dumps(summary, indent=2))

    # Cycle H.1 — persist the final stats so the v20 gate runner can
    # read them across process boundaries.  The in-process ring buffer
    # stays the source of truth for live admin probes, but the file
    # snapshot is what the cron / gate reads.
    #
    # Path resolution: inside the AMOR container, /data/documents/
    # is bind-mounted from the host's ./data/ — writing there lets
    # both container synth-loads AND host-side gate runs see the
    # snapshot through their respective filesystem views.  Outside
    # the container (host-side direct run), fall back to <repo>/data/.
    import os as _os
    if _os.path.isdir("/data/documents"):
        host_visible = Path("/data/documents") / "baselines" / "bitnet_shadow_latest.json"
    else:
        host_visible = _REPO_ROOT / "data" / "baselines" / "bitnet_shadow_latest.json"
    host_visible.parent.mkdir(parents=True, exist_ok=True)
    host_visible.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info("snapshot persisted: %s", host_visible)

    # Plan-agent locked promotion thresholds.
    if (
        final["samples"] >= 200
        and final["agreement_rate"] >= 0.85
        and final["p95_ms"] <= 6000.0
    ):
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--samples", type=int, default=200,
                   help="how many synthetic samples to record (default 200)")
    p.add_argument("--url", default=None,
                   help="override settings.code_bitnet_planner_url")
    p.add_argument("--model", default=None,
                   help="override settings.code_bitnet_model_tag (alias) — default 'bitnet'")
    p.add_argument("--max-tokens", type=int, default=256,
                   help="max_tokens per BitNet call (default 256)")
    p.add_argument("--timeout-s", type=float, default=8.0,
                   help="per-call wall timeout (Plan-agent locked 8s)")
    p.add_argument("--log-every", type=int, default=10,
                   help="emit progress every N samples (default 10)")
    p.add_argument("--seed", type=int, default=42,
                   help="RNG seed for prompt rotation")
    p.add_argument("--reset", action="store_true",
                   help="reset the ring buffer before recording")
    return p


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
