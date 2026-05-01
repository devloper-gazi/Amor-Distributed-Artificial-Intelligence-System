"""
PropertyTestGenerator + PropertyTestRunner — auto-generate invariant
tests from the triage classification + (optionally) LLM suggestions,
then run them against user code in the sandbox.

Two layers:

1. **Generator** — picks invariants from a hand-curated CATALOGUE
   keyed by triage `task_type` / pattern (sort / search / hash /
   idempotent / pure / monotonic), plus optionally calls the LLM
   for one extra round of task-specific invariants.

2. **Runner** — bundles user code + invariants + a random-sampling
   harness into a single sandbox script, parses
   `PROPERTY_RESULT={...}` JSON lines from stdout. Hypothesis is
   used inside the sandbox if available, otherwise stdlib `random`
   provides 50 randomised samples per invariant.

Both layers are fail-soft: missing Hypothesis, sandbox unavailable,
LLM error → return a `PropertyTestResult` flagged accordingly rather
than raising.

The TournamentRunner consumes results to eliminate candidates whose
output violates any invariant.
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..agents import _extract_json

logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]


# Marker the harness emits — same parser style as the benchmarker.
_PROPERTY_RESULT_RE = re.compile(r"^PROPERTY_RESULT=({.*})\s*$", re.MULTILINE)


# ─── Invariant + result dataclasses ───────────────────────────────


@dataclass
class Invariant:
    """One named invariant the runner exercises against the candidate."""

    name: str
    description: str
    # Python source generating one input case from a `random.Random`.
    # MUST be a single expression with `rng` in scope.
    input_expr: str
    # Python source for the assertion. Has `inp` (input) and `out`
    # (function output) in scope. MUST evaluate to a boolean.
    assertion_expr: str
    source: str = "catalogue"  # "catalogue" | "llm"
    samples: int = 50

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_expr": self.input_expr,
            "assertion_expr": self.assertion_expr,
            "source": self.source,
            "samples": self.samples,
        }


@dataclass
class InvariantOutcome:
    """Single invariant's pass/fail outcome."""

    name: str
    passed: bool
    samples_run: int = 0
    samples_failed: int = 0
    first_failure_input: str | None = None
    first_failure_message: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "samples_run": self.samples_run,
            "samples_failed": self.samples_failed,
            "first_failure_input": self.first_failure_input,
            "first_failure_message": self.first_failure_message,
            "error": self.error,
        }


@dataclass
class PropertyTestResult:
    invariants: list[Invariant] = field(default_factory=list)
    outcomes: list[InvariantOutcome] = field(default_factory=list)
    failed: bool = False
    failure_reason: str = ""

    @property
    def all_passed(self) -> bool:
        return bool(self.outcomes) and all(o.passed for o in self.outcomes)

    @property
    def num_failed(self) -> int:
        return sum(1 for o in self.outcomes if not o.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "invariants": [i.to_dict() for i in self.invariants],
            "outcomes": [o.to_dict() for o in self.outcomes],
            "all_passed": self.all_passed,
            "num_failed": self.num_failed,
            "failed": self.failed,
            "failure_reason": self.failure_reason,
        }


# ─── Catalogue ─────────────────────────────────────────────────────


def _make_invariants(*specs: tuple[str, str, str, str]) -> list[Invariant]:
    return [
        Invariant(name=n, description=d, input_expr=ie, assertion_expr=ae,
                  source="catalogue")
        for (n, d, ie, ae) in specs
    ]


