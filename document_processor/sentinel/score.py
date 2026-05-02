"""
Sentinel — confidence merging + CVSS scoring.

Bayesian merge per ``(file, line, cwe)`` triple:

    final_confidence = 1 − ∏(1 − cᵢ * wᵢ)

where ``cᵢ`` is the per-source confidence and ``wᵢ`` is the source
weight from ``data/source_weights.json``.  Sources are assumed
independent (rough but standard simplification).

CVSS 3.1 base score is taken from the upstream tool when supplied
(Trivy is the only tool that emits it natively); otherwise we fall
back to the bundled ``cwe_cvss_map.json`` which provides a
prior-distribution score for each CWE.

Repo risk score: Σ (severity_weight * confidence) / file_count,
clamped to [0, 10].

License: MIT.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import (
    ConfidenceScore,
    Finding,
    SeverityLevel,
    severity_rank,
)
from .rag import load_cwe_cvss_map, load_source_weights


# Severity → numeric weight (0..1) used for repo risk.
_SEVERITY_WEIGHT: dict[SeverityLevel, float] = {
    "info": 0.1,
    "low": 0.3,
    "medium": 0.5,
    "high": 0.8,
    "critical": 1.0,
}


# ─────────────────────────────────────────────────────────────────────
# Source weight resolution
# ─────────────────────────────────────────────────────────────────────


def resolve_source_weight(finding: Finding) -> float:
    """Pick the right Bayesian source weight for a Finding.

    Priority:
      1. Explicit ``finding.source_weight`` (if non-default).
      2. Tool-specific override from ``source_weights.json``.
      3. Source-kind default from same file.
      4. Hard-coded fallback 0.5.
    """
    if finding.source_weight not in (0.0, 0.5):
        # Non-default → trust the producer.
        return finding.source_weight
    config = load_source_weights() or {}
    overrides = (config.get("tool_overrides") or {})
    if finding.tool in overrides:
        return float(overrides[finding.tool])
    weights = config.get("weights") or {}
    if finding.source_kind in weights:
        return float(weights[finding.source_kind])
    return 0.5


# ─────────────────────────────────────────────────────────────────────
# Bayesian merge
# ─────────────────────────────────────────────────────────────────────


def bayesian_merge(findings: Iterable[Finding]) -> dict[tuple[str, int, str], ConfidenceScore]:
    """Merge confidences across sources for each ``(file, line, cwe)``
    triple.  Returns a dict keyed by ``Finding.merge_key()``.
    """
    grouped: dict[tuple[str, int, str], list[Finding]] = defaultdict(list)
    for f in findings:
        grouped[f.merge_key()].append(f)

    merged: dict[tuple[str, int, str], ConfidenceScore] = {}
    for key, items in grouped.items():
        product = 1.0
        breakdown: dict[str, float] = {}
        for f in items:
            w = resolve_source_weight(f)
            c = max(0.0, min(1.0, float(f.confidence)))
            term = max(0.0, min(1.0, c * w))
            product *= (1.0 - term)
            # Aggregate per source-kind so the breakdown is short.
            sk = f.source_kind
            breakdown[sk] = max(breakdown.get(sk, 0.0), c)
        merged[key] = ConfidenceScore(
            final=round(1.0 - product, 4),
            breakdown=breakdown,
            sources_count=len(items),
        )
    return merged


def apply_merge(findings: list[Finding]) -> list[Finding]:
    """Annotate findings with merged confidence + dedup to one
    representative Finding per ``(file, line, cwe)``.

    The representative is the highest-severity entry in the group;
    ties broken by highest confidence.  The merged confidence + the
    `extra["merge"]` payload preserve the contribution of the other
    sources.
    """
    grouped: dict[tuple[str, int, str], list[Finding]] = defaultdict(list)
    for f in findings:
        grouped[f.merge_key()].append(f)
    merged_scores = bayesian_merge(findings)

    out: list[Finding] = []
    for key, items in grouped.items():
        items.sort(
            key=lambda f: (severity_rank(f.severity), f.confidence),
            reverse=True,
        )
        rep = items[0]
        score = merged_scores.get(key)
        if score is not None:
            rep.confidence = score.final
            rep.extra = dict(rep.extra or {})
            rep.extra["merge"] = {
                "sources_count": score.sources_count,
                "breakdown": score.breakdown,
                "tools": sorted({f.tool for f in items}),
            }
        out.append(rep)
    return out


# ─────────────────────────────────────────────────────────────────────
# CVSS scoring
# ─────────────────────────────────────────────────────────────────────


def compute_cvss(finding: Finding) -> tuple[float, str]:
    """Return ``(base_score, vector_string)`` for a Finding.

    Trusts the upstream tool's CVSS when present, otherwise falls
    back to the bundled CWE → CVSS prior.
    """
    if finding.cvss_base_score and finding.cvss_base_score > 0:
        return float(finding.cvss_base_score), finding.cvss_vector or ""
    if finding.cwe:
        prior = load_cwe_cvss_map().get(finding.cwe) or {}
        return float(prior.get("score") or 0.0), str(prior.get("vector") or "")
    return 0.0, ""


def annotate_cvss(findings: Iterable[Finding]) -> list[Finding]:
    out: list[Finding] = []
    for f in findings:
        score, vector = compute_cvss(f)
        if score > f.cvss_base_score:
            f.cvss_base_score = score
        if vector and not f.cvss_vector:
            f.cvss_vector = vector
        out.append(f)
    return out


# ─────────────────────────────────────────────────────────────────────
# Severity classification
# ─────────────────────────────────────────────────────────────────────


def severity_class_from_score(score: float) -> SeverityLevel:
    """Map a CVSS-style 0..10 score back to a Sentinel severity bucket."""
    s = max(0.0, min(10.0, float(score or 0.0)))
    if s >= 9.0:
        return "critical"
    if s >= 7.0:
        return "high"
    if s >= 4.0:
        return "medium"
    if s >= 0.1:
        return "low"
    return "info"


# ─────────────────────────────────────────────────────────────────────
# Repo-level aggregate score
# ─────────────────────────────────────────────────────────────────────


def severity_histogram(findings: Iterable[Finding]) -> dict[str, int]:
    hist: dict[str, int] = {"info": 0, "low": 0, "medium": 0, "high": 0, "critical": 0}
    for f in findings:
        hist[f.severity] = hist.get(f.severity, 0) + 1
    return hist


def repo_risk_score(
    findings: Iterable[Finding],
    *,
    file_count: int = 1,
) -> float:
    """0..10 aggregate score.  Higher = more risk."""
    findings = list(findings)
    if not findings:
        return 0.0
    file_count = max(1, int(file_count or 1))
    weighted = 0.0
    for f in findings:
        w = _SEVERITY_WEIGHT.get(f.severity, 0.3)
        weighted += w * max(0.0, min(1.0, f.confidence))
    raw = weighted / file_count
    # Scale: a single critical-confidence-1.0 finding in a 1-file
    # scan should land near 10; 1 medium-confidence-0.5 in a 50-file
    # repo ≈ 0.005 → near 0.
    score = min(10.0, raw * 10.0)
    return round(score, 3)


__all__ = [
    "annotate_cvss",
    "apply_merge",
    "bayesian_merge",
    "compute_cvss",
    "repo_risk_score",
    "resolve_source_weight",
    "severity_class_from_score",
    "severity_histogram",
]
