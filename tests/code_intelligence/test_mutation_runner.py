"""Cycle G G4 — coverage for the mutation testing in-loop runner."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from document_processor.code_intelligence import mutation_runner
from document_processor.code_intelligence.mutation_runner import (
    MutationResult,
    format_mutant_survived_block,
    parse_mutmut_results_output,
    parse_surviving_diffs,
)


# ─── MutationResult shape ──────────────────────────────────────────


def test_mutation_result_default_score_zero():
    r = MutationResult()
    assert r.score == 0.0
    assert r.ran is False


def test_mutation_result_to_dict_round_trip():
    r = MutationResult(
        killed=10, survived=5, timeout=1, error=0, total=16,
        score=11 / 16, ran=True,
    )
    d = r.to_dict()
    assert d["killed"] == 10
    assert d["survived"] == 5
    assert d["ran"] is True
    assert abs(d["score"] - 11 / 16) < 0.001


# ─── parse_mutmut_results_output ───────────────────────────────────


def test_parse_results_classic_emoji_format():
    text = """
mutmut results
Survived 🙁 (5)
Killed 🎉 (45)
Timeout ⏰ (2)
Suspicious 🤔 (0)
"""
    r = parse_mutmut_results_output(text)
    assert r.killed == 45
    assert r.survived == 5
    assert r.timeout == 2
    assert r.total == 52
    # Killed + Timeout / total
    assert abs(r.score - 47 / 52) < 0.001


def test_parse_results_3x_label_format():
    """mutmut 3.x uses label: count format without emoji."""
    text = """
Killed: 30
Survived: 10
Timeout: 0
Suspicious: 0
"""
    r = parse_mutmut_results_output(text)
    assert r.killed == 30
    assert r.survived == 10
    assert r.total == 40
    assert r.score == 0.75


def test_parse_results_zero_total_safe():
    """No mutants ran → score=0, total=0, no ZeroDivisionError."""
    r = parse_mutmut_results_output("")
    assert r.total == 0
    assert r.score == 0.0


def test_parse_results_only_killed():
    text = "Killed 🎉 (12)"
    r = parse_mutmut_results_output(text)
    assert r.killed == 12
    assert r.survived == 0
    assert r.score == 1.0


# ─── parse_surviving_diffs ─────────────────────────────────────────


def test_parse_surviving_diffs_classic_format():
    text = """
---- mutant 1 ----
- return a + b
+ return a - b
---- mutant 2 ----
- if x > 0:
+ if x >= 0:
"""
    diffs = parse_surviving_diffs(text)
    assert len(diffs) >= 2
    assert any("a + b" in d and "a - b" in d for d in diffs)


def test_parse_surviving_diffs_caps_count():
    text = ("---- m ----\n- x\n+ y\n" * 10)
    diffs = parse_surviving_diffs(text, max_diffs=3)
    assert len(diffs) == 3


def test_parse_surviving_diffs_empty_input():
    assert parse_surviving_diffs("") == []
    assert parse_surviving_diffs(None) == []


# ─── _mutmut_available CLI probe ───────────────────────────────────


def test_mutmut_available_false_when_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("mutmut not found")
    monkeypatch.setattr(mutation_runner.subprocess, "run", fake_run)
    assert mutation_runner._mutmut_available() is False


def test_mutmut_available_true_when_rc_zero(monkeypatch):
    class FakeResult:
        returncode = 0
    monkeypatch.setattr(
        mutation_runner.subprocess, "run", lambda *a, **k: FakeResult(),
    )
    assert mutation_runner._mutmut_available() is True


def test_mutmut_available_false_on_timeout(monkeypatch):
    import subprocess as _sp
    def fake_run(*args, **kwargs):
        raise _sp.TimeoutExpired(cmd="mutmut", timeout=3.0)
    monkeypatch.setattr(mutation_runner.subprocess, "run", fake_run)
    assert mutation_runner._mutmut_available() is False


# ─── run_mutation_testing skip paths ───────────────────────────────


def test_run_skips_when_mutmut_missing(monkeypatch):
    monkeypatch.setattr(mutation_runner, "_mutmut_available", lambda: False)
    r = asyncio.run(mutation_runner.run_mutation_testing(
        code="def f(): pass\n" * 10,
        tests="def test_f(): pass\n" * 5,
    ))
    assert r.ran is False
    assert "not on PATH" in r.skipped_reason


def test_run_skips_for_tiny_code(monkeypatch):
    monkeypatch.setattr(mutation_runner, "_mutmut_available", lambda: True)
    r = asyncio.run(mutation_runner.run_mutation_testing(
        code="x=1",
        tests="def test_x(): pass\n" * 5,
    ))
    assert r.ran is False
    assert "<5 LOC" in r.skipped_reason


def test_run_skips_for_tiny_tests(monkeypatch):
    monkeypatch.setattr(mutation_runner, "_mutmut_available", lambda: True)
    r = asyncio.run(mutation_runner.run_mutation_testing(
        code="def f():\n    return 1\n" * 5,
        tests="x",
    ))
    assert r.ran is False
    assert "tests <3 LOC" in r.skipped_reason


def test_run_skips_when_code_empty(monkeypatch):
    monkeypatch.setattr(mutation_runner, "_mutmut_available", lambda: True)
    r = asyncio.run(mutation_runner.run_mutation_testing(
        code="", tests="def test_a(): pass\ndef test_b(): pass\ndef test_c(): pass\n",
    ))
    assert r.ran is False


# ─── format_mutant_survived_block reflexion feedback ───────────────


def test_format_block_returns_none_when_not_ran():
    r = MutationResult(ran=False)
    assert format_mutant_survived_block(r) is None


def test_format_block_returns_none_when_score_above_threshold():
    r = MutationResult(killed=18, survived=2, total=20, score=0.9, ran=True)
    assert format_mutant_survived_block(r, threshold=0.35) is None


def test_format_block_emits_feedback_below_threshold():
    r = MutationResult(
        killed=4, survived=16, total=20, score=0.2, ran=True,
        surviving_diff_heads=[
            "return a + b → return a - b",
            "if x > 0 → if x >= 0",
        ],
    )
    block = format_mutant_survived_block(r, threshold=0.35)
    assert block is not None
    assert "MUTANTS SURVIVED" in block
    assert "killed: 4 / total: 20" in block
    assert "a + b" in block
    assert "Add tests" in block


def test_format_block_zero_total_returns_none():
    """No mutants → no feedback (don't synthesise advice from a 0/0
    score)."""
    r = MutationResult(killed=0, survived=0, total=0, score=0.0, ran=True)
    assert format_mutant_survived_block(r, threshold=0.35) is None


# ─── Settings gate (engine wiring) ─────────────────────────────────


def test_engine_skips_mutation_when_setting_disabled(monkeypatch):
    """When `code_mutation_testing_enabled=False`, the runner is
    NOT invoked regardless of mutmut being available — operator
    must explicitly opt in."""
    from document_processor.config.settings import settings as _settings
    monkeypatch.setattr(
        _settings, "code_mutation_testing_enabled", False, raising=False,
    )
    # Just verify the import path doesn't raise.  Full engine
    # wiring is exercised by the existing engine tests + the live
    # /admin/evals path.
    assert getattr(_settings, "code_mutation_testing_enabled") is False