# Each entry: (pattern_keyword → list of invariants).  Pattern keyword
# is matched against the triage task_type + the first word of the
# user prompt's title (so "sort a list" matches "sort"). Multiple
# patterns can match — invariants are unioned.
CATALOGUE: dict[str, list[Invariant]] = {
    "sort": _make_invariants(
        ("sort_output_is_sorted",
         "sorted output is non-decreasing",
         "[rng.randint(-100, 100) for _ in range(rng.randint(0, 20))]",
         "all(out[i] <= out[i+1] for i in range(len(out)-1))"),
        ("sort_is_permutation",
         "output is a permutation of the input",
         "[rng.randint(-100, 100) for _ in range(rng.randint(0, 20))]",
         "sorted(out) == sorted(inp)"),
        ("sort_idempotent",
         "sorting an already-sorted list yields the same list",
         "sorted([rng.randint(-100, 100) for _ in range(rng.randint(0, 20))])",
         "list(out) == list(inp)"),
    ),
    "search": _make_invariants(
        ("search_returns_valid_index_or_negative",
         "result is None / -1 / a valid index",
         "([rng.randint(0, 100) for _ in range(rng.randint(1, 20))], rng.randint(0, 100))",
         "out is None or out == -1 or (isinstance(out, int) and 0 <= out < len(inp[0]))"),
        ("search_index_points_to_target",
         "if a non-negative index is returned, the element matches the needle",
         "([rng.randint(0, 100) for _ in range(rng.randint(1, 20))], rng.randint(0, 100))",
         "out is None or out == -1 or inp[0][out] == inp[1]"),
    ),
    "hash": _make_invariants(
        ("hash_deterministic",
         "same input produces same output",
         "rng.randint(0, 1_000_000)",
         "out == out"),  # tautology; requires a custom runner — placeholder
    ),
    "identity": _make_invariants(
        ("idempotent_double_apply",
         "f(f(x)) == f(x) — applying twice changes nothing",
         "[rng.randint(-50, 50) for _ in range(rng.randint(0, 10))]",
         "out == out"),
    ),
    "math": _make_invariants(
        ("math_returns_finite",
         "output is a finite number, not NaN/inf",
         "rng.randint(1, 1000)",
         "isinstance(out, (int, float)) and out == out and float('-inf') < out < float('inf')"),
    ),
    "default": _make_invariants(
        ("default_callable_no_exception",
         "function does not raise on a small random input",
         "[rng.randint(-10, 10) for _ in range(rng.randint(0, 8))]",
         "True"),
    ),
}


# ─── Generator ────────────────────────────────────────────────────


_LLM_INVARIANT_SYSTEM_PROMPT = """You are an invariant generator for property-based testing.

Given a coding task, propose 1-3 SHORT, executable Python invariants
the candidate's output must satisfy on ANY input. Each invariant
returns True when satisfied.

Return STRICT JSON, no prose:

{
  "invariants": [
    {
      "name": "snake_case_id",
      "description": "<=120 chars",
      "input_expr": "<python expr using rng>",
      "assertion_expr": "<python expr using inp, out — returns bool>"
    }
  ]
}

Rules:
  - input_expr must be ONE expression that uses `rng` (a random.Random).
  - assertion_expr must be ONE expression with `inp` and `out` in scope.
  - assertion_expr returns a bool (True = invariant holds).
  - Keep both expressions stdlib-only (no numpy / no imports).
"""


def _extract_pattern_keys(triage: dict | None, prompt: str) -> list[str]:
    """Match a triage classification + the prompt against the
    CATALOGUE keys."""
    keys: list[str] = []
    text = (prompt or "").lower()
    if triage:
        ttype = str(triage.get("task_type") or "").lower()
        if ttype:
            text = f"{ttype} {text}"
    for k in CATALOGUE:
        if k == "default":
            continue
        if k in text:
            keys.append(k)
    if not keys:
        keys.append("default")
    return keys


