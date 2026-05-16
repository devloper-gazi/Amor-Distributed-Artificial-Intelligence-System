#!/usr/bin/env python3
"""
Cycle G G6 — nvidia-smi → Prometheus sidecar exporter.

Polls ``nvidia-smi --query-gpu=...`` at a fixed interval and exposes
the result on a Prometheus scrape endpoint.

Why nvidia-smi instead of NVIDIA DCGM (Plan-agent recommendation)
------------------------------------------------------------------
On WSL2 + Docker Desktop, DCGM's userspace agent fights the
inference container for exclusive GPU access (the CUDA context
isolation in DXGI is brittle).  Reports from upstream NVIDIA docs
+ multiple GitHub issues: DCGM either fails to attach OR steals
the GPU mid-inference causing llama-swap stalls.

nvidia-smi is text-scraping (slower poll, ~50 ms per sample) but
holds NO persistent CUDA context — it queries the driver via the
NVML kernel module and exits.  No interference with the inference
container's CUDA context.  Tradeoff: 5s polling interval (vs DCGM
real-time) is good enough for VRAM trend lines + the launch gate's
"peak VRAM ≤ 7.6 GB" check.

Exposed metrics
---------------

  amor_gpu_memory_used_mb{index, name}      # current MB used
  amor_gpu_memory_free_mb{index, name}      # current MB free
  amor_gpu_memory_total_mb{index, name}     # capacity
  amor_gpu_utilization_pct{index, name}     # GPU util % (0-100)
  amor_gpu_memory_utilization_pct{index, name}  # memory bus util %
  amor_gpu_temperature_c{index, name}       # board temp °C
  amor_gpu_power_draw_w{index, name}        # current power W
  amor_gpu_poll_failures_total              # cumulative poll failures
  amor_gpu_poll_duration_seconds            # last successful poll wall

Run
---

  python monitoring/nvidia_smi_exporter.py --port 9835 --interval 5

  # Prometheus scrape (monitoring/prometheus.yml):
  #   - job_name: amor-gpu
  #     scrape_interval: 5s
  #     static_configs:
  #       - targets: ['nvidia-smi-exporter:9835']

Gracefully exits 0 when nvidia-smi is missing — the dashboard
panels just go to "no data" instead of bringing down the stack.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Optional

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


# ─── GPU sample shape ──────────────────────────────────────────────


@dataclass
class GPUSample:
    """One nvidia-smi row parsed from CSV."""
    index: int
    name: str
    memory_used_mb: float
    memory_free_mb: float
    memory_total_mb: float
    utilization_gpu_pct: float
    utilization_memory_pct: float
    temperature_c: float
    power_draw_w: float


# ─── nvidia-smi probe ──────────────────────────────────────────────


_QUERY_FIELDS = (
    "index",
    "name",
    "memory.used",
    "memory.free",
    "memory.total",
    "utilization.gpu",
    "utilization.memory",
    "temperature.gpu",
    "power.draw",
)


def nvidia_smi_available() -> bool:
    """True when the binary is on PATH and at least one GPU is
    visible to it.  Caller treats False as 'no GPU' and the exporter
    still serves an empty metrics page (so the Prometheus scrape
    doesn't 5xx)."""
    if not shutil.which("nvidia-smi"):
        return False
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
            capture_output=True, timeout=5.0, check=False,
        )
        return r.returncode == 0 and bool((r.stdout or b"").strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _parse_float(s: str) -> float:
    """nvidia-smi sometimes emits ``[N/A]`` or ``Not Supported`` for
    a field — return 0.0 instead of crashing the whole poll."""
    s = (s or "").strip()
    if not s or s.lower().startswith(("[n/a]", "n/a", "not supported")):
        return 0.0
    # Strip trailing units (e.g., "1234 MiB" → "1234")
    s = s.split()[0]
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def parse_nvidia_smi_csv(csv_text: str) -> List[GPUSample]:
    """Parse the comma-separated output of:
        nvidia-smi --query-gpu=<fields> --format=csv,noheader,nounits
    Returns one GPUSample per GPU.  Skips malformed rows."""
    samples: List[GPUSample] = []
    for line_raw in (csv_text or "").splitlines():
        cells = [c.strip() for c in line_raw.split(",")]
        if len(cells) < len(_QUERY_FIELDS):
            continue
        try:
            samples.append(GPUSample(
                index=int(_parse_float(cells[0])),
                name=cells[1] or f"gpu-{cells[0]}",
                memory_used_mb=_parse_float(cells[2]),
                memory_free_mb=_parse_float(cells[3]),
                memory_total_mb=_parse_float(cells[4]),
                utilization_gpu_pct=_parse_float(cells[5]),
                utilization_memory_pct=_parse_float(cells[6]),
                temperature_c=_parse_float(cells[7]),
                power_draw_w=_parse_float(cells[8]),
            ))
        except (ValueError, IndexError) as exc:
            logger.warning("malformed nvidia-smi row %r: %s", line_raw, exc)
            continue
    return samples


def poll_gpus(timeout_s: float = 5.0) -> List[GPUSample]:
    """Spawn nvidia-smi, parse, return samples.  On any error,
    return [] — caller increments poll_failures counter."""
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={','.join(_QUERY_FIELDS)}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, timeout=timeout_s, check=False,
        )
        if r.returncode != 0:
            return []
        return parse_nvidia_smi_csv((r.stdout or b"").decode("utf-8", errors="replace"))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


# ─── Prometheus text-format renderer ───────────────────────────────


