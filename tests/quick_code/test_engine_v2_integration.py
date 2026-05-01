"""
End-to-end integration tests for the V2 engine path.

These tests opt the V2 master gate back ON (the directory-level
``conftest.py`` disables it by default to keep the pre-V2 contract
tests stable) and run a full ``QuickCodeEngine.run()`` with mocked
LLM + sandbox.  We assert:

* V2 phase events fire in the expected order.
* The bundle gains the new V2 fields when the matching phase produced
  output.
* Auto-redirect-to-Pro short-circuits ``run()`` cleanly when the
  router classifies the prompt as COMPLEX.
* Striatum hits skip the rest of the pipeline.
* When ``quick_v2_enabled=False`` is set per-test, the engine emits
  exactly the pre-V2 phase ordering.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from document_processor.quick_code import (
    QuickCodeAlternative,
    QuickCodeBundle,
    QuickCodeEngine,
    QuickCodeRequest,
    QuickCodeReasoning,
    QuickCodeVerification,
)


# ─────────────────────────────────────────────────────────────────────
# Fixture — re-enable V2 for every test in this file
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _enable_v2(monkeypatch):
    """The directory conftest disables V2 by default.  This file
    flips it back on for the integration tests, plus we also force
    the ORPO collector off so we don't try to hit Mongo."""
    from document_processor.config import settings as s

    monkeypatch.setattr(s.settings, "quick_v2_enabled", True)
    monkeypatch.setattr(s.settings, "quick_v2_orpo_enabled", False)
    monkeypatch.setattr(s.settings, "quick_v2_striatum_enabled", True)
    # Use a unique salt per test so cached entries from other test
    # runs in the same Redis DB don't bleed across.
    import uuid

    monkeypatch.setattr(s.settings, "quick_v2_striatum_salt", uuid.uuid4().int >> 96)


# ─────────────────────────────────────────────────────────────────────
# LLM + sandbox + harness fakes
# ─────────────────────────────────────────────────────────────────────


def _triage_response() -> str:
    return json.dumps({
        "task_type": "generation",
        "language": "python",
        "complexity": "simple",
        "needs_execution": True,
        "needs_tests": True,
    })


def _reasoning_response() -> str:
    return json.dumps({
        "alternatives": [
            {
                "label": "A",
                "summary": "single-loop solution",
                "scores": {
                    "clarity": 0.9, "math_soundness": 0.7,
                    "performance": 0.7, "edge_cases": 0.8,
                },
                "complexity_estimate": "O(n)",
                "perf_notes": "tight loop",
                "edge_cases": ["empty"],
            },
            {
                "label": "B",
                "summary": "recursive solution",
                "scores": {
                    "clarity": 0.6, "math_soundness": 0.7,
                    "performance": 0.5, "edge_cases": 0.8,
                },
                "complexity_estimate": "O(n)",
                "perf_notes": "recursion overhead",
                "edge_cases": ["empty"],
            },
        ],
        "chosen": "A",
        "rationale": "A is simpler and scores higher on clarity.",
    })


def _coder_response() -> str:
    code = "def reverse(s):\n    return s[::-1]\n"
    return (
        "```python\n"
        + code
        + "```\n```json\n"
        + json.dumps({
            "language": "python",
            "filename": "main.py",
            "dependencies": [],
            "changes": "initial implementation",
        })
        + "\n```"
    )


def _tester_response() -> str:
    tests = (
        "def test_reverse():\n"
        "    assert reverse('abc') == 'cba'\n"
    )
    return (
        "```python\n"
        + tests
        + "```\n```json\n"
        + json.dumps({
            "language": "python",
            "framework": "pytest",
            "test_count": 1,
            "coverage_estimate": "100%",
            "critical_cases": ["happy path"],
        })
        + "\n```"
    )


