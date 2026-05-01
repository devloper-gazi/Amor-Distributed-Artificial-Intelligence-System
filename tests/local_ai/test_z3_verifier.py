"""
Tests for Z3Verifier — termination, overflow, exhaustive case
analysis, invariant satisfiability, predicate parsing.
"""

from __future__ import annotations

import pytest

from local_ai.z3_verifier import (
    AlgorithmSkeleton,
    CaseSplit,
    CheckResult,
    IntVarBound,
    LoopSpec,
    VerificationReport,
    Z3Verifier,
    _parse_predicate,
)


def _verifier() -> Z3Verifier:
    # Short timeout in tests so a runaway query never wedges CI.
    return Z3Verifier(timeout_ms=2_000)


# ─── predicate parser ────────────────────────────────────────────────


def test_parse_simple_comparison():
    from z3 import Int, Solver, sat
    x = Int("x")
    expr = _parse_predicate("x > 0", {"x": x})
    s = Solver()
    s.add(expr)
    assert s.check() == sat


def test_parse_compound_and():
    from z3 import Int, Solver, sat
    x = Int("x")
    expr = _parse_predicate("x > 0 and x < 10", {"x": x})
    s = Solver()
    s.add(expr)
    s.add(x == 5)
    assert s.check() == sat


def test_parse_not_predicate():
    from z3 import Int, Solver, sat
    x = Int("x")
    expr = _parse_predicate("not (x == 0)", {"x": x})
    s = Solver()
    s.add(expr)
    s.add(x == 1)
    assert s.check() == sat


def test_parse_arithmetic():
    from z3 import Int, Solver, sat
    n, i = Int("n"), Int("i")
    expr = _parse_predicate("n - i >= 0", {"n": n, "i": i})
    s = Solver()
    s.add(expr)
    s.add(n == 5)
    s.add(i == 3)
    assert s.check() == sat


def test_parse_unknown_variable_raises():
    with pytest.raises(ValueError, match="unknown variable"):
        _parse_predicate("y > 0", {})


def test_parse_unsupported_op_raises():
    """Division isn't in the supported set — must raise."""
    from z3 import Int
    with pytest.raises(ValueError):
        _parse_predicate("x / 2 > 0", {"x": Int("x")})


# ─── termination check ───────────────────────────────────────────────


def test_termination_passes_for_simple_decreasing_loop():
    """Classic for i in range(n) — measure n-i decreases by 1, bounded ≥ 0."""
    skeleton = AlgorithmSkeleton(
        skeleton_id="loop_simple",
        int_vars=[
            IntVarBound("n", low=0, high=10_000),
            IntVarBound("i", low=0, high=10_000),
        ],
        loops=[LoopSpec(measure_var="n", post_decreases_by=1, measure_low_bound=0)],
    )
    result = _verifier().check_termination(skeleton)
    assert result.passed, result.reason


def test_termination_fails_when_step_skips_floor():
    """Step of 3 with floor 0 — at measure=2 the body executes (2 > 0)
    but next value 2-3 = -1 drops below floor → not provably terminating."""
    skeleton = AlgorithmSkeleton(
        skeleton_id="loop_step_too_big",
        int_vars=[IntVarBound("n", low=0, high=1_000)],
        loops=[LoopSpec(measure_var="n", post_decreases_by=3, measure_low_bound=0)],
    )
    result = _verifier().check_termination(skeleton)
    assert result.status == "fail", result.reason
    # Counterexample should expose the offending state.
    assert "n" in result.counterexample


def test_termination_no_loops_passes_vacuously():
    skeleton = AlgorithmSkeleton(skeleton_id="trivial")
    result = _verifier().check_termination(skeleton)
    assert result.passed
    assert "no loops" in result.reason.lower()


def test_termination_step_zero_does_not_decrease():
    """post_decreases_by=0 means the measure stays the same → not a
    well-founded measure → fail."""
    skeleton = AlgorithmSkeleton(
        skeleton_id="loop_step_zero",
        int_vars=[IntVarBound("n", low=0, high=10)],
        loops=[LoopSpec(measure_var="n", post_decreases_by=0, measure_low_bound=0)],
    )
    result = _verifier().check_termination(skeleton)
    assert result.status == "fail"


# ─── overflow check ──────────────────────────────────────────────────


def test_overflow_passes_under_declared_bounds():
    skeleton = AlgorithmSkeleton(
        skeleton_id="overflow_safe",
        int_vars=[IntVarBound("n", low=0, high=1_000_000)],
        loops=[LoopSpec(measure_var="n", post_decreases_by=1)],
        int_width=64,
    )
    result = _verifier().check_overflow(skeleton)
    assert result.passed, result.reason


def test_overflow_no_loops_passes():
    skeleton = AlgorithmSkeleton(skeleton_id="no_loops")
    result = _verifier().check_overflow(skeleton)
    assert result.passed


# ─── exhaustive cases ────────────────────────────────────────────────


