"""Unit tests for ``document_processor/sentinel/static_swarm.py``.

Tests use mocked subprocess output (canned JSON / XML) so the suite
runs without any real tool installed.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from document_processor.sentinel.models import Finding
from document_processor.sentinel.static_swarm import (
    DEFAULT_TOOLS,
    StaticSwarm,
    detect_tool,
    parse_bandit_json,
    parse_cppcheck_xml,
    parse_gitleaks_json,
    parse_gosec_json,
    parse_mypy_json,
    parse_pylint_json,
    parse_semgrep_json,
    parse_trivy_json,
)


def _run(coro):
    return asyncio.run(coro)


# ─── Bandit parser ──────────────────────────────────────────────────


def test_parse_bandit_json_basic():
    payload = json.dumps({
        "results": [
            {
                "filename": "app/auth.py",
                "line_number": 45,
                "test_id": "B608",
                "test_name": "hardcoded_sql_expressions",
                "issue_severity": "HIGH",
                "issue_confidence": "HIGH",
                "issue_text": "Possible SQL injection vector",
                "code": "query = 'SELECT * FROM users WHERE id = ' + user_id",
                "cwe": {"id": 89},
            }
        ]
    })
    findings = parse_bandit_json(payload)
    assert len(findings) == 1
    f = findings[0]
    assert f.tool == "bandit"
    assert f.rule_id == "B608"
    assert f.severity == "high"
    assert f.confidence >= 0.8
    assert f.cwe == "CWE-89"
    assert f.language == "python"


def test_parse_bandit_handles_empty_payload():
    assert parse_bandit_json("") == []
    assert parse_bandit_json("not json") == []
    assert parse_bandit_json("{}") == []


def test_parse_bandit_severity_aliases():
    payload = json.dumps({
        "results": [
            {"filename": "x.py", "line_number": 1, "issue_severity": "MEDIUM",
             "test_id": "B101", "issue_text": "assert", "issue_confidence": "MEDIUM"},
            {"filename": "x.py", "line_number": 2, "issue_severity": "LOW",
             "test_id": "B102", "issue_text": "exec", "issue_confidence": "LOW"},
        ]
    })
    out = parse_bandit_json(payload)
    assert out[0].severity == "medium"
    assert out[1].severity == "low"


# ─── Semgrep parser ─────────────────────────────────────────────────


def test_parse_semgrep_extracts_cwe_from_metadata():
    payload = json.dumps({
        "results": [
            {
                "check_id": "python.lang.security.audit.eval-detected",
                "path": "scripts/run.py",
                "start": {"line": 12, "col": 4},
                "end": {"line": 12, "col": 30},
                "extra": {
                    "message": "Detected use of eval()",
                    "severity": "ERROR",
                    "lines": "    eval(user_input)",
                    "metadata": {"cwe": ["CWE-95: Improper Neutralization"]},
                },
            }
        ]
    })
    findings = parse_semgrep_json(payload)
    assert findings
    assert findings[0].cwe == "CWE-95"
    assert findings[0].severity == "high"  # ERROR → high


def test_parse_semgrep_owasp_metadata():
    payload = json.dumps({
        "results": [{
            "check_id": "java.lang.security.audit.weak-hash",
            "path": "src/Hash.java",
            "start": {"line": 10}, "end": {"line": 10},
            "extra": {"message": "Weak hash", "severity": "WARNING",
                      "metadata": {"owasp": "A02:2021 — Cryptographic Failures"}},
        }]
    })
    out = parse_semgrep_json(payload)
    assert out[0].owasp.startswith("A02:")


# ─── Gitleaks parser ────────────────────────────────────────────────


def test_parse_gitleaks_basic():
    payload = json.dumps([
        {
            "Description": "AWS Access Key",
            "File": "config.yml",
            "StartLine": 5,
            "EndLine": 5,
            "RuleID": "aws-access-token",
            "Match": "AKIAEXAMPLE",
            "Secret": "REDACTED",
        }
    ])
    out = parse_gitleaks_json(payload)
    assert len(out) == 1
    assert out[0].severity == "critical"
    assert out[0].cwe == "CWE-798"
    assert "<redacted secret>" in out[0].code_snippet


# ─── Trivy parser ───────────────────────────────────────────────────


def test_parse_trivy_extracts_cve_severity():
    payload = json.dumps({
        "Results": [{
            "Target": "package.json",
            "Vulnerabilities": [{
                "VulnerabilityID": "CVE-2023-12345",
                "PkgName": "lodash",
                "InstalledVersion": "4.17.20",
                "FixedVersion": "4.17.21",
                "Severity": "CRITICAL",
                "Title": "Prototype pollution",
                "Description": "...",
                "CweIDs": ["CWE-1321"],
                "CVSS": {"nvd": {"V3Score": 9.8}},
            }]
        }]
    })
    out = parse_trivy_json(payload)
    assert len(out) == 1
    assert out[0].severity == "critical"
    assert out[0].cwe == "CWE-1321"
    assert out[0].cvss_base_score == 9.8
    assert out[0].extra["package"] == "lodash"


# ─── Pylint parser ──────────────────────────────────────────────────


def test_parse_pylint_basic():
    payload = json.dumps([
        {"type": "error", "module": "x", "path": "x.py", "line": 5,
         "column": 0, "message": "Undefined variable",
         "message-id": "E0602", "symbol": "undefined-variable"},
        {"type": "convention", "module": "x", "path": "x.py", "line": 1,
         "column": 0, "message": "Missing docstring",
         "message-id": "C0114", "symbol": "missing-docstring"},
    ])
    out = parse_pylint_json(payload)
    assert out[0].severity == "high"
    assert out[1].severity == "low"


# ─── Mypy parser ────────────────────────────────────────────────────


def test_parse_mypy_jsonl():
    payload = (
        '{"file": "x.py", "line": 7, "column": 4, '
        '"severity": "error", "message": "Argument has incompatible type", '
        '"code": "arg-type"}\n'
        '{"file": "x.py", "line": 9, "severity": "note", '
        '"message": "Note: did you mean ...", "code": ""}\n'
    )
    out = parse_mypy_json(payload)
    assert len(out) == 2
    assert out[0].severity == "high"
    assert out[1].severity == "info"


# ─── Gosec parser ───────────────────────────────────────────────────


def test_parse_gosec_basic():
    payload = json.dumps({
        "Issues": [
            {"file": "main.go", "line": "30", "severity": "HIGH",
             "confidence": "HIGH", "rule_id": "G304", "details": "Potential file inclusion via variable",
             "cwe": {"ID": "22"}},
        ]
    })
    out = parse_gosec_json(payload)
    assert out[0].cwe == "CWE-22"
    assert out[0].language == "go"
    assert out[0].severity == "high"


# ─── Cppcheck parser ────────────────────────────────────────────────


def test_parse_cppcheck_xml():
    payload = """<?xml version="1.0"?>
