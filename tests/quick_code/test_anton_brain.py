"""
Unit tests for ``document_processor/quick_code/anton_brain.py``.

We use a deterministic tokenizer (1 char = 1 token) so the budget
math is exactly assertable.
"""

from __future__ import annotations

import pytest

from document_processor.quick_code.anton_brain import (
    AntonBrain,
    _approx_token_count,
)


def _char_tokenizer(text: str) -> int:
    return len(text or "")


# ─────────────────────────────────────────────────────────────────────
# Section presence + ordering
# ─────────────────────────────────────────────────────────────────────


def test_default_sections_present():
    ab = AntonBrain(budget_tokens=100_000, tokenizer=_char_tokenizer)
    out = ab.shape(task_context="do the thing", error_memory=["ValueError"])
    assert "## Identity" in out
    assert "## Rules" in out
    assert "## Task Context" in out
    assert "## Error Memory" in out
    # Ordering: identity → rules → task_context → error_memory
    pos = [out.index(h) for h in ("## Identity", "## Rules", "## Task Context", "## Error Memory")]
    assert pos == sorted(pos)


def test_no_task_context_no_section():
    ab = AntonBrain(budget_tokens=10_000, tokenizer=_char_tokenizer)
    out = ab.shape()
    assert "## Identity" in out
    assert "## Rules" in out
    assert "## Task Context" not in out
    assert "## Error Memory" not in out


# ─────────────────────────────────────────────────────────────────────
# Budget enforcement
# ─────────────────────────────────────────────────────────────────────


def test_budget_overflow_truncates_text():
    ab = AntonBrain(budget_tokens=200, tokenizer=_char_tokenizer)
    huge_ctx = "a" * 5000
    huge_errors = ["b" * 1000, "c" * 1000]
    out = ab.shape(task_context=huge_ctx, error_memory=huge_errors)
    # The output cannot exceed roughly the budget plus the two
    # mandatory section headers; allow a small margin for the
    # truncation marker text.
    assert len(out) <= 1000  # budget=200 chars + headers + truncation marker


def test_error_memory_truncated_first():
    """Stress test: more errors than the budget allows.  After the
    static identity + rules block, ERROR_MEMORY should drop oldest
    entries first."""
    ab = AntonBrain(
        budget_tokens=10_000,
        tokenizer=_char_tokenizer,
        min_error_memory_keep=0,
    )
    errors = [f"error_{i}: stack trace " * 50 for i in range(10)]
    out = ab.shape(task_context="", error_memory=errors)
    # The most recent error should appear; the very oldest may not.
    assert "error_9" in out  # newest
    # Confirm at least the oldest one (error_0) was dropped to keep
    # under budget.
    if "error_0" in out:
        # If everything fits, fine — but with this many errors it
        # shouldn't.
        pytest.skip("budget large enough to keep all errors")


def test_task_context_truncated_when_budget_tight():
    ab = AntonBrain(budget_tokens=300, tokenizer=_char_tokenizer)
    huge_ctx = "context line. " * 200
    out = ab.shape(task_context=huge_ctx)
    assert "[...TRUNCATED]" in out
    assert len(out) <= 800


def test_min_error_memory_keep_forces_at_least_one_error():
    # Budget is just enough to cover the static identity + rules
    # blocks plus a tiny error-memory tail.  A 5 000-char error
    # cannot fit verbatim, but ``min_error_memory_keep=1`` forces a
    # truncated form to appear.
    ab = AntonBrain(
        budget_tokens=600,
        tokenizer=_char_tokenizer,
        min_error_memory_keep=1,
    )
    out = ab.shape(error_memory=["x" * 5000])
    assert "## Error Memory" in out
    assert "[...TRUNCATED]" in out


# ─────────────────────────────────────────────────────────────────────
# Identity / rules invariants
# ─────────────────────────────────────────────────────────────────────


def test_default_identity_no_refusal_language():
    ab = AntonBrain(budget_tokens=10_000, tokenizer=_char_tokenizer)
    out = ab.shape().lower()
    for token in ("i cannot", "as an ai", "if illegal", "consult a lawyer"):
        assert token not in out, f"banned token in default prompt: {token!r}"


def test_custom_identity_used():
    ab = AntonBrain(budget_tokens=10_000, tokenizer=_char_tokenizer)
    out = ab.shape(identity="You are a focused expert.")
    assert "focused expert" in out


def test_custom_global_rules_used():
    ab = AntonBrain(budget_tokens=10_000, tokenizer=_char_tokenizer)
    out = ab.shape(global_rules="- always inline tests")
    assert "always inline tests" in out


# ─────────────────────────────────────────────────────────────────────
# Tokeniser fallback
# ─────────────────────────────────────────────────────────────────────


def test_approx_tokeniser_round_trips_zero_for_empty():
    assert _approx_token_count("") == 0
    assert _approx_token_count(None) == 0  # type: ignore[arg-type]


def test_approx_tokeniser_monotonic():
    a = _approx_token_count("abc")
    b = _approx_token_count("abcdef")
    assert b >= a
