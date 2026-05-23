"""
Cycle H Phase A.1 — BitNet shadow planner routing helper.

Decides whether a given planning request gets a parallel BitNet shadow
call.  The shadow call result is LOGGED for agreement-rate
measurement; the user-visible plan still comes from the main planner.
After 14-day shadow window with ≥85% agreement and p95 latency ≤6s,
operator promotes BitNet to active routing.

Why this is a routing helper, not an agent class
------------------------------------------------
The existing `PlannerAgent` (code_intelligence/agents.py:481-634)
already takes an injected `llm_call`.  The routing decision
("which backend gets this request?") belongs OUTSIDE the agent —
that's a higher-level concern about WHICH llm_call to inject.
Following the Cycle F Sprint 3 ContextVar pattern, this module
provides:

  * `should_shadow_to_bitnet(request_id) -> bool` — hash-based
    deterministic traffic split per session
  * `record_shadow_outcome(main_plan, shadow_plan, latency_ms,
    timed_out, fell_back) -> None` — appends to in-process ring
    buffer + emits Prometheus metrics
  * `get_shadow_stats() -> dict` — surfaces agreement rate, p50/p95
    latency, fallback count for the /admin/llm dashboard

The shadow call itself is fired by the orchestrator (engine.py) via
`asyncio.create_task` so the main planner's wall-clock isn't
affected.  Plan-agent locked: shadow mode NEVER blocks.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional

logger = logging.getLogger(__name__)


# ─── Per-process telemetry ring buffers ────────────────────────────


@dataclass
class ShadowSample:
    """One observation from a shadow call."""
    request_id: str
    main_plan_hash: str       # sha256[:16] of the main planner's JSON output
    shadow_plan_hash: str     # ditto for the shadow planner; "" if no result
    latency_ms: float
    timed_out: bool
    fell_back: bool           # main planner's output was returned because shadow failed
    agreement: bool           # main_plan_hash == shadow_plan_hash
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "main_plan_hash": self.main_plan_hash,
            "shadow_plan_hash": self.shadow_plan_hash,
            "latency_ms": round(self.latency_ms, 2),
            "timed_out": self.timed_out,
            "fell_back": self.fell_back,
            "agreement": self.agreement,
            "ts": self.timestamp,
        }


_SAMPLES: Deque[ShadowSample] = deque(maxlen=2000)


# ─── Traffic-split decision ────────────────────────────────────────


def _hash_to_percentile(request_id: str) -> float:
    """Deterministic 0..100 from a request_id so the same session
    consistently gets (or doesn't get) the shadow.  Hash-based
    splitting beats random() because it survives request retries +
    makes the per-session decision auditable."""
    h = hashlib.sha256(request_id.encode("utf-8", errors="replace")).hexdigest()
    # Take first 8 hex chars as a uint32 → percentile.
    return (int(h[:8], 16) % 10_000) / 100.0


def should_shadow_to_bitnet(
    request_id: str, *, traffic_pct: Optional[float] = None,
) -> bool:
    """True when this request should fork a BitNet shadow.

    Honours `settings.code_bitnet_planner_enabled` master gate AND
    `settings.code_bitnet_shadow_traffic_pct` (default 10).  Pass
    `traffic_pct` to override for tests.
    """
    if traffic_pct is None:
        try:
            from ..config.settings import settings  # noqa: PLC0415
            if not bool(getattr(settings, "code_bitnet_planner_enabled", False)):
                return False
            traffic_pct = float(getattr(settings, "code_bitnet_shadow_traffic_pct", 10.0))
        except Exception:
            return False
    traffic_pct = max(0.0, min(100.0, float(traffic_pct)))
    if traffic_pct <= 0.0:
        return False
    if traffic_pct >= 100.0:
        return True
    return _hash_to_percentile(request_id) < traffic_pct


# ─── Outcome recording ─────────────────────────────────────────────


def _plan_hash(plan: Any) -> str:
    """Canonical hash of a plan structure.  Treats None / empty as
    zero-hash so a fallback-to-main case is distinguishable."""
    if plan is None:
        return ""
    import json  # noqa: PLC0415
    try:
        canonical = json.dumps(plan, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = repr(plan)
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()[:16]


def record_shadow_outcome(
    request_id: str,
    main_plan: Any,
    shadow_plan: Any,
    *,
    latency_ms: float,
    timed_out: bool = False,
    fell_back: bool = False,
) -> ShadowSample:
    """Append a sample to the in-process ring buffer.  Caller-owned;
    no I/O hits the database (operator collects via /api/admin/llm)."""
    main_hash = _plan_hash(main_plan)
    shadow_hash = _plan_hash(shadow_plan)
    sample = ShadowSample(
        request_id=request_id,
        main_plan_hash=main_hash,
        shadow_plan_hash=shadow_hash,
        latency_ms=float(latency_ms),
        timed_out=bool(timed_out),
        fell_back=bool(fell_back),
        agreement=bool(main_hash and shadow_hash and main_hash == shadow_hash),
        timestamp=time.time(),
    )
    _SAMPLES.append(sample)
    return sample


# ─── Stats accessors ───────────────────────────────────────────────


def _percentile(values, pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(pct / 100.0 * (len(s) - 1)))))
    return s[k]


def get_shadow_stats(window: int = 200) -> Dict[str, Any]:
    """Return rolling-window stats over the last `window` samples.
    Plan-agent's promotion criteria (≥85% agreement, p95 ≤6s) are
    read directly off this payload."""
    recent = list(_SAMPLES)[-window:]
    total = len(recent)
    if total == 0:
        return {
            "samples": 0,
            "agreement_rate": None,
            "fallback_rate": None,
            "timeout_rate": None,
            "p50_ms": None,
            "p95_ms": None,
            "promotion_eligible": False,
        }
    latencies = [s.latency_ms for s in recent if not s.timed_out]
    agreed = sum(1 for s in recent if s.agreement)
    fell_back = sum(1 for s in recent if s.fell_back)
    timed_out = sum(1 for s in recent if s.timed_out)
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    agreement_rate = agreed / total if total else None
    return {
        "samples": total,
        "agreement_rate": round(agreement_rate, 4) if agreement_rate is not None else None,
        "fallback_rate": round(fell_back / total, 4) if total else None,
        "timeout_rate": round(timed_out / total, 4) if total else None,
        "p50_ms": round(p50, 2) if p50 is not None else None,
        "p95_ms": round(p95, 2) if p95 is not None else None,
        # Plan-agent v20.0.0 gate condition (ii): agreement ≥0.85
        # AND p95 ≤6s on a held-out 200-task slice.
        "promotion_eligible": (
            agreement_rate is not None
            and agreement_rate >= 0.85
            and p95 is not None
            and p95 <= 6000.0
            and total >= 200
        ),
    }


def reset_stats() -> None:
    """Clear the in-process ring buffer.  Used by tests + by the
    /admin/llm "Reset shadow stats" button after operator decisions."""
    _SAMPLES.clear()


__all__ = [
    "ShadowSample",
    "should_shadow_to_bitnet",
    "record_shadow_outcome",
    "get_shadow_stats",
    "reset_stats",
    "_hash_to_percentile",   # exposed for deterministic tests
    "_plan_hash",
]
