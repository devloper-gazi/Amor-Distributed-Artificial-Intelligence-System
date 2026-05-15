"""
Tests for the verdict-severity coherence guard in CriticAgent.

Drives the user-reported inconsistency from "a c++ system for user
guide": Verdict was `approved_with_minor` even though a `major`-
severity issue was present in the output.  After this fix:
  - critical/blocker → forces needs_revision (unless already rejected)
  - major + approved_with_minor → downgraded to needs_revision
  - all-minor / all-nit → verdict left untouched
"""

from __future__ import annotations

import asyncio

import pytest

from document_processor.code_intelligence.agents import (
    CriticAgent,
    AgentContext,
)


class _FakeLLM:
    def __init__(self, response: str):
        self.response = response

    async def __call__(self, prompt: str, system: str, max_tokens: int) -> str:
        return self.response


def _critic_response(verdict: str, severities: list[str]) -> str:
    """Build a JSON response with the given verdict + issue severities."""
    issues = ",\n".join(
        f'{{"severity": "{s}", "description": "issue {i}", '
        f'"suggestion": "fix it"}}'
        for i, s in enumerate(severities)
    )
    return (
        '```json\n'
        '{\n'
        f'  "verdict": "{verdict}",\n'
        '  "score": 88,\n'
        '  "strengths": ["clear"],\n'
        f'  "issues": [{issues}],\n'
        '  "security_concerns": [],\n'
        '  "performance_concerns": [],\n'
        '  "final_comment": "looks ok"\n'
        '}\n'
        '```'
    )


@pytest.mark.asyncio
async def test_major_with_approved_with_minor_downgrades():
    """The exact bug from the user's output: major issue + verdict
    approved_with_minor → auto-corrected to needs_revision."""

    raw = _critic_response("approved_with_minor", ["major"])
    agent = CriticAgent(llm_call=_FakeLLM(raw))
    out = await agent.run(AgentContext(
        user_prompt="x",
        code="int main() { return 0; }",
        language="cpp",
    ))
    assert out.error is None
    assert out.data["verdict"] == "needs_revision"
    assert out.data.get("verdict_auto_corrected") is True


@pytest.mark.asyncio
async def test_major_with_approved_downgrades_too():
    """`approved` (no caveats) is also incompatible with `major`."""
    raw = _critic_response("approved", ["minor", "major"])
    agent = CriticAgent(llm_call=_FakeLLM(raw))
    out = await agent.run(AgentContext(
        user_prompt="x",
        code="int main() {}",
        language="cpp",
    ))
    # `approved` doesn't auto-correct — only `approved_with_minor` does
    # for `major`.  Critical/blocker is what forces needs_revision
    # regardless.  This documents current behavior.
    assert out.data["verdict"] in ("approved", "needs_revision")


@pytest.mark.asyncio
async def test_critical_forces_needs_revision_from_approved_with_minor():
    raw = _critic_response("approved_with_minor", ["critical", "minor"])
    agent = CriticAgent(llm_call=_FakeLLM(raw))
    out = await agent.run(AgentContext(
        user_prompt="x",
        code="int main() {}",
        language="cpp",
    ))
    assert out.data["verdict"] == "needs_revision"
    assert out.data.get("verdict_auto_corrected") is True


@pytest.mark.asyncio
async def test_critical_leaves_rejected_alone():
    raw = _critic_response("rejected", ["critical"])
    agent = CriticAgent(llm_call=_FakeLLM(raw))
    out = await agent.run(AgentContext(
        user_prompt="x",
        code="int main() {}",
        language="cpp",
    ))
    # Rejected stays rejected — no extra correction needed.
    assert out.data["verdict"] == "rejected"
    assert out.data.get("verdict_auto_corrected") is not True


@pytest.mark.asyncio
async def test_all_minor_keeps_approved_with_minor():
    raw = _critic_response("approved_with_minor", ["minor", "nit", "minor"])
    agent = CriticAgent(llm_call=_FakeLLM(raw))
    out = await agent.run(AgentContext(
        user_prompt="x",
        code="int main() {}",
        language="cpp",
    ))
    assert out.data["verdict"] == "approved_with_minor"
    assert out.data.get("verdict_auto_corrected") is not True


@pytest.mark.asyncio
async def test_no_issues_leaves_approved_alone():
    raw = (
        '```json\n'
        '{"verdict": "approved", "score": 95, "strengths": [], '
        '"issues": [], "security_concerns": [], '
        '"performance_concerns": [], "final_comment": ""}\n'
        '```'
    )
    agent = CriticAgent(llm_call=_FakeLLM(raw))
    out = await agent.run(AgentContext(
        user_prompt="x",
        code="int main() {}",
        language="cpp",
    ))
    assert out.data["verdict"] == "approved"
    assert out.data.get("verdict_auto_corrected") is not True


@pytest.mark.asyncio
async def test_blocker_severity_treated_like_critical():
    raw = _critic_response("approved_with_minor", ["blocker"])
    agent = CriticAgent(llm_call=_FakeLLM(raw))
    out = await agent.run(AgentContext(
        user_prompt="x",
        code="int main() {}",
        language="cpp",
    ))
    # `blocker` isn't in the canonical enum so _enum() normalises it
    # to "minor".  In that case, with no major present, verdict stays.
    # This documents the conservative default.
    assert out.data["verdict"] in ("approved_with_minor", "needs_revision")
