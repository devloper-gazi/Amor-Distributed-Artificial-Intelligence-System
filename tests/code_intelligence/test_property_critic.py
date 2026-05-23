"""Cycle F Sprint 2 — tests for property-based tester mode.

Covers the prompt augmentation (`tester_prompt(property_mode=True)`)
and the TesterAgent's surface (`property_tests_present`).  All offline
— stubs the LLM call.
"""

from __future__ import annotations

from typing import Any

import pytest

from document_processor.code_intelligence import agents, prompts as P
from document_processor.code_intelligence.agents import (
    AgentContext,
    AgentOutput,
    TesterAgent,
)


# ─── tester_prompt(property_mode=...) ───────────────────────────────


def test_tester_prompt_default_omits_property_block():
    """When property_mode is False, no @given directive is injected."""

    text = P.tester_prompt(
        "fizzbuzz",
        code="def fb(n): return n",
        plan={"language": "python"},
    )
    assert "@given" not in text
    assert "Hypothesis" not in text
    assert "PROPERTY-BASED REQUIREMENTS" not in text


def test_tester_prompt_python_with_property_mode_injects_directive():
    text = P.tester_prompt(
        "fizzbuzz",
        code="def fb(n): return n",
        plan={"language": "python"},
        property_mode=True,
    )
    assert "@given" in text
    assert "Hypothesis" in text or "hypothesis" in text
    assert "PROPERTY-BASED REQUIREMENTS" in text
    # The directive should mention key invariants concepts.
    assert "invariant" in text.lower() or "INVARIANTS" in text


def test_tester_prompt_non_python_property_mode_is_noop():
    """Hypothesis is a Python library — property block must NOT fire
    for JavaScript / Go / Rust / etc. even when property_mode=True."""

    for lang in ("javascript", "typescript", "go", "rust", "cpp", "java"):
        text = P.tester_prompt(
            "fizzbuzz",
            code="function fb(n) { return n; }",
            plan={"language": lang},
            property_mode=True,
        )
        assert "@given" not in text, f"property block leaked into {lang}"
        assert "PROPERTY-BASED REQUIREMENTS" not in text, f"leak in {lang}"


def test_tester_prompt_uppercase_language_still_matches_python():
    """`language` field can come in mixed case from the planner."""

    text = P.tester_prompt(
        "fizzbuzz",
        code="def fb(n): return n",
        plan={"language": "Python"},
        property_mode=True,
    )
    assert "@given" in text


# ─── TesterAgent property_mode plumbing ─────────────────────────────


@pytest.mark.asyncio
async def test_tester_agent_default_property_mode_false():
    agent = TesterAgent(lambda *a, **kw: "")
    assert agent.property_mode is False


@pytest.mark.asyncio
async def test_tester_agent_property_mode_true_when_passed():
    agent = TesterAgent(lambda *a, **kw: "", property_mode=True)
    assert agent.property_mode is True


@pytest.mark.asyncio
async def test_tester_agent_run_detects_property_tests_present():
    """If the tester output contains @given decorators, surface as True."""

    fake_response = (
        "```python\n"
        "from hypothesis import given, strategies as st\n"
        "\n"
        "@given(st.integers())\n"
        "def test_idempotent(x):\n"
        "    assert abs(abs(x)) == abs(x)\n"
        "```\n"
        "```json\n"
        '{"language":"python","framework":"pytest","test_count":1}\n'
        "```\n"
    )

    async def fake_llm(messages, *_, **__):
        return fake_response

    agent = TesterAgent(fake_llm, property_mode=True)
    out = await agent.run(AgentContext(
        user_prompt="test it",
        plan={"language": "python"},
        code="def f(x): return x",
        language="python",
    ))
    assert out.error is None
    assert out.code is not None
    assert out.data["property_tests_present"] is True
    assert out.data["property_mode"] is True


@pytest.mark.asyncio
async def test_tester_agent_run_no_property_tests_present():
    """Example-based tester output: property_tests_present should be False."""

    fake_response = (
        "```python\n"
        "import pytest\n"
        "def test_basic():\n"
        "    assert 1 == 1\n"
        "```\n"
        "```json\n"
        '{"language":"python","framework":"pytest","test_count":1}\n'
        "```\n"
    )

    async def fake_llm(messages, *_, **__):
        return fake_response

    agent = TesterAgent(fake_llm, property_mode=True)
    out = await agent.run(AgentContext(
        user_prompt="test it",
        plan={"language": "python"},
        code="def f(x): return x",
        language="python",
    ))
    assert out.data["property_tests_present"] is False
    assert out.data["property_mode"] is True


@pytest.mark.asyncio
async def test_tester_agent_run_empty_response_returns_error():
    """Defensive: empty LLM response shouldn't pass property_tests_present."""

    async def fake_llm(*_a, **_kw):
        return ""

    agent = TesterAgent(fake_llm, property_mode=True)
    out = await agent.run(AgentContext(
        user_prompt="test it",
        plan={"language": "python"},
        code="def f(x): return x",
        language="python",
    ))
    assert out.error is not None  # tester failed cleanly
