"""
Cycle H Phase A.3 — verifier-derived reward signal for GRPO.

Converts AMOR's existing verifier stack outputs (Hypothesis property
tests, mutmut surviving mutants, branch coverage, pylint/mypy/bandit
errors, critic verdict) into a SCALAR reward in [0.0, 1.0] suitable
for TRL's GRPOTrainer.

Why a separate module
---------------------
The existing `engine.py:_score_candidate()` (line 1334) returns a
0-100 score with 4-slot breakdown for the Reflexion threshold check.
That number is well-tuned for "should we re-generate?" decisions but
is NOT the right shape for GRPO:

  * GRPO wants [0.0, 1.0] floats with smooth gradients
  * GRPO benefits from MORE fine-grained signal (per-verifier weights)
  * GRPO uses the difference between chosen + rejected, so absolute
    100-pt scaling matters less than RELATIVE ordering

This module re-uses the SAME verifier outputs (zero new infrastructure)
but produces a different shape.  It does NOT replace `_score_candidate`
— that still gates Reflexion.  It augments preference pairs with a
reward field that the GRPO trainer reads.

Plan-agent caveats (acknowledged in docs/v18_2_release_notes.md)
---------------------------------------------------------------
* Verifier outputs are NON-DIFFERENTIABLE — that's FINE for GRPO
  (uses scalar reward; no gradient through the verifier) but blocks
  B2 Titans plug-in which needs a differentiable surrogate.  We do
  NOT try to smooth-relax here; that's a B2-scope concern.
* TRL GRPOTrainer API drifted between 0.14 and 0.18.  The reward
  function signature is `(completions: list[list[dict]], **kwargs)
  -> list[float]` in 0.18+.  This module's `compute_reward_scalar()`
  produces a list-element-shaped float; the per-batch wrapper at the
  GRPOTrainer integration point converts lists.

Reward signal composition
-------------------------
Same 4 slots as `_score_candidate` but normalised to [0, 1]:

  * execution_passed (35%)     — 1.0 if exit_code==0 + not timed out
  * test_execution_passed (25%) — 1.0 if pytest passed
  * static_clean (15%)         — 1.0 - (errors / max_errors)  capped
  * critic_score (25%)         — critic's 0-100 / 100.0

Bonus / penalty (capped at +0.10 / -0.15):
  * property_test_present:      +0.03
  * branch_coverage_ratio:      +0.05 * (ratio - 0.80) if ratio > 0.80
  * mutmut_score:               +0.05 * mutation_score
  * surviving_mutants:          -0.05 * min(survived / 10, 1.0)
  * missed_branches > 5:        -0.05

The hard floors (0.0) and ceiling (1.0) prevent any single signal
from dominating.  Mean reward should land in [0.4, 0.8] for the
typical Sprint-0 prompt; a perfect-pass deliverable scores 0.95-1.00,
a hard-fail (no execution, no tests) scores 0.0-0.15.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# ─── Weights table (Plan-agent locked) ─────────────────────────────


# Mirrors `engine.py:_score_candidate` 4-slot breakdown normalised
# to [0, 1].  Re-tuning these would invalidate any GRPO checkpoint
# trained against this signal — bump a config-version field below
# if you ever do, so old checkpoints can be detected + retired.
WEIGHTS_VERSION = "v1.0"

WEIGHTS: Dict[str, float] = {
    "execution_passed":   0.35,
    "test_execution_passed": 0.25,
    "static_clean":       0.15,
    "critic_score":       0.25,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "WEIGHTS must sum to 1.0"

BONUS_PROPERTY_TESTS = 0.03
BONUS_BRANCH_COVERAGE_PER_POINT = 0.05    # × (ratio - 0.80) when > 0.80
BONUS_MUTATION_SCORE = 0.05               # × mutation_score
PENALTY_SURVIVING_MUTANTS = 0.05          # × min(survived / 10, 1.0)
PENALTY_MISSED_BRANCHES_OVER_5 = 0.05
BONUS_CAP = 0.10
PENALTY_CAP = 0.15


# ─── Single-output reward computation ──────────────────────────────


@dataclass
class VerifierObservation:
    """One agent-output evaluation snapshot.  Built by the eval
    harness or read from preference_pairs JSONL annotations."""
    # Hard signals (all default to neutral/missing)
    execution_exit_code: Optional[int] = None    # 0 = pass, !=0 = fail
    execution_timed_out: bool = False
    test_exit_code: Optional[int] = None
    test_skipped: bool = False
    static_error_count: int = 0
    static_warning_count: int = 0
    critic_score: Optional[float] = None         # 0-100 from critic
    # Soft signals
    property_tests_present: bool = False
    branch_coverage_ratio: Optional[float] = None
    line_coverage_ratio: Optional[float] = None
    missed_branches: int = 0
    mutation_score: Optional[float] = None       # killed / total in [0, 1]
    surviving_mutants: int = 0

    @classmethod
    def from_session(cls, session: Dict[str, Any]) -> "VerifierObservation":
        """Build from an `engine.snapshot()` dict.  Tolerates missing
        keys (engine's pipeline often skips phases for non-codegen
        tasks)."""
        exec_results = session.get("execution_results") or []
        last_exec = exec_results[-1] if exec_results else {}
        test_result = session.get("test_execution_result") or {}
        static = session.get("static_analysis") or {}
        review = session.get("review") or {}
        coverage_report = session.get("coverage_report") or {}
        mutation_result = session.get("mutation_result") or {}
        breakdown = (
            (session.get("score") or {}).get("breakdown")
            or session.get("breakdown")
            or {}
        )
        # severity_counts is the dict from StaticAnalysisResult.severity_counts()
        severity_counts = static.get("severity_counts") or {}
        return cls(
            execution_exit_code=last_exec.get("exit_code"),
            execution_timed_out=bool(last_exec.get("timed_out")),
            test_exit_code=test_result.get("exit_code"),
            test_skipped=bool(test_result.get("skipped")),
            static_error_count=int(severity_counts.get("error", 0)),
            static_warning_count=int(severity_counts.get("warning", 0)),
            critic_score=(
                float(review.get("score"))
                if isinstance(review.get("score"), (int, float))
                else None
            ),
            property_tests_present=bool(
                breakdown.get("property_tests")
                or (session.get("test_metadata") or {}).get("property_tests_present"),
            ),
            branch_coverage_ratio=coverage_report.get("branch_coverage_ratio"),
            line_coverage_ratio=coverage_report.get("line_coverage_ratio"),
            missed_branches=len(coverage_report.get("missed_branches") or []),
            mutation_score=(
                float(mutation_result.get("score"))
                if isinstance(mutation_result.get("score"), (int, float))
                else None
            ),
            surviving_mutants=int(mutation_result.get("survived", 0)),
        )


def _slot_score_execution(obs: VerifierObservation) -> float:
    """Plot: pass=1.0, timeout=0.0, fail=0.0, no-execution=0.5
    (neutral — the deliverable just doesn't have an execution
    phase, like a docs/explanation task)."""
    if obs.execution_exit_code is None:
        return 0.5
    if obs.execution_timed_out:
        return 0.0
    return 1.0 if obs.execution_exit_code == 0 else 0.0


def _slot_score_test(obs: VerifierObservation) -> float:
    """Same posture as execution slot."""
    if obs.test_skipped:
        return 0.5
    if obs.test_exit_code is None:
        return 0.5
    return 1.0 if obs.test_exit_code == 0 else 0.0


def _slot_score_static(obs: VerifierObservation) -> float:
    """1.0 when zero errors; degrades by 0.1 per error capped at 1.0
    decrement.  Warnings count as 1/5 of an error."""
    err_units = obs.static_error_count + (obs.static_warning_count / 5.0)
    return max(0.0, 1.0 - 0.1 * err_units)


def _slot_score_critic(obs: VerifierObservation) -> float:
    """Map critic's 0-100 to 0.0-1.0.  Missing critic → 0.5 neutral."""
    if obs.critic_score is None:
        return 0.5
    return max(0.0, min(1.0, obs.critic_score / 100.0))


