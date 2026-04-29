"""
Integration tests for QuickCodeEngine + Multi-ML Mesh.

These run with `request.use_mesh=True` (the new default) and verify
that the engine drives the mesh's reasoning + audit + arbiter phases
in the right order, populates the bundle's mesh_* fields, and falls
back gracefully when any mesh phase errors.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from document_processor.quick_code import (
    QuickCodeEngine, QuickCodeRequest,
)


# ─── Fakes ──────────────────────────────────────────────────────────


class _RouterLLM:
    """Routes calls based on the system prompt so a single fake LLM
    drives every phase deterministically. Mirrors the production
    role-detection logic the mesh uses internally."""

    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, prompt, system, max_tokens):
        s = (system or "").lower()
        if "meta-arbiter" in s:
            role = "arbiter"
            ret = json.dumps({
                "verdict": "approve", "confidence": 0.92,
                "production_readiness": 88,
                "top_risks": [], "top_strengths": ["clear", "tested"],
                "summary": "ready to ship",
            })
        elif "auditor" in s:
            role = "auditor"
            ret = json.dumps({
                "verdict": "approve", "confidence": 0.85,
                "summary": "looks fine",
            })
        elif "reasoning agent" in s:
            role = "reasoner"
            ret = json.dumps({
                "alternatives": [{
                    "label": "A", "summary": "iterative loop",
                    "scores": {"clarity": 0.85, "math_soundness": 0.85,
                               "performance": 0.75, "edge_cases": 0.75},
                    "complexity_estimate": "O(n)", "perf_notes": "ok",
                    "edge_cases": [],
                }],
                "chosen": "A", "rationale": "A wins on clarity",
            })
        elif "tester agent" in s or "qa engineer" in s:
            role = "tester"
            ret = "```python\ndef test_x(): assert 1\n```\n```json\n{\"language\": \"python\"}\n```"
        elif "coder agent" in s:
            role = "coder"
            ret = "```python\nprint('hi')\n```\n```json\n{\"language\": \"python\"}\n```"
        elif "triage classifier" in s:
            role = "triage"
            ret = json.dumps({
                "task_type": "generation", "language": "python",
                "complexity": "moderate",
                "needs_execution": True, "needs_tests": True,
            })
        else:
            role = "other"
            ret = "{}"
        self.calls.append(role)
        return ret


class _Sandbox:
    async def execute(self, code, language="python", timeout=30):
        class _R:
            exit_code = 0; stdout = ""; stderr = ""; timed_out = False
            error = None; duration_ms = 5; language = "python"; skipped = False

            @property
            def success(self): return True

            def to_dict(self):
                return {"exit_code": 0, "stdout": "", "stderr": "",
                        "timed_out": False, "error": None,
                        "duration_ms": 5, "language": "python",
                        "skipped": False, "success": True}
        return _R()


class _StaticHarness:
    async def analyze(self, code, language="python"):
        class _R:
            def to_dict(self):
                return {
                    "language": language, "issues": [],
                    "complexity_score": 1.0, "maintainability_index": 80.0,
                    "lines_of_code": 1, "syntax_valid": True,
                    "syntax_error": None, "ast_summary": {},
                    "severity_counts": {"error": 0, "warning": 0,
                                         "info": 0, "security": 0},
                }
        return _R()


# ─── happy-path integration ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_mesh_run_emits_audit_and_arbiter_phases():
    """With use_mesh=True the engine emits two extra phase events
    after the existing triage→reason→implement→verify→refine sequence:
    `audit` and `arbiter`, populating the bundle's mesh_* fields."""
    events: list[dict[str, Any]] = []

    async def on_event(e): events.append(e)

    engine = QuickCodeEngine(
        session_id="m1",
        request=QuickCodeRequest(
            prompt="implement a stable softmax",
            language="python", max_refine=0, use_mesh=True,
        ),
        on_event=on_event,
        llm_call=_RouterLLM(),
        sandbox=_Sandbox(),
        static_harness=_StaticHarness(),
    )
    bundle = await engine.run()

    starts = [e["phase"] for e in events
              if e.get("type") == "quick_code_phase_start"]
    # The two new mesh phases come at the end.
    assert "audit" in starts
    assert "arbiter" in starts
    # And in order, they're after verify.
    verify_idx = starts.index("verify")
    assert starts.index("audit") > verify_idx
    assert starts.index("arbiter") > starts.index("audit")

    # Bundle carries all three mesh outputs.
    assert bundle.mesh_reasoning is not None
    assert bundle.mesh_audit is not None
    assert bundle.meta_verdict is not None
    assert bundle.meta_verdict["verdict"] == "approve"
    assert bundle.meta_verdict["production_readiness"] == 88


