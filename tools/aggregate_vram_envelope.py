#!/usr/bin/env python3
"""Cycle H.0.4 follow-on — VRAM envelope aggregator.

Reads nvidia-smi over a sampling window, tracks the peak ``memory.used``
across all polls, and persists the result to:

    data/baselines/vram_envelope_latest.json

The v20 launch gate condition #6 (``vram_peak_gb <= 7.2``) resolves
``peak_vram_mb`` from this snapshot.  When operators want the
canonical 14-day envelope, run this script via cron / Task Scheduler
with ``--duration-s 1209600`` (14 days) and a 30s poll cadence.  For
ad-hoc checkpoint measurements, default 30s × 10 polls is enough to
catch transient peaks during normal usage.

Sources:
  1. ``nvidia-smi --query-gpu=memory.used,memory.total --format=csv``
     (direct, no Prometheus dependency)
  2. monitoring/nvidia_smi_exporter.py's rolling state when
     ``--from-exporter`` is set (uses /metrics endpoint instead)

Usage::

    # One-shot 30s × 10 polls (default)
    python tools/aggregate_vram_envelope.py

    # 14-day window with 30s cadence
    python tools/aggregate_vram_envelope.py --duration-s 1209600 --interval-s 30

    # Hands-off ops mode (single sample, no loop)
    python tools/aggregate_vram_envelope.py --one-shot

Exit codes:
  0   snapshot written (operator inspects peak vs gate threshold)
  1   nvidia-smi missing / GPU unreachable / IO error
  2   bad CLI args
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

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
_OUT_ROOT = _REPO_ROOT / "data" / "baselines"


def _poll_nvidia_smi() -> Optional[List[Tuple[float, float]]]:
    """Return [(used_mb, total_mb), ...] per GPU, or None on failure.

    Uses subprocess directly so the script doesn't depend on the
    nvidia_smi_exporter module (decoupled from the monitoring stack)."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=5.0,
        )
    except FileNotFoundError:
        logger.warning("nvidia-smi not found on PATH")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("nvidia-smi timed out")
        return None
    if result.returncode != 0:
        logger.warning("nvidia-smi returned %d: %s", result.returncode, result.stderr[:200])
        return None
    out: List[Tuple[float, float]] = []
    for line in result.stdout.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) >= 2:
            try:
                out.append((float(cells[0]), float(cells[1])))
            except ValueError:
                continue
    return out or None


def _poll_from_exporter(url: str) -> Optional[List[Tuple[float, float]]]:
    """Alternative source: scrape the nvidia_smi_exporter /metrics
    endpoint.  Pulls ``amor_gpu_memory_used_mb`` + ``amor_gpu_memory_total_mb``
    gauges for each GPU.  Returns None when unreachable."""
    try:
        import urllib.request  # noqa: PLC0415
        with urllib.request.urlopen(url, timeout=5.0) as r:
            text = r.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("exporter scrape failed: %s", exc)
        return None
    used: dict[str, float] = {}
    total: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("amor_gpu_memory_used_mb{"):
            idx = line.split('index="', 1)[-1].split('"', 1)[0]
            val = line.rsplit(" ", 1)[-1]
            try:
                used[idx] = float(val)
            except ValueError:
                continue
        elif line.startswith("amor_gpu_memory_total_mb{"):
            idx = line.split('index="', 1)[-1].split('"', 1)[0]
            val = line.rsplit(" ", 1)[-1]
            try:
                total[idx] = float(val)
            except ValueError:
                continue
    if not used:
        return None
    return [(used[k], total.get(k, 0.0)) for k in sorted(used)]


def run(args: argparse.Namespace) -> int:
    use_exporter = bool(args.from_exporter)
    if use_exporter:
        url = args.exporter_url
        logger.info("source: nvidia_smi_exporter at %s", url)
    else:
        logger.info("source: direct nvidia-smi subprocess")

    interval_s = max(1.0, float(args.interval_s))
    duration_s = float(args.duration_s)
    one_shot = bool(args.one_shot)

    started = time.time()
    peak_mb = 0.0
    poll_count = 0
    failures = 0

    def _poll_once() -> Optional[float]:
        if use_exporter:
            samples = _poll_from_exporter(args.exporter_url)
        else:
            samples = _poll_nvidia_smi()
        if not samples:
            return None
        # Per-poll peak across all GPUs.
        return max(s[0] for s in samples)

    if one_shot:
        peak = _poll_once()
        if peak is None:
            logger.error("single poll failed; no snapshot written")
            return 1
        peak_mb = peak
        poll_count = 1
    else:
        while True:
            current = _poll_once()
            poll_count += 1
            if current is None:
                failures += 1
            else:
                if current > peak_mb:
                    peak_mb = current
                    logger.info(
                        "[%d/%s] new peak: %.0f MB",
                        poll_count,
                        "∞" if duration_s <= 0 else f"~{int(duration_s/interval_s)}",
                        current,
                    )
            elapsed = time.time() - started
            if duration_s > 0 and elapsed >= duration_s:
                break
            if not args.continuous and poll_count >= int(args.max_polls):
                break
            time.sleep(interval_s)

    elapsed = time.time() - started

    if peak_mb <= 0.0 and failures > 0:
        logger.error("all polls failed; no snapshot written")
        return 1

    snapshot = {
        "peak_vram_mb": round(peak_mb, 1),
        "peak_vram_gb": round(peak_mb / 1024.0, 3),
        "poll_count": poll_count,
        "poll_failures": failures,
        "interval_s": interval_s,
        "duration_s": round(elapsed, 1),
        "source": "exporter" if use_exporter else "nvidia-smi",
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    # Pick the host-visible path inside the container; else local.
    if os.path.isdir("/data/documents"):
        out_root = Path("/data/documents") / "baselines"
    else:
        out_root = _OUT_ROOT
    out_root.mkdir(parents=True, exist_ok=True)
    out = out_root / "vram_envelope_latest.json"
    out.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    logger.info(
        "vram_envelope_latest written: %s (peak=%.1f MB / %.2f GB, polls=%d, failures=%d)",
        out, snapshot["peak_vram_mb"], snapshot["peak_vram_gb"],
        poll_count, failures,
    )
    print(json.dumps(snapshot, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--interval-s", type=float, default=3.0,
                   help="seconds between polls (default 3)")
    p.add_argument("--duration-s", type=float, default=0.0,
                   help="run for this many seconds (0 → use --max-polls)")
    p.add_argument("--max-polls", type=int, default=10,
                   help="poll count cap when duration_s=0 (default 10)")
    p.add_argument("--continuous", action="store_true",
                   help="ignore --max-polls; loop until --duration-s reached")
    p.add_argument("--one-shot", action="store_true",
                   help="single nvidia-smi poll + exit (fastest)")
    p.add_argument("--from-exporter", action="store_true",
                   help="scrape monitoring/nvidia_smi_exporter /metrics instead of nvidia-smi")
    p.add_argument("--exporter-url", default="http://localhost:9835/metrics",
                   help="exporter URL (default :9835)")
    return p


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except KeyboardInterrupt:
        logger.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
