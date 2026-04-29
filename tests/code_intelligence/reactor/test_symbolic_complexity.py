"""
Tests for SymbolicComplexityAnalyzer — AST-only Big-O upper-bound
derivation. Pure stdlib, deterministic.
"""

from __future__ import annotations

from document_processor.code_intelligence.reactor.symbolic_complexity import (
    FunctionComplexity,
    SymbolicComplexity,
    SymbolicComplexityAnalyzer,
)


def _analyse(src: str) -> SymbolicComplexity:
    return SymbolicComplexityAnalyzer().analyse(src)


# ── trivial cases ───────────────────────────────────────────────────


def test_empty_source_returns_constant_bound():
    r = _analyse("")
    assert r.worst_bound == "O(1)"
    assert r.functions == {}


def test_no_loops_no_recursion_is_constant():
    r = _analyse("def f(x):\n    return x + 1\n")
    assert r.worst_bound == "O(1)"
    fc = r.functions["f"]
    assert fc.loop_depth == 0
    assert fc.recursive_calls == 0
    assert fc.recursion == "none"


# ── polynomial loops ────────────────────────────────────────────────


def test_single_for_loop_is_linear():
    src = "def f(n):\n    for i in range(n):\n        pass\n"
    r = _analyse(src)
    assert r.worst_bound == "O(n)"
    assert r.functions["f"].loop_depth == 1


def test_nested_for_is_quadratic():
    src = (
        "def f(n):\n"
        "    for i in range(n):\n"
        "        for j in range(n):\n"
        "            pass\n"
    )
    r = _analyse(src)
    assert r.worst_bound == "O(n^2)"
    assert r.functions["f"].loop_depth == 2


def test_triple_nested_for_is_cubic():
    src = (
        "def f(n):\n"
        "    for i in range(n):\n"
        "        for j in range(n):\n"
        "            for k in range(n):\n"
        "                pass\n"
    )
    r = _analyse(src)
    assert r.worst_bound == "O(n^3)"


def test_while_loop_counted_as_loop():
    src = (
        "def f(n):\n"
        "    i = 0\n"
        "    while i < n:\n"
        "        i += 1\n"
    )
    r = _analyse(src)
    assert r.worst_bound == "O(n)"


def test_deep_nesting_caps_at_polynomial_k():
    src = (
        "def f(n):\n"
        "    for a in range(n):\n"
        "        for b in range(n):\n"
        "            for c in range(n):\n"
        "                for d in range(n):\n"
        "                    for e in range(n):\n"
        "                        for g in range(n):\n"
        "                            for h in range(n):\n"
        "                                pass\n"
    )
    r = _analyse(src)
    # 7 nested loops collapses to O(n^k) (high polynomial — cap).
    assert r.worst_bound == "O(n^k)"


# ── comprehensions count as loops ─────────────────────────────────


def test_listcomp_is_linear():
    src = "def f(xs):\n    return [x * 2 for x in xs]\n"
    r = _analyse(src)
    assert r.worst_bound == "O(n)"


def test_listcomp_with_two_for_clauses_is_quadratic():
    src = "def f(xs):\n    return [x * y for x in xs for y in xs]\n"
    r = _analyse(src)
    assert r.worst_bound == "O(n^2)"


def test_listcomp_inside_for_loop_is_quadratic():
    src = (
        "def f(n):\n"
        "    for i in range(n):\n"
        "        _ = [x for x in range(n)]\n"
    )
    r = _analyse(src)
    assert r.worst_bound == "O(n^2)"


def test_genexp_is_linear():
    src = "def f(xs):\n    return sum(x for x in xs)\n"
    r = _analyse(src)
    assert r.worst_bound == "O(n)"


# ── recursion ──────────────────────────────────────────────────────


