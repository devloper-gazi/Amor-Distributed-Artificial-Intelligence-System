"""
Tests for code auditors — math/performance/edge-case verdict parsing,
parallel runner, MeshCodeAudit envelope helpers.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from document_processor.code_intelligence.mesh.code_auditors import (
    AuditorOutput,
    EdgeCaseCodeAuditor,
    MathCodeAuditor,
    MeshCodeAudit,
    PerformanceCodeAuditor,
    run_auditors_parallel,
)


def _audit_payload(verdict="approve", confidence=0.9):
    return json.dumps({
        "verdict": verdict,
        "confidence": confidence,
        "summary": "looks good",
    })


# ── parsing ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_parses_approve_verdict():
    async def llm(p, s, m): return _audit_payload("approve", 0.85)
    audit = MathCodeAuditor(llm)
    out = await audit.audit(user_prompt="x", code="print('hi')")
    assert out.verdict == "approve"
    assert out.confidence == 0.85
    assert out.error is None


@pytest.mark.asyncio
async def test_audit_parses_reject_verdict():
    async def llm(p, s, m): return _audit_payload("reject", 0.95)
    audit = PerformanceCodeAuditor(llm)
    out = await audit.audit(user_prompt="x", code="print('hi')")
    assert out.verdict == "reject"


@pytest.mark.asyncio
async def test_audit_unknown_verdict_falls_back_to_unknown():
    async def llm(p, s, m): return json.dumps({"verdict": "wat", "confidence": 0.5})
    audit = EdgeCaseCodeAuditor(llm)
    out = await audit.audit(user_prompt="x", code="print('hi')")
    assert out.verdict == "unknown"


@pytest.mark.asyncio
async def test_audit_handles_invalid_json():
    async def llm(p, s, m): return "not json"
    audit = MathCodeAuditor(llm)
    out = await audit.audit(user_prompt="x", code="print('hi')")
    assert out.error is not None
    assert "JSON parse" in out.error


@pytest.mark.asyncio
async def test_audit_handles_llm_error():
    async def llm(p, s, m):
        raise RuntimeError("ollama down")
    audit = MathCodeAuditor(llm)
    out = await audit.audit(user_prompt="x", code="print('hi')")
    assert out.error is not None
    assert "ollama" in out.error.lower()


@pytest.mark.asyncio
async def test_audit_skips_when_no_code():
    async def llm(p, s, m): return _audit_payload()
    audit = MathCodeAuditor(llm)
    out = await audit.audit(user_prompt="x", code="")
    assert out.error == "no code to audit"


# ── confidence clamping ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_clamps_confidence_to_0_1():
    async def llm(p, s, m):
        return json.dumps({"verdict": "approve", "confidence": 5.0,
                           "summary": "x"})
    audit = MathCodeAuditor(llm)
    out = await audit.audit(user_prompt="x", code="print('hi')")
    assert out.confidence == 1.0


# ── MeshCodeAudit envelope helpers ───────────────────────────────


def test_mesh_code_audit_any_rejected():
    a = AuditorOutput(role="math", role_label="Math",
                       verdict="approve", confidence=0.9)
    b = AuditorOutput(role="performance", role_label="Perf",
                       verdict="reject", confidence=0.8)
    audit = MeshCodeAudit(auditors=[a, b])
    assert audit.any_rejected is True
    assert audit.any_changes_requested is True


def test_mesh_code_audit_average_confidence_excludes_errors():
    a = AuditorOutput(role="math", role_label="Math",
                       verdict="approve", confidence=0.9)
    b = AuditorOutput(role="performance", role_label="Perf",
                       error="boom")  # excluded from average
    audit = MeshCodeAudit(auditors=[a, b])
    assert audit.average_confidence == 0.9


def test_mesh_code_audit_by_role_returns_dict_keyed_by_role_id():
    a = AuditorOutput(role="math", role_label="Math",
                       verdict="approve", confidence=0.9, summary="m")
    b = AuditorOutput(role="edge_case", role_label="Edge",
                       verdict="reject", confidence=0.8, summary="e")
    audit = MeshCodeAudit(auditors=[a, b])
    by_role = audit.by_role()
    assert set(by_role.keys()) == {"math", "edge_case"}
    assert by_role["math"]["verdict"] == "approve"


# ── parallel runner ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_auditors_parallel_collects_findings_for_non_approve():
    async def approve_llm(p, s, m): return _audit_payload("approve", 0.9)
    async def reject_llm(p, s, m): return _audit_payload("reject", 0.8)

    audit = await run_auditors_parallel(
        [MathCodeAuditor(approve_llm),
         PerformanceCodeAuditor(reject_llm)],
        user_prompt="x", code="print('hi')",
    )
    # The runner emits a finding line for the reject (so the
    # meta-arbiter sees it).
    assert any("REJECT" in f.upper() or "reject" in f.lower()
               for f in audit.findings)


@pytest.mark.asyncio
async def test_run_auditors_parallel_empty_list():
    audit = await run_auditors_parallel(
        [], user_prompt="x", code="print('hi')",
    )
    assert audit.auditors == []
    assert any("no auditors" in f for f in audit.findings)


@pytest.mark.asyncio
async def test_run_auditors_parallel_timeout_returns_error_outputs():
    async def slow(p, s, m):
        await asyncio.sleep(10.0)
        return _audit_payload()

    audit = await run_auditors_parallel(
        [MathCodeAuditor(slow), PerformanceCodeAuditor(slow)],
        user_prompt="x", code="print('hi')",
        timeout_s=0.05,
    )
    assert len(audit.auditors) == 2
    assert all(a.error and "timed out" in a.error for a in audit.auditors)
