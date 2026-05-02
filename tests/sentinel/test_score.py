"""Unit tests for ``document_processor/sentinel/score.py``."""

from __future__ import annotations

import pytest

from document_processor.sentinel.models import Finding
from document_processor.sentinel.score import (
    annotate_cvss,
    apply_merge,
    bayesian_merge,
    compute_cvss,
    repo_risk_score,
    resolve_source_weight,
    severity_class_from_score,
    severity_histogram,
)


# ─── Source weight resolution ───────────────────────────────────────


def test_source_weight_tool_override_for_gitleaks():
    f = Finding(tool="gitleaks", source_kind="static_tool", source_weight=0.5)
    assert resolve_source_weight(f) >= 0.8


def test_source_weight_kind_default():
    f = Finding(tool="custom_tool", source_kind="redteam", source_weight=0.5)
    assert resolve_source_weight(f) >= 0.85


def test_source_weight_explicit_overrides_lookup():
    f = Finding(tool="bandit", source_weight=0.42)
    assert resolve_source_weight(f) == pytest.approx(0.42)


# ─── Bayesian merge ─────────────────────────────────────────────────


def test_merge_two_independent_sources_increases_confidence():
    a = Finding(
        tool="bandit",   source_kind="static_tool",
        file="x.py", line_start=10, cwe="CWE-89",
        confidence=0.6, source_weight=0.6,
    )
    b = Finding(
        tool="auditor",  source_kind="auditor",
        file="x.py", line_start=10, cwe="CWE-89",
        confidence=0.7, source_weight=0.75,
    )
    merged = bayesian_merge([a, b])
    score = merged[("x.py", 10, "CWE-89")]
    # Each source alone gives c*w; combining must exceed both.
    assert score.final > 0.7 * 0.75
    assert score.final > 0.6 * 0.6
    assert score.sources_count == 2


def test_merge_separate_keys_keep_independent_scores():
    a = Finding(tool="bandit", file="x.py", line_start=10, cwe="CWE-89", confidence=0.6)
    b = Finding(tool="bandit", file="y.py", line_start=10, cwe="CWE-89", confidence=0.6)
    merged = bayesian_merge([a, b])
    assert len(merged) == 2


def test_apply_merge_picks_highest_severity_representative():
    items = [
        Finding(tool="bandit", file="x.py", line_start=10, cwe="CWE-79",
                severity="low",  confidence=0.5),
        Finding(tool="auditor", file="x.py", line_start=10, cwe="CWE-79",
                severity="high", confidence=0.7),
    ]
    out = apply_merge(items)
    assert len(out) == 1
    rep = out[0]
    assert rep.severity == "high"
    assert rep.extra.get("merge", {}).get("sources_count") == 2


# ─── CVSS ───────────────────────────────────────────────────────────


def test_compute_cvss_uses_tool_score_when_present():
    f = Finding(tool="trivy", cwe="CWE-89", cvss_base_score=8.8,
                cvss_vector="AV:N/AC:L/...")
    score, vector = compute_cvss(f)
    assert score == 8.8


def test_compute_cvss_falls_back_to_cwe_prior():
    f = Finding(tool="bandit", cwe="CWE-89")  # no upstream score
    score, vector = compute_cvss(f)
    assert score >= 9.0
    assert "AV:N" in vector


def test_compute_cvss_zero_when_no_cwe():
    f = Finding(tool="bandit")
    score, _vec = compute_cvss(f)
    assert score == 0.0


def test_annotate_cvss_in_place():
    f = Finding(tool="bandit", cwe="CWE-78")
    annotate_cvss([f])
    assert f.cvss_base_score > 0
    assert f.cvss_vector


# ─── Severity ladder ────────────────────────────────────────────────


def test_severity_class_from_score():
    assert severity_class_from_score(0.0) == "info"
    assert severity_class_from_score(0.5) == "low"
    assert severity_class_from_score(5.5) == "medium"
    assert severity_class_from_score(7.5) == "high"
    assert severity_class_from_score(9.5) == "critical"
    assert severity_class_from_score(99.0) == "critical"  # clamps


# ─── Histogram + repo risk ──────────────────────────────────────────


def test_severity_histogram():
    items = [
        Finding(tool="x", severity="high"),
        Finding(tool="y", severity="high"),
        Finding(tool="z", severity="low"),
    ]
    h = severity_histogram(items)
    assert h["high"] == 2
    assert h["low"] == 1
    assert h["medium"] == 0


def test_repo_risk_score_zero_when_no_findings():
    assert repo_risk_score([]) == 0.0


def test_repo_risk_score_critical_in_single_file_high():
    f = Finding(tool="bandit", severity="critical", confidence=1.0)
    score = repo_risk_score([f], file_count=1)
    assert score >= 9.0


def test_repo_risk_score_diluted_in_large_repo():
    f = Finding(tool="bandit", severity="medium", confidence=0.4)
    small = repo_risk_score([f], file_count=1)
    big = repo_risk_score([f], file_count=200)
    assert big < small  # same finding, more files → less risk per file