def _bonus(obs: VerifierObservation) -> float:
    """Soft positive signals.  Capped at BONUS_CAP."""
    total = 0.0
    if obs.property_tests_present:
        total += BONUS_PROPERTY_TESTS
    if obs.branch_coverage_ratio is not None and obs.branch_coverage_ratio > 0.80:
        total += BONUS_BRANCH_COVERAGE_PER_POINT * (obs.branch_coverage_ratio - 0.80)
    if obs.mutation_score is not None:
        total += BONUS_MUTATION_SCORE * max(0.0, min(1.0, obs.mutation_score))
    return min(total, BONUS_CAP)


def _penalty(obs: VerifierObservation) -> float:
    """Soft negative signals.  Capped at PENALTY_CAP."""
    total = 0.0
    if obs.surviving_mutants > 0:
        total += PENALTY_SURVIVING_MUTANTS * min(obs.surviving_mutants / 10.0, 1.0)
    if obs.missed_branches > 5:
        total += PENALTY_MISSED_BRANCHES_OVER_5
    return min(total, PENALTY_CAP)


def compute_reward_scalar(obs: VerifierObservation) -> float:
    """The top-level reward function.  Returns [0.0, 1.0]."""
    base = (
        WEIGHTS["execution_passed"]      * _slot_score_execution(obs)
        + WEIGHTS["test_execution_passed"] * _slot_score_test(obs)
        + WEIGHTS["static_clean"]        * _slot_score_static(obs)
        + WEIGHTS["critic_score"]        * _slot_score_critic(obs)
    )
    bonus = _bonus(obs)
    penalty = _penalty(obs)
    reward = base + bonus - penalty
    return max(0.0, min(1.0, reward))