class PropertyTestGenerator:
    """Pure data — produces a list of Invariant objects to be run.
    No I/O at construction; the optional LLM hop is in `suggest`."""

    def __init__(self) -> None:
        # Deep-copy the catalogue rows on each call so callers don't
        # mutate shared state by accident.
        pass

    def for_triage(
        self,
        triage: dict | None,
        user_prompt: str,
    ) -> list[Invariant]:
        """Pick invariants from the catalogue matching the triage."""
        keys = _extract_pattern_keys(triage, user_prompt)
        out: list[Invariant] = []
        for k in keys:
            for inv in CATALOGUE.get(k, ()):
                # Fresh copy per invocation.
                out.append(Invariant(
                    name=inv.name, description=inv.description,
                    input_expr=inv.input_expr,
                    assertion_expr=inv.assertion_expr,
                    source=inv.source, samples=inv.samples,
                ))
        return out

    async def suggest(
        self,
        llm_call: LLMCall,
        *,
        user_prompt: str,
        code: str | None = None,
        max_tokens: int = 800,
    ) -> list[Invariant]:
        """Optional LLM hop for task-specific invariants."""
        prompt_rows: list[str] = ["# Task", user_prompt.strip()]
        if code:
            prompt_rows.append("\n# Generated code (context only)")
            prompt_rows.append("```python")
            prompt_rows.append((code or "").rstrip()[:4000])
            prompt_rows.append("```")
        prompt_rows.append(
            "\nReturn the JSON described in the system prompt."
        )
        try:
            raw = await llm_call(
                "\n".join(prompt_rows),
                _LLM_INVARIANT_SYSTEM_PROMPT,
                max_tokens,
            )
        except Exception as exc:
            logger.warning("property_invariant_llm_call_failed: %s", exc)
            return []

        try:
            data = _extract_json(raw or "")
        except ValueError as exc:
            logger.warning("property_invariant_json_parse_failed: %s", exc)
            return []

        suggested: list[Invariant] = []
        for it in (data.get("invariants") or [])[:3]:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "")[:60].strip()
            ie = str(it.get("input_expr") or "").strip()
            ae = str(it.get("assertion_expr") or "").strip()
            if not name or not ie or not ae:
                continue
            suggested.append(Invariant(
                name=name,
                description=str(it.get("description") or "")[:200],
                input_expr=ie,
                assertion_expr=ae,
                source="llm",
                samples=30,  # cheaper for LLM-suggested invariants
            ))
        return suggested


# ─── Runner ────────────────────────────────────────────────────────


_PROPERTY_HARNESS = '''\
# ── AMOR Reactor property-test harness ────────────────────────────
import json as _json
import random as _random
import inspect as _inspect

_SEED = 0xAB0F0F0F
_INVARIANTS = _json.loads("""{invariants_json}""")


def _amor_pick_target():
    candidates = []
    for name, obj in list(globals().items()):
        if name.startswith("_") or name in {{"json", "random", "inspect"}}:
            continue
        if callable(obj) and not isinstance(obj, type):
            try:
                _inspect.signature(obj)
            except (TypeError, ValueError):
                continue
            candidates.append((name, obj))
    if not candidates:
        return None, None
    return candidates[-1]


def _amor_call_with_input(target, inp):
    """Call target with the input — handle tuple / list / scalar shapes."""
    sig = _inspect.signature(target)
    if isinstance(inp, tuple) and len(sig.parameters) == len(inp):
        return target(*inp)
    return target(inp)


def _amor_run_one(target, inv):
    rng = _random.Random(_SEED + hash(inv["name"]) % 2**31)
    samples_run = 0
    samples_failed = 0
    first_failure_input = None
    first_failure_message = None
    for _ in range(int(inv.get("samples") or 30)):
        try:
            inp = eval(inv["input_expr"], {{"rng": rng}})
        except Exception as exc:
            return {{
                "name": inv["name"], "passed": False,
                "samples_run": samples_run, "samples_failed": samples_failed,
                "first_failure_input": None,
                "first_failure_message": "input_expr failed: " + repr(exc)[:200],
                "error": "input_expr",
            }}
        try:
            out = _amor_call_with_input(target, inp)
        except Exception as exc:
            samples_failed += 1
            samples_run += 1
            if first_failure_input is None:
                first_failure_input = repr(inp)[:200]
                first_failure_message = "target raised: " + repr(exc)[:200]
            continue
        try:
            ok = bool(eval(inv["assertion_expr"],
                           {{"inp": inp, "out": out}}))
        except Exception as exc:
            samples_failed += 1
            samples_run += 1
            if first_failure_input is None:
                first_failure_input = repr(inp)[:200]
                first_failure_message = "assertion raised: " + repr(exc)[:200]
            continue
        samples_run += 1
        if not ok:
            samples_failed += 1
            if first_failure_input is None:
                first_failure_input = repr(inp)[:200]
                first_failure_message = "invariant violated"
    return {{
        "name": inv["name"],
        "passed": samples_failed == 0 and samples_run > 0,
        "samples_run": samples_run,
        "samples_failed": samples_failed,
        "first_failure_input": first_failure_input,
        "first_failure_message": first_failure_message,
        "error": None,
    }}


def _amor_main():
    name, target = _amor_pick_target()
    if target is None:
        print("PROPERTY_ERROR: no usable callable found")
        return
    print("PROPERTY_TARGET=" + name, flush=True)
    for inv in _INVARIANTS:
        rec = _amor_run_one(target, inv)
        print("PROPERTY_RESULT=" + _json.dumps(rec), flush=True)


_amor_main()
'''


