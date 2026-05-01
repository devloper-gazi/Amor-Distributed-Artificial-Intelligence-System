"""
Tests for the MultiMLMesh orchestrator — runs reasoning + audit +
arbiter through a single API and emits per-phase events with stable
event_id stamping.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from document_processor.code_intelligence.mesh import (
    MeshConfig, MultiMLMesh,
)


def _reasoning_payload(label="A"):
    return json.dumps({
        "alternatives": [{
            "label": label, "summary": f"approach {label}",
            "scores": {"clarity": 0.8, "math_soundness": 0.8,
                       "performance": 0.7, "edge_cases": 0.7},
            "complexity_estimate": "O(n)", "perf_notes": "ok",
            "edge_cases": [],
        }],
        "chosen": label, "rationale": "ok",
    })


def _audit_payload(verdict="approve"):
    return json.dumps({
        "verdict": verdict, "confidence": 0.85,
        "summary": "looks fine",
    })


def _arbiter_payload():
    return json.dumps({
        "verdict": "approve", "confidence": 0.92,
        "production_readiness": 88,
        "top_risks": [], "top_strengths": ["clean", "tested"],
        "summary": "ready to ship",
    })


class _RouterLLM:
    """Routes calls based on system-prompt keywords so a single fake
    LLM can drive every mesh phase deterministically."""

    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, prompt, system, max_tokens):
        s = (system or "").lower()
        if "meta-arbiter" in s:
            role = "arbiter"
            ret = _arbiter_payload()
        elif "auditor" in s:
            role = "auditor"
            ret = _audit_payload()
        elif "reasoning agent" in s:
            role = "reasoner"
            ret = _reasoning_payload()
        else:
            role = "other"
            ret = "{}"
        self.calls.append(role)
        return ret


# ── reasoning ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_reasoning_emits_start_and_complete():
    events: list[dict[str, Any]] = []

    async def on_event(e): events.append(e)

    mesh = MultiMLMesh(llm_call=_RouterLLM(), on_event=on_event,
                       session_id="m1")
    out = await mesh.run_reasoning(user_prompt="x")
    types = [e["type"] for e in events]
    assert "mesh_reasoning_start" in types
    assert "mesh_reasoning_complete" in types
    # One specialist_complete per role in default config (4 roles).
    spec_count = sum(1 for t in types if t == "mesh_specialist_complete")
    assert spec_count == 4
    assert out.reasoning.chosen_label  # non-empty
    # Every event has a stable event_id.
    ids = {e["event_id"] for e in events}
    assert len(ids) == len(events)


@pytest.mark.asyncio
async def test_run_reasoning_runs_one_specialist_when_config_overridden():
    cfg = MeshConfig(reasoning_roles=["math"])
    mesh = MultiMLMesh(llm_call=_RouterLLM(), config=cfg, session_id="m2")
    out = await mesh.run_reasoning(user_prompt="x")
    # Only math should have been called for reasoning, but with the
    # default config of 1 role we still aggregate over 1 input.
    assert len(out.reasoning.alternatives) == 1


# ── audit ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_code_audit_emits_per_auditor_events():
    events: list[dict[str, Any]] = []

    async def on_event(e): events.append(e)

    mesh = MultiMLMesh(llm_call=_RouterLLM(), on_event=on_event,
                       session_id="a1")
    audit = await mesh.run_code_audit(
        user_prompt="x", code="print('hi')", tests=None, language="python",
    )
    types = [e["type"] for e in events]
    assert "mesh_audit_start" in types
    # Default config has 3 auditors.
    auditor_completes = [e for e in events if e["type"] == "mesh_auditor_complete"]
    assert len(auditor_completes) == 3
    assert "mesh_audit_complete" in types
    assert audit.average_confidence > 0.0


@pytest.mark.asyncio
async def test_run_code_audit_skips_when_no_code():
    mesh = MultiMLMesh(llm_call=_RouterLLM(), session_id="a2")
    audit = await mesh.run_code_audit(
        user_prompt="x", code="", tests=None,
    )
    # No code → no auditors run; envelope still returns cleanly.
    assert audit.auditors == []
    assert any("no code" in f.lower() for f in audit.findings)


# ── arbiter ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_meta_arbiter_emits_complete_event():
    events: list[dict[str, Any]] = []

    async def on_event(e): events.append(e)

    mesh = MultiMLMesh(llm_call=_RouterLLM(), on_event=on_event,
                       session_id="ar1")
    v = await mesh.run_meta_arbiter(
        user_prompt="x", chosen_summary="A", chosen_rationale="ok",
        code="print('hi')", tests=None,
        execution_summary="ok", static_summary="ok",
        mesh_audit=None, refine_iterations=0,
    )
    types = [e["type"] for e in events]
    assert "mesh_arbiter_start" in types
    assert "mesh_arbiter_complete" in types
    assert v.verdict == "approve"
    assert v.production_readiness == 88


@pytest.mark.asyncio
async def test_run_meta_arbiter_disabled_returns_unknown():
    cfg = MeshConfig(enable_meta_arbiter=False)
    mesh = MultiMLMesh(llm_call=_RouterLLM(), config=cfg)
    v = await mesh.run_meta_arbiter(
        user_prompt="x", chosen_summary="", chosen_rationale="",
        code="x", tests=None,
        execution_summary="ok", static_summary="ok",
        mesh_audit=None, refine_iterations=0,
    )
    assert v.verdict == "unknown"
    assert "disabled" in v.summary


# ── cancellation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_check_short_circuits_reasoning():
    cancelled = {"v": True}

    async def llm(p, s, m): return _reasoning_payload()

    mesh = MultiMLMesh(
        llm_call=llm,
        cancel_check=lambda: cancelled["v"],
    )
    out = await mesh.run_reasoning(user_prompt="x")
    # Cancelled before reasoning → fallback alternative + a finding.
    assert any("cancel" in f.lower() for f in out.findings)


@pytest.mark.asyncio
async def test_cancel_check_short_circuits_audit():
    cancelled = {"v": True}

    async def llm(p, s, m): return _audit_payload()

    mesh = MultiMLMesh(
        llm_call=llm,
        cancel_check=lambda: cancelled["v"],
    )
    audit = await mesh.run_code_audit(
        user_prompt="x", code="print('hi')",
    )
    assert audit.auditors == []
    assert any("cancel" in f.lower() for f in audit.findings)
