"""
PerformanceBenchmarker — runs user code at progressive input scales,
measures wall time + memory peak via ``tracemalloc`` inside the sandbox,
fits a power-law exponent to the runtime curve, and surfaces the
delta between LLM-claimed complexity and what we actually measured.

Strategy
--------
We don't add a multi-input mode to the sandbox (that's a bigger
change). Instead we BUILD a single script that bundles the user's
code with a small bench harness, run it as one sandbox call, and
parse `BENCH_RESULT={...}` JSON lines from stdout.

The harness:
  1. Imports the user code as a top-level module.
  2. Picks the most likely callable (first non-underscore, non-class
     callable defined in the module).
  3. For each scale in the configured list, generates a synthetic
     input (default = list of ints), times the call, captures peak
     memory via tracemalloc, prints one BENCH_RESULT line.

Power-law fit
-------------
We fit ``log(ms) = b * log(n) + log(a)`` over completed scales.
NumPy provides the polyfit; if NumPy isn't available we fall back to
a 2-point closed-form using the smallest + largest scale (still
useful as a directional signal). The exponent ``b`` is what we
compare to the claimed Big-O.

Auto-shrink
-----------
If a scale completes in more than ``80 % * timeout_per_scale``, the
benchmarker stops adding larger scales — protects the whole sandbox
budget from a runaway final scale. Reports only completed scales.
Fail-soft: a sandbox crash, a per-scale exception, or all-scales-
timed-out yields a `BenchmarkResult` with `failed=True` rather than
raising.
"""

from __future__ import annotations

import json
import logging
import math
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


_BENCH_RESULT_RE = re.compile(r"^BENCH_RESULT=({.*})\s*$", re.MULTILINE)
_BENCH_ERROR_RE = re.compile(r"^BENCH_ERROR(?:_AT_SCALE_(\d+))?:\s*(.+)$", re.MULTILINE)


# Map a measured exponent `b` (from runtime ~ n^b) to the closest
# named bound on our complexity ladder. Used to label the curve fit.
def _exponent_to_label(b: float) -> str:
    if b < 0.15:
        return "O(1)"
    if b < 0.5:
        return "O(log n)"
    if b < 1.4:
        return "O(n)"
    if b < 1.8:
        return "O(n log n)"
    if b < 2.4:
        return "O(n^2)"
    if b < 3.4:
        return "O(n^3)"
    if b < 5.0:
        return f"O(n^{int(round(b))})"
    return "O(2^n)"  # treat very steep growth as exponential


@dataclass
class BenchmarkRecord:
    """One scale's measurement."""

    scale: int
    runtime_ms: float
    peak_kb: int
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.runtime_ms > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "runtime_ms": round(self.runtime_ms, 3),
            "peak_kb": self.peak_kb,
            "error": self.error,
        }


@dataclass
class BenchmarkFit:
    """Power-law fit summary."""

    exponent: float = 0.0
    intercept: float = 0.0
    measured_label: str = "O(?)"
    samples_used: int = 0
    method: str = "none"  # "polyfit_log" | "two_point_log" | "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "exponent": round(self.exponent, 4),
            "intercept": round(self.intercept, 4),
            "measured_label": self.measured_label,
            "samples_used": self.samples_used,
            "method": self.method,
        }


@dataclass
class BenchmarkResult:
    """Full benchmark output for one candidate implementation."""

    records: list[BenchmarkRecord] = field(default_factory=list)
    fit: BenchmarkFit = field(default_factory=BenchmarkFit)
    claimed_label: str = ""
    claim_vs_measured: int = 0    # -1 / 0 / +1 from compare_bounds
    failed: bool = False
    failure_reason: str = ""
    raw_stdout_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [r.to_dict() for r in self.records],
            "fit": self.fit.to_dict(),
            "claimed_label": self.claimed_label,
            "claim_vs_measured": self.claim_vs_measured,
            "failed": self.failed,
            "failure_reason": self.failure_reason,
            "raw_stdout_excerpt": (self.raw_stdout_excerpt or "")[:800],
        }


# ─── Bench harness (rendered into the sandbox script) ─────────────