def test_linear_recursion_is_polynomial():
    src = (
        "def fact(n):\n"
        "    if n <= 1: return 1\n"
        "    return n * fact(n - 1)\n"
    )
    r = _analyse(src)
    # Single recursive call, no loop → O(n) (loop_depth=0 + 1 for recursion).
    assert r.worst_bound == "O(n)"
    assert r.functions["fact"].recursion == "linear"


def test_binary_recursion_is_exponential():
    src = (
        "def fib(n):\n"
        "    if n < 2: return n\n"
        "    return fib(n - 1) + fib(n - 2)\n"
    )
    r = _analyse(src)
    assert r.worst_bound == "O(2^n)"
    assert r.functions["fib"].recursion == "binary"


def test_ternary_recursion_is_3pow_n():
    src = (
        "def f(n):\n"
        "    if n < 2: return 0\n"
        "    return f(n-1) + f(n-2) + f(n-3)\n"
    )
    r = _analyse(src)
    assert r.worst_bound == "O(3^n)"


def test_method_self_recursion_detected():
    """`self.method(...)` inside a method named `method` should still
    be flagged as recursive."""
    src = (
        "class C:\n"
        "    def fib(self, n):\n"
        "        if n < 2: return n\n"
        "        return self.fib(n-1) + self.fib(n-2)\n"
    )
    r = _analyse(src)
    assert r.worst_bound == "O(2^n)"


def test_recursion_with_loop_increases_polynomial():
    src = (
        "def f(n):\n"
        "    if n == 0: return 0\n"
        "    s = 0\n"
        "    for i in range(n):\n"
        "        s += i\n"
        "    return s + f(n - 1)\n"
    )
    r = _analyse(src)
    # 1 recursive call + loop_depth=1 → O(n^2).
    assert r.worst_bound == "O(n^2)"


# ── multiple functions: pick the worst ─────────────────────────────


def test_worst_function_dominates():
    src = (
        "def fast(xs):\n"
        "    return xs[0]\n"
        "def slow(xs):\n"
        "    out = []\n"
        "    for x in xs:\n"
        "        for y in xs:\n"
        "            out.append(x + y)\n"
        "    return out\n"
    )
    r = _analyse(src)
    assert r.worst_bound == "O(n^2)"
    assert r.worst_function == "slow"
    assert r.functions["fast"].bound == "O(1)"


# ── module-level loops ──────────────────────────────────────────────


def test_module_level_loop_recognised_when_no_functions():
    src = (
        "for i in range(100):\n"
        "    print(i)\n"
    )
    r = _analyse(src)
    assert r.worst_bound == "O(n)"
    assert r.worst_function == "<module>"


# ── syntax error path ──────────────────────────────────────────────


def test_syntax_error_returns_invalid_flag():
    r = _analyse("def broken(:\n    pass\n")
    assert r.syntax_valid is False
    assert r.syntax_error is not None


# ── compare_bounds helper ──────────────────────────────────────────


def test_compare_bounds_detects_underclaim():
    """Claim O(n) but symbolic bound is O(n^2) → claim is BELOW measured."""
    cmp = SymbolicComplexity.compare_bounds("O(n)", "O(n^2)")
    assert cmp == -1


def test_compare_bounds_equal():
    assert SymbolicComplexity.compare_bounds("O(n)", "O(n)") == 0


def test_compare_bounds_overclaim():
    assert SymbolicComplexity.compare_bounds("O(n^2)", "O(n)") == 1


def test_compare_bounds_unknown_returns_zero():
    """Unknown symbols → no judgement (don't penalise)."""
    assert SymbolicComplexity.compare_bounds("O(funky)", "O(n)") == 0


# ── to_dict ────────────────────────────────────────────────────────


def test_to_dict_round_trip():
    r = _analyse("def f(n):\n    for i in range(n):\n        pass\n")
    d = r.to_dict()
    assert "functions" in d
    assert d["worst_bound"] == "O(n)"
    assert d["worst_function"] == "f"
    assert d["total_loop_depth"] == 1
    assert d["functions"]["f"]["bound"] == "O(n)"
