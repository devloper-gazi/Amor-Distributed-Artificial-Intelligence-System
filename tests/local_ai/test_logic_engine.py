"""
Tests for LogicEngine — keyword-matched template selection,
rule-based skeleton output shape, fallback when no template matches,
and end-to-end "skeleton verifies cleanly" round-trip with the Z3
verifier.
"""

from __future__ import annotations

import pytest

from local_ai.logic_engine import (
    CATALOGUE,
    LogicEngine,
    LogicSkeleton,
    _match_template,
    _slug,
)
from local_ai.z3_verifier import Z3Verifier


def _engine() -> LogicEngine:
    return LogicEngine(strategy="rule_based")


# ── helpers ─────────────────────────────────────────────────────────


def test_slug_normalises_punctuation_and_case():
    assert _slug("Implement Quick Sort!!!") == "implement_quick_sort"


def test_slug_truncates_long_strings():
    s = _slug("x " * 100)
    assert len(s) <= 60
    # No trailing underscore.
    assert not s.endswith("_")


def test_slug_returns_default_on_empty():
    assert _slug("") == "task"
    assert _slug("!!!") == "task"


def test_match_template_picks_binary_search_first():
    """`binary search` wins even though `search` would also match."""
    tmpl = _match_template("implement a binary search over a sorted list")
    assert tmpl is not None
    assert tmpl.name == "binary_search"


def test_match_template_falls_back_to_linear_search():
    tmpl = _match_template("write a function to find the target in a list")
    assert tmpl is not None
    assert tmpl.name == "linear_search"


def test_match_template_no_match_returns_none():
    tmpl = _match_template("compute the integral of f(x) using Gauss")
    assert tmpl is None


# ── per-template generation (full structural assertions) ──────────


@pytest.mark.asyncio
async def test_sort_template_emits_structured_skeleton():
    skel = await _engine().generate("implement merge sort on integers")
    assert isinstance(skel, LogicSkeleton)
    assert skel.algorithm_type == "sort"
    assert skel.matched_template == "sort"
    assert skel.confidence > 0.5
    # Every contract-bearing field is populated.
    assert skel.pseudocode_steps
    assert skel.state_machine.get("states")
    assert skel.ast_skeleton.get("function_name") == "sort"
    assert skel.invariants
    assert skel.termination_argument
    # Verifier-side skeleton is filled in.
    assert skel.verifier_skeleton is not None
    assert skel.verifier_skeleton.skeleton_id == skel.skeleton_id


@pytest.mark.asyncio
async def test_linear_search_template_marks_two_outcomes():
    skel = await _engine().generate("find target in a list")
    assert skel.matched_template == "linear_search"
    # Two case splits cover found / not-found.
    assert skel.verifier_skeleton is not None
    assert len(skel.verifier_skeleton.case_splits) == 2


@pytest.mark.asyncio
async def test_binary_search_skeleton_advertises_log_n():
    skel = await _engine().generate("write a binary search")
    assert skel.matched_template == "binary_search"
    assert skel.complexity_hint == "O(log n)"


@pytest.mark.asyncio
async def test_count_template_caps_count_to_n():
    skel = await _engine().generate("count occurrences of even numbers")
    assert skel.matched_template == "count"
    # The verifier-side invariants should constrain count <= n.
    assert any("count" in inv and "n" in inv
               for inv in (skel.verifier_skeleton.invariants
                           if skel.verifier_skeleton else []))


@pytest.mark.asyncio
async def test_three_way_compare_has_three_case_splits():
    skel = await _engine().generate("classify the sign of x")
    assert skel.matched_template == "three_way_compare"
    assert skel.verifier_skeleton is not None
    assert len(skel.verifier_skeleton.case_splits) == 3


@pytest.mark.asyncio
async def test_hashtable_lookup_no_loops():
    skel = await _engine().generate("dict lookup by key")
    assert skel.matched_template == "hashtable_lookup"
    assert skel.verifier_skeleton is not None
    assert skel.verifier_skeleton.loops == []