def test_exhaustive_cases_passes_when_predicates_cover():
    """Three predicates that together cover all integers under bounds."""
    skeleton = AlgorithmSkeleton(
        skeleton_id="cases_covered",
        int_vars=[IntVarBound("x", low=-100, high=100)],
        case_splits=[
            CaseSplit(predicate="x < 0"),
            CaseSplit(predicate="x == 0"),
            CaseSplit(predicate="x > 0"),
        ],
    )
    result = _verifier().check_exhaustive_cases(skeleton)
    assert result.passed, result.reason


def test_exhaustive_cases_fails_with_hole():
    """Missing 'x == 0' branch — Z3 finds the hole."""
    skeleton = AlgorithmSkeleton(
        skeleton_id="cases_hole",
        int_vars=[IntVarBound("x", low=-100, high=100)],
        case_splits=[
            CaseSplit(predicate="x < 0"),
            CaseSplit(predicate="x > 0"),
            # x == 0 missing
        ],
    )
    result = _verifier().check_exhaustive_cases(skeleton)
    assert result.status == "fail", result.reason
    # The counterexample should pin x to 0.
    assert result.counterexample.get("x") == 0


def test_exhaustive_cases_no_splits_passes_vacuously():
    skeleton = AlgorithmSkeleton(skeleton_id="trivial")
    result = _verifier().check_exhaustive_cases(skeleton)
    assert result.passed


def test_exhaustive_cases_unknown_when_no_int_vars():
    """No declared variables → can't reason → UNKNOWN."""
    skeleton = AlgorithmSkeleton(
        skeleton_id="no_vars",
        case_splits=[CaseSplit(predicate="x > 0")],
    )
    result = _verifier().check_exhaustive_cases(skeleton)
    assert result.status == "unknown"


def test_exhaustive_cases_handles_unparseable_predicate():
    """An unsupported op (division) should yield UNKNOWN, not crash."""
    skeleton = AlgorithmSkeleton(
        skeleton_id="bad_pred",
        int_vars=[IntVarBound("x", low=-10, high=10)],
        case_splits=[CaseSplit(predicate="x / 2 > 0")],
    )
    result = _verifier().check_exhaustive_cases(skeleton)
    assert result.status == "unknown"


# ─── invariants satisfiability ──────────────────────────────────────


def test_invariants_satisfiable_passes_for_consistent_set():
    skeleton = AlgorithmSkeleton(
        skeleton_id="inv_ok",
        int_vars=[IntVarBound("x", low=-10, high=10)],
        invariants=["x >= 0", "x <= 5"],
    )
    result = _verifier().check_invariants_satisfiable(skeleton)
    assert result.passed


def test_invariants_satisfiable_fails_for_contradiction():
    skeleton = AlgorithmSkeleton(
        skeleton_id="inv_bad",
        int_vars=[IntVarBound("x", low=-10, high=10)],
        invariants=["x > 0", "x < 0"],
    )
    result = _verifier().check_invariants_satisfiable(skeleton)
    assert result.status == "fail"


def test_invariants_satisfiable_no_invariants_passes():
    skeleton = AlgorithmSkeleton(skeleton_id="empty")
    result = _verifier().check_invariants_satisfiable(skeleton)
    assert result.passed


# ─── orchestrator ────────────────────────────────────────────────────


def test_verify_skeleton_full_pipeline_passes():
    """A reasonable skeleton — all four checks should pass."""
    skeleton = AlgorithmSkeleton(
        skeleton_id="full_ok",
        int_vars=[
            IntVarBound("n", low=0, high=10_000),
            IntVarBound("x", low=-10, high=10),
        ],
        loops=[LoopSpec(measure_var="n", post_decreases_by=1, measure_low_bound=0)],
        case_splits=[
            CaseSplit(predicate="x < 0"),
            CaseSplit(predicate="x == 0"),
            CaseSplit(predicate="x > 0"),
        ],
        invariants=["n >= 0"],
    )
    report = _verifier().verify_skeleton(skeleton)
    assert isinstance(report, VerificationReport)
    assert report.overall == "pass", report.to_dict()
    assert report.all_passed
    assert len(report.checks) == 4


def test_verify_skeleton_fails_on_any_check_failure():
    """One failing check (case split hole) → overall fail."""
    skeleton = AlgorithmSkeleton(
        skeleton_id="full_with_hole",
        int_vars=[IntVarBound("x", low=-10, high=10)],
        case_splits=[
            CaseSplit(predicate="x < 0"),
            CaseSplit(predicate="x > 0"),
        ],
    )
    report = _verifier().verify_skeleton(skeleton)
    assert report.overall == "fail"
    assert not report.all_passed
    failed_checks = [c for c in report.checks if c.status == "fail"]
    assert len(failed_checks) == 1
    assert failed_checks[0].name == "exhaustive_cases"


def test_verify_skeleton_to_dict_round_trip():
    skeleton = AlgorithmSkeleton(
        skeleton_id="serialise",
        int_vars=[IntVarBound("n", low=0, high=100)],
        loops=[LoopSpec(measure_var="n")],
    )
    report = _verifier().verify_skeleton(skeleton)
    d = report.to_dict()
    assert d["overall"] == "pass"
    assert d["skeleton_id"] == "serialise"
    assert isinstance(d["checks"], list)
    assert len(d["checks"]) == 4
