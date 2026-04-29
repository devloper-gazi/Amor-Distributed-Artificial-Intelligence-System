"""
SymbolicComplexityAnalyzer — pure-AST Big-O upper-bound derivation.

Walks each function body in parsed Python source and conservatively
upper-bounds the time complexity of the function by inspecting:

  • loop-nesting depth (each `for`/`while` adds an `n` factor)
  • recursion structure (single recursive call = same complexity;
    multiple recursive calls in one body = exponential branch factor)
  • generator-expression / list-comprehension nesting (counted as
    nested loops)

The output is intentionally CONSERVATIVE — we'd rather upper-bound
loose (claim O(n²) when actual is O(n log n)) than under-claim.
The Reactor's TournamentRunner uses this as a sanity check on the
LLM's stated complexity: a >1-tier disagreement is a red flag.

Pure stdlib — no Hypothesis, no numpy. Deterministic. Cheap enough to
run on every implementation in the tournament.

Output schema
-------------

    SymbolicComplexity(
        functions={
            "fn_name": FunctionComplexity(
                name=...,
                loop_depth=2,
                recursion="binary",
                bound="O(n^2)",
                bound_factor=2,
            ),
        },
        worst_bound="O(2^n)",
        worst_function="fib",
        total_loop_depth=2,
        total_recursive_functions=1,
    )

Complexity ladder (lowest→highest):

    O(1)  <  O(log n)  <  O(n)  <  O(n log n)  <  O(n^2)  <  O(n^3)
    <  O(n^k)  <  O(2^n)  <  O(n!)
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


RecursionKind = Literal["none", "linear", "binary", "multi"]


# Complexity ladder rank — higher = worse. Used to pick the worst
# function when summarising a whole module's complexity.
_RANK: dict[str, int] = {
    "O(1)":          0,
    "O(log n)":      1,
    "O(n)":          2,
    "O(n log n)":    3,
    "O(n^2)":        4,
    "O(n^3)":        5,
    "O(n^4)":        6,
    "O(n^5)":        7,
    "O(n^k)":        9,
    "O(2^n)":        12,
    "O(3^n)":        13,
    "O(n!)":         15,
}


def _polynomial_bound(loop_depth: int) -> str:
    """Bound for non-recursive functions with a given loop nesting."""
    if loop_depth <= 0:
        return "O(1)"
    if loop_depth == 1:
        return "O(n)"
    if loop_depth >= 6:
        return "O(n^k)"
    return f"O(n^{loop_depth})"


def _exponential_bound(recursive_calls: int, loop_depth: int) -> str:
    """Conservative bound for recursive functions.

    `recursive_calls` is the count of self-call sites in the body.
    Two or more calls per invocation → branching exponential.
    A linear single recursive call combined with a loop_depth of d
    gives O(n^{d+1}) (loop body recurses into smaller-n invocations).
    """
    if recursive_calls >= 2:
        # 2 calls → O(2^n), 3 → O(3^n).  Cap at "factorial-ish" threshold.
        if recursive_calls >= 5:
            return "O(n!)"
        return f"O({recursive_calls}^n)"
    # Single recursive call — depth + body work.
    return _polynomial_bound(loop_depth + 1)


@dataclass
class FunctionComplexity:
    """Per-function bound + the inputs we used to derive it."""

    name: str
    loop_depth: int = 0
    recursive_calls: int = 0
    recursion: RecursionKind = "none"
    bound: str = "O(1)"
    bound_factor: int = 0
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "loop_depth": self.loop_depth,
            "recursive_calls": self.recursive_calls,
            "recursion": self.recursion,
            "bound": self.bound,
            "bound_factor": self.bound_factor,
            "line": self.line,
        }


@dataclass
class SymbolicComplexity:
    """Whole-source bound + per-function breakdown."""

    functions: dict[str, FunctionComplexity] = field(default_factory=dict)
    worst_bound: str = "O(1)"
    worst_function: str | None = None
    total_loop_depth: int = 0
    total_recursive_functions: int = 0
    syntax_valid: bool = True
    syntax_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "functions": {n: f.to_dict() for n, f in self.functions.items()},
            "worst_bound": self.worst_bound,
            "worst_function": self.worst_function,
            "total_loop_depth": self.total_loop_depth,
            "total_recursive_functions": self.total_recursive_functions,
            "syntax_valid": self.syntax_valid,
            "syntax_error": self.syntax_error,
        }

    @staticmethod
    def compare_bounds(claim: str, measured: str) -> int:
        """Return -1/0/+1 if claim is below / equal / above measured.

        The TournamentRunner uses this to flag candidates whose claimed
        complexity is *better than* what the symbolic analysis can
        prove (a red flag for over-promising).
        """
        c = _RANK.get(claim.strip(), -1)
        m = _RANK.get(measured.strip(), -1)
        if c == -1 or m == -1:
            return 0
        if c < m:
            return -1
        if c > m:
            return 1
        return 0


# ─── Visitor ─────────────────────────────────────────────────────────


class _FunctionVisitor(ast.NodeVisitor):
    """Walks a single function body. Tracks max loop nesting + count
    of self-recursive calls."""

    def __init__(self, function_name: str):
        self.function_name = function_name
        self._depth = 0
        self.max_depth = 0
        self.recursive_calls = 0

    def _enter_loop(self) -> None:
        self._depth += 1
        if self._depth > self.max_depth:
            self.max_depth = self._depth

    def _exit_loop(self) -> None:
        self._depth -= 1

    def visit_For(self, node: ast.For) -> None:
        self._enter_loop()
        self.generic_visit(node)
        self._exit_loop()

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._enter_loop()
        self.generic_visit(node)
        self._exit_loop()

    def visit_While(self, node: ast.While) -> None:
        self._enter_loop()
        self.generic_visit(node)
        self._exit_loop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        # Each `for` clause in the comprehension counts as one loop level.
        depth = max(1, len(node.generators))
        for _ in range(depth):
            self._enter_loop()
        self.generic_visit(node)
        for _ in range(depth):
            self._exit_loop()

    visit_SetComp  = visit_ListComp  # same shape
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_Call(self, node: ast.Call) -> None:
        # Self-recursion detection — bare-name call matching the
        # enclosing function. Doesn't catch attribute-style calls
        # (e.g. Class.fib(n-1) inside a method), which is acceptable
        # for an upper-bound check.
        func = node.func
        if isinstance(func, ast.Name) and func.id == self.function_name:
            self.recursive_calls += 1
        elif isinstance(func, ast.Attribute):
            # `self.fib(n-1)` or `cls.fib(n-1)` — heuristic: if the
            # method name matches our function name, count it.
            if func.attr == self.function_name:
                self.recursive_calls += 1
        self.generic_visit(node)


def _analyse_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> FunctionComplexity:
    visitor = _FunctionVisitor(node.name)
    visitor.visit(node)
    recursion: RecursionKind = (
        "none"   if visitor.recursive_calls == 0
        else "linear" if visitor.recursive_calls == 1
        else "binary" if visitor.recursive_calls == 2
        else "multi"
    )
    if visitor.recursive_calls > 0:
        bound = _exponential_bound(visitor.recursive_calls, visitor.max_depth)
        bound_factor = visitor.recursive_calls
    else:
        bound = _polynomial_bound(visitor.max_depth)
        bound_factor = visitor.max_depth
    return FunctionComplexity(
        name=node.name,
        loop_depth=visitor.max_depth,
        recursive_calls=visitor.recursive_calls,
        recursion=recursion,
        bound=bound,
        bound_factor=bound_factor,
        line=node.lineno,
    )


class SymbolicComplexityAnalyzer:
    """Pure-AST Big-O upper-bound derivation. Stateless."""

    def analyse(self, source: str) -> SymbolicComplexity:
        if not (source or "").strip():
            return SymbolicComplexity()
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return SymbolicComplexity(
                syntax_valid=False,
                syntax_error=f"Line {exc.lineno}: {exc.msg}",
            )

        functions: dict[str, FunctionComplexity] = {}
        total_depth = 0
        total_recursive = 0

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fc = _analyse_function(node)
                # Ambiguity policy — when two functions share a name,
                # keep the worst.
                existing = functions.get(fc.name)
                if existing is None or _RANK.get(fc.bound, 0) > _RANK.get(existing.bound, 0):
                    functions[fc.name] = fc
                total_depth += fc.loop_depth
                if fc.recursive_calls > 0:
                    total_recursive += 1

        if not functions:
            # Top-level statements with loops — analyse module-as-function.
            visitor = _FunctionVisitor("<module>")
            for stmt in tree.body:
                visitor.visit(stmt)
            if visitor.max_depth > 0:
                fc = FunctionComplexity(
                    name="<module>",
                    loop_depth=visitor.max_depth,
                    recursive_calls=0,
                    recursion="none",
                    bound=_polynomial_bound(visitor.max_depth),
                    bound_factor=visitor.max_depth,
                    line=1,
                )
                functions["<module>"] = fc
                total_depth = visitor.max_depth

        worst_name: str | None = None
        worst_rank = -1
        for n, fc in functions.items():
            rank = _RANK.get(fc.bound, 0)
            if rank > worst_rank:
                worst_rank = rank
                worst_name = n

        return SymbolicComplexity(
            functions=functions,
            worst_bound=(
                functions[worst_name].bound if worst_name else "O(1)"
            ),
            worst_function=worst_name,
            total_loop_depth=total_depth,
            total_recursive_functions=total_recursive,
        )
