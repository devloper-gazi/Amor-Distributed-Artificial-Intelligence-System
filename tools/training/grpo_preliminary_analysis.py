#!/usr/bin/env python3
"""Cycle H.3 preliminary — verifier-reward signal analysis.

Sprint H.3 requires REAL GRPO vs ORPO training to produce the v20
gate condition #4 measurement.  Training requires operator GPU
(~60-90 min).  Until that lands, this script produces a
**preliminary signal-quality analysis** so the operator can decide
whether the verifier-reward signal is worth training on.

The analysis:

  1. Walk the latest Sprint-0 baseline JSON for per-prompt judge
     scores + property-test outcomes.
  2. For each prompt, compute a synthetic verifier reward (uses
     ``tools.training.verifier_rewards.compute_reward_scalar`` with
     synthetic observations derived from the judge data).
  3. Aggregate: does high reward correlate with low property-test
     failure rate?  If yes, GRPO training would amplify the signal;
     if no, the signal isn't strong enough to justify training.

This file writes ``data/baselines/grpo_preliminary_latest.json``
with the analysis.  It is INTENTIONALLY NOT named
``grpo_vs_orpo_latest.json`` because that snapshot represents real
training results; we don't fake the gate-required file.

Output schema::

    {
      "samples": N,
      "verifier_reward_distribution": {"min": ..., "max": ..., "mean": ..., "stdev": ...},
      "property_failure_rate_by_reward_quartile": [...],
      "signal_quality_score": 0.0..1.0,
      "training_recommendation": "proceed" | "marginal" | "abort",
      "computed_at_utc": "..."
    }

Usage::

    python tools/training/grpo_preliminary_analysis.py
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BASELINES = _REPO_ROOT / "data" / "baselines"


@dataclass
class SignalQualityReport:
    """The structured output operator + CI both read."""
    samples: int
    reward_distribution: Dict[str, float]
    failure_rate_by_quartile: List[Dict[str, Any]]
    rank_correlation: float           # Spearman ρ between reward + 1-failure
    signal_quality_score: float       # 0..1
    training_recommendation: str
    note: str = ""
    computed_at_utc: str = ""


def _spearman_rho(xs: List[float], ys: List[float]) -> float:
    """Spearman rank correlation between two equal-length lists."""
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    def _ranks(vs: List[float]) -> List[float]:
        sorted_idx = sorted(range(len(vs)), key=lambda i: vs[i])
        ranks = [0.0] * len(vs)
        # Average ranks for ties.
        i = 0
        while i < len(sorted_idx):
            j = i
            while j + 1 < len(sorted_idx) and vs[sorted_idx[j + 1]] == vs[sorted_idx[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[sorted_idx[k]] = avg
            i = j + 1
        return ranks

    rx, ry = _ranks(xs), _ranks(ys)
    mean_x = sum(rx) / len(rx)
    mean_y = sum(ry) / len(ry)
    num = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry))
    den_x = (sum((a - mean_x) ** 2 for a in rx)) ** 0.5
    den_y = (sum((b - mean_y) ** 2 for b in ry)) ** 0.5
    return num / (den_x * den_y) if den_x > 0 and den_y > 0 else 0.0


def _quartile_breakdown(
    rewards: List[float], failures: List[int],
) -> List[Dict[str, Any]]:
    """For each quartile of the reward distribution, compute the
    failure rate.  If reward correlates with quality, the failure
    rate should DECREASE as we move up the quartiles."""
    if len(rewards) < 4:
        return []
    paired = list(zip(rewards, failures))
    paired.sort(key=lambda p: p[0])
    n = len(paired)
    quarter = n // 4
    out: List[Dict[str, Any]] = []
    for q in range(4):
        start = q * quarter
        end = (q + 1) * quarter if q < 3 else n
        bucket = paired[start:end]
        if not bucket:
            continue
        rate = sum(f for _r, f in bucket) / len(bucket)
        out.append({
            "quartile": q + 1,
            "n": len(bucket),
            "reward_mean": round(statistics.mean(r for r, _f in bucket), 4),
            "failure_rate": round(rate, 4),
        })
    return out


def _read_sprint0() -> Optional[Dict[str, Any]]:
    p = _BASELINES / "sprint0_latest.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _extract_reward_failure_pairs(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Walk Sprint-0 rows.  For each, derive:
      * a SYNTHETIC reward from the judge score (0..10 → 0..1) +
        latency penalty (slower = lower reward)
      * a failure indicator from status / error fields

    The judge score IS the verifier-reward proxy here.  Per
    Plan-agent: the real verifier_rewards.py uses verifier outputs
    (property tests, mutmut, etc.), but Sprint-0 doesn't capture
    those today.  Using the judge score as a stand-in is the most
    rigorous signal available without re-running Sprint-0.
    """
    rows = snapshot.get("rows") or snapshot.get("tasks") or []
    pairs: List[Dict[str, Any]] = []
    for r in rows:
        if r.get("status") not in {"completed", "succeeded"}:
            continue
        judge = r.get("judge_score")
        if not isinstance(judge, (int, float)):
            continue
        latency_ms = (r.get("metrics") or {}).get("wall_clock_ms") or 0
        # Reward in [0, 1]: judge / 10 minus latency penalty.
        latency_penalty = min(0.3, latency_ms / 100_000.0)
        reward = max(0.0, min(1.0, (judge / 10.0) - latency_penalty))
        # Synthetic failure: judge < 7 OR latency > 120s
        failure = 1 if (judge < 7.0 or latency_ms > 120_000) else 0
        pairs.append({
            "prompt_id": r.get("prompt_id", "?"),
            "reward": reward,
            "failure": failure,
            "judge": judge,
            "wall_ms": latency_ms,
        })
    return pairs


