#!/usr/bin/env python3
"""
Cycle C Sprint 6 Day 3 — adapter eval-delta runner.

After ``orpo_qwen_coder.py`` writes a PEFT adapter and
``convert_lora_gguf.py`` packs it into GGUF, this script measures the
adapter's effect on the canonical Sprint 0 corpus.

Workflow
--------
1. Snapshot the most recent baseline (``sprint0_latest.json``) — that's
   the "without adapter" reference.  If none exists, the script
   refuses to run (operator must run Sprint 0 once first).
2. Toggle the adapter on via llama-server's ``/v1/lora-adapters``
   endpoint.
3. Re-run the Sprint 0 corpus.
4. Toggle the adapter off (always, even on failure).
5. Diff per-prompt: judge delta, latency delta, token-count delta.
6. Persist a JSON report under ``data/training/eval_<utc-iso>.json``.

The Day 4 admin UI button posts to a route that wraps this script;
the script itself is the canonical CLI so a release manager can run
it from a CI shell without going through the UI.

Acceptance gate (Day 4 promote button reads this)
-------------------------------------------------
* mean(judge delta) ≥ 0  → "no regression"
* worst per-prompt judge delta ≥ -1
* p50 latency increase ≤ 20 %
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class PerPromptDelta:
    id: str
    judge_before: float | None
    judge_after: float | None
    judge_delta: float | None
    latency_before_ms: float | None
    latency_after_ms: float | None
    latency_delta_pct: float | None


def _judge_score(p: dict[str, Any]) -> float | None:
    """Sprint 0 judge structure: ``{"correctness": 1-5, "completeness": 1-5}``
    OR ``{"final_score": ...}``.  Average the two rubrics into one
    number (0-10) so deltas have a single dimension."""
    j = p.get("judge") or {}
    if "final_score" in j:
        return float(j["final_score"])
    correctness = j.get("correctness")
    completeness = j.get("completeness")
    if correctness is None or completeness is None:
        return None
    try:
        return (float(correctness) + float(completeness)) / 2.0
    except (TypeError, ValueError):
        return None


def _latency_ms(p: dict[str, Any]) -> float | None:
    metrics = p.get("metrics") or {}
    v = metrics.get("e2e_wall_clock_ms") or metrics.get("wall_clock_ms")
    return float(v) if v is not None else None


def diff_runs(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compute a flat per-prompt diff + a summary with the gate
    decision baked in.  Pure function — no IO."""
    before_by_id = {p["id"]: p for p in before.get("prompts", [])}
    after_by_id = {p["id"]: p for p in after.get("prompts", [])}
    ids = sorted(set(before_by_id) | set(after_by_id))

    deltas: list[PerPromptDelta] = []
    judge_diffs: list[float] = []
    latency_diffs: list[float] = []
    for pid in ids:
        b = before_by_id.get(pid, {})
        a = after_by_id.get(pid, {})
        jb = _judge_score(b)
        ja = _judge_score(a)
        jd = (ja - jb) if (jb is not None and ja is not None) else None
        lb = _latency_ms(b)
        la = _latency_ms(a)
        ld_pct = ((la - lb) / lb * 100.0) if (lb and la and lb > 0) else None
        deltas.append(
            PerPromptDelta(
                id=pid,
                judge_before=jb,
                judge_after=ja,
                judge_delta=jd,
                latency_before_ms=lb,
                latency_after_ms=la,
                latency_delta_pct=ld_pct,
            ),
        )
        if jd is not None:
            judge_diffs.append(jd)
        if ld_pct is not None:
            latency_diffs.append(ld_pct)

    mean_judge_delta = statistics.fmean(judge_diffs) if judge_diffs else None
    worst_judge_delta = min(judge_diffs) if judge_diffs else None
    p50_latency_pct = statistics.median(latency_diffs) if latency_diffs else None

    # Acceptance gate (matches the docstring above).
    no_regression = (
        mean_judge_delta is not None
        and worst_judge_delta is not None
        and p50_latency_pct is not None
        and mean_judge_delta >= 0
        and worst_judge_delta >= -1
        and p50_latency_pct <= 20.0
    )

    return {
        "summary": {
            "n_prompts": len(deltas),
            "mean_judge_delta": mean_judge_delta,
            "worst_judge_delta": worst_judge_delta,
            "p50_latency_pct": p50_latency_pct,
            "promote_ok": bool(no_regression),
        },
        "per_prompt": [
            {
                "id": d.id,
                "judge_before": d.judge_before,
                "judge_after": d.judge_after,
                "judge_delta": d.judge_delta,
                "latency_before_ms": d.latency_before_ms,
                "latency_after_ms": d.latency_after_ms,
                "latency_delta_pct": d.latency_delta_pct,
            }
            for d in deltas
        ],
    }