_BENCH_HARNESS = '''\
# ── AMOR Reactor benchmark harness ─────────────────────────────────
import json as _json
import sys as _sys
import time as _time
import tracemalloc as _tm
import inspect as _inspect

_SCALES = [int(s) for s in "{scales}".split(",") if s]
_TIMEOUT_PER_SCALE_S = float({timeout_per_scale})


def _amor_pick_target():
    """Find the most likely user-facing callable in this module."""
    candidates = []
    for name, obj in list(globals().items()):
        if name.startswith("_") or name in {{"json", "sys", "time", "tracemalloc", "inspect"}}:
            continue
        if callable(obj) and not isinstance(obj, type):
            try:
                _inspect.signature(obj)
            except (TypeError, ValueError):
                continue
            candidates.append((name, obj))
    if not candidates:
        return None, None
    # Prefer functions whose name suggests "main entry": main, run,
    # solve, sort, search, find. Fall back to the LAST defined fn so
    # later-defined wrappers override imported helpers.
    PREFERRED = ("solve", "main", "run", "sort", "search", "find", "compute")
    for name, obj in candidates:
        if any(p in name.lower() for p in PREFERRED):
            return name, obj
    return candidates[-1]


def _amor_default_input(scale, target):
    """Pick a synthetic input. Default is list(range(scale))."""
    sig = _inspect.signature(target)
    n_args = len([p for p in sig.parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                  and p.default is p.empty])
    if n_args == 0:
        return ()
    # Look at first non-self param; pick the input type by hint/name.
    first = next(iter(sig.parameters.values()))
    name = (first.name or "").lower()
    if "n" == name or name.endswith("_n") or name in ("count", "size", "limit"):
        return (scale,)
    if name in ("s", "string", "text", "word", "name"):
        return ("a" * scale,)
    return (list(range(scale)),)


def _amor_run_one(scale, target):
    args = _amor_default_input(scale, target)
    _tm.start()
    t0 = _time.perf_counter()
    try:
        target(*args)
    except Exception as exc:
        _tm.stop()
        return {{"scale": scale, "ms": 0.0, "peak_kb": 0,
                "error": "{{}}: {{}}".format(type(exc).__name__, str(exc)[:200])}}
    elapsed_ms = (_time.perf_counter() - t0) * 1000.0
    _, peak = _tm.get_traced_memory()
    _tm.stop()
    return {{"scale": scale, "ms": elapsed_ms,
            "peak_kb": int(peak // 1024)}}


def _amor_main():
    name, target = _amor_pick_target()
    if target is None:
        print("BENCH_ERROR: no usable callable found in module")
        return
    print("BENCH_TARGET=" + name, flush=True)
    for scale in _SCALES:
        rec = _amor_run_one(scale, target)
        print("BENCH_RESULT=" + _json.dumps(rec), flush=True)
        # Auto-shrink — bail out if this scale ate more than 80 % of
        # the per-scale budget; later scales would just time out.
        if rec.get("ms", 0) >= 0.8 * _TIMEOUT_PER_SCALE_S * 1000:
            print("BENCH_AUTO_SHRINK=" + str(scale), flush=True)
            break


_amor_main()
'''