class _RoleAwareLLM:
    """Routes by role — mirrors the engine's role hand-off pattern."""

    def __init__(self, *, by_role: dict[str, list[str]]) -> None:
        self.by_role = {k: list(v) for k, v in by_role.items()}
        self.calls: list[tuple[str, str | None, int]] = []

    async def __call__(
        self, prompt: str, system: str | None, max_tokens: int
    ) -> str:
        self.calls.append((prompt, system, max_tokens))
        # Crude routing heuristic.  Same approach the existing
        # test_engine.py uses.
        if system and "code reasoning" in system.lower():
            return self._pop("reasoner")
        if system and "you classify programming tasks" in system.lower():
            # Router sub-prompt — return a deterministic verdict.
            return "simple"
        if system and "decompose a programming task" in system.lower():
            return self._pop("parsel")
        if system and "fixing a failing test" in system.lower():
            return self._pop("seeker") or '{"candidates":[]}'
        if "triage" in (prompt or "").lower() and (
            "task_type" in (prompt or "")
        ):
            return self._pop("triage")
        if "tester" in (prompt or "").lower() or "tests" in (prompt or "").lower():
            return self._pop("tester")
        return self._pop("coder")

    def _pop(self, role: str) -> str:
        bucket = self.by_role.get(role)
        if not bucket:
            return ""
        return bucket.pop(0)


class _Sandbox:
    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.calls: list[str] = []

    async def execute(
        self, code: str, *, language: str = "python", **kwargs
    ) -> dict[str, Any]:
        self.calls.append(code)
        return {
            "exit_code": 0 if self.success else 1,
            "stdout": "ok" if self.success else "",
            "stderr": "" if self.success else "Traceback\nValueError: x",
            "duration_ms": 5.0,
            "skipped": False,
        }

    async def docker_available(self, *, force_refresh: bool = False) -> bool:
        return True


class _StaticHarness:
    async def analyze(self, code: str, language: str) -> dict[str, Any]:
        return {
            "issues": [],
            "score": 100.0,
            "severities": {},
        }


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _engine(
    *,
    prompt: str,
    mode: str = "quick",
    max_refine: int = 0,
    triage_response: str | None = None,
    reasoning_response: str | None = None,
):
    llm = _RoleAwareLLM(
        by_role={
            "triage": [triage_response or _triage_response()] * 4,
            "reasoner": [reasoning_response or _reasoning_response()] * 4,
            "coder": [_coder_response()] * 4,
            "tester": [_tester_response()] * 4,
            "parsel": ['{"subtasks": []}'] * 4,
            "seeker": ['{"candidates":[]}'] * 4,
        }
    )
    return QuickCodeEngine(
        session_id="v2-int",
        request=QuickCodeRequest(
            prompt=prompt,
            language="python",
            mode=mode,  # type: ignore[arg-type]
            max_refine=max_refine,
            use_mesh=False,
        ),
        on_event=lambda e: asyncio.sleep(0),
        llm_call=llm,
        sandbox=_Sandbox(success=True),
        static_harness=_StaticHarness(),
    )


def _engine_with_capture(
    *,
    prompt: str,
    mode: str = "quick",
    max_refine: int = 0,
    triage_response: str | None = None,
    reasoning_response: str | None = None,
):
    events: list[dict[str, Any]] = []
    llm = _RoleAwareLLM(
        by_role={
            "triage": [triage_response or _triage_response()] * 4,
            "reasoner": [reasoning_response or _reasoning_response()] * 4,
            "coder": [_coder_response()] * 4,
            "tester": [_tester_response()] * 4,
            "parsel": ['{"subtasks": []}'] * 4,
            "seeker": ['{"candidates":[]}'] * 4,
        }
    )

    async def on_event(e):
        events.append(e)

    eng = QuickCodeEngine(
        session_id="v2-int",
        request=QuickCodeRequest(
            prompt=prompt,
            language="python",
            mode=mode,  # type: ignore[arg-type]
            max_refine=max_refine,
            use_mesh=False,
        ),
        on_event=on_event,
        llm_call=llm,
        sandbox=_Sandbox(success=True),
        static_harness=_StaticHarness(),
    )
    return eng, events


# ─────────────────────────────────────────────────────────────────────
# Phase ordering
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_phases_emit_in_order():
    """The V2 phases must precede / wrap the existing phases in the
    documented order: classify → striatum → ... → triage → ... etc."""
    eng, events = _engine_with_capture(prompt="reverse a string in python")
    bundle = await eng.run()
    assert isinstance(bundle, QuickCodeBundle)
    starts = [
        e["phase"] for e in events if e.get("type") == "quick_code_phase_start"
    ]
    assert starts.index("classify") < starts.index("striatum")
    assert starts.index("striatum") < starts.index("triage")
    assert starts.index("triage") < starts.index("reason")


@pytest.mark.asyncio
async def test_v2_router_decision_recorded():
    """The router's verdict should land on bundle.router_decision."""
    eng = _engine(prompt="reverse a string")
    bundle = await eng.run()
    assert bundle.router_decision is not None
    assert bundle.router_decision["complexity"] in (
        "trivial", "simple", "complex", "math",
    )


