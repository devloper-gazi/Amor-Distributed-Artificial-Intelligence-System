"""
Unit tests for ``document_processor/quick_code/symcode.py``.

These spawn real subprocesses (sys.executable -I -).  We keep the
inputs small so test runtime stays under a few seconds.
"""

from __future__ import annotations

import asyncio

import pytest

from document_processor.quick_code.contracts import SymValidationResult
from document_processor.quick_code.symcode import (
    DEFAULT_ALLOWED,
    SymCode,
)


def _run(coro):
    return asyncio.run(coro)


# Skip the whole module if SymPy isn't installed in the test env —
# the SymCode class will degrade gracefully but the tests want to
# assert true equivalence.
sympy = pytest.importorskip("sympy")


# ─────────────────────────────────────────────────────────────────────
# Construction + invariants
# ─────────────────────────────────────────────────────────────────────


def test_max_iters_clamped_to_three():
    sc = SymCode(max_iters=10)
    assert sc.max_iters == 3
    sc2 = SymCode(max_iters=0)
    assert sc2.max_iters == 1


def test_default_allowed_imports_only_sympy():
    sc = SymCode()
    assert sc.allowed_imports == DEFAULT_ALLOWED
    assert "sympy" in sc.allowed_imports
    assert "os" not in sc.allowed_imports


def test_extending_allowed_imports_keeps_sympy():
    sc = SymCode(allowed_imports={"mpmath"})
    assert "sympy" in sc.allowed_imports
    assert "mpmath" in sc.allowed_imports


# ─────────────────────────────────────────────────────────────────────
# Expression equivalence
# ─────────────────────────────────────────────────────────────────────


def test_equivalent_expression_passes():
    sc = SymCode()
    out = _run(
        sc.validate_expression("(x + 1)**2", expected="x**2 + 2*x + 1")
    )
    assert isinstance(out, SymValidationResult)
    assert out.ok is True
    assert out.equivalence_class == "equivalent"


def test_non_equivalent_expression_fails():
    sc = SymCode()
    out = _run(sc.validate_expression("x + 1", expected="x + 2"))
    assert out.ok is False
    assert out.equivalence_class == "not_equivalent"


def test_expression_without_expected_just_parses():
    sc = SymCode()
    out = _run(sc.validate_expression("sin(x)**2 + cos(x)**2"))
    assert out.ok is True


# ─────────────────────────────────────────────────────────────────────
# Code-block path
# ─────────────────────────────────────────────────────────────────────


def test_block_with_result_assignment_passes():
    sc = SymCode()
    out = _run(
        sc.validate_code_block(
            code_block="from sympy import symbols\nx = symbols('x')\nresult = (x + 1)**2",
            expected="x**2 + 2*x + 1",
        )
    )
    assert out.ok is True


def test_block_without_result_fails_cleanly():
    sc = SymCode()
    out = _run(
        sc.validate_code_block(
            code_block="from sympy import symbols\nx = symbols('x')\n_ = x + 1",
        )
    )
    # Failure but not crash; iterations should be 1.
    assert out.ok is False
    assert out.iterations == 1
    assert out.error and "result" in out.error


# ─────────────────────────────────────────────────────────────────────
# Sandbox enforcement
# ─────────────────────────────────────────────────────────────────────


def test_disallowed_import_blocked():
    sc = SymCode()
    out = _run(
        sc.validate_code_block(
            code_block="import os\nresult = 0",
        )
    )
    assert out.ok is False
    assert out.error and ("os" in out.error or "ImportError" in out.error)


def test_subprocess_error_returned_as_failure():
    sc = SymCode()
    out = _run(
        sc.validate_code_block(
            code_block="raise ValueError('boom')",
        )
    )
    assert out.ok is False
    assert out.error and "boom" in out.error


# ─────────────────────────────────────────────────────────────────────
# Timeout
# ─────────────────────────────────────────────────────────────────────


def test_timeout_skips_softly():
    """Force a long-running sympy call past the timeout.  SymCode
    should still return a result (fail-soft) instead of raising."""
    sc = SymCode(timeout_s=1, max_iters=1)
    # Generate an expression that's slow to simplify; sympy will
    # spend > 1s on it.  We don't really need the answer — only
    # that the call completes.
    big = " + ".join(f"x**{i}" for i in range(40))
    out = _run(
        sc.validate_expression(f"({big})*({big})", expected="x")
    )
    # Either the timeout fires (ok=True, equivalence_class="skipped")
    # or sympy is fast enough to return a real verdict.  Either way
    # we must get a SymValidationResult.
    assert isinstance(out, SymValidationResult)


# ─────────────────────────────────────────────────────────────────────
# validate() dispatch heuristic
# ─────────────────────────────────────────────────────────────────────


def test_validate_dispatches_expression():
    sc = SymCode()
    out = _run(sc.validate("(x + 1)**2", expected="x**2 + 2*x + 1"))
    assert out.ok is True


def test_validate_dispatches_block():
    sc = SymCode()
    out = _run(
        sc.validate(
            "from sympy import symbols\nx = symbols('x')\nresult = (x + 1)**2",
            expected="x**2 + 2*x + 1",
        )
    )
    assert out.ok is True


def test_validate_empty_code_returns_failure():
    sc = SymCode()
    out = _run(sc.validate(""))
    assert out.ok is False
    assert out.error == "empty_code"
