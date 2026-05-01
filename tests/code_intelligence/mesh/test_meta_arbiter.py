"""
Tests for the MetaArbiterAgent — verdict parsing, confidence clamping,
production_readiness range, deterministic fallback when LLM is down.
"""

from __future__ import annotations

import json

import pytest

from document_processor.code_intelligence.mesh.code_auditors import (
    AuditorOutput, MeshCodeAudit,
)
from document_processor.code_intelligence.mesh.meta_arbiter import (
    MetaArbiterAgent, MetaVerdict,
)


def _arbiter_payload(
    verdict="approve", confidence=0.9, readiness=85.0,
    risks=None, strengths=None,
):
    return json.dumps({
        "verdict": verdict,
        "confidence": confidence,
        "production_readiness": readiness,
        "top_risks": risks or [],
        "top_strengths": strengths or ["clear", "tested"],
        "summary": "ok",
    })


# ── happy path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_arbitrate_parses_full_payload():
    async def llm(p, s, m):
        return _arbiter_payload(
            verdict="approve_with_changes",
            confidence=0.78, readiness=72.0,
            risks=[{"severity": "high", "description": "no input validation"}],
            strengths=["clean naming", "deterministic"],
        )
    arb = MetaArbiterAgent(llm)
    audit = MeshCodeAudit(auditors=[
        AuditorOutput(role="math", role_label="Math",
                     verdict="approve", confidence=0.9, summary="ok"),
    ])
    v = await arb.arbitrate(
        user_prompt="x", chosen_summary="approach A",
        chosen_rationale="A is clearest",
        code="print('hi')", tests=None,
        execution_summary="ok", static_summary="0 errors",
        mesh_audit=audit, refine_iterations=0,
    )
    assert isinstance(v, MetaVerdict)
    assert v.verdict == "approve_with_changes"
    assert 0.77 < v.confidence < 0.79
    assert v.production_readiness == 72.0
    assert len(v.top_risks) == 1
    assert v.top_risks[0]["severity"] == "high"
    assert v.top_strengths == ["clean naming", "deterministic"]


@pytest.mark.asyncio
async def test_arbitrate_clamps_out_of_range_values():
    async def llm(p, s, m):
        return _arbiter_payload(
            confidence=2.5,         # >1, should clamp to 1.0
            readiness=200.0,        # >100, should clamp to 100
        )
    arb = MetaArbiterAgent(llm)
    v = await arb.arbitrate(
        user_prompt="x", chosen_summary="", chosen_rationale="",
        code="x", tests=None,
        execution_summary="ok", static_summary="ok",
        mesh_audit=None, refine_iterations=0,
    )
    assert v.confidence == 1.0
    assert v.production_readiness == 100.0


@pytest.mark.asyncio
async def test_arbitrate_normalises_unknown_verdict():
    async def llm(p, s, m):
        return json.dumps({
            "verdict": "very-good-yes",  # not a recognised value
            "confidence": 0.5,
            "production_readiness": 50,
            "top_risks": [], "top_strengths": [], "summary": "x",
        })
    arb = MetaArbiterAgent(llm)
    v = await arb.arbitrate(
        user_prompt="x", chosen_summary="", chosen_rationale="",
        code="x", tests=None,
        execution_summary="ok", static_summary="ok",
        mesh_audit=None, refine_iterations=0,
    )
    assert v.verdict == "unknown"


@pytest.mark.asyncio
async def test_arbitrate_caps_top_risks_and_strengths():
    """Pathological responses with 50 risks shouldn't blow up the UI."""
    async def llm(p, s, m):
        return _arbiter_payload(
            risks=[{"severity": "low", "description": f"r{i}"} for i in range(20)],
            strengths=[f"s{i}" for i in range(20)],
        )
    arb = MetaArbiterAgent(llm)
    v = await arb.arbitrate(
        user_prompt="x", chosen_summary="", chosen_rationale="",
        code="x", tests=None,
        execution_summary="ok", static_summary="ok",
        mesh_audit=None, refine_iterations=0,
    )
    assert len(v.top_risks) <= 5
    assert len(v.top_strengths) <= 5


# ── deterministic fallback paths ─────────────────────────────────


@pytest.mark.asyncio
async def test_arbitrate_fallback_when_llm_throws():
    async def boom(p, s, m):
        raise RuntimeError("ollama unreachable")
    arb = MetaArbiterAgent(boom)
    v = await arb.arbitrate(
        user_prompt="x", chosen_summary="", chosen_rationale="",
        code="print('hi')", tests=None,
        execution_summary="ok", static_summary="ok",
        mesh_audit=None, refine_iterations=0,
    )
    # Fallback is conservative: capped confidence, deterministic verdict.
    assert v.error is not None
    assert v.verdict == "approve"
    assert v.confidence <= 0.6
    assert "fallback" in v.summary.lower()


@pytest.mark.asyncio
async def test_arbitrate_fallback_rejects_when_auditor_rejected():
    async def boom(p, s, m):
        raise RuntimeError("down")
    arb = MetaArbiterAgent(boom)
    audit = MeshCodeAudit(auditors=[
        AuditorOutput(role="math", role_label="Math",
                     verdict="reject", confidence=0.9, summary="bad math"),
    ])
    v = await arb.arbitrate(
        user_prompt="x", chosen_summary="", chosen_rationale="",
        code="print('hi')", tests=None,
        execution_summary="ok", static_summary="ok",
        mesh_audit=audit, refine_iterations=0,
    )
    assert v.verdict == "reject"
    # Fallback surfaces the audit reject as a top risk.
    assert any("auditor" in r.get("description", "").lower()
               for r in v.top_risks)


@pytest.mark.asyncio
async def test_arbitrate_fallback_when_invalid_json():
    async def llm(p, s, m): return "not json"
    arb = MetaArbiterAgent(llm)
    v = await arb.arbitrate(
        user_prompt="x", chosen_summary="", chosen_rationale="",
        code="print('hi')", tests=None,
        execution_summary="ok", static_summary="ok",
        mesh_audit=None, refine_iterations=0,
    )
    assert v.error is not None
    assert "JSON parse" in v.error


@pytest.mark.asyncio
async def test_arbitrate_no_code_yields_reject():
    async def boom(p, s, m): raise RuntimeError("x")
    arb = MetaArbiterAgent(boom)
    v = await arb.arbitrate(
        user_prompt="x", chosen_summary="", chosen_rationale="",
        code="", tests=None,
        execution_summary="not run", static_summary="",
        mesh_audit=None, refine_iterations=0,
    )
    assert v.verdict == "reject"


# ── to_dict shape ─────────────────────────────────────────────────


def test_meta_verdict_to_dict_omits_raw():
    """raw_llm can be huge — must NOT leak into the public dict."""
    v = MetaVerdict(
        verdict="approve", confidence=0.9, production_readiness=85.0,
        raw="X" * 50_000,  # huge debug payload
    )
    d = v.to_dict()
    assert "raw" not in d
    assert d["verdict"] == "approve"
    assert d["confidence"] == 0.9
