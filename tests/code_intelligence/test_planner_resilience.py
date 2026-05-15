"""
Tests for planner resilience (Cycle D Fix #6).

The user observed back-to-back planner failures wedge the entire
Build pipeline at the plan phase:
  - "could not parse model JSON: Expecting ',' delimiter"
  - "empty model output"

After this fix:
  - First parse failure → retry once with a stricter prompt suffix
  - Retry success → pipeline proceeds with the retry's plan
  - Retry failure → minimal-fallback plan keeps the pipeline alive
  - Empty output (raw == "") → minimal-fallback (no retry needed)
"""

from __future__ import annotations

import asyncio
import pytest

from document_processor.code_intelligence.agents import (
    PlannerAgent,
    AgentContext,
)


class _SequencedLLM:
    """Returns a sequence of pre-canned responses across consecutive calls."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[str] = []

    async def __call__(self, prompt: str, system: str, max_tokens: int) -> str:
        self.calls.append(prompt)
        if not self.responses:
            return ""
        return self.responses.pop(0)


_GOOD_PLAN_JSON = (
    '{"task_type": "generation", "language": "python", '
    '"complexity": "simple", "title": "fizzbuzz", '
    '"plan": [{"step": 1, "action": "write fizzbuzz", "agent": "coder", '
    '"description": "implement"}], '
    '"context_needed": [], "risks": [], '
    '"test_strategy": "unit", "deliverable_type": "code_snippet", '
    '"spec": {}}'
)

_MALFORMED_JSON = '{"task_type": "generation", "language": "python", broken'


# ─── Retry path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_parse_failure_triggers_retry_with_stricter_prompt():
    """User-reported case: first call returns malformed JSON → retry
    with an appended directive succeeds → pipeline proceeds normally."""

    llm = _SequencedLLM([_MALFORMED_JSON, _GOOD_PLAN_JSON])
    agent = PlannerAgent(llm_call=llm)
    out = await agent.run(AgentContext(
        user_prompt="fizzbuzz", triage={"language": "python"},
    ))
    assert out.error is None
    assert out.data["language"] == "python"
    assert out.data.get("_resilience_fallback") is not True
    assert len(llm.calls) == 2
    # Second call MUST include the stricter directive
    assert "valid JSON" in llm.calls[1]


@pytest.mark.asyncio
async def test_retry_failure_falls_back_to_minimal_plan():
    """Both attempts return garbage → fallback plan is used.
    Pipeline still proceeds (no error / no data=None)."""

    llm = _SequencedLLM([_MALFORMED_JSON, _MALFORMED_JSON])
    agent = PlannerAgent(llm_call=llm)
    out = await agent.run(AgentContext(
        user_prompt="fizzbuzz", triage={"language": "python"},
    ))
    assert out.error is None
    assert out.data is not None
    assert out.data.get("_resilience_fallback") is True
    assert out.data["language"] == "python"
    # Minimal plan has at least one step
    assert len(out.data["plan"]) >= 1


@pytest.mark.asyncio
async def test_empty_first_output_falls_through_via_retry():
    """User's second observed case: planner returns empty string.
    _extract_json raises → retry path.  Retry also empty → fallback."""

    llm = _SequencedLLM(["", ""])
    agent = PlannerAgent(llm_call=llm)
    out = await agent.run(AgentContext(
        user_prompt="x", triage={"language": "cpp"},
    ))
    assert out.error is None
    assert out.data.get("_resilience_fallback") is True
    # Triage language is honored in the fallback
    assert out.data["language"] == "cpp"


@pytest.mark.asyncio
async def test_retry_then_success_uses_retry_data():
    """Empty first call → retry with sharper prompt succeeds → that
    plan is used (NOT the fallback)."""

    llm = _SequencedLLM(["", _GOOD_PLAN_JSON])
    agent = PlannerAgent(llm_call=llm)
    out = await agent.run(AgentContext(
        user_prompt="x", triage={"language": "python"},
    ))
    assert out.error is None
    assert out.data.get("_resilience_fallback") is not True
    assert out.data["title"] == "fizzbuzz"


# ─── _minimal_fallback_plan deterministic shape ───────────────────


def test_minimal_fallback_plan_inherits_triage_language():
    plan = PlannerAgent._minimal_fallback_plan(AgentContext(
        user_prompt="build me a thing",
        triage={"language": "rust", "task_type": "generation"},
    ))
    assert plan["language"] == "rust"
    assert plan["task_type"] == "generation"
    assert plan["_resilience_fallback"] is True
    assert len(plan["plan"]) == 1


def test_minimal_fallback_plan_default_python_when_no_triage():
    plan = PlannerAgent._minimal_fallback_plan(AgentContext(
        user_prompt="x", triage=None,
    ))
    assert plan["language"] == "python"


def test_minimal_fallback_plan_truncates_long_prompt_in_title():
    long = "a" * 500
    plan = PlannerAgent._minimal_fallback_plan(AgentContext(
        user_prompt=long, triage={},
    ))
    assert len(plan["title"]) <= 80


# ─── Happy path unchanged ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_zero_retries_zero_fallback():
    """The common case: first call returns valid JSON.  No retry, no
    fallback flag, exactly one LLM call."""

    llm = _SequencedLLM([_GOOD_PLAN_JSON])
    agent = PlannerAgent(llm_call=llm)
    out = await agent.run(AgentContext(
        user_prompt="fizzbuzz", triage={"language": "python"},
    ))
    assert out.error is None
    assert out.data.get("_resilience_fallback") is not True
    assert len(llm.calls) == 1


# ─── Critic resilience (Cycle D Fix #6 second-half) ──────────────


from document_processor.code_intelligence.agents import CriticAgent


_GOOD_CRITIC_JSON = (
    '{"verdict": "approved", "score": 92, "strengths": ["clear"], '
    '"issues": [], "security_concerns": [], '
    '"performance_concerns": [], "final_comment": "good"}'
)


@pytest.mark.asyncio
async def test_critic_first_parse_failure_retries():
    llm = _SequencedLLM([_MALFORMED_JSON, _GOOD_CRITIC_JSON])
    agent = CriticAgent(llm_call=llm)
    out = await agent.run(AgentContext(
        user_prompt="x", code="int main(){}", language="cpp",
    ))
    assert out.error is None
    assert out.data["verdict"] == "approved"
    assert out.data.get("_resilience_fallback") is not True
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_critic_retry_failure_returns_neutral_review():
    """The user-observed case: critic emits empty + retry also fails.
    Pipeline gets a neutral 'approved_with_minor / 70' review so it
    can reach 'done' instead of erroring at the final phase."""

    llm = _SequencedLLM(["", ""])
    agent = CriticAgent(llm_call=llm)
    out = await agent.run(AgentContext(
        user_prompt="x", code="int main(){}", language="cpp",
    ))
    assert out.error is None
    assert out.data["verdict"] == "approved_with_minor"
    assert out.data["score"] == 70
    assert out.data.get("_resilience_fallback") is True
    assert "review unavailable" in out.data["final_comment"].lower() or \
           "automated" in out.data["final_comment"].lower()


@pytest.mark.asyncio
async def test_critic_happy_path_one_call():
    llm = _SequencedLLM([_GOOD_CRITIC_JSON])
    agent = CriticAgent(llm_call=llm)
    out = await agent.run(AgentContext(
        user_prompt="x", code="int main(){}", language="cpp",
    ))
    assert out.error is None
    assert out.data["verdict"] == "approved"
    assert out.data.get("_resilience_fallback") is not True
    assert len(llm.calls) == 1