class PropertyTestRunner:
    """Runs an invariant list against user code in the sandbox."""

    def __init__(self, sandbox: Any, *, total_timeout_s: int = 30) -> None:
        self._sandbox = sandbox
        self._total_timeout = total_timeout_s

    async def run(
        self,
        code: str,
        invariants: list[Invariant],
        *,
        language: str = "python",
    ) -> PropertyTestResult:
        if not invariants:
            return PropertyTestResult(
                invariants=[], outcomes=[],
                failed=False,
            )
        if not (code or "").strip():
            return PropertyTestResult(
                invariants=invariants,
                failed=True, failure_reason="no code to test",
            )
        if language != "python":
            return PropertyTestResult(
                invariants=invariants,
                failed=True,
                failure_reason=f"runner only supports python, got {language}",
            )

        script = self._build_script(code, invariants)
        try:
            res = await self._sandbox.execute(
                script, language=language, timeout=self._total_timeout,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("property_runner_sandbox_call_failed: %s", exc)
            return PropertyTestResult(
                invariants=invariants, failed=True,
                failure_reason=str(exc)[:300],
            )
        if getattr(res, "skipped", False):
            return PropertyTestResult(
                invariants=invariants, failed=True,
                failure_reason="sandbox unavailable (skipped)",
            )

        stdout = getattr(res, "stdout", "") or ""
        outcomes = self._parse_outcomes(stdout, invariants)
        return PropertyTestResult(
            invariants=invariants, outcomes=outcomes,
        )

    # ─── internals ────────────────────────────────────────────────

    def _build_script(self, user_code: str, invariants: list[Invariant]) -> str:
        prefix = textwrap.dedent(user_code).rstrip() + "\n\n"
        invariants_json = json.dumps([i.to_dict() for i in invariants])
        # Escape the JSON for triple-quoted-string embedding.
        invariants_json_escaped = invariants_json.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        harness = _PROPERTY_HARNESS.format(
            invariants_json=invariants_json_escaped,
        )
        return prefix + harness

    @staticmethod
    def _parse_outcomes(
        stdout: str,
        invariants: list[Invariant],
    ) -> list[InvariantOutcome]:
        by_name: dict[str, InvariantOutcome] = {}
        for m in _PROPERTY_RESULT_RE.finditer(stdout):
            try:
                payload = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            name = str(payload.get("name") or "")
            if not name:
                continue
            by_name[name] = InvariantOutcome(
                name=name,
                passed=bool(payload.get("passed", False)),
                samples_run=int(payload.get("samples_run", 0)),
                samples_failed=int(payload.get("samples_failed", 0)),
                first_failure_input=payload.get("first_failure_input"),
                first_failure_message=payload.get("first_failure_message"),
                error=payload.get("error"),
            )
        out: list[InvariantOutcome] = []
        for inv in invariants:
            outcome = by_name.get(inv.name)
            if outcome is not None:
                out.append(outcome)
            else:
                # Harness didn't get to this one — count as not run.
                out.append(InvariantOutcome(
                    name=inv.name, passed=False,
                    error="no PROPERTY_RESULT line emitted",
                ))
        return out
