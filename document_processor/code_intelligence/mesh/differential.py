"""
DifferentialTester — output-hash consensus across tournament candidates.

When K candidates each ran the same input through the sandbox, we
hash their outputs and check whether they agree. Disagreement is a
strong signal:

  • If 3 of 4 candidates produced output hash `abc...` and 1 produced
    `def...`, the minority candidate is almost certainly wrong (at
    least relative to the consensus). We surface it as "off-consensus"
    so the meta-arbiter can cite the disagreement.

  • If exactly 2 of 4 agree on each side, there's no clear consensus —
    every candidate is treated as equally likely (no penalty applied).

  • A single-survivor bracket has trivial consensus by definition.

The hash is sha256 of the candidate's stdout (or the BENCH_RESULT
output digest the benchmarker emits when wrapping the candidate).
We use the first 16 chars of the hex digest — enough to avoid
collision in a 5-candidate field while keeping the bundle JSON
compact.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DifferentialResult:
    """Output of running differential consensus across N candidates."""

    candidate_hashes: dict[str, str] = field(default_factory=dict)
    """Map candidate label → output hash (or empty when candidate
    didn't produce a hashable output)."""

    consensus_hash: str | None = None
    """The majority hash when one exists; None when 2-2 split or
    every candidate has a different hash."""

    majority_labels: list[str] = field(default_factory=list)
    """Labels that landed on the consensus hash."""

    minority_labels: list[str] = field(default_factory=list)
    """Labels that did NOT land on the consensus hash. Never includes
    candidates with no hash (those are abstainers, not minorities)."""

    abstainers: list[str] = field(default_factory=list)
    """Labels that produced no hashable output (sandbox error, etc.).
    Excluded from consensus calculation."""

    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_hashes": dict(self.candidate_hashes),
            "consensus_hash": self.consensus_hash,
            "majority_labels": list(self.majority_labels),
            "minority_labels": list(self.minority_labels),
            "abstainers": list(self.abstainers),
            "findings": list(self.findings),
            "has_consensus": self.consensus_hash is not None,
        }


def hash_output(text: str | bytes | None) -> str:
    """Stable 16-char sha256 prefix for any candidate output. Empty
    input maps to a sentinel so two empty outputs collide deterministic-
    ally (which is the right behaviour — both candidates "agreed"
    on producing nothing)."""
    if text is None:
        return ""
    if isinstance(text, str):
        b = text.encode("utf-8", errors="replace")
    else:
        b = bytes(text)
    if not b:
        return "empty"
    return hashlib.sha256(b).hexdigest()[:16]


class DifferentialTester:
    """Stateless utility — one method, one DifferentialResult.

    Caller passes a dict of ``{candidate_label: output_text}``. Any
    label whose output is None / empty-after-stripping is treated as
    an abstainer (no opinion contributed).
    """

    @staticmethod
    def compare(outputs: dict[str, str | bytes | None]) -> DifferentialResult:
        result = DifferentialResult()

        if not outputs:
            result.findings.append("no candidates supplied to differential")
            return result

        for label, raw in outputs.items():
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                result.abstainers.append(label)
                continue
            result.candidate_hashes[label] = hash_output(raw)

        if not result.candidate_hashes:
            result.findings.append(
                "every candidate abstained — no differential possible"
            )
            return result

        if len(result.candidate_hashes) == 1:
            # Trivial: one survivor, it IS the consensus by itself.
            label, h = next(iter(result.candidate_hashes.items()))
            result.consensus_hash = h
            result.majority_labels = [label]
            return result

        counter = Counter(result.candidate_hashes.values())
        most_common = counter.most_common()
        top_hash, top_count = most_common[0]
        # Strict majority test — count must exceed half the participants
        # (NOT including abstainers).
        n = sum(counter.values())
        if top_count * 2 <= n:
            # No strict majority (2-2 split, or every hash unique). The
            # tournament treats this as "no clear consensus"; each
            # candidate stays in the running on equal footing.
            result.findings.append(
                f"no consensus — {n} candidates, top hash held by "
                f"{top_count}"
            )
            return result

        result.consensus_hash = top_hash
        for label, h in result.candidate_hashes.items():
            if h == top_hash:
                result.majority_labels.append(label)
            else:
                result.minority_labels.append(label)

        if result.minority_labels:
            result.findings.append(
                f"off-consensus: {', '.join(sorted(result.minority_labels))}"
            )
        return result
