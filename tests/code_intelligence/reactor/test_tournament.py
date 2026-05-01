"""
Tests for TournamentRunner — N=3 candidate generation, parallel
verification, Pareto election, all-fail fallback.

Sandbox + LLM are mocked so tests run offline + fast.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from document_processor.code_intelligence.reactor.benchmarker import (
    PerformanceBenchmarker,
)
from document_processor.code_intelligence.reactor.property_tests import (
    Invariant, PropertyTestRunner,
)
from document_processor.code_intelligence.reactor.symbolic_complexity import (
    SymbolicComplexityAnalyzer,
)
from document_processor.code_intelligence.reactor.tournament import (
    TournamentBundle,
    TournamentCandidate,
    TournamentRunner,
    _make_plan,
)


# ─── Fakes ──────────────────────────────────────────────────────────


class _DeterministicLLM:
    """Returns canned coder responses keyed by the seasoning hint
    embedded in the plan title."""

    def __init__(self, responses: dict[str, str]):
        self._responses = responses
        self.calls: list[str] = []

    async def __call__(self, prompt, system, max_tokens):
        # The plan dict is rendered into the user prompt as the
        # `title:` line + step description. We inspect the prompt
        # to decide which seasoning we're being asked for.
        for keyword, payload in self._responses.items():
            if keyword in prompt:
                self.calls.append(keyword)
                return payload
        self.calls.append("default")
        # Fallback — a no-op coder response.
        return ("```python\ndef f(x): return x\n```\n"
                "```json\n{\"language\": \"python\"}\n```")


def _coder_response(code: str = "def f(x):\n    return x\n") -> str:
    return f"```python\n{code}```\n```json\n{json.dumps({'language': 'python'})}\n```"


class _ScriptedSandbox:
    """Sandbox that replays canned BENCH_RESULT + PROPERTY_RESULT
    output keyed by the candidate label embedded in the user code."""

    def __init__(self, scripts: dict[str, str], skipped: bool = False):
        self._scripts = scripts
        self.skipped = skipped
        self.execute_count = 0

    async def execute(self, code, language="python", timeout=30):
        self.execute_count += 1
        # Pull the keyword that's in the code so we can emit a
        # candidate-specific scripted response. Each candidate's code
        # will have a distinctive marker we put in its body.
        for marker, stdout in self._scripts.items():
            if marker in code:
                return self._make_result(stdout)
        return self._make_result("")

    def _make_result(self, stdout: str):
        skipped = self.skipped

        class _R:
            def __init__(s):
                s.stdout = stdout
                s.stderr = ""
                s.exit_code = 0
                s.skipped = skipped
                s.success = not skipped
        return _R()


# ─── Plan helpers ──────────────────────────────────────────────────


def test_make_plan_seasonings_differ_in_title():
    p1 = _make_plan("sort a list", "performance", "use radix sort", "python")
    p2 = _make_plan("sort a list", "edge_case", "handle empties", "python")
    p3 = _make_plan("sort a list", "standard", None, "python")
    assert p1["title"] != p2["title"]
    assert p2["title"] != p3["title"]
    assert "performance" in p1["title"].lower()
    assert "edge-case" in p2["title"].lower() or "edge_case" in p2["seasoning"]


def test_make_plan_falls_back_to_user_prompt_when_no_mesh_summary():
    p = _make_plan("compute primes", "performance", None, "python")
    # No mesh summary → title is the user prompt, action is generic.
    assert "compute primes" in p["title"].lower()


# ─── End-to-end tournament ─────────────────────────────────────────


def _bench_payload(scale_ms: list[tuple[int, float]]) -> str:
    """Build a stdout string with one BENCH_RESULT line per (scale, ms) pair."""
    lines: list[str] = ["BENCH_TARGET=f"]
    for scale, ms in scale_ms:
        lines.append(
            f'BENCH_RESULT={{"scale":{scale},"ms":{ms},"peak_kb":{scale}}}'
        )
    return "\n".join(lines) + "\n"


def _property_payload(name_passed: list[tuple[str, bool]]) -> str:
    lines: list[str] = ["PROPERTY_TARGET=f"]
    for name, passed in name_passed:
        rec = {
            "name": name, "passed": passed,
            "samples_run": 50,
            "samples_failed": 0 if passed else 50,
            "first_failure_input": None if passed else "x",
            "first_failure_message": None if passed else "boom",
            "error": None,
        }
        lines.append("PROPERTY_RESULT=" + json.dumps(rec))
    return "\n".join(lines) + "\n"


@pytest.mark.asyncio
async def test_tournament_picks_correct_candidate_when_one_buggy():
    """Three candidates: A + B pass property tests, C fails. Winner
    must be A or B (composite tie broken by linear/quadratic)."""

    # Embed candidate-distinctive markers in the canned codes so the
    # scripted sandbox can route bench/property output per-candidate.
    code_A = "# CAND_A\ndef f(xs):\n    return sorted(xs)\n"
    code_B = "# CAND_B\ndef f(xs):\n    return sorted(xs, reverse=False)\n"
    code_C = "# CAND_C\ndef f(xs):\n    return xs[::-1]\n"  # buggy

    llm = _DeterministicLLM({
        # Match the plan title's seasoning hint to a canned coder reply.
        "performance": _coder_response(code_B),
        "edge-case":   _coder_response(code_C),
    })
    # The "standard" seasoning hits no keyword → fallback path.
    # We override the LLM to always return code_A for the standard plan
    # by inserting a "Code task" marker (which appears for non-mesh
    # seasoning).
    llm._responses["Code task"] = _coder_response(code_A)

    sandbox = _ScriptedSandbox({
        "CAND_A": _bench_payload([(10, 1.0), (100, 10.0), (1000, 100.0)])
                  + _property_payload([("sort_output_is_sorted", True),
                                        ("sort_is_permutation", True)]),
        "CAND_B": _bench_payload([(10, 1.0), (100, 11.0), (1000, 110.0)])
                  + _property_payload([("sort_output_is_sorted", True),
                                        ("sort_is_permutation", True)]),
        "CAND_C": _bench_payload([(10, 1.0), (100, 10.0), (1000, 100.0)])
                  + _property_payload([("sort_output_is_sorted", False),
                                        ("sort_is_permutation", False)]),
    })

    invariants = [
        Invariant(
            name="sort_output_is_sorted", description="sorted",
            input_expr="[]", assertion_expr="True",
        ),
        Invariant(
            name="sort_is_permutation", description="permutation",
            input_expr="[]", assertion_expr="True",
        ),
    ]
    tr = TournamentRunner(llm_call=llm, sandbox=sandbox)
    bundle = await tr.run(
        user_prompt="implement quicksort",
        invariants=invariants,
    )
    assert isinstance(bundle, TournamentBundle)
    assert bundle.failed is False
    assert bundle.degraded is False
    # 3 candidates generated.
    assert len(bundle.candidates) == 3
    # Buggy C must be eliminated.
    assert "C" in bundle.elimination_reasons
    assert "property" in bundle.elimination_reasons["C"].lower()
    # Winner is one of the surviving candidates.
    assert bundle.winner_label in {"A", "B"}


@pytest.mark.asyncio
async def test_tournament_all_failed_uses_degraded_fallback():
    """Every candidate fails property tests → pick the least-broken
    one and mark `degraded=True`."""
    code_A = "# CAND_A\ndef f(xs): return xs\n"
    code_B = "# CAND_B\ndef f(xs): return xs\n"
    code_C = "# CAND_C\ndef f(xs): return xs\n"
    llm = _DeterministicLLM({
        "performance": _coder_response(code_B),
        "edge-case":   _coder_response(code_C),
        "Code task":   _coder_response(code_A),
    })
    bench_ok = _bench_payload([(10, 1.0), (100, 10.0)])
    sandbox = _ScriptedSandbox({
        # A: 1/2 failed; B: 2/2 failed; C: 2/2 failed
        # → A is least-broken, must win as degraded.
        "CAND_A": bench_ok + _property_payload([("a", True), ("b", False)]),
        "CAND_B": bench_ok + _property_payload([("a", False), ("b", False)]),
        "CAND_C": bench_ok + _property_payload([("a", False), ("b", False)]),
    })
    invs = [
        Invariant(name="a", description="x", input_expr="[]",
                  assertion_expr="True"),
        Invariant(name="b", description="x", input_expr="[]",
                  assertion_expr="True"),
    ]
    tr = TournamentRunner(llm_call=llm, sandbox=sandbox)
    bundle = await tr.run(
        user_prompt="x", invariants=invs,
    )
    assert bundle.degraded is True
    assert bundle.winner_label == "A"
    # B and C still flagged with eliminate reasons.
    assert "B" in bundle.elimination_reasons
    assert "C" in bundle.elimination_reasons


@pytest.mark.asyncio
async def test_tournament_failed_when_every_candidate_fails_to_compile():
    """If every coder call returns an error, the bundle is `failed`."""

    async def boom(prompt, system, max_tokens):
        return ""  # empty → CoderAgent error path

    sandbox = _ScriptedSandbox({})
    tr = TournamentRunner(llm_call=boom, sandbox=sandbox)
    bundle = await tr.run(
        user_prompt="x", invariants=[],
    )
    assert bundle.failed is True
    assert "every candidate" in bundle.failure_reason.lower()


@pytest.mark.asyncio
async def test_tournament_n_clamps_to_5():
    sandbox = _ScriptedSandbox({})
    llm = _DeterministicLLM({})
    tr = TournamentRunner(llm_call=llm, sandbox=sandbox, n_candidates=99)
    # We can't easily verify 5 candidates without a CoderAgent that
    # produces code, but the constructor should have clamped the value.
    assert tr._n == 5


@pytest.mark.asyncio
async def test_tournament_no_invariants_skips_property_filter():
    """When invariants=[] the property gate is bypassed; the highest-
    composite candidate wins on bench + symbolic alone."""
    code_A = "# CAND_A\ndef f(xs): return xs\n"
    llm = _DeterministicLLM({
        "Code task":   _coder_response(code_A),
        "performance": _coder_response(code_A),
        "edge-case":   _coder_response(code_A),
    })
    sandbox = _ScriptedSandbox({
        "CAND_A": _bench_payload([(10, 1.0), (100, 10.0)]),
    })
    tr = TournamentRunner(llm_call=llm, sandbox=sandbox)
    bundle = await tr.run(user_prompt="x", invariants=[])
    # No invariants → no property gate → all candidates pass through.
    # Property test result is None → correctness=0.5 (neutral).
    # Winner must be picked from the candidate pool.
    assert bundle.winner_label in {"A", "B", "C"}
    assert bundle.failed is False


# ─── score breakdown ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_winner_score_carries_correctness_and_growth():
    code_A = "# CAND_A\ndef f(xs): return xs\n"
    llm = _DeterministicLLM({
        "Code task":   _coder_response(code_A),
        "performance": _coder_response(code_A),
        "edge-case":   _coder_response(code_A),
    })
    sandbox = _ScriptedSandbox({
        "CAND_A": _bench_payload([(10, 1.0), (100, 10.0), (1000, 100.0)])
                  + _property_payload([("a", True)]),
    })
    invs = [Invariant(name="a", description="x",
                      input_expr="[]", assertion_expr="True")]
    tr = TournamentRunner(llm_call=llm, sandbox=sandbox)
    bundle = await tr.run(user_prompt="x", invariants=invs)
    winner = bundle.winner
    assert winner is not None
    # Linear growth → exponent ~1.0.
    assert 0.85 <= winner.score.growth_factor <= 1.15
    # All invariants passed → correctness=1.0.
    assert winner.score.correctness == 1.0
    # Composite is positive (correctness pulls it positive).
    assert winner.score.composite > 0
