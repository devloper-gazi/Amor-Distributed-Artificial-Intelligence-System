"""
Tests for PropertyTestGenerator + PropertyTestRunner.

Sandbox + LLM are mocked so tests stay fast and offline.
"""

from __future__ import annotations

import json

import pytest

from document_processor.code_intelligence.reactor.property_tests import (
    CATALOGUE,
    Invariant,
    InvariantOutcome,
    PropertyTestGenerator,
    PropertyTestResult,
    PropertyTestRunner,
    _extract_pattern_keys,
)


# ── pattern key extraction ─────────────────────────────────────────


def test_pattern_key_picks_sort_when_prompt_mentions_it():
    keys = _extract_pattern_keys(
        triage={"task_type": "generation"},
        prompt="implement merge sort",
    )
    assert "sort" in keys


def test_pattern_key_picks_search_from_triage_task_type():
    """Triage classification (binary search → 'search') trumps prompt
    when prompt is generic."""
    keys = _extract_pattern_keys(
        triage={"task_type": "search"},
        prompt="find x",
    )
    assert "search" in keys


def test_pattern_key_falls_back_to_default():
    keys = _extract_pattern_keys(
        triage=None,
        prompt="random unspecified thing",
    )
    assert keys == ["default"]


def test_pattern_key_unions_multiple_patterns():
    keys = _extract_pattern_keys(
        triage={"task_type": "math"},
        prompt="hash a math expression",
    )
    # "math" + "hash" both match.
    assert set(keys) >= {"hash", "math"}


# ── Generator: catalogue selection ─────────────────────────────────


def test_generator_for_sort_returns_sort_invariants():
    gen = PropertyTestGenerator()
    invs = gen.for_triage(
        triage={"task_type": "generation"},
        user_prompt="implement quicksort",
    )
    names = {i.name for i in invs}
    assert "sort_output_is_sorted" in names
    assert "sort_is_permutation" in names
    assert all(i.source == "catalogue" for i in invs)


def test_generator_for_unknown_returns_default_invariant():
    gen = PropertyTestGenerator()
    invs = gen.for_triage(
        triage=None, user_prompt="something unrelated",
    )
    assert len(invs) >= 1
    assert any(i.name.startswith("default_") for i in invs)


def test_generator_creates_fresh_invariants_each_call():
    """Catalogue rows must not be shared across calls."""
    gen = PropertyTestGenerator()
    a = gen.for_triage(triage=None, user_prompt="search a list")
    b = gen.for_triage(triage=None, user_prompt="search a list")
    assert a is not b
    assert a[0] is not b[0]


# ── Generator: LLM suggest ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_generator_suggest_parses_llm_response():
    payload = json.dumps({
        "invariants": [
            {"name": "x_returns_positive", "description": "non-negative",
             "input_expr": "rng.randint(0, 100)",
             "assertion_expr": "out >= 0"},
        ]
    })

    async def llm(prompt, system, max_tokens):
        return payload

    gen = PropertyTestGenerator()
    invs = await gen.suggest(llm, user_prompt="x")
    assert len(invs) == 1
    assert invs[0].source == "llm"
    assert invs[0].samples == 30  # cheaper LLM-suggested default


@pytest.mark.asyncio
async def test_generator_suggest_drops_malformed_entries():
    payload = json.dumps({
        "invariants": [
            {"name": "ok", "description": "x",
             "input_expr": "rng.randint(0,1)", "assertion_expr": "True"},
            {"name": "missing_input"},
            {"description": "no_name",
             "input_expr": "rng.randint(0,1)", "assertion_expr": "True"},
        ]
    })

    async def llm(prompt, system, max_tokens):
        return payload

    gen = PropertyTestGenerator()
    invs = await gen.suggest(llm, user_prompt="x")
    assert len(invs) == 1
    assert invs[0].name == "ok"


@pytest.mark.asyncio
async def test_generator_suggest_handles_invalid_json():
    async def llm(prompt, system, max_tokens):
        return "not json at all"

    gen = PropertyTestGenerator()
    invs = await gen.suggest(llm, user_prompt="x")
    assert invs == []


@pytest.mark.asyncio
async def test_generator_suggest_handles_llm_error():
    async def llm(prompt, system, max_tokens):
        raise RuntimeError("ollama down")

    gen = PropertyTestGenerator()
    invs = await gen.suggest(llm, user_prompt="x")
    assert invs == []


# ── Runner: sandbox-script assembly ────────────────────────────────


class _FakeSandbox:
    def __init__(self, stdout: str = "", skipped: bool = False):
        self.last_code: str | None = None
        self.stdout = stdout
        self.skipped = skipped

    async def execute(self, code, language="python", timeout=30):
        self.last_code = code

        class _R:
            def __init__(s):
                s.stdout = self.stdout
                s.stderr = ""
                s.exit_code = 0
                s.skipped = self.skipped
        return _R()