def compute_reward_breakdown(obs: VerifierObservation) -> Dict[str, float]:
    """Diagnostic breakdown (sums to within 1e-9 of compute_reward_scalar)."""
    return {
        "weights_version": WEIGHTS_VERSION,
        "execution": round(WEIGHTS["execution_passed"] * _slot_score_execution(obs), 4),
        "test": round(WEIGHTS["test_execution_passed"] * _slot_score_test(obs), 4),
        "static": round(WEIGHTS["static_clean"] * _slot_score_static(obs), 4),
        "critic": round(WEIGHTS["critic_score"] * _slot_score_critic(obs), 4),
        "bonus": round(_bonus(obs), 4),
        "penalty": round(-_penalty(obs), 4),
        "total": round(compute_reward_scalar(obs), 4),
    }


# ─── Pair annotation (preference_pairs JSONL extension) ────────────


@dataclass
class PreferencePairWithReward:
    """JSONL row shape extended with reward fields.  TRL ORPO ignores
    extra fields; GRPO consumes them."""
    prompt: str
    chosen: str
    rejected: str
    reward_chosen: Optional[float] = None
    reward_rejected: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "prompt": self.prompt,
            "chosen": self.chosen,
            "rejected": self.rejected,
        }
        if self.reward_chosen is not None:
            d["reward_chosen"] = round(self.reward_chosen, 4)
        if self.reward_rejected is not None:
            d["reward_rejected"] = round(self.reward_rejected, 4)
        if self.metadata:
            d["metadata"] = self.metadata
        return d


def annotate_pair_with_rewards(
    pair: Dict[str, Any],
    chosen_obs: Optional[VerifierObservation],
    rejected_obs: Optional[VerifierObservation],
) -> Dict[str, Any]:
    """Take an existing preference_pairs JSONL row + two verifier
    observations and return the row with `reward_chosen` +
    `reward_rejected` added.  Backwards-compat: existing fields
    untouched."""
    out = dict(pair)
    if chosen_obs is not None:
        out["reward_chosen"] = round(compute_reward_scalar(chosen_obs), 4)
    if rejected_obs is not None:
        out["reward_rejected"] = round(compute_reward_scalar(rejected_obs), 4)
    return out


# ─── JSONL batch annotation ────────────────────────────────────────


def annotate_jsonl_file(
    src_path: Path,
    dst_path: Path,
    *,
    observation_lookup: Optional[Dict[str, VerifierObservation]] = None,
) -> Dict[str, int]:
    """Walk a preference_pairs JSONL, attach reward fields where the
    `observation_lookup` (keyed on pair `id` or `hash`) has data.
    Pairs without observations pass through unchanged.

    Returns stats: {rows_total, rows_annotated_chosen,
    rows_annotated_rejected}.
    """
    obs_map: Dict[str, VerifierObservation] = observation_lookup or {}
    stats = {"rows_total": 0, "rows_annotated_chosen": 0, "rows_annotated_rejected": 0}
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with src_path.open("r", encoding="utf-8") as src, \
         dst_path.open("w", encoding="utf-8") as dst:
        for line in src:
            line = line.strip()
            if not line:
                continue
            try:
                pair = json.loads(line)
            except json.JSONDecodeError:
                continue
            stats["rows_total"] += 1
            pair_id = pair.get("id") or pair.get("hash") or ""
            chosen_obs = obs_map.get(f"{pair_id}:chosen")
            rejected_obs = obs_map.get(f"{pair_id}:rejected")
            annotated = annotate_pair_with_rewards(pair, chosen_obs, rejected_obs)
            if chosen_obs is not None:
                stats["rows_annotated_chosen"] += 1
            if rejected_obs is not None:
                stats["rows_annotated_rejected"] += 1
            dst.write(json.dumps(annotated, ensure_ascii=False) + "\n")
    return stats


__all__ = [
    "WEIGHTS_VERSION", "WEIGHTS",
    "VerifierObservation",
    "PreferencePairWithReward",
    "compute_reward_scalar",
    "compute_reward_breakdown",
    "annotate_pair_with_rewards",
    "annotate_jsonl_file",
]