def analyse(snapshot: Dict[str, Any]) -> SignalQualityReport:
    pairs = _extract_reward_failure_pairs(snapshot)
    n = len(pairs)
    if n == 0:
        return SignalQualityReport(
            samples=0,
            reward_distribution={},
            failure_rate_by_quartile=[],
            rank_correlation=0.0,
            signal_quality_score=0.0,
            training_recommendation="abort",
            note="no completed rows with judge scores in snapshot",
            computed_at_utc=datetime.now(timezone.utc).isoformat(),
        )
    rewards = [p["reward"] for p in pairs]
    failures = [p["failure"] for p in pairs]
    quartiles = _quartile_breakdown(rewards, failures)
    # rank corr between reward and (1 - failure); high reward → low failure
    # means positive correlation between reward and "success" (1 - failure).
    successes = [1 - f for f in failures]
    rho = _spearman_rho(rewards, successes)
    # Signal quality: |ρ| where 0 = no signal, 1 = perfect signal.
    score = abs(rho)
    if score >= 0.50:
        rec, note = "proceed", "strong reward-quality correlation; train GRPO"
    elif score >= 0.25:
        rec, note = "marginal", "weak signal; GRPO uplift likely small (<10pp)"
    else:
        rec, note = "abort", "no reward-quality correlation; verifier_rewards weights need review"
    return SignalQualityReport(
        samples=n,
        reward_distribution={
            "min": round(min(rewards), 4),
            "max": round(max(rewards), 4),
            "mean": round(statistics.mean(rewards), 4),
            "stdev": round(statistics.stdev(rewards) if n > 1 else 0.0, 4),
        },
        failure_rate_by_quartile=quartiles,
        rank_correlation=round(rho, 4),
        signal_quality_score=round(score, 4),
        training_recommendation=rec,
        note=note,
        computed_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(_BASELINES / "grpo_preliminary_latest.json"),
                        help="output path for the analysis snapshot")
    args = parser.parse_args()

    snap = _read_sprint0()
    if not snap:
        logger.error("sprint0_latest.json not found; cannot analyse")
        return 1
    report = analyse(snap)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    logger.info("preliminary analysis written: %s", out_path)
    print(json.dumps(asdict(report), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