@pytest.mark.asyncio
async def test_mesh_reasoning_replaces_single_path_payload():
    """Mesh-driven reasoning must yield aggregated alternatives, not
    the single-path JSON the legacy reasoner returns."""
    engine = QuickCodeEngine(
        session_id="m2",
        request=QuickCodeRequest(prompt="x", max_refine=0, use_mesh=True),
        llm_call=_RouterLLM(),
        sandbox=_Sandbox(),
        static_harness=_StaticHarness(),
    )
    bundle = await engine.run()
    # mesh_reasoning is the aggregator's envelope (per_specialist_picks
    # is the unique fingerprint of the mesh path).
    assert bundle.mesh_reasoning is not None
    assert "per_specialist_picks" in bundle.mesh_reasoning
    # All four default specialists should have been invoked.
    picks = bundle.mesh_reasoning["per_specialist_picks"]
    assert set(picks.keys()) == {"general", "math", "performance", "edge_case"}


@pytest.mark.asyncio
async def test_mesh_gates_appended_after_audit_and_arbiter():
    engine = QuickCodeEngine(
        session_id="m3",
        request=QuickCodeRequest(prompt="x", max_refine=0, use_mesh=True),
        llm_call=_RouterLLM(),
        sandbox=_Sandbox(),
        static_harness=_StaticHarness(),
    )
    bundle = await engine.run()
    phases_in_gates = [g.phase for g in bundle.gates]
    assert "audit" in phases_in_gates
    assert "arbiter" in phases_in_gates


# ─── degraded-path integration ─────────────────────────────────────


@pytest.mark.asyncio
async def test_mesh_audit_failure_does_not_block_arbiter():
    """A timeout / crash in the audit phase must not kill the run.
    The arbiter phase should still produce a (fallback) verdict."""
    class _Flaky:
        def __init__(self):
            self.calls = 0

        async def __call__(self, prompt, system, max_tokens):
            self.calls += 1
            s = (system or "").lower()
            if "auditor" in s:
                # Auditor calls always return invalid JSON.
                return "boom — not json"
            return _RouterLLM()._reasoner_or_else(prompt, system) \
                if False else await _RouterLLM()(prompt, system, max_tokens)

    # Simpler: use the regular RouterLLM but throw on auditor calls.
    base = _RouterLLM()

    async def flaky(prompt, system, max_tokens):
        s = (system or "").lower()
        if "auditor" in s:
            raise RuntimeError("auditor backend down")
        return await base(prompt, system, max_tokens)

    engine = QuickCodeEngine(
        session_id="m4",
        request=QuickCodeRequest(prompt="x", max_refine=0, use_mesh=True),
        llm_call=flaky,
        sandbox=_Sandbox(),
        static_harness=_StaticHarness(),
    )
    bundle = await engine.run()
    # Audit phase still emitted (every auditor errored individually).
    # The audit gate is appended even when no auditor approves.
    phases = [g.phase for g in bundle.gates]
    assert "audit" in phases
    # Arbiter still ran with the failed audit and produced a verdict.
    assert bundle.meta_verdict is not None


@pytest.mark.asyncio
async def test_use_mesh_false_skips_audit_and_arbiter():
    """When use_mesh=False, the engine MUST stick to the legacy
    triage→reason→implement→verify(→refine) flow with no audit or
    arbiter phases."""
    events: list[dict[str, Any]] = []

    async def on_event(e): events.append(e)

    engine = QuickCodeEngine(
        session_id="m5",
        request=QuickCodeRequest(prompt="x", max_refine=0, use_mesh=False),
        on_event=on_event,
        llm_call=_RouterLLM(),
        sandbox=_Sandbox(),
        static_harness=_StaticHarness(),
    )
    bundle = await engine.run()
    starts = [e["phase"] for e in events
              if e.get("type") == "quick_code_phase_start"]
    assert "audit" not in starts
    assert "arbiter" not in starts
    # And the bundle's mesh_* fields are None.
    assert bundle.mesh_reasoning is None
    assert bundle.mesh_audit is None
    assert bundle.meta_verdict is None


# ─── arbiter feed-through ────────────────────────────────────────


@pytest.mark.asyncio
async def test_arbiter_verdict_drives_arbiter_gate_score():
    """The arbiter gate's score should equal production_readiness."""
    engine = QuickCodeEngine(
        session_id="m6",
        request=QuickCodeRequest(prompt="x", max_refine=0, use_mesh=True),
        llm_call=_RouterLLM(),
        sandbox=_Sandbox(),
        static_harness=_StaticHarness(),
    )
    bundle = await engine.run()
    arbiter_gate = next(g for g in bundle.gates if g.phase == "arbiter")
    assert arbiter_gate.score == 88
    # Top-level verdict implied passed status because verdict=approve
    # AND readiness >= 80.
    assert arbiter_gate.status == "passed"


@pytest.mark.asyncio
async def test_deliverable_markdown_lists_arbiter_section():
    engine = QuickCodeEngine(
        session_id="m7",
        request=QuickCodeRequest(prompt="implement softmax", max_refine=0,
                                 use_mesh=True),
        llm_call=_RouterLLM(),
        sandbox=_Sandbox(),
        static_harness=_StaticHarness(),
    )
    bundle = await engine.run()
    md = bundle.deliverable_markdown
    assert "Production-readiness verdict" in md
    assert "Mesh code audit" in md
    assert "Mesh reasoning" in md
    # Confidence + readiness numbers visible.
    assert "0.92" in md
    assert "88" in md