def render_metrics(samples: List[GPUSample], *, poll_failures: int, poll_duration_s: float) -> str:
    """Emit Prometheus text-format from the latest poll snapshot."""
    lines: List[str] = []
    lines.append("# HELP amor_gpu_memory_used_mb GPU memory used in MB.")
    lines.append("# TYPE amor_gpu_memory_used_mb gauge")
    for s in samples:
        labels = f'{{index="{s.index}",name="{s.name}"}}'
        lines.append(f"amor_gpu_memory_used_mb{labels} {s.memory_used_mb}")

    lines.append("# HELP amor_gpu_memory_free_mb GPU memory free in MB.")
    lines.append("# TYPE amor_gpu_memory_free_mb gauge")
    for s in samples:
        labels = f'{{index="{s.index}",name="{s.name}"}}'
        lines.append(f"amor_gpu_memory_free_mb{labels} {s.memory_free_mb}")

    lines.append("# HELP amor_gpu_memory_total_mb GPU memory capacity in MB.")
    lines.append("# TYPE amor_gpu_memory_total_mb gauge")
    for s in samples:
        labels = f'{{index="{s.index}",name="{s.name}"}}'
        lines.append(f"amor_gpu_memory_total_mb{labels} {s.memory_total_mb}")

    lines.append("# HELP amor_gpu_utilization_pct GPU utilization 0-100.")
    lines.append("# TYPE amor_gpu_utilization_pct gauge")
    for s in samples:
        labels = f'{{index="{s.index}",name="{s.name}"}}'
        lines.append(f"amor_gpu_utilization_pct{labels} {s.utilization_gpu_pct}")

    lines.append("# HELP amor_gpu_memory_utilization_pct memory bus utilization 0-100.")
    lines.append("# TYPE amor_gpu_memory_utilization_pct gauge")
    for s in samples:
        labels = f'{{index="{s.index}",name="{s.name}"}}'
        lines.append(f"amor_gpu_memory_utilization_pct{labels} {s.utilization_memory_pct}")

    lines.append("# HELP amor_gpu_temperature_c GPU board temperature C.")
    lines.append("# TYPE amor_gpu_temperature_c gauge")
    for s in samples:
        labels = f'{{index="{s.index}",name="{s.name}"}}'
        lines.append(f"amor_gpu_temperature_c{labels} {s.temperature_c}")

    lines.append("# HELP amor_gpu_power_draw_w GPU power draw watts.")
    lines.append("# TYPE amor_gpu_power_draw_w gauge")
    for s in samples:
        labels = f'{{index="{s.index}",name="{s.name}"}}'
        lines.append(f"amor_gpu_power_draw_w{labels} {s.power_draw_w}")

    lines.append("# HELP amor_gpu_poll_failures_total Cumulative nvidia-smi poll failures.")
    lines.append("# TYPE amor_gpu_poll_failures_total counter")
    lines.append(f"amor_gpu_poll_failures_total {poll_failures}")

    lines.append("# HELP amor_gpu_poll_duration_seconds Last successful poll wall seconds.")
    lines.append("# TYPE amor_gpu_poll_duration_seconds gauge")
    lines.append(f"amor_gpu_poll_duration_seconds {poll_duration_s}")

    return "\n".join(lines) + "\n"


# ─── Poller thread ─────────────────────────────────────────────────


class _State:
    """Shared between poller + HTTP handler."""

    def __init__(self) -> None:
        self.samples: List[GPUSample] = []
        self.poll_failures: int = 0
        self.poll_duration_s: float = 0.0
        self.lock = threading.Lock()


def _poller_loop(state: _State, interval_s: float, stop_event: threading.Event) -> None:
    """Background loop: poll every interval_s seconds, update state."""
    while not stop_event.wait(0):
        t_start = time.perf_counter()
        samples = poll_gpus(timeout_s=max(2.0, interval_s - 0.5))
        elapsed = time.perf_counter() - t_start
        with state.lock:
            if samples:
                state.samples = samples
                state.poll_duration_s = round(elapsed, 4)
            else:
                state.poll_failures += 1
        if stop_event.wait(interval_s):
            return


# ─── HTTP handler ──────────────────────────────────────────────────


def _make_handler(state: _State):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silence stdlib's noisy log
            return

        def do_GET(self):
            if self.path == "/healthz":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok\n")
                return
            if self.path in ("/metrics", "/", ""):
                with state.lock:
                    body = render_metrics(
                        state.samples,
                        poll_failures=state.poll_failures,
                        poll_duration_s=state.poll_duration_s,
                    )
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return
            self.send_response(404)
            self.end_headers()

    return _Handler


# ─── CLI ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=9835,
                   help="HTTP port for the metrics endpoint")
    p.add_argument("--interval", type=float, default=5.0,
                   help="polling interval in seconds (default 5)")
    p.add_argument("--bind", default="0.0.0.0",
                   help="interface to bind (default 0.0.0.0)")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if not nvidia_smi_available():
        logger.warning(
            "nvidia-smi unavailable — exporter will serve empty metrics "
            "(dashboard panels show 'no data', stack stays up)",
        )

    state = _State()
    stop_event = threading.Event()
    poller = threading.Thread(
        target=_poller_loop,
        args=(state, args.interval, stop_event),
        daemon=True,
        name="nvidia-smi-poller",
    )
    poller.start()

    server = HTTPServer((args.bind, args.port), _make_handler(state))
    logger.info(
        "nvidia-smi-exporter listening on %s:%d (poll every %ss)",
        args.bind, args.port, args.interval,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