# ── fallback ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_match_returns_low_confidence_fallback():
    skel = await _engine().generate(
        "perform a Gauss-Seidel iteration on a sparse matrix",
    )
    assert skel.matched_template == ""
    assert skel.confidence == 0.0
    assert skel.algorithm_type == "generic"
    # Even the fallback returns a verifier-shaped skeleton (empty).
    assert skel.verifier_skeleton is not None


@pytest.mark.asyncio
async def test_empty_prompt_returns_fallback():
    skel = await _engine().generate("")
    assert skel.confidence == 0.0
    assert skel.algorithm_type == "generic"


@pytest.mark.asyncio
async def test_whitespace_only_prompt_returns_fallback():
    skel = await _engine().generate("   \n   ")
    assert skel.confidence == 0.0


# ── strategy fallthrough ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_small_model_strategy_falls_through_to_rule_based():
    """Phase 1A: small_model isn't implemented yet; should still
    return a usable rule-based skeleton when a template matches."""
    eng = LogicEngine(strategy="small_model")
    skel = await eng.generate("implement bubble sort")
    assert skel.matched_template == "sort"


@pytest.mark.asyncio
async def test_funsearch_strategy_falls_through_to_rule_based():
    eng = LogicEngine(strategy="funsearch")
    skel = await eng.generate("implement bubble sort")
    assert skel.matched_template == "sort"


@pytest.mark.asyncio
async def test_unknown_strategy_returns_fallback():
    eng = LogicEngine(strategy="bogus")  # type: ignore[arg-type]
    skel = await eng.generate("implement bubble sort")
    assert skel.confidence == 0.0


# ── end-to-end: every template's skeleton VERIFIES under Z3 ───────


@pytest.mark.parametrize("prompt,expected_template", [
    ("write merge sort", "sort"),
    ("find element in list", "linear_search"),
    ("binary search a sorted array", "binary_search"),
    ("count even numbers", "count"),
    ("classify sign of x", "three_way_compare"),
    ("dict lookup by key", "hashtable_lookup"),
])
@pytest.mark.asyncio
async def test_every_template_skeleton_verifies_cleanly(
    prompt: str, expected_template: str,
):
    """For every catalogue entry, the verifier-side skeleton must
    pass Z3's full report. This is the strongest correctness contract:
    the engine only emits skeletons we can actually prove."""
    skel = await _engine().generate(prompt)
    assert skel.matched_template == expected_template
    assert skel.verifier_skeleton is not None
    report = Z3Verifier(timeout_ms=2_000).verify_skeleton(
        skel.verifier_skeleton,
    )
    assert report.overall == "pass", report.to_dict()


# ── catalogue invariants ──────────────────────────────────────────


def test_catalogue_has_every_template_named():
    names = {tmpl.name for tmpl in CATALOGUE}
    expected = {
        "binary_search", "linear_search", "hashtable_lookup",
        "sort", "count", "three_way_compare",
    }
    assert names == expected


def test_catalogue_keywords_are_lowercase():
    """Avoid case-mismatch bugs at runtime."""
    for tmpl in CATALOGUE:
        for kw in tmpl.keywords:
            assert kw == kw.lower(), (
                f"template {tmpl.name} has non-lowercase keyword {kw!r}"
            )


# ── to_dict shape ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_to_dict_carries_verifier_skeleton_and_metadata():
    skel = await _engine().generate("implement bubble sort")
    d = skel.to_dict()
    for key in (
        "skeleton_id", "algorithm_type", "pseudocode_steps",
        "state_machine", "ast_skeleton", "invariants",
        "termination_argument", "complexity_hint",
        "matched_template", "confidence", "verifier_skeleton",
    ):
        assert key in d, key
    # Verifier skeleton is itself a dict.
    assert isinstance(d["verifier_skeleton"], dict)