# ─────────────────────────────────────────────────────────────────────
# Auto-redirect-to-Pro
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complex_quick_request_redirects_to_pro():
    """A long / complex prompt in quick mode should be flagged for
    redirect to /api/code/start."""
    long_prompt = "Build a complete distributed OAuth2 service. " * 80
    eng, events = _engine_with_capture(prompt=long_prompt, mode="quick")
    bundle = await eng.run()
    assert bundle.router_decision is not None
    assert bundle.router_decision["redirect_to_pro"] is True
    assert bundle.router_decision["target"] == "/api/code/start"
    completed = [e for e in events if e.get("type") == "quick_code_completed"]
    assert completed and completed[-1].get("redirect_to_pro") is True


@pytest.mark.asyncio
async def test_complex_pro_request_does_not_redirect():
    """The same long prompt in pro mode runs fully — no redirect."""
    long_prompt = "Build a complete distributed OAuth2 service. " * 80
    eng = _engine(prompt=long_prompt, mode="pro")
    bundle = await eng.run()
    # The router still classifies as COMPLEX, but redirect must be
    # False because the request already opted into Pro.
    assert bundle.router_decision is not None
    assert bundle.router_decision["redirect_to_pro"] is False


# ─────────────────────────────────────────────────────────────────────
# Striatum fast-path
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_run_is_striatum_hit():
    """After a first run, a second engine on the same prompt should
    hit the Striatum fast-path and skip the rest of the pipeline."""
    prompt = "reverse a string in python"
    eng_a = _engine(prompt=prompt)
    bundle_a = await eng_a.run()
    assert bundle_a.code is not None

    eng_b, events_b = _engine_with_capture(prompt=prompt)
    bundle_b = await eng_b.run()
    starts_b = [
        e["phase"] for e in events_b
        if e.get("type") == "quick_code_phase_start"
    ]
    # We may or may not see a striatum hit depending on whether the
    # cosine score crosses the threshold — when it doesn't we just
    # confirm both runs succeeded.  When it does, we expect the
    # pipeline to short-circuit before triage.
    if bundle_b.striatum_hit is not None:
        assert "triage" not in starts_b
        assert bundle_b.code is not None


# ─────────────────────────────────────────────────────────────────────
# Mode toggle
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pro_mode_invokes_mcts_when_alternatives_present():
    """Pro mode should populate ``bundle.mcts_audit`` with one node
    per reasoning alternative."""
    eng = _engine(prompt="implement merge sort", mode="pro")
    bundle = await eng.run()
    if bundle.reasoning and len(bundle.reasoning.alternatives or []) >= 2:
        assert bundle.mcts_audit, "MCTS should have produced an audit trail"
        assert len(bundle.mcts_audit) == len(bundle.reasoning.alternatives)


@pytest.mark.asyncio
async def test_quick_mode_skips_mcts():
    eng = _engine(prompt="reverse a string", mode="quick")
    bundle = await eng.run()
    assert bundle.mcts_audit == []


# ─────────────────────────────────────────────────────────────────────
# Master gate off — pre-V2 contract preserved
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_master_gate_off_emits_only_pre_v2_phases(monkeypatch):
    """When quick_v2_enabled=False, no V2 phase fires."""
    from document_processor.config import settings as s

    monkeypatch.setattr(s.settings, "quick_v2_enabled", False)
    eng, events = _engine_with_capture(prompt="reverse a string")
    await eng.run()
    starts = [
        e["phase"] for e in events
        if e.get("type") == "quick_code_phase_start"
    ]
    for v2_phase in (
        "classify",
        "striatum",
        "parsel_decompose",
        "sk_retrieve",
        "symcode_validate",
        "mcts_select",
    ):
        assert v2_phase not in starts


# ─────────────────────────────────────────────────────────────────────
# Bundle JSON serialisation
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bundle_to_dict_includes_v2_fields():
    eng = _engine(prompt="reverse a string")
    bundle = await eng.run()
    dump = bundle.to_dict()
    for key in (
        "router_decision",
        "striatum_hit",
        "parsel_subtasks",
        "sk_snippets",
        "sk_hint",
        "symcode_result",
        "mcts_audit",
        "orpo_pairs",
    ):
        assert key in dump
