"""
Tests for specialist reasoning agents — system-prompt routing,
JSON parse error handling, parallel runner timeout, and the
specialist→aggregator hand-off.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from document_processor.code_intelligence.mesh.specialists import (
    EdgeCaseReasonerAgent,
    GeneralReasonerAgent,
    MathReasonerAgent,
    PerformanceReasonerAgent,
    SpecialistOutput,
    _BaseSpecialist,
    run_specialists_parallel,
)


def _payload():
    return json.dumps({
        "alternatives": [
            {"label": "A", "summary": "x",
             "scores": {"clarity": 0.5, "math_soundness": 0.5,
                        "performance": 0.5, "edge_cases": 0.5},
             "complexity_estimate": "O(n)", "perf_notes": "ok",
             "edge_cases": []}
        ],
        "chosen": "A", "rationale": "ok",
    })


# ── per-role wiring ────────────────────────────────────────────────


def test_specialist_role_attributes_set_correctly():
    assert GeneralReasonerAgent.role == "general"
    assert MathReasonerAgent.role == "math"
    assert PerformanceReasonerAgent.role == "performance"
    assert EdgeCaseReasonerAgent.role == "edge_case"


def test_each_specialist_uses_distinct_system_prompt():
    """The whole mesh value comes from prompt-engineering each role's
    perspective. If two roles share a prompt, kill the duplicate."""
    seen: set[str] = set()
    for cls in (GeneralReasonerAgent, MathReasonerAgent,
                PerformanceReasonerAgent, EdgeCaseReasonerAgent):
        # Tiny instance to read the system_prompt property.
        async def _llm(*a, **kw): return ""
        inst = cls(_llm)  # type: ignore[arg-type]
        assert inst.system_prompt not in seen, (
            f"{cls.__name__} shares system_prompt with another specialist"
        )
        seen.add(inst.system_prompt)


def test_each_specialist_uses_distinct_routing_key():
    seen: set[str] = set()
    for cls in (GeneralReasonerAgent, MathReasonerAgent,
                PerformanceReasonerAgent, EdgeCaseReasonerAgent):
        async def _llm(*a, **kw): return ""
        inst = cls(_llm)  # type: ignore[arg-type]
        assert inst.routing_key not in seen
        seen.add(inst.routing_key)


# ── reason() happy + error paths ──────────────────────────────────


@pytest.mark.asyncio
async def test_reason_returns_parsed_payload():
    captured: dict[str, Any] = {}

    async def fake_llm(prompt, system, max_tokens):
        captured["system"] = system
        return _payload()

    spec = MathReasonerAgent(fake_llm)
    out = await spec.reason(
        user_prompt="implement softmax",
        triage={"language": "python", "complexity": "moderate"},
    )
    assert isinstance(out, SpecialistOutput)
    assert out.role == "math"
    assert "mathematics" in captured["system"].lower()
    assert out.parsed["chosen"] == "A"
    assert len(out.alternatives()) == 1


@pytest.mark.asyncio
async def test_reason_handles_llm_error():
    async def boom(prompt, system, max_tokens):
        raise RuntimeError("ollama down")

    spec = GeneralReasonerAgent(boom)
    out = await spec.reason(user_prompt="hi")
    assert out.error is not None
    assert "ollama down" in out.error
    assert out.alternatives() == []


@pytest.mark.asyncio
async def test_reason_handles_invalid_json():
    async def bad_json(prompt, system, max_tokens):
        return "not json — just prose"

    spec = PerformanceReasonerAgent(bad_json)
    out = await spec.reason(user_prompt="hi")
    assert out.error is not None
    assert "JSON parse" in out.error


@pytest.mark.asyncio
async def test_reason_calls_role_setter_with_routing_key():
    """role_setter must be called with the routing key (not the
    role-id). The routing layer uses these keys to look up per-role
    model preferences."""
    captured: list[str | None] = []

    def setter(role):
        captured.append(role)

    async def fake(prompt, system, max_tokens):
        return _payload()

    spec = EdgeCaseReasonerAgent(fake, role_setter=setter)
    await spec.reason(user_prompt="x")
    assert captured == ["edge_case_specialist"]


# ── parallel runner ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_specialists_parallel_returns_in_input_order():
    async def fake(prompt, system, max_tokens):
        # Different roles share the same FAKE response — the runner's
        # job is just to fan out + collect, not interpret.
        return _payload()

    specialists = [
        GeneralReasonerAgent(fake),
        MathReasonerAgent(fake),
        PerformanceReasonerAgent(fake),
        EdgeCaseReasonerAgent(fake),
    ]
    outputs = await run_specialists_parallel(
        specialists, user_prompt="x",
    )
    assert [o.role for o in outputs] == [
        "general", "math", "performance", "edge_case",
    ]


@pytest.mark.asyncio
async def test_run_specialists_parallel_isolates_failures():
    """A single specialist failure must not kill the rest."""
    async def fake_ok(prompt, system, max_tokens):
        return _payload()

    async def fake_fail(prompt, system, max_tokens):
        raise RuntimeError("boom")

    specialists = [
        GeneralReasonerAgent(fake_ok),
        MathReasonerAgent(fake_fail),
        PerformanceReasonerAgent(fake_ok),
    ]
    outputs = await run_specialists_parallel(
        specialists, user_prompt="x",
    )
    assert outputs[0].error is None
    assert outputs[1].error is not None
    assert outputs[2].error is None


@pytest.mark.asyncio
async def test_run_specialists_parallel_timeout_returns_error_outputs():
    """If the whole gather exceeds the timeout, every specialist
    returns a fresh SpecialistOutput with an error message — the
    pipeline never wedges on a single hang."""
    async def slow(prompt, system, max_tokens):
        await asyncio.sleep(10.0)  # > timeout
        return _payload()

    specialists = [
        GeneralReasonerAgent(slow),
        MathReasonerAgent(slow),
    ]
    outputs = await run_specialists_parallel(
        specialists, user_prompt="x", timeout_s=0.05,
    )
    assert len(outputs) == 2
    assert all(o.error and "timed out" in o.error for o in outputs)


@pytest.mark.asyncio
async def test_run_specialists_parallel_empty_list():
    outputs = await run_specialists_parallel([], user_prompt="x")
    assert outputs == []