@pytest.mark.asyncio
async def test_runner_with_no_invariants_returns_empty_pass():
    runner = PropertyTestRunner(_FakeSandbox())
    res = await runner.run(code="def f(x): return x\n", invariants=[])
    assert res.failed is False
    # No invariants → no outcomes; all_passed is False (vacuous).
    assert res.outcomes == []


@pytest.mark.asyncio
async def test_runner_assembles_script_with_user_code_and_invariants():
    sb = _FakeSandbox()
    runner = PropertyTestRunner(sb)
    invs = [Invariant(
        name="x", description="x",
        input_expr="rng.randint(0, 5)",
        assertion_expr="out == out",
    )]
    await runner.run(code="def f(n): return n\n", invariants=invs)
    assert sb.last_code is not None
    # User code present.
    assert "def f(n):" in sb.last_code
    # Harness markers present.
    assert "_amor_pick_target" in sb.last_code
    assert "PROPERTY_RESULT=" in sb.last_code
    # Invariant JSON embedded.
    assert "rng.randint(0, 5)" in sb.last_code


# ── Runner: outcome parsing ────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_parses_passing_outcomes():
    invs = [
        Invariant(name="a", description="x",
                  input_expr="0", assertion_expr="True"),
        Invariant(name="b", description="x",
                  input_expr="0", assertion_expr="True"),
    ]
    stdout = (
        'PROPERTY_TARGET=f\n'
        'PROPERTY_RESULT={"name":"a","passed":true,"samples_run":50,"samples_failed":0,'
        '"first_failure_input":null,"first_failure_message":null,"error":null}\n'
        'PROPERTY_RESULT={"name":"b","passed":true,"samples_run":50,"samples_failed":0,'
        '"first_failure_input":null,"first_failure_message":null,"error":null}\n'
    )
    sb = _FakeSandbox(stdout=stdout)
    runner = PropertyTestRunner(sb)
    res = await runner.run(code="def f(n): return n\n", invariants=invs)
    assert res.all_passed
    assert len(res.outcomes) == 2
    assert all(o.passed for o in res.outcomes)


@pytest.mark.asyncio
async def test_runner_parses_failing_invariant():
    invs = [Invariant(name="a", description="x",
                      input_expr="0", assertion_expr="False")]
    stdout = (
        'PROPERTY_RESULT={"name":"a","passed":false,"samples_run":50,'
        '"samples_failed":50,"first_failure_input":"5",'
        '"first_failure_message":"invariant violated","error":null}\n'
    )
    sb = _FakeSandbox(stdout=stdout)
    runner = PropertyTestRunner(sb)
    res = await runner.run(code="def f(n): return n\n", invariants=invs)
    assert not res.all_passed
    assert res.num_failed == 1
    assert res.outcomes[0].first_failure_message == "invariant violated"


@pytest.mark.asyncio
async def test_runner_missing_outcome_marked_as_not_run():
    """If the sandbox cuts off before the harness emits the result
    line, the outcome is recorded as failed with a note."""
    invs = [Invariant(name="a", description="x",
                      input_expr="0", assertion_expr="True")]
    sb = _FakeSandbox(stdout="PROPERTY_TARGET=f\n")  # no result line
    runner = PropertyTestRunner(sb)
    res = await runner.run(code="def f(n): return n\n", invariants=invs)
    assert res.outcomes[0].passed is False
    assert res.outcomes[0].error is not None


# ── fail-soft paths ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_empty_code_fails_softly():
    invs = [Invariant(name="a", description="x",
                      input_expr="0", assertion_expr="True")]
    runner = PropertyTestRunner(_FakeSandbox())
    res = await runner.run(code="", invariants=invs)
    assert res.failed
    assert "no code" in res.failure_reason.lower()


@pytest.mark.asyncio
async def test_runner_skipped_sandbox_fails_softly():
    invs = [Invariant(name="a", description="x",
                      input_expr="0", assertion_expr="True")]
    runner = PropertyTestRunner(_FakeSandbox(skipped=True))
    res = await runner.run(code="def f(): return 1\n", invariants=invs)
    assert res.failed
    assert "skipped" in res.failure_reason.lower()


# ── catalogue sanity ──────────────────────────────────────────────


def test_catalogue_has_sort_search_default_at_minimum():
    assert "sort" in CATALOGUE
    assert "search" in CATALOGUE
    assert "default" in CATALOGUE


def test_every_catalogue_invariant_has_required_fields():
    for key, invs in CATALOGUE.items():
        for inv in invs:
            assert inv.name
            assert inv.input_expr
            assert inv.assertion_expr
            assert inv.source == "catalogue"