class PerformanceBenchmarker:
    """Wraps user code + bench harness into one sandbox script.

    `sandbox` must implement `async execute(code, language, timeout) →
    object with .stdout, .stderr, .exit_code, .skipped`. The real
    `ExecutionSandbox` works; tests can pass a stand-in.
    """

    def __init__(
        self,
        sandbox: Any,
        *,
        scales: list[int] | None = None,
        timeout_per_scale_s: int = 8,
    ) -> None:
        self._sandbox = sandbox
        self._scales = list(scales or [10, 100, 1_000, 10_000])
        self._timeout_per_scale_s = max(1, int(timeout_per_scale_s))

    async def run(
        self,
        code: str,
        *,
        language: str = "python",
        claimed_label: str = "",
    ) -> BenchmarkResult:
        if not (code or "").strip():
            return BenchmarkResult(failed=True, failure_reason="no code to benchmark")
        if language != "python":
            # The harness is Python-only this round.
            return BenchmarkResult(
                failed=True,
                failure_reason=f"benchmarker only supports python, got {language}",
            )

        script = self._build_script(code)
        total_timeout = self._timeout_per_scale_s * len(self._scales) + 10
        try:
            result = await self._sandbox.execute(
                script, language=language, timeout=total_timeout,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("benchmarker_sandbox_call_failed: %s", exc)
            return BenchmarkResult(failed=True, failure_reason=str(exc)[:300])

        if getattr(result, "skipped", False):
            return BenchmarkResult(
                failed=True, failure_reason="sandbox unavailable (skipped)",
                claimed_label=claimed_label,
                raw_stdout_excerpt=getattr(result, "stdout", "") or "",
            )

        stdout = getattr(result, "stdout", "") or ""
        records = self._parse_records(stdout)
        if not records:
            err = self._parse_error(stdout)
            return BenchmarkResult(
                failed=True,
                failure_reason=err or "no BENCH_RESULT lines parsed",
                claimed_label=claimed_label,
                raw_stdout_excerpt=stdout[:800],
            )

        fit = self._fit_power_law(records)
        cmp = self._compare_to_claim(claimed_label, fit.measured_label)

        return BenchmarkResult(
            records=records,
            fit=fit,
            claimed_label=claimed_label,
            claim_vs_measured=cmp,
            raw_stdout_excerpt=stdout[:800],
        )

    # ─── Internals ─────────────────────────────────────────────────

    def _build_script(self, user_code: str) -> str:
        scales_str = ",".join(str(s) for s in self._scales)
        # Trailing newline ensures the harness starts on a fresh line.
        prefix = textwrap.dedent(user_code).rstrip() + "\n\n"
        harness = _BENCH_HARNESS.format(
            scales=scales_str,
            timeout_per_scale=self._timeout_per_scale_s,
        )
        return prefix + harness

    @staticmethod
    def _parse_records(stdout: str) -> list[BenchmarkRecord]:
        out: list[BenchmarkRecord] = []
        for m in _BENCH_RESULT_RE.finditer(stdout):
            try:
                payload = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            out.append(BenchmarkRecord(
                scale=int(payload.get("scale", 0)),
                runtime_ms=float(payload.get("ms", 0.0)),
                peak_kb=int(payload.get("peak_kb", 0)),
                error=payload.get("error"),
            ))
        return out

    @staticmethod
    def _parse_error(stdout: str) -> str:
        m = _BENCH_ERROR_RE.search(stdout)
        if not m:
            return ""
        scale = m.group(1) or "?"
        msg = m.group(2)
        return f"scale={scale}: {msg}"

    @staticmethod
    def _fit_power_law(records: list[BenchmarkRecord]) -> BenchmarkFit:
        good = [r for r in records if r.succeeded]
        if len(good) < 2:
            return BenchmarkFit(
                samples_used=len(good),
                method="none",
                measured_label="O(?)",
            )
        xs = [math.log(r.scale) for r in good if r.scale > 0]
        ys = [math.log(r.runtime_ms) for r in good if r.runtime_ms > 0]
        if len(xs) != len(ys) or len(xs) < 2:
            return BenchmarkFit(
                samples_used=len(good), method="none", measured_label="O(?)",
            )
        try:
            import numpy as np  # noqa: PLC0415
            coeffs = np.polyfit(xs, ys, 1)  # [slope, intercept]
            b, a = float(coeffs[0]), float(coeffs[1])
            return BenchmarkFit(
                exponent=b,
                intercept=a,
                measured_label=_exponent_to_label(b),
                samples_used=len(good),
                method="polyfit_log",
            )
        except Exception:
            # Two-point closed-form fallback — slope between the
            # smallest and largest log-log points.
            x0, y0 = xs[0], ys[0]
            x1, y1 = xs[-1], ys[-1]
            if x1 == x0:
                return BenchmarkFit(samples_used=len(good), method="none",
                                    measured_label="O(?)")
            b = (y1 - y0) / (x1 - x0)
            a = y0 - b * x0
            return BenchmarkFit(
                exponent=b, intercept=a,
                measured_label=_exponent_to_label(b),
                samples_used=len(good), method="two_point_log",
            )

    @staticmethod
    def _compare_to_claim(claimed: str, measured: str) -> int:
        if not claimed or not measured:
            return 0
        # Reuse the SymbolicComplexity comparator so the ladder ranks
        # are consistent across the reactor.
        from .symbolic_complexity import SymbolicComplexity  # noqa: PLC0415
        return SymbolicComplexity.compare_bounds(claimed, measured)
