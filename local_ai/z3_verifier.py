"""
Z3Verifier — SMT-backed sanity gate for algorithmic skeletons.

The Logic Engine produces a structured ``AlgorithmSkeleton`` describing
a candidate algorithm: invariants, loop measure, integer-bounded
variables, and a list of conditional cases. This module asks Z3 four
questions about that skeleton:

  1. **Termination** — is the loop's measure a non-negative integer
     that strictly decreases each iteration?
  2. **Overflow** — given declared bounds, do arithmetic operations
     stay within range under the loop body's transformation?
  3. **Exhaustive case analysis** — do the listed condition predicates
     cover the entire input domain (no hole, no missing branch)?
  4. **Invariants are satisfiable** — is there at least one state where
     all invariants hold simultaneously? (vacuous-truth check)

Each check returns a structured ``CheckResult`` with PASS / FAIL /
UNKNOWN + reason. The orchestrator (``Z3Verifier.verify_skeleton``)
runs all four and aggregates into a ``VerificationReport``.

Failure mode: when Z3 itself errors (timeout, can't parse) the
returned status is UNKNOWN with a reason — the caller can fall back
to whatever it does without the proof. The verifier never raises
into the engine layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from z3 import (
    And,
    Implies,
    Int,
    Not,
    Or,
    Solver,
    sat,
    unknown,
    unsat,
)

logger = logging.getLogger(__name__)


# ─── Public types ────────────────────────────────────────────────────


CheckStatus = Literal["pass", "fail", "unknown"]


@dataclass
class CheckResult:
    """One Z3 question's outcome."""

    name: str
    status: CheckStatus
    reason: str = ""
    counterexample: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "counterexample": dict(self.counterexample),
        }


@dataclass
class VerificationReport:
    """Aggregated verdict over all checks."""

    overall: CheckStatus
    checks: list[CheckResult] = field(default_factory=list)
    skeleton_id: str = ""

    @property
    def all_passed(self) -> bool:
        return self.overall == "pass" and all(c.passed for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "skeleton_id": self.skeleton_id,
            "checks": [c.to_dict() for c in self.checks],
        }


# ─── Skeleton schema (matches Logic Engine output) ──────────────────


@dataclass
class IntVarBound:
    """Declared bound for an integer variable used in the skeleton.

    The verifier asserts ``low <= var <= high`` so subsequent checks
    operate within the declared range. ``low``/`high`` may be ``None``
    to express a half-open bound.
    """

    name: str
    low: int | None = None
    high: int | None = None


@dataclass
class LoopSpec:
    """A single loop's termination contract.

    ``measure_var`` is the name of an integer variable that the loop
    body decreases each iteration (e.g. ``"n - i"`` would be the
    measure for a typical ``for i in range(n)``).  We model that with
    two states: pre- and post-iteration. ``post_expr`` is a Z3
    expression (as a string of supported ops) describing what
    ``measure_var`` becomes after one iteration.
    """

    measure_var: str
    post_decreases_by: int = 1   # how much measure_var drops per iter
    measure_low_bound: int = 0   # measure must stay ≥ this (default 0)


@dataclass
class CaseSplit:
    """One arm of an if/elif chain.

    ``predicate`` is a string like ``"x < 0"``, ``"x >= 0 and x < 10"``,
    etc. The verifier joins them via OR and asks Z3 whether the
    disjunction covers the integer domain (i.e. their negation is
    unsat).
    """

    predicate: str


@dataclass
class AlgorithmSkeleton:
    """Verifier-side representation of a Logic Engine candidate."""

    skeleton_id: str
    int_vars: list[IntVarBound] = field(default_factory=list)
    loops: list[LoopSpec] = field(default_factory=list)
    case_splits: list[CaseSplit] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)
    # For overflow checks: declared signed integer width. Default 64.
    int_width: int = 64

    def to_dict(self) -> dict[str, Any]:
        return {
            "skeleton_id": self.skeleton_id,
            "int_vars": [
                {"name": v.name, "low": v.low, "high": v.high}
                for v in self.int_vars
            ],
            "loops": [
                {"measure_var": l.measure_var,
                 "post_decreases_by": l.post_decreases_by,
                 "measure_low_bound": l.measure_low_bound}
                for l in self.loops
            ],
            "case_splits": [{"predicate": c.predicate}
                            for c in self.case_splits],
            "invariants": list(self.invariants),
            "int_width": self.int_width,
        }


