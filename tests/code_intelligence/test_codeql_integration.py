"""Cycle G G3 — coverage for CodeQL hot-path integration."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from document_processor.code_intelligence import codeql_runner


# ─── SARIF parsing ─────────────────────────────────────────────────


def _sarif(results, rules=None):
    """Fixture builder — minimal SARIF 2.1.0 document."""
    return {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "CodeQL", "rules": rules or []}},
            "results": results,
        }],
    }


def test_parse_sarif_extracts_basic_finding():
    sarif = _sarif([{
        "ruleId": "py/syntax-error",
        "level": "error",
        "message": {"text": "Missing colon"},
        "locations": [{
            "physicalLocation": {
                "artifactLocation": {"uri": "main.py"},
                "region": {"startLine": 5, "startColumn": 10},
            }
        }],
    }])
    findings = codeql_runner.parse_sarif(sarif)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "error"
    assert f.rule_id == "py/syntax-error"
    assert f.line == 5
    assert f.col == 10
    assert f.file == "main.py"


def test_parse_sarif_maps_levels_to_severities():
    sarif = _sarif([
        {"ruleId": "r1", "level": "error", "message": {"text": "e"}},
        {"ruleId": "r2", "level": "warning", "message": {"text": "w"}},
        {"ruleId": "r3", "level": "note", "message": {"text": "n"}},
        {"ruleId": "r4", "level": "info", "message": {"text": "i"}},
    ])
    findings = codeql_runner.parse_sarif(sarif)
    severities = {f.rule_id: f.severity for f in findings}
    assert severities["r1"] == "error"
    assert severities["r2"] == "warning"
    assert severities["r3"] == "info"
    assert severities["r4"] == "info"


def test_parse_sarif_escalates_security_tags_to_security_severity():
    """A `level=warning` finding with security tags should bump to
    severity=security so it surfaces above non-security warnings on
    the code review UI."""
    sarif = _sarif(
        [{
            "ruleId": "py/sql-injection",
            "level": "warning",   # would be 'warning' otherwise
            "message": {"text": "User input flows to SQL"},
        }],
        rules=[{
            "id": "py/sql-injection",
            "properties": {"tags": ["security", "external/cwe/cwe-089"]},
        }],
    )
    findings = codeql_runner.parse_sarif(sarif)
    assert findings[0].severity == "security"


def test_parse_sarif_escalates_by_rule_id_keyword():
    """When `properties.tags` is missing but the rule_id contains a
    security keyword, still escalate."""
    sarif = _sarif([{
        "ruleId": "py/path-injection",
        "level": "warning",
        "message": {"text": "Path traversal risk"},
    }])
    findings = codeql_runner.parse_sarif(sarif)
    assert findings[0].severity == "security"


def test_parse_sarif_handles_missing_location():
    sarif = _sarif([{
        "ruleId": "r1", "level": "warning",
        "message": {"text": "no location"},
    }])
    findings = codeql_runner.parse_sarif(sarif)
    assert findings[0].line is None
    assert findings[0].col is None
    assert findings[0].file is None


def test_parse_sarif_empty_runs():
    assert codeql_runner.parse_sarif({"version": "2.1.0", "runs": []}) == []


def test_parse_sarif_tolerates_malformed_result():
    """A result missing ruleId/level still produces a finding (with
    fallback severity=warning) — don't drop the WHOLE sweep because
    one row is incomplete."""
    sarif = _sarif([
        {"message": {"text": "no ruleId, no level"}},
    ])
    findings = codeql_runner.parse_sarif(sarif)
    assert len(findings) == 1
    assert findings[0].rule_id == "unknown"
    assert findings[0].severity == "warning"


# ─── Availability + cache ──────────────────────────────────────────


def test_codeql_available_false_when_binary_missing(monkeypatch):
    monkeypatch.setattr(codeql_runner.shutil, "which", lambda name: None)
    assert codeql_runner._codeql_available() is False


def test_codeql_available_true_when_binary_present(monkeypatch):
    monkeypatch.setattr(codeql_runner.shutil, "which", lambda name: "/usr/bin/codeql")
    assert codeql_runner._codeql_available() is True


def test_code_hash_stable_and_deterministic():
    h1 = codeql_runner._code_hash("hello world")
    h2 = codeql_runner._code_hash("hello world")
    assert h1 == h2
    assert h1 != codeql_runner._code_hash("hello world!")


# ─── Run path: skip when binary missing ────────────────────────────


def test_run_codeql_python_skips_when_binary_missing(monkeypatch):
    monkeypatch.setattr(codeql_runner, "_codeql_available", lambda: False)
    findings = asyncio.run(codeql_runner.run_codeql_python("def f(): pass"))
    assert findings == []


def test_run_codeql_python_skips_when_code_empty(monkeypatch):
    monkeypatch.setattr(codeql_runner, "_codeql_available", lambda: True)
    findings = asyncio.run(codeql_runner.run_codeql_python(""))
    assert findings == []


# ─── findings_to_analysis_issues mapping ───────────────────────────


def test_findings_to_analysis_issues_shape():
    f = codeql_runner.CodeQLFinding(
        severity="security", rule_id="py/sql-injection",
        message="m", line=10, col=5, file="main.py",
    )
    issues = codeql_runner.findings_to_analysis_issues([f])
    assert len(issues) == 1
    issue = issues[0]
    assert issue["severity"] == "security"
    assert issue["code"] == "py/sql-injection"
    assert issue["source"] == "codeql"
    assert issue["line"] == 10


# ─── Settings gate ─────────────────────────────────────────────────


def test_static_analysis_skips_codeql_when_setting_disabled(monkeypatch):
    """The harness must NOT invoke CodeQL when code_codeql_enabled=False
    even if the binary is present.  Default OFF posture."""
    from document_processor.code_intelligence.static_analysis import (
        StaticAnalysisHarness, StaticAnalysisResult,
    )
    from document_processor.config.settings import settings as _settings
    monkeypatch.setattr(_settings, "code_codeql_enabled", False, raising=False)

    invoked = {"called": False}

    async def fake_run_codeql_python(*args, **kwargs):
        invoked["called"] = True
        return []

    # Stub: settings flag is FALSE, so even if we monkey-patch the
    # runner, _run_codeql should return BEFORE invoking it.
    monkeypatch.setattr(
        codeql_runner, "run_codeql_python", fake_run_codeql_python,
    )

    harness = StaticAnalysisHarness()
    result = StaticAnalysisResult(language="python")
    code = "def f():\n    pass\n" * 50   # 200+ chars so size gate passes

    asyncio.run(harness._run_codeql(code, result))
    assert invoked["called"] is False, (
        "code_codeql_enabled=False but CodeQL was invoked anyway"
    )


def test_static_analysis_skips_codeql_for_tiny_snippets(monkeypatch):
    """Cost-control: snippets under 200 chars skip CodeQL even when
    enabled — bandit / pylint already cover that surface."""
    from document_processor.code_intelligence.static_analysis import (
        StaticAnalysisHarness, StaticAnalysisResult,
    )
    from document_processor.config.settings import settings as _settings
    monkeypatch.setattr(_settings, "code_codeql_enabled", True, raising=False)

    invoked = {"called": False}

    async def fake_run_codeql_python(*args, **kwargs):
        invoked["called"] = True
        return []

    monkeypatch.setattr(
        codeql_runner, "run_codeql_python", fake_run_codeql_python,
    )

    harness = StaticAnalysisHarness()
    result = StaticAnalysisResult(language="python")
    tiny_code = "print('hi')"   # 11 chars

    asyncio.run(harness._run_codeql(tiny_code, result))
    assert invoked["called"] is False


def test_static_analysis_records_codeql_findings_when_enabled(monkeypatch):
    """End-to-end stub: when enabled + binary present, findings land
    as `AnalysisIssue` rows in the harness result."""
    from document_processor.code_intelligence.static_analysis import (
        StaticAnalysisHarness, StaticAnalysisResult,
    )
    from document_processor.config.settings import settings as _settings
    monkeypatch.setattr(_settings, "code_codeql_enabled", True, raising=False)

    async def fake_run_codeql_python(code, **kwargs):
        return [
            codeql_runner.CodeQLFinding(
                severity="security",
                rule_id="py/sql-injection",
                message="taint flow",
                line=3, col=1, file="main.py",
            )
        ]

    # Patch BOTH the source module export AND the static_analysis lazy
    # import (which re-binds at call-site).
    monkeypatch.setattr(
        codeql_runner, "run_codeql_python", fake_run_codeql_python,
    )
    # Reload-aware patch path: the lazy `from .codeql_runner import
    # run_codeql_python` re-fetches the symbol per call, so the
    # module-level patch above is sufficient.

    harness = StaticAnalysisHarness()
    result = StaticAnalysisResult(language="python")
    code = "x = 1\n" * 60   # >200 chars

    asyncio.run(harness._run_codeql(code, result))
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.severity == "security"
    assert issue.code == "py/sql-injection"
    assert issue.source == "codeql"
