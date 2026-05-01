"""
LogicEngine — produces algorithmic skeletons (Z3-verifiable) from
free-text user requests.

This is the FIRST stage of the neuro-symbolic pipeline:

    user request ──► LogicEngine ──► AlgorithmSkeleton (JSON)
                                          │
                                          ▼
                                     Z3Verifier
                                          │
                                          ▼
                                     LLM Translator
                                          │
                                          ▼
                                       Sandbox

Phase 1A ships the **rule_based** strategy: pattern-match the user's
prompt against a curated catalogue of algorithm templates (sort,
search, count, hash-table ops, recursion, simple math). Each template
emits a fully-formed AlgorithmSkeleton that Z3 can verify without
needing an LLM hop.

Two more strategies are scaffolded for future rounds:
  - ``small_model``: ask a small (~1.5B) LLM for pseudocode, parse
    into a skeleton.
  - ``funsearch``: produce N candidate skeletons, Z3-verify all,
    pick the highest-confidence survivor.

Design note: the catalogue templates each map to a SHAPE of skeleton
the Z3Verifier can prove cleanly:

  - Loop with strictly-decreasing measure ≥ 0 (sort, search, count).
  - Three-way comparison case split (sign, three-way merge sort).
  - Single recursive call on a smaller input (binary search, list
    halve).

Each template's invariants are conservative — they describe the
loop's *frame* without claiming optimality.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from .z3_verifier import (
    AlgorithmSkeleton,
    CaseSplit,
    IntVarBound,
    LoopSpec,
)

logger = logging.getLogger(__name__)


Strategy = Literal["rule_based", "small_model", "funsearch"]
LLMCall = Callable[[str, str | None, int], Awaitable[str]]


# ─── Public output shape ─────────────────────────────────────────────


@dataclass
class LogicSkeleton:
    """LogicEngine's full output: a verifier-ready skeleton plus the
    metadata the LLM Translator and the Memory layer want."""

    skeleton_id: str
    algorithm_type: str
    pseudocode_steps: list[str] = field(default_factory=list)
    state_machine: dict[str, Any] = field(default_factory=dict)
    ast_skeleton: dict[str, Any] = field(default_factory=dict)
    invariants: list[str] = field(default_factory=list)
    termination_argument: str = ""
    complexity_hint: str = ""
    matched_template: str = ""
    confidence: float = 0.0
    verifier_skeleton: AlgorithmSkeleton | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "skeleton_id": self.skeleton_id,
            "algorithm_type": self.algorithm_type,
            "pseudocode_steps": list(self.pseudocode_steps),
            "state_machine": dict(self.state_machine),
            "ast_skeleton": dict(self.ast_skeleton),
            "invariants": list(self.invariants),
            "termination_argument": self.termination_argument,
            "complexity_hint": self.complexity_hint,
            "matched_template": self.matched_template,
            "confidence": round(self.confidence, 4),
            "verifier_skeleton": (
                self.verifier_skeleton.to_dict()
                if self.verifier_skeleton else None
            ),
        }


# ─── Catalogue ───────────────────────────────────────────────────────


@dataclass
class _Template:
    """One catalogue entry. Matches against keywords in the user
    prompt and produces a LogicSkeleton."""

    name: str
    keywords: tuple[str, ...]
    algorithm_type: str
    builder: Callable[[str, str], LogicSkeleton]


def _slug(text: str) -> str:
    """Snake-case slug, truncated to 60 chars. Trailing/leading
    underscores stripped *after* truncation so a mid-word cut can't
    leave a hanging `_`."""
    base = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return base[:60].strip("_") or "task"


# ── individual builders ───────────────────────────────────────────────


def _build_sort(prompt: str, sid: str) -> LogicSkeleton:
    """Generic in-place sort skeleton (selection / bubble shape)."""
    skeleton = AlgorithmSkeleton(
        skeleton_id=sid,
        int_vars=[
            IntVarBound("n", low=0, high=10_000),
        ],
        loops=[
            LoopSpec(measure_var="n", post_decreases_by=1, measure_low_bound=0),
        ],
        invariants=["n >= 0"],
        int_width=64,
    )
    return LogicSkeleton(
        skeleton_id=sid,
        algorithm_type="sort",
        pseudocode_steps=[
            "validate input is a non-empty list",
            "iterate from i=0 to n-1",
            "  find the smallest element in input[i:]",
            "  swap it with input[i]",
            "return input",
        ],
        state_machine={
            "states": ["INIT", "SCAN", "SWAP", "DONE"],
            "transitions": [
                {"from": "INIT", "to": "SCAN", "condition": "i < n"},
                {"from": "SCAN", "to": "SWAP", "condition": "min_found"},
                {"from": "SWAP", "to": "SCAN", "condition": "i < n"},
                {"from": "SCAN", "to": "DONE", "condition": "i == n"},
            ],
        },
        ast_skeleton={
            "function_name": "sort",
            "parameters": [{"name": "data", "type": "List[int]"}],
            "return_type": "List[int]",
            "loops": [{
                "type": "for", "bound": "len(data)",
                "body_hint": "find min in suffix and swap",
            }],
            "conditions": [{"predicate": "data is not None",
                             "exhaustive": True}],
        },
        invariants=[
            "after iteration i, data[0..i] is sorted",
            "data length never changes",
        ],
        termination_argument=(
            "outer loop counter i strictly increases by 1 from 0 to n-1; "
            "n is fixed and finite"
        ),
        complexity_hint="O(n^2)",
        matched_template="sort",
        confidence=0.85,
        verifier_skeleton=skeleton,
    )


def _build_linear_search(prompt: str, sid: str) -> LogicSkeleton:
    skeleton = AlgorithmSkeleton(
        skeleton_id=sid,
        int_vars=[
            IntVarBound("n", low=0, high=10_000),
            IntVarBound("idx", low=-1, high=10_000),
        ],
        loops=[
            LoopSpec(measure_var="n", post_decreases_by=1, measure_low_bound=0),
        ],
        case_splits=[
            CaseSplit(predicate="idx == -1"),     # not found
            CaseSplit(predicate="idx >= 0"),       # found
        ],
        invariants=["n >= 0", "idx >= -1"],
    )
    return LogicSkeleton(
        skeleton_id=sid,
        algorithm_type="search",
        pseudocode_steps=[
            "iterate over index i from 0 to len(data)-1",
            "  if data[i] equals target: return i",
            "return -1 (not found)",
        ],
        state_machine={
            "states": ["INIT", "PROBE", "FOUND", "NOT_FOUND"],
            "transitions": [
                {"from": "INIT", "to": "PROBE", "condition": "i < n"},
                {"from": "PROBE", "to": "FOUND", "condition": "data[i] == target"},
                {"from": "PROBE", "to": "PROBE", "condition": "data[i] != target"},
                {"from": "PROBE", "to": "NOT_FOUND", "condition": "i == n"},
            ],
        },
        ast_skeleton={
            "function_name": "search",
            "parameters": [
                {"name": "data", "type": "List[int]"},
                {"name": "target", "type": "int"},
            ],
            "return_type": "int",
            "loops": [{
                "type": "for", "bound": "len(data)",
                "body_hint": "compare each element to target",
            }],
            "conditions": [
                {"predicate": "data[i] == target", "exhaustive": False},
                {"predicate": "data[i] != target", "exhaustive": False},
            ],
        },
        invariants=[
            "result is either -1 or a valid index 0..n-1",
            "if result >= 0 then data[result] == target",
        ],
        termination_argument=(
            "loop counter i strictly increases from 0 to n; n is fixed"
        ),
        complexity_hint="O(n)",
        matched_template="linear_search",
        confidence=0.90,
        verifier_skeleton=skeleton,
    )


def _build_binary_search(prompt: str, sid: str) -> LogicSkeleton:
    """Binary search — measure is `high - low`, halves each iteration.

    The verifier_skeleton's invariants must be PARSEABLE predicates
    over declared int_vars. Higher-level natural-language invariants
    (`if found, target is at returned index`, `data is sorted`) live
    on LogicSkeleton.invariants instead — they'll inform the Z3
    Translator stage downstream but aren't asserted by Z3 itself.
    """
    skeleton = AlgorithmSkeleton(
        skeleton_id=sid,
        int_vars=[
            IntVarBound("range_size", low=0, high=10_000),
        ],
        loops=[
            # We model halving as a strictly-decreasing-by-1 measure
            # via the upper bound: while range_size > 0, body shrinks
            # it by at least 1. Z3 can prove this terminates.
            LoopSpec(measure_var="range_size", post_decreases_by=1,
                     measure_low_bound=0),
        ],
        case_splits=[
            CaseSplit(predicate="range_size == 0"),    # exit
            CaseSplit(predicate="range_size > 0"),      # continue
        ],
        invariants=[
            "range_size >= 0",
        ],
    )
    return LogicSkeleton(
        skeleton_id=sid,
        algorithm_type="search",
        pseudocode_steps=[
            "set low = 0, high = len(data) - 1",
            "while low <= high:",
            "  mid = (low + high) // 2",
            "  if data[mid] == target: return mid",
            "  if data[mid] < target: low = mid + 1",
            "  else: high = mid - 1",
            "return -1",
        ],
        state_machine={
            "states": ["INIT", "PROBE", "FOUND", "NARROW_LEFT", "NARROW_RIGHT", "DONE"],
            "transitions": [
                {"from": "INIT", "to": "PROBE", "condition": "low <= high"},
                {"from": "PROBE", "to": "FOUND",
                 "condition": "data[mid] == target"},
                {"from": "PROBE", "to": "NARROW_LEFT",
                 "condition": "data[mid] < target"},
                {"from": "PROBE", "to": "NARROW_RIGHT",
                 "condition": "data[mid] > target"},
                {"from": "PROBE", "to": "DONE", "condition": "low > high"},
            ],
        },
        ast_skeleton={
            "function_name": "binary_search",
            "parameters": [
                {"name": "data", "type": "List[int]"},
                {"name": "target", "type": "int"},
            ],
            "return_type": "int",
            "loops": [{"type": "while", "bound": "high - low",
                        "body_hint": "halve the range each iteration"}],
            "conditions": [
                {"predicate": "data[mid] == target", "exhaustive": False},
                {"predicate": "data[mid] < target", "exhaustive": False},
                {"predicate": "data[mid] > target", "exhaustive": False},
            ],
        },
        invariants=[
            "data is sorted (precondition)",
            "if target is in data, it lies in [low, high] until found",
            "if returned index >= 0 then data[index] == target",
        ],
        termination_argument=(
            "high - low strictly decreases by at least 1 each iteration "
            "(in fact halves); the loop exits when high < low"
        ),
        complexity_hint="O(log n)",
        matched_template="binary_search",
        confidence=0.88,
        verifier_skeleton=skeleton,
    )


def _build_count(prompt: str, sid: str) -> LogicSkeleton:
    skeleton = AlgorithmSkeleton(
        skeleton_id=sid,
        int_vars=[
            IntVarBound("n", low=0, high=10_000),
            IntVarBound("count", low=0, high=10_000),
        ],
        loops=[LoopSpec(measure_var="n", post_decreases_by=1, measure_low_bound=0)],
        invariants=["n >= 0", "count >= 0", "count <= n"],
    )
    return LogicSkeleton(
        skeleton_id=sid,
        algorithm_type="aggregate",
        pseudocode_steps=[
            "set count = 0",
            "iterate over each element x in data",
            "  if predicate(x): count += 1",
            "return count",
        ],
        state_machine={
            "states": ["INIT", "SCAN", "DONE"],
            "transitions": [
                {"from": "INIT", "to": "SCAN", "condition": "i < n"},
                {"from": "SCAN", "to": "DONE", "condition": "i == n"},
            ],
        },
        ast_skeleton={
            "function_name": "count",
            "parameters": [
                {"name": "data", "type": "List[Any]"},
            ],
            "return_type": "int",
            "loops": [{"type": "for", "bound": "len(data)",
                        "body_hint": "increment count if predicate holds"}],
            "conditions": [{"predicate": "predicate(x)", "exhaustive": True}],
        },
        invariants=["count is non-decreasing", "count <= number of elements seen"],
        termination_argument="loop counter strictly increases from 0 to n",
        complexity_hint="O(n)",
        matched_template="count",
        confidence=0.85,
        verifier_skeleton=skeleton,
    )


def _build_hashtable_lookup(prompt: str, sid: str) -> LogicSkeleton:
    skeleton = AlgorithmSkeleton(
        skeleton_id=sid,
        int_vars=[
            IntVarBound("found", low=0, high=1),       # bool
        ],
        case_splits=[
            CaseSplit(predicate="found == 0"),
            CaseSplit(predicate="found == 1"),
        ],
        invariants=["found == 0 or found == 1"],
    )
    return LogicSkeleton(
        skeleton_id=sid,
        algorithm_type="lookup",
        pseudocode_steps=[
            "compute hash of key",
            "fetch slot from table",
            "if slot exists and key matches: return value",
            "else: return None / default",
        ],
        state_machine={
            "states": ["HASH", "FETCH", "MATCH", "MISS"],
            "transitions": [
                {"from": "HASH", "to": "FETCH", "condition": "always"},
                {"from": "FETCH", "to": "MATCH", "condition": "slot.key == key"},
                {"from": "FETCH", "to": "MISS", "condition": "slot.key != key"},
            ],
        },
        ast_skeleton={
            "function_name": "lookup",
            "parameters": [
                {"name": "table", "type": "Dict"},
                {"name": "key", "type": "Any"},
            ],
            "return_type": "Optional[Any]",
            "loops": [],
            "conditions": [
                {"predicate": "key in table", "exhaustive": False},
                {"predicate": "key not in table", "exhaustive": False},
            ],
        },
        invariants=["table is not modified during lookup"],
        termination_argument=(
            "no loops; hash + dict access are constant-time stdlib ops"
        ),
        complexity_hint="O(1) amortised",
        matched_template="hashtable_lookup",
        confidence=0.80,
        verifier_skeleton=skeleton,
    )


def _build_three_way_compare(prompt: str, sid: str) -> LogicSkeleton:
    """sign(x), classify-by-comparison, etc. Three-way case split."""
    skeleton = AlgorithmSkeleton(
        skeleton_id=sid,
        int_vars=[
            IntVarBound("x", low=-1_000_000_000, high=1_000_000_000),
        ],
        case_splits=[
            CaseSplit(predicate="x < 0"),
            CaseSplit(predicate="x == 0"),
            CaseSplit(predicate="x > 0"),
        ],
        invariants=[],
    )
    return LogicSkeleton(
        skeleton_id=sid,
        algorithm_type="classify",
        pseudocode_steps=[
            "if x < 0: return -1",
            "if x == 0: return 0",
            "return 1",
        ],
        state_machine={
            "states": ["TEST", "NEG", "ZERO", "POS"],
            "transitions": [
                {"from": "TEST", "to": "NEG", "condition": "x < 0"},
                {"from": "TEST", "to": "ZERO", "condition": "x == 0"},
                {"from": "TEST", "to": "POS", "condition": "x > 0"},
            ],
        },
        ast_skeleton={
            "function_name": "classify",
            "parameters": [{"name": "x", "type": "int"}],
            "return_type": "int",
            "loops": [],
            "conditions": [
                {"predicate": "x < 0", "exhaustive": False},
                {"predicate": "x == 0", "exhaustive": False},
                {"predicate": "x > 0", "exhaustive": False},
            ],
        },
        invariants=["return value in {-1, 0, 1}"],
        termination_argument="no loops; constant-time decision",
        complexity_hint="O(1)",
        matched_template="three_way_compare",
        confidence=0.85,
        verifier_skeleton=skeleton,
    )


# Catalogue order matters — earlier entries win on tie. Place
# more-specific keywords first.
CATALOGUE: tuple[_Template, ...] = (
    _Template(name="binary_search",
              keywords=("binary search", "binary-search"),
              algorithm_type="search", builder=_build_binary_search),
    _Template(name="linear_search",
              keywords=("search", "find", "lookup in list",
                        "linear search"),
              algorithm_type="search", builder=_build_linear_search),
    _Template(name="hashtable_lookup",
              keywords=("hash table", "hash map", "dict lookup",
                        "hashmap"),
              algorithm_type="lookup", builder=_build_hashtable_lookup),
    _Template(name="sort",
              keywords=("sort", "sorted", "ordering"),
              algorithm_type="sort", builder=_build_sort),
    _Template(name="count",
              keywords=("count", "tally", "how many"),
              algorithm_type="aggregate", builder=_build_count),
    _Template(name="three_way_compare",
              keywords=("classify", "sign of", "categorize",
                        "three-way", "positive negative zero"),
              algorithm_type="classify",
              builder=_build_three_way_compare),
)


def _match_template(prompt: str) -> _Template | None:
    """First-keyword-match wins."""
    text = (prompt or "").lower()
    for tmpl in CATALOGUE:
        for kw in tmpl.keywords:
            if kw.lower() in text:
                return tmpl
    return None


# ─── LogicEngine ────────────────────────────────────────────────────


class LogicEngine:
    """Pluggable strategy. Default = rule_based.

    Public method: ``async def generate(user_prompt) -> LogicSkeleton``.
    The async surface is preserved so the small_model / funsearch
    strategies can plug in without changing callers.
    """

    def __init__(
        self,
        *,
        strategy: Strategy = "rule_based",
        llm_call: LLMCall | None = None,
    ) -> None:
        self._strategy = strategy
        self._llm_call = llm_call

    @property
    def strategy(self) -> Strategy:
        return self._strategy

    async def generate(self, user_prompt: str) -> LogicSkeleton:
        if not (user_prompt or "").strip():
            return self._fallback_skeleton(user_prompt, reason="empty prompt")

        if self._strategy == "rule_based":
            return self._generate_rule_based(user_prompt)

        if self._strategy == "small_model":
            # Phase 1A scaffolds the surface only; the actual small-
            # model strategy lands when we have a 1.5B coder model
            # bound to a per-role slot. Until then, fall through.
            logger.info(
                "logic_engine_small_model_strategy_not_yet_implemented; "
                "falling back to rule_based"
            )
            return self._generate_rule_based(user_prompt)

        if self._strategy == "funsearch":
            logger.info(
                "logic_engine_funsearch_strategy_not_yet_implemented; "
                "falling back to rule_based"
            )
            return self._generate_rule_based(user_prompt)

        return self._fallback_skeleton(
            user_prompt, reason=f"unknown strategy {self._strategy}",
        )

    # ── strategies ──────────────────────────────────────────────────

    def _generate_rule_based(self, user_prompt: str) -> LogicSkeleton:
        sid = _slug(user_prompt)
        tmpl = _match_template(user_prompt)
        if tmpl is None:
            return self._fallback_skeleton(
                user_prompt, reason="no template matched",
                sid=sid,
            )
        try:
            skeleton = tmpl.builder(user_prompt, sid)
        except Exception as exc:
            logger.warning(
                "logic_engine_template_%s_failed: %s", tmpl.name, exc,
            )
            return self._fallback_skeleton(
                user_prompt,
                reason=f"template {tmpl.name} crashed: {exc}",
                sid=sid,
            )
        return skeleton

    def _fallback_skeleton(
        self,
        user_prompt: str,
        *,
        reason: str,
        sid: str | None = None,
    ) -> LogicSkeleton:
        """When no template matches OR something goes wrong, return a
        minimal generic skeleton tagged as low-confidence so the
        downstream pipeline can fall back to the LLM-only path."""
        sid = sid or _slug(user_prompt or "fallback")
        return LogicSkeleton(
            skeleton_id=sid,
            algorithm_type="generic",
            pseudocode_steps=[
                "validate inputs",
                "compute result",
                "return result",
            ],
            state_machine={
                "states": ["INIT", "PROCESS", "DONE"],
                "transitions": [
                    {"from": "INIT", "to": "PROCESS", "condition": "valid"},
                    {"from": "PROCESS", "to": "DONE", "condition": "complete"},
                ],
            },
            ast_skeleton={
                "function_name": "solve",
                "parameters": [{"name": "input", "type": "Any"}],
                "return_type": "Any",
                "loops": [],
                "conditions": [],
            },
            invariants=[],
            termination_argument="trivial — no loops, no recursion",
            complexity_hint="unknown",
            matched_template="",
            confidence=0.0,
            verifier_skeleton=AlgorithmSkeleton(skeleton_id=sid),
        )
