"""Unit tests for ``document_processor/sentinel/models.py``."""

from __future__ import annotations

import pytest

from document_processor.sentinel.models import (
    AgentVerdict,
    ConfidenceScore,
    Finding,
    SentinelBundle,
    SentinelGate,
    SentinelRequest,
    coerce_scan_profile,
    coerce_severity,
    severity_rank,
)


# ─── Severity coercion ──────────────────────────────────────────────


def test_coerce_severity_canonical_passthrough():
    for s in ("info", "low", "medium", "high", "critical"):
        assert coerce_severity(s) == s


def test_coerce_severity_aliases():
    assert coerce_severity("warning") == "medium"
    assert coerce_severity("Warning") == "medium"
    assert coerce_severity("error") == "high"
    assert coerce_severity("blocker") == "critical"
    assert coerce_severity("trivial") == "info"


def test_coerce_severity_numeric():
    assert coerce_severity(0) == "info"
    assert coerce_severity(4) == "critical"
    assert coerce_severity(99) == "critical"  # clamps
    assert coerce_severity(-1) == "info"


def test_coerce_severity_unknown_falls_back():
    assert coerce_severity("nonsense") == "low"
    assert coerce_severity(None) == "low"
    assert coerce_severity("") == "low"


def test_severity_rank_ordering():
    assert severity_rank("info") < severity_rank("low") < severity_rank("medium")
    assert severity_rank("medium") < severity_rank("high") < severity_rank("critical")


# ─── Scan profile coercion ──────────────────────────────────────────


def test_coerce_scan_profile_known():
    for p in ("quick", "standard", "deep", "paranoid"):
        assert coerce_scan_profile(p) == p
        assert coerce_scan_profile(p.upper()) == p


def test_coerce_scan_profile_default_on_unknown():
    assert coerce_scan_profile("ultra") == "standard"
    assert coerce_scan_profile(None) == "standard"
    assert coerce_scan_profile("") == "standard"
    assert coerce_scan_profile("paranoid", default="quick") == "paranoid"


# ─── Finding ────────────────────────────────────────────────────────


def test_finding_minimal_construction():
    f = Finding(tool="bandit")
    assert f.tool == "bandit"
    assert f.severity == "low"
    assert 0.0 <= f.confidence <= 1.0
    assert f.fingerprint  # auto-computed


def test_finding_post_init_clamps_confidence():
    f = Finding(tool="bandit", confidence=2.0)
    assert f.confidence == 1.0
    f2 = Finding(tool="bandit", confidence=-0.5)
    assert f2.confidence == 0.0


def test_finding_fingerprint_stable():
    f1 = Finding(tool="bandit", file="a.py", line_start=10, cwe="CWE-89")
    f2 = Finding(tool="semgrep", file="a.py", line_start=10, cwe="CWE-89")
    # Same file/line/cwe → same fingerprint regardless of tool
    assert f1.fingerprint == f2.fingerprint


def test_finding_merge_key():
    f = Finding(tool="bandit", file="x.py", line_start=42, cwe="CWE-78")
    assert f.merge_key() == ("x.py", 42, "CWE-78")


def test_finding_round_trip_dict():
    f = Finding(
        tool="bandit",
        rule_id="B608",
        file="app/auth.py",
        line_start=45,
        line_end=48,
        raw_message="Possible SQL injection vector through string-based query construction",
        severity="high",
        cwe="CWE-89",
        cvss_base_score=8.8,
    )
    d = f.to_dict()
    assert d["severity"] == "high"
    assert d["cvss_base_score"] == 8.8
    f2 = Finding.from_dict(d)
    assert f2.tool == f.tool
    assert f2.cwe == f.cwe
    assert f2.fingerprint == f.fingerprint


def test_finding_severity_normalised():
    f = Finding(tool="bandit", severity="WARNING")  # type: ignore[arg-type]
    assert f.severity == "medium"


def test_finding_line_end_floors_to_line_start():
    f = Finding(tool="bandit", line_start=42, line_end=10)
    # line_end can't be before line_start
    assert f.line_end >= f.line_start


# ─── AgentVerdict ───────────────────────────────────────────────────


def test_agent_verdict_default_construction():
    v = AgentVerdict(role="auditor")
    assert v.role == "auditor"
    assert 0.0 <= v.confidence <= 1.0
    assert v.suggested_severity in ("info", "low", "medium", "high", "critical")


def test_agent_verdict_to_dict_drops_raw_llm_output():
    v = AgentVerdict(
        role="reasoner",
        verdict="true_positive",
        confidence=0.9,
        rationale="exploit chain visible",
        raw_llm_output="<huge LLM blob>" * 1000,
    )
    d = v.to_dict()
    assert "raw_llm_output" not in d
    assert d["rationale"] == "exploit chain visible"


def test_agent_verdict_severity_normalised():
    v = AgentVerdict(role="auditor", suggested_severity="error")  # type: ignore[arg-type]
    assert v.suggested_severity == "high"


# ─── ConfidenceScore ────────────────────────────────────────────────


def test_confidence_score_clamping():
    c = ConfidenceScore(final=2.0, sources_count=-1)
    assert c.final == 1.0
    assert c.sources_count == 0


def test_confidence_score_round_trip():
    c = ConfidenceScore(
        final=0.87,
        breakdown={"static_tool": 0.6, "auditor": 0.75},
        sources_count=2,
    )
    assert c.to_dict()["sources_count"] == 2


# ─── SentinelGate ───────────────────────────────────────────────────


def test_gate_status_round_trip():
    g = SentinelGate(
        phase="static_swarm",
        status="passed",
        score=92.5,
        findings_count=4,
        summary="bandit + semgrep agreed on 4 critical issues",
    )
    d = g.to_dict()
    assert d["phase"] == "static_swarm"
    assert d["status"] == "passed"


# ─── SentinelRequest ────────────────────────────────────────────────


def test_request_normalize_dedups_paths():
    r = SentinelRequest(paths=["a.py", "b.py", "a.py", "", "  ", "b.py"])
    r.normalize()
    assert r.paths == ["a.py", "b.py"]


def test_request_default_scan_profile():
    r = SentinelRequest()
    r.normalize()
    assert r.scan_profile == "standard"


def test_request_unknown_profile_falls_back():
    r = SentinelRequest(scan_profile="nuclear")  # type: ignore[arg-type]
    r.normalize()
    assert r.scan_profile == "standard"


def test_request_paranoid_preserved():
    r = SentinelRequest(scan_profile="PARANOID")  # type: ignore[arg-type]
    r.normalize()
    assert r.scan_profile == "paranoid"


# ─── SentinelBundle ─────────────────────────────────────────────────


def test_bundle_to_dict_shape():
    b = SentinelBundle(
        session_id="t-1",
        request=SentinelRequest(scan_profile="quick"),
        static_findings=[Finding(tool="bandit", severity="high")],
    )
    d = b.to_dict()
    assert d["session_id"] == "t-1"
    assert d["request"]["scan_profile"] == "quick"
    assert isinstance(d["static_findings"], list)
    assert d["static_findings"][0]["severity"] == "high"


def test_bundle_severity_count():
    b = SentinelBundle(severity_histogram={"low": 3, "high": 1})
    assert b.severity_count("low") == 3
    assert b.severity_count("high") == 1
    assert b.severity_count("critical") == 0


def test_bundle_empty_round_trip_json_safe():
    """An empty bundle must serialise to JSON without failing."""
    import json

    b = SentinelBundle(session_id="e-1")
    s = json.dumps(b.to_dict(), default=str)
    assert "e-1" in s
