"""
MeshAggregator — merges specialist outputs into a single ranked
reasoning result the QuickCode engine can consume.

Strategy
--------
1. Each specialist emits 0..3 alternatives (each with a 4-axis score).
2. Identical alternatives across specialists are collapsed into one,
   averaging their scores axis-by-axis (so consensus boosts a winner).
3. Where specialists disagree on scores for the same alternative, the
   *specialist's specialty axis* gets extra weight — math reasoner's
   `math_soundness` is trusted more than the general reasoner's, etc.
4. Composite is recomputed via the QuickCode formula (0.30 / 0.30 /
   0.20 / 0.20). Aggregator picks the highest.
5. Aggregator also surfaces *per-specialist* picks so the UI can
   show "math chose A, perf chose B, edge chose C — aggregator
   picks A".

The output is a ``QuickCodeReasoning`` (re-using the existing model)
plus an envelope of metadata so the engine can record per-session
self-evolution metrics.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ...quick_code.models import (
    COMPOSITE_WEIGHTS,
    QuickCodeAlternative,
    QuickCodeReasoning,
)
from .specialists import SpecialistOutput, SpecialistRoleId

logger = logging.getLogger(__name__)


# Per-specialist axis weights for the consensus blend. The diagonal
# (axis the specialist owns) gets a 1.5× weight; off-diagonal axes
# get 1.0×. Total weight per alternative-score is normalized.
_SPECIALTY_AXIS: dict[SpecialistRoleId, str] = {
    "general":     "",  # no specialty bias
    "math":        "math_soundness",
    "performance": "performance",
    "edge_case":   "edge_cases",
}


def _normalise_label(s: str) -> str:
    """Two specialists may both label the same approach 'A' — collapse
    by the *summary text* not the label."""
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


@dataclass
class AggregatedReasoning:
    """Output of the aggregator — a QuickCodeReasoning plus
    per-specialist visibility for the UI / metrics."""

    reasoning: QuickCodeReasoning
    per_specialist_picks: dict[str, str] = field(default_factory=dict)
    specialist_errors: dict[str, str] = field(default_factory=dict)
    specialist_alt_counts: dict[str, int] = field(default_factory=dict)
    consensus_count: int = 0  # how many alternatives ≥2 specialists agreed on
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reasoning": self.reasoning.to_dict(),
            "per_specialist_picks": dict(self.per_specialist_picks),
            "specialist_errors": dict(self.specialist_errors),
            "specialist_alt_counts": dict(self.specialist_alt_counts),
            "consensus_count": self.consensus_count,
            "findings": list(self.findings),
        }


class MeshAggregator:
    """Stateless merger; one instance per call."""

    def merge(
        self,
        outputs: list[SpecialistOutput],
    ) -> AggregatedReasoning:
        per_specialist_picks: dict[str, str] = {}
        specialist_errors: dict[str, str] = {}
        specialist_alt_counts: dict[str, int] = {}
        findings: list[str] = []

        # Bucket by normalised summary so duplicates collapse.
        # Each bucket: {summary_key: {"label_votes": {label: count},
        #                              "specialists": list of role,
        #                              "score_samples": list of {axis: weight}}}
        buckets: dict[str, dict[str, Any]] = {}

        for out in outputs:
            specialist_alt_counts[out.role] = len(out.alternatives())
            if out.error:
                specialist_errors[out.role] = out.error
                findings.append(
                    f"{out.role_label}: {out.error[:120]}"
                )
                continue

            # Per-specialist's own pick (advisory; aggregator may override).
            chosen_label = str(out.parsed.get("chosen") or "")[:8]
            if chosen_label:
                per_specialist_picks[out.role] = chosen_label

            for alt in out.alternatives():
                summary = str(alt.get("summary") or "")
                if not summary.strip():
                    continue
                key = _normalise_label(summary)[:200]
                if not key:
                    continue
                bucket = buckets.setdefault(key, {
                    "summary": summary,
                    "label_votes": {},
                    "specialists": [],
                    "score_samples": [],
                    "complexity_estimate_votes": {},
                    "perf_notes": [],
                    "edge_cases": [],
                })
                label = str(alt.get("label") or "")[:8] or "A"
                bucket["label_votes"][label] = bucket["label_votes"].get(label, 0) + 1
                bucket["specialists"].append(out.role)
                # Score sample weighted by specialty axis.
                scores = alt.get("scores") or {}
                if isinstance(scores, dict):
                    bucket["score_samples"].append({
                        "scores": {
                            k: self._clamp_score(scores.get(k))
                            for k in COMPOSITE_WEIGHTS.keys()
                        },
                        "specialist": out.role,
                    })
                ce = str(alt.get("complexity_estimate") or "")[:80]
                if ce:
                    bucket["complexity_estimate_votes"][ce] = (
                        bucket["complexity_estimate_votes"].get(ce, 0) + 1
                    )
                pn = str(alt.get("perf_notes") or "")[:400]
                if pn:
                    bucket["perf_notes"].append(pn)
                for ec in alt.get("edge_cases") or []:
                    if isinstance(ec, str) and ec.strip():
                        bucket["edge_cases"].append(ec.strip()[:200])

        if not buckets:
            # Every specialist failed → synthesise a single fallback.
            return AggregatedReasoning(
                reasoning=QuickCodeReasoning(
                    alternatives=[QuickCodeAlternative(
                        label="A",
                        summary="(mesh produced no usable alternatives — degraded to single-path)",
                        scores={k: 0.5 for k in COMPOSITE_WEIGHTS},
                    )],
                    chosen_label="A",
                    rationale="The mesh failed to return usable JSON from "
                              "any specialist. Engine fell back to a "
                              "single-path baseline.",
                    findings=["mesh produced no alternatives"]
                              + findings,
                ),
                per_specialist_picks=per_specialist_picks,
                specialist_errors=specialist_errors,
                specialist_alt_counts=specialist_alt_counts,
                findings=["all specialists failed"] + findings,
            )

        # Build aggregated alternatives.
        consensus_count = 0
        merged_alts: list[QuickCodeAlternative] = []
        # Stable-sort buckets by first appearance order — keeps output
        # diff-stable across runs when specialist counts are equal.
        for key, bucket in buckets.items():
            participants = list(set(bucket["specialists"]))
            if len(participants) >= 2:
                consensus_count += 1
            # Pick the most-voted label as the canonical one.
            label = max(
                bucket["label_votes"].items(),
                key=lambda kv: kv[1],
            )[0]
            scores = self._blend_scores(
                bucket["score_samples"], participants,
            )
            ce = ""
            if bucket["complexity_estimate_votes"]:
                ce = max(
                    bucket["complexity_estimate_votes"].items(),
                    key=lambda kv: kv[1],
                )[0]
            # Concatenate distinct edge_cases up to a sensible cap.
            unique_edges: list[str] = []
            seen: set[str] = set()
            for e in bucket["edge_cases"]:
                k = e.lower().strip()
                if k and k not in seen:
                    seen.add(k)
                    unique_edges.append(e)
                if len(unique_edges) >= 8:
                    break
            merged_alts.append(QuickCodeAlternative(
                label=label,
                summary=bucket["summary"][:400],
                scores=scores,
                complexity_estimate=ce,
                perf_notes=(bucket["perf_notes"][0] if bucket["perf_notes"] else "")[:400],
                edge_cases=unique_edges,
            ))

        # Re-label A/B/C/D after merging so the user sees a clean ladder.
        merged_alts.sort(key=lambda a: a.composite, reverse=True)
        for i, alt in enumerate(merged_alts):
            alt.label = chr(ord("A") + i)

        # Cap to top 4 — anything below that is noise.
        merged_alts = merged_alts[:4]
        chosen = merged_alts[0]

        # Compose rationale: list the top-2's distinguishing scores.
        rationale_parts = [
            f"Aggregated {len(buckets)} unique approach(es) from "
            f"{len(outputs)} specialist(s); "
            f"{consensus_count} reached ≥2-specialist consensus. "
            f"Chosen: {chosen.label} (composite {chosen.composite:.2f}).",
        ]
        if len(merged_alts) >= 2:
            runner_up = merged_alts[1]
            rationale_parts.append(
                f"Runner-up {runner_up.label} composite {runner_up.composite:.2f}: "
                f"{runner_up.summary[:140]}"
            )
        # Translate per-specialist picks into a transparency line.
        if per_specialist_picks:
            picks_str = ", ".join(
                f"{role}={lbl}"
                for role, lbl in sorted(per_specialist_picks.items())
            )
            rationale_parts.append(f"Per-specialist picks: {picks_str}.")
        rationale = " ".join(rationale_parts)

        # Findings about the mesh shape itself.
        if not consensus_count:
            findings.append("no alternative reached ≥2-specialist consensus")
        if specialist_errors:
            findings.append(
                f"{len(specialist_errors)} specialist(s) failed and "
                "did not contribute"
            )

        reasoning = QuickCodeReasoning(
            alternatives=merged_alts,
            chosen_label=chosen.label,
            rationale=rationale,
            findings=findings,
        )

        return AggregatedReasoning(
            reasoning=reasoning,
            per_specialist_picks=per_specialist_picks,
            specialist_errors=specialist_errors,
            specialist_alt_counts=specialist_alt_counts,
            consensus_count=consensus_count,
            findings=findings,
        )

    @staticmethod
    def _clamp_score(value: Any) -> float:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, f))

    @staticmethod
    def _blend_scores(
        samples: list[dict[str, Any]],
        participants: list[str],
    ) -> dict[str, float]:
        """Weighted average of scores across specialists.

        Each specialist contributes 1× weight on every axis except its
        specialty axis where it gets 1.5×. This means a math reasoner
        scoring 0.9 on math_soundness is trusted more than the general
        reasoner's 0.6 on the same axis without ignoring the latter
        entirely.
        """
        if not samples:
            return {k: 0.5 for k in COMPOSITE_WEIGHTS}
        sums: dict[str, float] = {k: 0.0 for k in COMPOSITE_WEIGHTS}
        wts:  dict[str, float] = {k: 0.0 for k in COMPOSITE_WEIGHTS}
        for s in samples:
            specialist: SpecialistRoleId = s.get("specialist") or "general"
            scores = s.get("scores") or {}
            specialty = _SPECIALTY_AXIS.get(specialist, "")
            for axis in COMPOSITE_WEIGHTS:
                w = 1.5 if axis == specialty else 1.0
                sums[axis] += w * float(scores.get(axis, 0.0) or 0.0)
                wts[axis] += w
        return {
            axis: round(sums[axis] / wts[axis], 4) if wts[axis] else 0.0
            for axis in COMPOSITE_WEIGHTS
        }