<results version="2">
<errors>
<error id="bufferOverrun" severity="error" msg="Buffer overrun" cwe="120">
<location file="src/main.c" line="42"/>
</error>
</errors>
</results>"""
    out = parse_cppcheck_xml(payload)
    assert len(out) == 1
    assert out[0].cwe == "CWE-120"
    assert out[0].file == "src/main.c"
    assert out[0].line_start == 42


# ─── Tool detection ─────────────────────────────────────────────────


def test_detect_tool_python_module():
    """``detect_tool`` returns True when the python_module is
    importable.  We pick ``sys`` which is always available so the
    test doesn't depend on bandit being installed in the env."""
    from document_processor.sentinel.static_swarm import ToolSpec
    spec = ToolSpec(name="sys-test", python_module="sys")
    assert detect_tool(spec) is True


def test_detect_tool_missing_module_returns_false():
    from document_processor.sentinel.static_swarm import ToolSpec
    fake = ToolSpec(name="zzz", python_module="zzz_module_does_not_exist_xyz")
    assert detect_tool(fake) is False


def test_detect_tool_missing_binary_returns_false():
    from document_processor.sentinel.static_swarm import ToolSpec
    fake = ToolSpec(name="zzz_does_not_exist", binary="zzz_does_not_exist_xyz")
    assert detect_tool(fake) is False


# ─── StaticSwarm orchestrator ───────────────────────────────────────


def test_static_swarm_no_paths_skips_everything():
    sw = StaticSwarm()
    res = _run(sw.scan([]))
    assert res.findings == []
    assert res.tools_run == []


def test_static_swarm_skip_unavailable_tools():
    """Force every tool to look unavailable; verify clean skip path."""
    seen_events: list[tuple[str, dict]] = []

    async def cb(name: str, payload: dict) -> None:
        seen_events.append((name, payload))

    sw = StaticSwarm(tools=("bandit", "semgrep", "gitleaks", "trivy"), on_event=cb)
    with patch(
        "document_processor.sentinel.static_swarm.detect_tool",
        return_value=False,
    ):
        res = _run(sw.scan(["x.py"]))
    assert res.findings == []
    assert set(res.tools_skipped) == {"bandit", "semgrep", "gitleaks", "trivy"}
    skip_events = [name for name, _ in seen_events if name == "tool_skipped"]
    assert len(skip_events) == 4


def test_static_swarm_runs_only_known_tools():
    sw = StaticSwarm(tools=("bandit", "totally_made_up_tool"))
    # The fake tool name is silently dropped.
    assert sw._tools == ("bandit",)