# ─── Predicate parser ───────────────────────────────────────────────


def _parse_predicate(expr: str, env: dict[str, Any]) -> Any:
    """Translate a small subset of Python boolean expressions into Z3.

    Supports:
      - integer literals
      - declared variable names from `env`
      - +, -, *
      - <, <=, ==, !=, >=, >
      - and, or, not (Python keywords)
      - parentheses

    Anything else raises ``ValueError`` and the caller treats the
    check as UNKNOWN.
    """
    # We use Python's own AST to walk the expression, then map nodes
    # to Z3 constructors. This keeps us off ``eval()``.
    import ast

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"unparseable predicate: {exc}") from exc

    def _walk(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, bool)):
                return node.value
            raise ValueError(f"unsupported literal {node.value!r}")
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            raise ValueError(f"unknown variable {node.id}")
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return -_walk(node.operand)
            if isinstance(node.op, ast.Not):
                return Not(_walk(node.operand))
            raise ValueError(f"unsupported unary op {type(node.op).__name__}")
        if isinstance(node, ast.BinOp):
            left = _walk(node.left)
            right = _walk(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            raise ValueError(f"unsupported binary op {type(node.op).__name__}")
        if isinstance(node, ast.BoolOp):
            parts = [_walk(v) for v in node.values]
            if isinstance(node.op, ast.And):
                return And(*parts)
            if isinstance(node.op, ast.Or):
                return Or(*parts)
            raise ValueError(f"unsupported bool op {type(node.op).__name__}")
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise ValueError("chained comparisons not supported")
            left = _walk(node.left)
            right = _walk(node.comparators[0])
            op = node.ops[0]
            if isinstance(op, ast.Lt):     return left < right
            if isinstance(op, ast.LtE):    return left <= right
            if isinstance(op, ast.Gt):     return left > right
            if isinstance(op, ast.GtE):    return left >= right
            if isinstance(op, ast.Eq):     return left == right
            if isinstance(op, ast.NotEq):  return left != right
            raise ValueError(f"unsupported comparator {type(op).__name__}")
        raise ValueError(f"unsupported AST node {type(node).__name__}")

    return _walk(tree)


# ─── Verifier ───────────────────────────────────────────────────────


class Z3Verifier:
    """Stateless. One method per check + a ``verify_skeleton`` orchestrator."""

    def __init__(self, *, timeout_ms: int = 30_000) -> None:
        self._timeout = max(100, int(timeout_ms))

    # ── individual checks ───────────────────────────────────────────

    def check_termination(
        self, skeleton: AlgorithmSkeleton,
    ) -> CheckResult:
        """For each loop, prove its measure stays non-negative AND
        strictly decreases each iteration.

        Z3 query:
          ∃ measure_var, decl_bounds. measure_var >= 0
            ∧ (measure_var - step) < measure_var          ← decrease
            ∧ ¬(measure_var - step >= measure_low_bound)  ← measure
                                                            never below floor

        If unsat, the loop is well-founded (PASS). If sat, we have a
        counterexample (FAIL).
        """
        if not skeleton.loops:
            return CheckResult(
                name="termination",
                status="pass",
                reason="no loops declared (vacuously terminates)",
            )
        for loop in skeleton.loops:
            env = self._build_env([loop.measure_var] + [
                v.name for v in skeleton.int_vars
            ])
            measure = env.get(loop.measure_var)
            if measure is None:
                return CheckResult(
                    name="termination", status="unknown",
                    reason=f"measure_var {loop.measure_var} not in env",
                )
            solver = Solver()
            solver.set("timeout", self._timeout)
            # Apply declared bounds.
            self._assert_var_bounds(solver, env, skeleton.int_vars)
            # The loop body executes only while `measure > floor`;
            # the loop EXITS at `measure <= floor`. So a termination
            # violation requires the body to actually execute (i.e.
            # `measure > floor`) AND the post-step value to either
            # drop below floor (underflow inside the loop) OR not
            # strictly decrease.
            # Note the explicit `is not None` check — `int(0 or 1)`
            # silently coerces a step of zero to one, which would
            # mask the real bug of a non-decreasing measure.
            step = (
                int(loop.post_decreases_by)
                if loop.post_decreases_by is not None
                else 1
            )
            next_measure = measure - step
            solver.add(measure > loop.measure_low_bound)   # body runs
            solver.add(Or(
                next_measure < loop.measure_low_bound,     # post drops below floor
                next_measure >= measure,                    # post didn't decrease
            ))
            res = solver.check()
            if res == unsat:
                continue   # this loop is fine
            if res == unknown:
                return CheckResult(
                    name="termination", status="unknown",
                    reason="Z3 timed out / undecidable",
                )
            # sat → counterexample.
            model = solver.model()
            ce = self._extract_counterexample(model, env)
            return CheckResult(
                name="termination", status="fail",
                reason=(
                    f"loop on {loop.measure_var} can decrement by "
                    f"{step} but is not provably bounded below by "
                    f"{loop.measure_low_bound}"
                ),
                counterexample=ce,
            )
        return CheckResult(
            name="termination", status="pass",
            reason=f"all {len(skeleton.loops)} loop(s) terminate",
        )

    def check_overflow(
        self, skeleton: AlgorithmSkeleton,
    ) -> CheckResult:
        """Given declared bounds + the loop's decrement, ensure the
        measure post-step doesn't underflow / overflow the int_width.

        Phase 1A: only the loop measure path is checked. A future round
        can extend to general arithmetic in the body."""
        max_int = (1 << (skeleton.int_width - 1)) - 1
        min_int = -(1 << (skeleton.int_width - 1))

        for loop in skeleton.loops:
            env = self._build_env([loop.measure_var] + [
                v.name for v in skeleton.int_vars
            ])
            measure = env.get(loop.measure_var)
            if measure is None:
                continue
            solver = Solver()
            solver.set("timeout", self._timeout)
            self._assert_var_bounds(solver, env, skeleton.int_vars)
            step = (
                int(loop.post_decreases_by)
                if loop.post_decreases_by is not None
                else 1
            )
            next_measure = measure - step
            # Counterexample: a state where the post-step value escapes
            # the integer range.
            solver.add(measure >= loop.measure_low_bound)
            solver.add(Or(next_measure > max_int, next_measure < min_int))
            res = solver.check()
            if res == sat:
                model = solver.model()
                ce = self._extract_counterexample(model, env)
                return CheckResult(
                    name="overflow", status="fail",
                    reason=(
                        f"measure {loop.measure_var} can underflow / "
                        f"overflow the {skeleton.int_width}-bit range"
                    ),
                    counterexample=ce,
                )
            if res == unknown:
                return CheckResult(
                    name="overflow", status="unknown",
                    reason="Z3 timed out / undecidable",
                )
        return CheckResult(
            name="overflow", status="pass",
            reason="loop measures stay within declared int range",
        )

    def check_exhaustive_cases(
        self, skeleton: AlgorithmSkeleton,
    ) -> CheckResult:
        """Asks: is there an integer assignment under declared bounds
        where NONE of the case predicates hold? If sat, we have a hole
        in the case split → FAIL."""
        if not skeleton.case_splits:
            return CheckResult(
                name="exhaustive_cases", status="pass",
                reason="no case splits declared (vacuously exhaustive)",
            )
        env = self._build_env([v.name for v in skeleton.int_vars])
        if not env and not skeleton.int_vars:
            return CheckResult(
                name="exhaustive_cases", status="unknown",
                reason="no int_vars declared; cannot reason about domain",
            )

        try:
            preds = [_parse_predicate(c.predicate, env)
                     for c in skeleton.case_splits]
        except ValueError as exc:
            return CheckResult(
                name="exhaustive_cases", status="unknown",
                reason=f"predicate parse failed: {exc}",
            )

        solver = Solver()
        solver.set("timeout", self._timeout)
        self._assert_var_bounds(solver, env, skeleton.int_vars)
        # ¬(p1 ∨ p2 ∨ … ∨ pn) — sat means we found a hole.
        solver.add(Not(Or(*preds)))
        res = solver.check()
        if res == unsat:
            return CheckResult(
                name="exhaustive_cases", status="pass",
                reason=f"{len(preds)} case(s) cover the declared domain",
            )
        if res == unknown:
            return CheckResult(
                name="exhaustive_cases", status="unknown",
                reason="Z3 timed out / undecidable",
            )
        # sat → hole.
        ce = self._extract_counterexample(solver.model(), env)
        return CheckResult(
            name="exhaustive_cases", status="fail",
            reason="case split has a hole — not all inputs covered",
            counterexample=ce,
        )

    def check_invariants_satisfiable(
        self, skeleton: AlgorithmSkeleton,
    ) -> CheckResult:
        """Vacuous-truth check: is there at least one state where
        every declared invariant holds simultaneously? Useful catch
        for self-contradictory invariants like ``x > 0 ∧ x < 0``."""
        if not skeleton.invariants:
            return CheckResult(
                name="invariants_satisfiable", status="pass",
                reason="no invariants declared",
            )
        env = self._build_env([v.name for v in skeleton.int_vars])
        try:
            inv_exprs = [_parse_predicate(p, env)
                         for p in skeleton.invariants]
        except ValueError as exc:
            return CheckResult(
                name="invariants_satisfiable", status="unknown",
                reason=f"invariant parse failed: {exc}",
            )

        solver = Solver()
        solver.set("timeout", self._timeout)
        self._assert_var_bounds(solver, env, skeleton.int_vars)
        solver.add(And(*inv_exprs))
        res = solver.check()
        if res == sat:
            return CheckResult(
                name="invariants_satisfiable", status="pass",
                reason=f"all {len(inv_exprs)} invariant(s) co-satisfiable",
            )
        if res == unsat:
            return CheckResult(
                name="invariants_satisfiable", status="fail",
                reason=(
                    "declared invariants contradict each other — no "
                    "state satisfies them all"
                ),
            )
        return CheckResult(
            name="invariants_satisfiable", status="unknown",
            reason="Z3 timed out / undecidable",
        )

    # ── orchestrator ────────────────────────────────────────────────

    def verify_skeleton(
        self, skeleton: AlgorithmSkeleton,
    ) -> VerificationReport:
        """Run every check, aggregate."""
        try:
            checks = [
                self.check_invariants_satisfiable(skeleton),
                self.check_termination(skeleton),
                self.check_overflow(skeleton),
                self.check_exhaustive_cases(skeleton),
            ]
        except Exception as exc:
            logger.warning("z3_verifier_unexpected_failure: %s", exc)
            return VerificationReport(
                overall="unknown",
                skeleton_id=skeleton.skeleton_id,
                checks=[CheckResult(
                    name="orchestrator", status="unknown",
                    reason=str(exc)[:300],
                )],
            )

        # Aggregation: any FAIL → fail. All PASS → pass. Otherwise unknown.
        if any(c.status == "fail" for c in checks):
            overall: CheckStatus = "fail"
        elif all(c.status == "pass" for c in checks):
            overall = "pass"
        else:
            overall = "unknown"
        return VerificationReport(
            overall=overall, checks=checks,
            skeleton_id=skeleton.skeleton_id,
        )

    # ── helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _build_env(var_names: list[str]) -> dict[str, Any]:
        seen: set[str] = set()
        env: dict[str, Any] = {}
        for name in var_names:
            if name and name not in seen:
                env[name] = Int(name)
                seen.add(name)
        return env

    @staticmethod
    def _assert_var_bounds(
        solver: Solver,
        env: dict[str, Any],
        int_vars: list[IntVarBound],
    ) -> None:
        for v in int_vars:
            sym = env.get(v.name)
            if sym is None:
                continue
            if v.low is not None:
                solver.add(sym >= v.low)
            if v.high is not None:
                solver.add(sym <= v.high)

    @staticmethod
    def _extract_counterexample(model: Any, env: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, sym in env.items():
            try:
                val = model.eval(sym, model_completion=True)
                out[name] = int(val.as_long())
            except Exception:
                continue
        return out
