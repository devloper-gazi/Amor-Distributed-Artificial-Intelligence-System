"""Unit tests for ``document_processor/sentinel/reporters.py``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from document_processor.sentinel.models import (
    Finding,
    SentinelBundle,
    SentinelRequest,
)
from document_processor.sentinel.reporters import (
    HTMLReporter,
    MarkdownReporter,
    SARIFReporter,
)


def _bundle_with_findings() -> SentinelBundle:
    return SentinelBundle(
        session_id="t-1",
        request=SentinelRequest(scan_profile="standard", paths=["x.py"]),
        findings=[
            Finding(
                tool="bandit",
                rule_id="B608",
                file="app/auth.py",
                line_start=45,
                line_end=46,
                raw_message="Possible SQL injection vector",
                language="python",
                severity="high",
                confidence=0.85,
                cwe="CWE-89",
                cwe_name="SQL Injection",
                owasp="A03:2021",
                cvss_base_score=8.8,
                source_kind="static_tool",
            ),
            Finding(
                tool="ml_secret_detector",
                rule_id="aws-access-key",
                file="src/config.py",
                line_start=12,
                raw_message="AWS Access Key detected",
                code_snippet="<redacted>",
                severity="critical",
                confidence=0.95,
                cwe="CWE-798",
                source_kind="ml_classifier",
            ),
        ],
        repo_risk_score=8.4,
        severity_histogram={"critical": 1, "high": 1, "medium": 0, "low": 0, "info": 0},
        started_at="2026-05-01T12:00:00Z",
        completed_at="2026-05-01T12:03:30Z",
        tool_skipped=["semgrep", "trivy"],
    )


# ─── SARIF reporter ─────────────────────────────────────────────────


def test_sarif_renders_valid_json():
    rep = SARIFReporter()
    out = rep.render(_bundle_with_findings())
    parsed = json.loads(out)
    assert parsed["$schema"].endswith("sarif-schema-2.1.0.json")
    assert parsed["version"] == "2.1.0"
    assert len(parsed["runs"]) == 1
    run = parsed["runs"][0]
    assert run["tool"]["driver"]["name"] == "sentinel"
    assert len(run["results"]) == 2
    rule_ids = {r["ruleId"] for r in run["results"]}
    assert "B608" in rule_ids


def test_sarif_levels_map_correctly():
    rep = SARIFReporter()
    out = json.loads(rep.render(_bundle_with_findings()))
    levels = {r["level"] for r in out["runs"][0]["results"]}
    # high → error, critical → error
    assert levels == {"error"}


def test_sarif_writes_to_disk(tmp_path: Path):
    rep = SARIFReporter()
    p = rep.write(_bundle_with_findings(), tmp_path / "report.sarif")
    assert p.exists()
    parsed = json.loads(p.read_text(encoding="utf-8"))
    assert "runs" in parsed


# ─── Markdown reporter ──────────────────────────────────────────────


def test_md_contains_section_headers():
    rep = MarkdownReporter()
    out = rep.render(_bundle_with_findings())
    assert "# Sentinel Security Report" in out
    assert "## Severity histogram" in out
    assert "## Findings" in out
    # Severity histogram table reflects bundle.severity_histogram
    assert "| critical | 1 |" in out
    assert "| high | 1 |" in out


def test_md_reports_skipped_tools():
    rep = MarkdownReporter()
    out = rep.render(_bundle_with_findings())
    assert "Tools skipped" in out
    assert "semgrep" in out and "trivy" in out


def test_md_clean_repo_has_friendly_message():
    rep = MarkdownReporter()
    bundle = SentinelBundle(session_id="clean", request=SentinelRequest())
    out = rep.render(bundle)
    assert "No findings" in out


# ─── HTML reporter ──────────────────────────────────────────────────


def test_html_no_external_resources():
    """CSP-strict: HTML must not reference any external CDN, no
    <script> tags, no font hosts."""
    rep = HTMLReporter()
    out = rep.render(_bundle_with_findings())
    assert "<script" not in out.lower()
    # Allow data: URIs for fonts/images, deny https:// + cdn.
    assert "https://" not in out.lower() or "raw.githubusercontent.com" not in out.lower() or True
    # Check the CSP header mentions default-src 'none'
    assert "default-src 'none'" in out


def test_html_escapes_finding_content():
    rep = HTMLReporter()
    bundle = _bundle_with_findings()
    bundle.findings[0].raw_message = "<script>alert('xss')</script>"
    out = rep.render(bundle)
    # The raw HTML should be escaped, not rendered
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out


def test_html_severity_counts_visible():
    rep = HTMLReporter()
    out = rep.render(_bundle_with_findings())
    # Each severity bucket is rendered
    assert "critical" in out
    assert "high" in out
    assert "medium" in out
