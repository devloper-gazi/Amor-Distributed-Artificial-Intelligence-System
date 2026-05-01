"""
Integration tests for QuickCodeEngine + CodeSynthesisReactor (v10).

Asserts that `_phase_reactor_verify` runs after `_phase_verify` (and
`_phase_refine_if_needed`), populates `bundle.reactor_bundle`, and
appends a `reactor` gate. All deps mocked — no Docker, Mongo, Redis.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from document_processor.quick_code import QuickCodeEngine, QuickCodeRequest


# ── Fakes ──────────────────────────────────────────────────────


class _RouterLLM:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, prompt, system, max_tokens):
        s = (system or "").lower()
        if "meta-arbiter" in s:
            self.calls.append("arbiter")
            return json.dumps({
                "verdict": "approve", "confidence": 0.92,
                "production_readiness": 88,
                "top_risks": [], "top_strengths": ["clean"],
                "summary": "ok",
            })
        if "auditor" in s:
            self.calls.append("auditor")
            return json.dumps({
                "verdict": "approve", "confidence": 0.85, "summary": "ok",
            })
        if "reasoning agent" in s:
            self.calls.append("reasoner")
            return json.dumps({
                "alternatives": [{
                    "label": "A", "summary": "iterative",
                    "scores": {"clarity": 0.85, "math_soundness": 0.85,
                               "performance": 0.75, "edge_cases": 0.75},
                    "complexity_estimate": "O(n)", "perf_notes": "ok",
                    "edge_cases": [],
                }],
                "chosen": "A", "rationale": "A",
            })
        if "tester agent" in s or "qa engineer" in s:
            self.calls.append("tester")
            return ('```python\ndef test_x(): assert 1\n```\n'
                    '```json\n{"language": "python"}\n```')
        if "coder agent" in s:
            self.calls.append("coder")
            return ('```python\ndef f(xs): return sum(xs)\n```\n'
                    '```json\n{"language": "python"}\n```')
        if "triage classifier" in s:
            self.calls.append("triage")
            return json.dumps({
                "task_type": "generation", "language": "python",
                "complexity": "moderate",
                "needs_execution": True, "needs_tests": True,
            })
        if "invariant generator" in s:
            # Reactor's LLM-suggested invariants path.
            self.calls.append("invariant")
            return json.dumps({"invariants": []})
        self.calls.append("other")
        return "{}"


class _ScriptedSandbox:
    """Sandbox that emits canned BENCH_RESULT + PROPERTY_RESULT lines."""

    def __init__(self):
        self.execute_count = 0

    async def execute(self, code, language="python", timeout=30):
        self.execute_count += 1
        # Reactor calls run after the standard verify; we recognise
        # them by the bench harness marker `_amor_pick_target`.
        if "BENCH_RESULT" in code or "_amor_pick_target" in code:
            stdout = (
                "BENCH_TARGET=f\n"
                'BENCH_RESULT={"scale":10,"ms":1.0,"peak_kb":10}\n'
                'BENCH_RESULT={"scale":100,"ms":10.0,"peak_kb":100}\n'
                'BENCH_RESULT={"scale":1000,"ms":100.0,"peak_kb":1000}\n'
            )
        elif "PROPERTY_RESULT" in code or "_amor_run_one" in code:
            stdout = (
                "PROPERTY_TARGET=f\n"
                'PROPERTY_RESULT={"name":"default_callable_no_exception",'
                '"passed":true,"samples_run":50,"samples_failed":0,'
                '"first_failure_input":null,"first_failure_message":null,'
                '"error":null}\n'
            )
        else:
            # Standard QuickCode verify path — clean exec.
            stdout = ""

        class _R:
            def __init__(s):
                s.stdout = stdout
                s.stderr = ""
                s.exit_code = 0
                s.skipped = False

            @property
            def success(self): return True

            def to_dict(self):
                return {"exit_code": 0, "stdout": stdout, "stderr": "",
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
                    "per_function_complexity": {"f": 1},
                }
        return _R()


# ── Tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reactor_phase_runs_after_verify_and_populates_bundle():
    events: list[dict[str, Any]] = []

    async def on_event(e):
        events.append(e)

    engine = QuickCodeEngine(
        session_id="r10",
        request=QuickCodeRequest(
            prompt="sum a list of integers", language="python",
            max_refine=0, use_mesh=False,  # keep flow simple
        ),
        on_event=on_event,
        llm_call=_RouterLLM(),
        sandbox=_ScriptedSandbox(),
        static_harness=_StaticHarness(),
    )
    bundle = await engine.run()

    # Reactor phase emitted its start + complete events in order.
    starts = [e["phase"] for e in events
              if e.get("type") == "quick_code_phase_start"]
    assert "verify" in starts
    assert "reactor" in starts
    assert starts.index("reactor") > starts.index("verify")

    # Bundle carries the reactor envelope.
    assert bundle.reactor_bundle is not None
    assert "symbolic" in bundle.reactor_bundle
    assert "benchmark" in bundle.reactor_bundle
    assert "property_tests" in bundle.reactor_bundle
    assert "config" in bundle.reactor_bundle

    # A reactor gate was appended.
    assert any(g.phase == "reactor" for g in bundle.gates)


@pytest.mark.asyncio
async def test_reactor_disabled_via_settings_skips_phase(monkeypatch):
    from document_processor.config import settings as settings_mod
    monkeypatch.setattr(
        settings_mod.settings, "code_reactor_enabled", False,
    )
    events: list[dict[str, Any]] = []

    async def on_event(e):
        events.append(e)

    engine = QuickCodeEngine(
        session_id="r10b",
        request=QuickCodeRequest(prompt="x", max_refine=0, use_mesh=False),
        on_event=on_event,
        llm_call=_RouterLLM(),
        sandbox=_ScriptedSandbox(),
        static_harness=_StaticHarness(),
    )
    bundle = await engine.run()

    # No reactor phase.
    starts = [e["phase"] for e in events
              if e.get("type") == "quick_code_phase_start"]
    assert "reactor" not in starts
    assert bundle.reactor_bundle is None


@pytest.mark.asyncio
async def test_reactor_no_code_skips_phase():
    """If the implement phase produced no code (which shouldn't happen
    in practice, but is fail-soft territory), the reactor must skip."""
    # Use an LLM that returns no code from CoderAgent.
    class _NoCodeLLM(_RouterLLM):
        async def __call__(self, prompt, system, max_tokens):
            s = (system or "").lower()
            if "coder agent" in s:
                return ""  # CoderAgent will error out
            return await super().__call__(prompt, system, max_tokens)

    engine = QuickCodeEngine(
        session_id="r10c",
        request=QuickCodeRequest(prompt="x", max_refine=0, use_mesh=False),
        llm_call=_NoCodeLLM(),
        sandbox=_ScriptedSandbox(),
        static_harness=_StaticHarness(),
    )
    bundle = await engine.run()
    # CoderAgent failed → engine raised → bundle.code is None →
    # reactor never ran. Bundle still complete.
    assert bundle.reactor_bundle is None