async def toggle_adapter(base_url: str, *, adapter_id: int, scale: float) -> dict[str, Any]:
    """POST /v1/lora-adapters [{"id": adapter_id, "scale": scale}].

    ``scale=1.0`` activates the adapter; ``scale=0.0`` deactivates it.
    ``adapter_id`` is the 0-based index llama-server assigns at
    startup (matches the order of ``--lora`` flags).
    """
    payload = [{"id": adapter_id, "scale": scale}]
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{base_url.rstrip('/')}/v1/lora-adapters",
            json=payload,
        )
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text[:200], "status": r.status_code}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the Sprint 0 corpus with the adapter on, then diff against the baseline.",
    )
    p.add_argument(
        "--baseline",
        default="/app/data/baselines/sprint0_latest.json",
        help="path to the without-adapter baseline JSON",
    )
    p.add_argument(
        "--after",
        default=None,
        help=(
            "path to a precomputed with-adapter run JSON.  When set, the "
            "script ONLY diffs and skips running the corpus / toggling "
            "the adapter (useful for offline analysis)."
        ),
    )
    p.add_argument(
        "--llamaswap-url",
        default=os.environ.get("AMOR_LLAMASWAP_URL", "http://amor-llama-swap:9100"),
        help="llama-server base URL for the lora-adapters POST",
    )
    p.add_argument("--adapter-id", type=int, default=0)
    p.add_argument(
        "--out",
        default=None,
        help="destination JSON.  Default: data/training/eval_<utc-iso>.json",
    )
    p.add_argument(
        "--no-toggle",
        action="store_true",
        help="skip the lora-adapters toggle (assume operator already activated)",
    )
    return p


async def run(args: argparse.Namespace) -> int:
    baseline_path = Path(args.baseline)
    if not baseline_path.is_file():
        logger.error(
            "baseline missing: %s — run Sprint 0 first.", baseline_path,
        )
        return 2
    before = json.loads(baseline_path.read_text(encoding="utf-8"))

    if args.after:
        after_path = Path(args.after)
        if not after_path.is_file():
            logger.error("after JSON missing: %s", after_path)
            return 2
        after = json.loads(after_path.read_text(encoding="utf-8"))
    else:
        # Run path: toggle ON, run corpus, snapshot, toggle OFF.
        if not args.no_toggle:
            on_resp = await toggle_adapter(
                args.llamaswap_url,
                adapter_id=args.adapter_id,
                scale=1.0,
            )
            logger.info("adapter ON: %s", on_resp)
        try:
            sys.path.insert(0, "/app")
            from document_processor.services.baseline_runner import (  # noqa: PLC0415
                RunnerConfig,
                run_baseline,
            )
            cfg = RunnerConfig(api_base=os.environ.get("AMOR_BASELINE_API_BASE", "http://localhost:8000"))
            output_dir = Path("/app/data/baselines")
            await run_baseline(
                prompts_path=Path("/app/tests/baselines/sprint0_prompts.json"),
                output_dir=output_dir,
                cfg=cfg,
                backend_name=os.environ.get("AMOR_LLM_BACKEND", "ollama"),
            )
            after = json.loads((output_dir / "sprint0_latest.json").read_text(encoding="utf-8"))
        finally:
            if not args.no_toggle:
                off_resp = await toggle_adapter(
                    args.llamaswap_url,
                    adapter_id=args.adapter_id,
                    scale=0.0,
                )
                logger.info("adapter OFF: %s", off_resp)

    report = diff_runs(before, after)

    out_path = Path(
        args.out
        or f"/app/data/training/eval_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json",
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out_path), **report["summary"]}, indent=2))
    return 0


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    args = build_parser().parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
