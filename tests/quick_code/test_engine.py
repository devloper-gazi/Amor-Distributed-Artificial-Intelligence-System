"""
Unit tests for QuickCodeEngine — phase ordering, gate scoring,
malformed-JSON fallback, refine triggers, cancellation. The LLM,
sandbox, and static-analysis harness are mocked at construction time
so the test runs in milliseconds and never hits Ollama / Docker.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from document_processor.quick_code import (
    QuickCodeAlternative,
    QuickCodeBundle,
    QuickCodeEngine,
    QuickCodeRequest,
)


# ─── Fakes ─────────────────────────────────────────────────────────


class FakeLLM:
    """Role-keyed canned-response LLM. Each call increments call_count
    so tests can assert how many times each role was hit."""

    def __init__(self, responses: dict[str, list[str]]):
        # responses: {"reasoner": ["..."], "coder": ["...", "..."], ...}
        self._responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[tuple[str, str]] = []  # (system_excerpt, prompt_excerpt)
        self._role_seq: list[str] = []  # Set by _set_role spy if used

    async def __call__(self, prompt: str, system: str | None, max_tokens: int) -> str:
        # Substring-match against the agent system prompts. Order
        # matters — debugger before coder, tester before triage, etc.
        sys_low = (system or "").lower()
        if "reasoning agent" in sys_low:
            role = "reasoner"
        elif "triage classifier" in sys_low:
            role = "triage"
        elif "debugger agent" in sys_low:
            role = "debugger"
        elif "tester agent" in sys_low or "qa engineer" in sys_low:
            role = "tester"
        elif "coder agent" in sys_low:
            role = "coder"
        else:
            role = "other"
        self.calls.append((role, prompt[:80]))
        bucket = self._responses.get(role)
        if not bucket:
            # Try a generic fallback bucket.
            bucket = self._responses.get("default") or []
        if bucket:
            return bucket.pop(0) if len(bucket) > 1 else bucket[0]
        return "{}"


class FakeSandbox:
    """ExecutionSandbox stand-in. Returns the dict the engine expects."""

    def __init__(self, *, success: bool = True, skipped: bool = False,
                 stderr: str = "", exit_code: int = 0):
        self._success = success
        self._skipped = skipped
        self._stderr = stderr
        self._exit_code = exit_code
        self.execute_calls = 0

    async def execute(self, code: str, language: str = "python", timeout: int = 30):
        self.execute_calls += 1

        class _R:
            def __init__(s):
                s.exit_code = self._exit_code
                s.stdout = ""
                s.stderr = self._stderr
                s.timed_out = False
                s.error = None
                s.duration_ms = 12
                s.language = language
                s.skipped = self._skipped

            @property
            def success(self):
                if self.skipped:
                    return True
                return self.exit_code == 0

            def to_dict(self):
                return {
                    "exit_code": self.exit_code, "stdout": s.stdout,
                    "stderr": s.stderr, "timed_out": s.timed_out,
                    "error": s.error, "duration_ms": s.duration_ms,
                    "language": s.language, "skipped": s.skipped,
                    "success": s.success,
                }

        s = _R()
        return s


class FakeStaticHarness:
    """StaticAnalysisHarness stand-in."""

    def __init__(self, *, errors: int = 0, security: int = 0):
        self._errors = errors
        self._security = security

    async def analyze(self, code: str, language: str = "python"):
        errors_self = self._errors
        security_self = self._security

        class _R:
            def to_dict(self):
                return {
                    "language": language,
                    "issues": [],
                    "complexity_score": 1.0,
                    "maintainability_index": 80.0,
                    "lines_of_code": len(code.splitlines()),
                    "syntax_valid": True,
                    "syntax_error": None,
                    "ast_summary": {},
                    "severity_counts": {
                        "error": errors_self, "warning": 0,
                        "info": 0, "security": security_self,
                    },
                }

        return _R()


def _reasoning_json(alts: list[dict[str, Any]] | None = None,
                    chosen: str = "A",
                    rationale: str = "A is simplest with strong math soundness "
                                     "and good edge case coverage; performance is "
                                     "acceptable for the expected input size.") -> str:
    if alts is None:
        alts = [
            {"label": "A", "summary": "iterative",
             "scores": {"clarity": 0.85, "math_soundness": 0.9,
                        "performance": 0.7, "edge_cases": 0.8},
             "complexity_estimate": "O(n)", "perf_notes": "fits in cache"},
            {"label": "B", "summary": "recursive",
             "scores": {"clarity": 0.6, "math_soundness": 0.8,
                        "performance": 0.5, "edge_cases": 0.6},
             "complexity_estimate": "O(n)", "perf_notes": "stack overflow risk"},
        ]
    return json.dumps({
        "alternatives": alts, "chosen": chosen, "rationale": rationale,
    })


def _coder_response(code: str = "print('hi')\n", language: str = "python") -> str:
    return f"```{language}\n{code}```\n```json\n" + json.dumps({
        "language": language, "filename": "main.py",
        "dependencies": [], "changes": "initial implementation",
    }) + "\n```"


def _tester_response(tests: str = "def test_hi():\n    assert 1\n",
                     language: str = "python") -> str:
    return f"```{language}\n{tests}```\n```json\n" + json.dumps({
        "language": language, "framework": "pytest",
        "test_count": 1, "coverage_estimate": "100%",
        "critical_cases": ["happy path"],
    }) + "\n```"


def _debugger_response(code: str = "print('fixed')\n") -> str:
    return f"```python\n{code}```\n```json\n" + json.dumps({
        "language": "python", "root_cause": "off-by-one",
        "fix_description": "fixed it", "lines_changed": 1,
        "confidence": "high",
    }) + "\n```"


def _triage_response(language: str = "python", needs_tests: bool = True) -> str:
    return json.dumps({
        "task_type": "generation", "language": language,
        "complexity": "moderate",
        "needs_execution": True, "needs_tests": needs_tests,
        "estimated_phases": ["plan", "code", "test"],
    })


# ─── Phase ordering ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_emits_phases_in_order():
    """The five phase_start events must fire in order:
    triage → reason → implement → verify → (refine if triggered)."""
    events: list[dict[str, Any]] = []
    llm = FakeLLM({
        "triage": [_triage_response()],
        "reasoner": [_reasoning_json()],
        "coder": [_coder_response()],
        "tester": [_tester_response()],
    })
    engine = QuickCodeEngine(
        session_id="t1",
        request=QuickCodeRequest(prompt="hello", language="python",
                                 max_refine=0),
        on_event=lambda e: events.append(e) or asyncio.sleep(0),
        llm_call=llm,
        sandbox=FakeSandbox(success=True),
        static_harness=FakeStaticHarness(errors=0),
    )
    bundle = await engine.run()
    assert isinstance(bundle, QuickCodeBundle)

    starts = [e["phase"] for e in events
              if e.get("type") == "quick_code_phase_start"]
    # No refine phase because max_refine=0 AND verification was clean.
    assert starts == ["triage", "reason", "implement", "verify"]
    completes = [e["phase"] for e in events
                 if e.get("type") == "quick_code_phase_complete"]
    assert completes == ["triage", "reason", "implement", "verify"]

    # Every event has a unique event_id.
    ids = {e["event_id"] for e in events}
    assert len(ids) == len(events)


@pytest.mark.asyncio
async def test_completed_event_summarizes_gates():
    events: list[dict[str, Any]] = []
    llm = FakeLLM({
        "triage": [_triage_response()],
        "reasoner": [_reasoning_json()],
        "coder": [_coder_response()],
        "tester": [_tester_response()],
    })
    engine = QuickCodeEngine(
        session_id="t2",
        request=QuickCodeRequest(prompt="x", max_refine=0),
        on_event=lambda e: events.append(e) or asyncio.sleep(0),
        llm_call=llm, sandbox=FakeSandbox(), static_harness=FakeStaticHarness(),
    )
    bundle = await engine.run()
    completed = [e for e in events if e["type"] == "quick_code_completed"]
    assert len(completed) == 1
    # gates list is in the final event so an SSE consumer doesn't have
    # to assemble it from individual gate events.
    assert isinstance(completed[0]["gates"], list)
    assert {g["phase"] for g in completed[0]["gates"]} >= {"reason", "verify"}
    # bundle gates match the event gates.
    assert len(bundle.gates) == len(completed[0]["gates"])


# ─── Reasoning JSON fallback ───────────────────────────────────────


@pytest.mark.asyncio
async def test_reasoning_malformed_json_falls_back_to_single_path():
    llm = FakeLLM({
        "triage": [_triage_response()],
        "reasoner": ["not json at all <<<"],
        "coder": [_coder_response()],
        "tester": [_tester_response()],
    })
    engine = QuickCodeEngine(
        session_id="t3",
        request=QuickCodeRequest(prompt="x", max_refine=0),
        llm_call=llm, sandbox=FakeSandbox(), static_harness=FakeStaticHarness(),
    )
    bundle = await engine.run()
    assert bundle.reasoning is not None
    # Fallback synthesizes exactly one alternative with all 0.5 scores.
    assert len(bundle.reasoning.alternatives) == 1
    assert bundle.reasoning.alternatives[0].composite == pytest.approx(0.5)
    # Finding tells the user we degraded.
    assert any("malformed" in f or "degraded" in f
               for f in bundle.reasoning.findings)
    # Gate is passed_warn (60–79), not failed.
    reason_gate = next(g for g in bundle.gates if g.phase == "reason")
    assert reason_gate.status == "passed_warn"


@pytest.mark.asyncio
async def test_reasoning_engine_overrides_chosen_when_composite_disagrees():
    """The model picks 'A' but B has the higher composite score → engine
    must override and add an audit line to findings."""
    alts = [
        {"label": "A",
         "scores": {"clarity": 0.4, "math_soundness": 0.4,
                    "performance": 0.4, "edge_cases": 0.4}},
        {"label": "B",
         "scores": {"clarity": 0.9, "math_soundness": 0.9,
                    "performance": 0.9, "edge_cases": 0.9}},
    ]
    llm = FakeLLM({
        "triage": [_triage_response()],
        "reasoner": [_reasoning_json(alts=alts, chosen="A")],
        "coder": [_coder_response()],
        "tester": [_tester_response()],
    })
    engine = QuickCodeEngine(
        session_id="t4",
        request=QuickCodeRequest(prompt="x", max_refine=0),
        llm_call=llm, sandbox=FakeSandbox(), static_harness=FakeStaticHarness(),
    )
    bundle = await engine.run()
    assert bundle.reasoning is not None
    assert bundle.reasoning.chosen_label == "B"  # engine overrode
    assert any("override" in f.lower() for f in bundle.reasoning.findings)


# ─── Gate scoring boundary cases ───────────────────────────────────


def test_reason_gate_passed_when_full_payload():
    engine = QuickCodeEngine(
        session_id="g1",
        request=QuickCodeRequest(prompt="x"),
        llm_call=FakeLLM({}),
    )
    from document_processor.quick_code.models import QuickCodeReasoning
    r = QuickCodeReasoning(
        alternatives=[
            QuickCodeAlternative(label="A",
                scores={"clarity": 1.0, "math_soundness": 1.0,
                        "performance": 1.0, "edge_cases": 1.0}),
            QuickCodeAlternative(label="B",
                scores={"clarity": 1.0, "math_soundness": 1.0,
                        "performance": 1.0, "edge_cases": 1.0}),
        ],
        chosen_label="A",
        rationale="x" * 100,  # >= 80 chars
    )
    g = engine._gate_reasoning(r)
    # base 60 + 15 (≥2 alts) + 10 (all axes) + 5 (long rationale) = 90 → passed
    assert g.score == pytest.approx(90.0)
    assert g.status == "passed"


def test_reason_gate_passed_warn_when_minimal():
    engine = QuickCodeEngine(
        session_id="g2",
        request=QuickCodeRequest(prompt="x"),
        llm_call=FakeLLM({}),
    )
    from document_processor.quick_code.models import QuickCodeReasoning
    r = QuickCodeReasoning(
        alternatives=[QuickCodeAlternative(label="A",
                                            scores={"clarity": 0.5})],
        chosen_label="A", rationale="short",
    )
    g = engine._gate_reasoning(r)
    # base 60, no bonuses → 60 → passed_warn (>=60 < 80)
    assert g.score == pytest.approx(60.0)
    assert g.status == "passed_warn"


def test_verify_gate_failed_when_exec_failed_and_no_refine_budget():
    engine = QuickCodeEngine(
        session_id="g3",
        request=QuickCodeRequest(prompt="x", max_refine=0),
        llm_call=FakeLLM({}),
    )
    from document_processor.quick_code.models import QuickCodeVerification
    v = QuickCodeVerification(
        execution={"success": False, "skipped": False,
                   "exit_code": 1, "stderr": "boom"},
        static={"severity_counts": {"error": 1}},
        score=40.0,
        severities={"error": 1},
    )
    g = engine._gate_verification(v)
    assert g.status == "failed"


def test_verify_gate_passed_warn_when_exec_skipped():
    """Skipped execution (Docker missing) is neutral, not a failure."""
    engine = QuickCodeEngine(
        session_id="g4",
        request=QuickCodeRequest(prompt="x", max_refine=0),
        llm_call=FakeLLM({}),
    )
    from document_processor.quick_code.models import QuickCodeVerification
    v = QuickCodeVerification(
        execution={"success": True, "skipped": True, "exit_code": 0},
        static={"severity_counts": {"error": 0}},
        score=70.0,
        severities={},
    )
    g = engine._gate_verification(v)
    assert g.status == "passed_warn"
    assert any("skipped" in f for f in g.findings)


# ─── Refine triggers ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refine_skipped_when_verification_clean():
    """No exec failure + 0 critical static issues → refine never runs
    even though max_refine > 0."""
    events: list[dict[str, Any]] = []
    llm = FakeLLM({
        "triage": [_triage_response()],
        "reasoner": [_reasoning_json()],
        "coder": [_coder_response()],
        "tester": [_tester_response()],
    })
    engine = QuickCodeEngine(
        session_id="r1",
        request=QuickCodeRequest(prompt="x", max_refine=2),
        on_event=lambda e: events.append(e) or asyncio.sleep(0),
        llm_call=llm, sandbox=FakeSandbox(success=True),
        static_harness=FakeStaticHarness(errors=0),
    )
    bundle = await engine.run()
    starts = [e["phase"] for e in events
              if e.get("type") == "quick_code_phase_start"]
    assert "refine" not in starts
    assert bundle.refine_iterations == 0


@pytest.mark.asyncio
async def test_refine_triggers_when_exec_fails():
    """First verify fails → refine runs once. After debugger fixes the
    code the second verify is clean, so no further iterations."""
    events: list[dict[str, Any]] = []
    llm = FakeLLM({
        "triage": [_triage_response()],
        "reasoner": [_reasoning_json()],
        "coder": [_coder_response()],
        "tester": [_tester_response()],
        "debugger": [_debugger_response()],
    })
    # Sandbox that fails the first time, succeeds the second.
    class FlipResult:
        def __init__(self, success: bool, language: str):
            self._success = success
            self.exit_code = 0 if success else 1
            self.stdout = ""
            self.stderr = "" if success else "boom"
            self.timed_out = False
            self.error = None
            self.duration_ms = 5
            self.language = language
            self.skipped = False

        @property
        def success(self):
            return self._success

        def to_dict(self):
            return {
                "exit_code": self.exit_code, "stdout": self.stdout,
                "stderr": self.stderr, "timed_out": self.timed_out,
                "error": self.error, "duration_ms": self.duration_ms,
                "language": self.language, "skipped": self.skipped,
                "success": self.success,
            }

    class FlipSandbox:
        def __init__(self):
            self.calls = 0

        async def execute(self, code, language="python", timeout=30):
            self.calls += 1
            return FlipResult(success=self.calls > 1, language=language)

    sb = FlipSandbox()
    engine = QuickCodeEngine(
        session_id="r2",
        request=QuickCodeRequest(prompt="x", max_refine=2),
        on_event=lambda e: events.append(e) or asyncio.sleep(0),
        llm_call=llm, sandbox=sb,
        static_harness=FakeStaticHarness(errors=0),
    )
    bundle = await engine.run()
    starts = [e["phase"] for e in events
              if e.get("type") == "quick_code_phase_start"]
    assert "refine" in starts
    assert bundle.refine_iterations == 1
    # Refine iteration event was emitted with improved=True.
    iter_events = [e for e in events if e.get("type") == "quick_code_refine_iteration"]
    assert len(iter_events) == 1
    assert iter_events[0]["improved"] is True


@pytest.mark.asyncio
async def test_refine_caps_at_max_refine():
    """Sandbox always fails — refine should still stop after
    request.max_refine iterations rather than loop forever."""
    events: list[dict[str, Any]] = []
    llm = FakeLLM({
        "triage": [_triage_response()],
        "reasoner": [_reasoning_json()],
        "coder": [_coder_response()],
        "tester": [_tester_response()],
        "debugger": [_debugger_response(), _debugger_response(), _debugger_response()],
    })
    engine = QuickCodeEngine(
        session_id="r3",
        request=QuickCodeRequest(prompt="x", max_refine=2),
        on_event=lambda e: events.append(e) or asyncio.sleep(0),
        llm_call=llm,
        sandbox=FakeSandbox(success=False, exit_code=1, stderr="still broken"),
        static_harness=FakeStaticHarness(errors=0),
    )
    bundle = await engine.run()
    iter_events = [e for e in events if e.get("type") == "quick_code_refine_iteration"]
    assert len(iter_events) == 2  # exactly max_refine, no more
    assert bundle.refine_iterations == 2


@pytest.mark.asyncio
async def test_refine_disabled_when_max_refine_exceeds_cap():
    """User asks for max_refine=99 → normalize() clamps to 3 → refine
    runs at most 3 iterations even with persistent failures."""
    events: list[dict[str, Any]] = []
    llm = FakeLLM({
        "triage": [_triage_response()],
        "reasoner": [_reasoning_json()],
        "coder": [_coder_response()],
        "tester": [_tester_response()],
        "debugger": [_debugger_response()] * 10,
    })
    engine = QuickCodeEngine(
        session_id="r4",
        request=QuickCodeRequest(prompt="x", max_refine=99),
        on_event=lambda e: events.append(e) or asyncio.sleep(0),
        llm_call=llm,
        sandbox=FakeSandbox(success=False, exit_code=1),
        static_harness=FakeStaticHarness(errors=0),
    )
    bundle = await engine.run()
    iter_events = [e for e in events if e.get("type") == "quick_code_refine_iteration"]
    # Cap from MAX_REFINE_ITERATIONS = 3.
    assert len(iter_events) == 3
    assert bundle.refine_iterations == 3


# ─── Cancellation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_short_circuits_remaining_phases():
    """A cancel flag set before run() yields a cancelled event and
    skips reason/implement/verify entirely."""
    events: list[dict[str, Any]] = []
    llm = FakeLLM({
        "triage": [_triage_response()],
    })
    engine = QuickCodeEngine(
        session_id="c1",
        request=QuickCodeRequest(prompt="x", cancel_requested=True),
        on_event=lambda e: events.append(e) or asyncio.sleep(0),
        llm_call=llm, sandbox=FakeSandbox(),
        static_harness=FakeStaticHarness(),
    )
    bundle = await engine.run()
    assert any(e["type"] == "quick_code_cancelled" for e in events)
    starts = [e["phase"] for e in events
              if e.get("type") == "quick_code_phase_start"]
    # cancel_requested fires in _phase_triage's _check_cancel before
    # any phase emits its start event.
    assert starts == []
    assert "Cancelled" in bundle.deliverable_markdown


# ─── Triage language honour ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_explicit_language_overrides_triage():
    """If the user passed `language='go'`, triage's `language='python'`
    must lose."""
    llm = FakeLLM({
        "triage": [_triage_response(language="python")],
        "reasoner": [_reasoning_json()],
        "coder": [_coder_response(language="go", code="package main\n")],
        "tester": [_tester_response(language="go", tests="package main\n")],
    })
    engine = QuickCodeEngine(
        session_id="lang1",
        request=QuickCodeRequest(prompt="x", language="go", max_refine=0),
        llm_call=llm, sandbox=FakeSandbox(),
        static_harness=FakeStaticHarness(),
    )
    bundle = await engine.run()
    assert bundle.triage["language"] == "go"
    art = bundle.to_implementation_artifact()
    assert art.language == "go"


# ─── Constructor validation ─────────────────────────────────────────


def test_constructor_rejects_empty_prompt():
    with pytest.raises(ValueError):
        QuickCodeEngine(
            session_id="e1",
            request=QuickCodeRequest(prompt="   "),
            llm_call=FakeLLM({}),
        )


def test_constructor_assigns_session_id_when_missing():
    engine = QuickCodeEngine(
        request=QuickCodeRequest(prompt="x"),
        llm_call=FakeLLM({}),
    )
    assert engine.session_id  # non-empty hex


# ─── Deliverable markdown ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliverable_markdown_lists_alternatives_with_scores():
    llm = FakeLLM({
        "triage": [_triage_response()],
        "reasoner": [_reasoning_json()],
        "coder": [_coder_response()],
        "tester": [_tester_response()],
    })
    engine = QuickCodeEngine(
        session_id="d1",
        request=QuickCodeRequest(prompt="hello world",
                                 max_refine=0),
        llm_call=llm, sandbox=FakeSandbox(),
        static_harness=FakeStaticHarness(),
    )
    bundle = await engine.run()
    md = bundle.deliverable_markdown
    assert md.startswith("# ")
    assert "## Reasoning" in md
    assert "composite" in md
    assert "← chosen" in md
    assert "## Verification" in md
    assert "## Phase gates" in md
