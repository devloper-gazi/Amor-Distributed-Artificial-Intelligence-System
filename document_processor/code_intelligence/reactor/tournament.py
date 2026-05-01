"""
TournamentRunner — generate N parallel candidate implementations,
run benchmarker + symbolic analyzer + property tests on each, elect
the Pareto-optimal winner, archive the losers with reasons.

Pipeline per candidate
----------------------

  Coder(llm) ──► code  ──┬─► PerformanceBenchmarker
                         ├─► SymbolicComplexityAnalyzer
                         └─► PropertyTestRunner
                                       │
                                       ▼
                                ScoringRecord
                                       │
                                       ▼
                                Pareto elect

Generation seasonings
---------------------
A: standard CoderAgent
B: plan biased toward the mesh's `performance` specialist's chosen
   alternative summary
C: plan biased toward the mesh's `edge_case` specialist's chosen
   alternative summary

When mesh alternatives aren't available, fall back to three runs of
the standard agent — diversity then comes purely from LLM stochasticity.

Election
--------
1. Filter: drop candidates whose property tests have any failed
   invariant.
2. Pareto: sort survivors by composite scalar
     0.5 * correctness − 0.3 * log10(growth_factor)
                       − 0.15 * log10(memory_kb + 1)
                       − 0.05 * static_issues_norm
3. All-fail fallback: if every candidate fails, pick the one with the
   FEWEST failed invariants and mark `degraded=True`. The whole
   bundle is still returned to the caller; the meta-arbiter decides.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..agents import AgentContext, CoderAgent
from .benchmarker import BenchmarkResult, PerformanceBenchmarker
from .property_tests import (
    Invariant,
    PropertyTestResult,
    PropertyTestRunner,
)
from .symbolic_complexity import (
    SymbolicComplexity,
    SymbolicComplexityAnalyzer,
)

logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]


# ─── Dataclasses ──────────────────────────────────────────────────


@dataclass
class CandidateScore:
    """Numeric breakdown the Pareto sort consumes."""

    correctness: float = 0.0      # 1.0 if all invariants pass, else 0..1
    growth_factor: float = 1.0    # measured exponent (default linear)
    memory_kb: int = 0
    static_issues: int = 0
    composite: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "correctness": round(self.correctness, 4),
            "growth_factor": round(self.growth_factor, 4),
            "memory_kb": self.memory_kb,
            "static_issues": self.static_issues,
            "composite": round(self.composite, 4),
        }


@dataclass
class TournamentCandidate:
    """One candidate's full envelope."""

    label: str
    seasoning: str               # "standard" | "performance" | "edge_case"
    code: str | None = None
    error: str | None = None
    benchmark: dict[str, Any] | None = None
    symbolic: dict[str, Any] | None = None
    property_tests: dict[str, Any] | None = None
    score: CandidateScore = field(default_factory=CandidateScore)
    elimination_reason: str | None = None
    runtime_ms_total: float = 0.0  # wall-time budget consumed for this candidate

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "seasoning": self.seasoning,
            "code_chars": len(self.code or ""),
            "error": self.error,
            "benchmark": self.benchmark,
            "symbolic": self.symbolic,
            "property_tests": self.property_tests,
            "score": self.score.to_dict(),
            "elimination_reason": self.elimination_reason,
            "runtime_ms_total": round(self.runtime_ms_total, 1),
        }


@dataclass
class TournamentBundle:
    """Full result the engine consumes."""

    candidates: list[TournamentCandidate] = field(default_factory=list)
    winner_label: str | None = None
    pareto_front: list[str] = field(default_factory=list)
    elimination_reasons: dict[str, str] = field(default_factory=dict)
    degraded: bool = False
    failed: bool = False
    failure_reason: str = ""

    @property
    def winner(self) -> TournamentCandidate | None:
        if not self.winner_label:
            return None
        for c in self.candidates:
            if c.label == self.winner_label:
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "winner_label": self.winner_label,
            "pareto_front": list(self.pareto_front),
            "elimination_reasons": dict(self.elimination_reasons),
            "degraded": self.degraded,
            "failed": self.failed,
            "failure_reason": self.failure_reason,
        }


# ─── Runner ───────────────────────────────────────────────────────


_SEASONINGS: list[str] = ["standard", "performance", "edge_case"]


def _label_for(idx: int) -> str:
    return chr(ord("A") + idx)


def _make_plan(
    user_prompt: str,
    seasoning: str,
    mesh_alternative_summary: str | None,
    language: str,
) -> dict[str, Any]:
    """Build a synthetic plan dict CoderAgent will consume.

    The plan's `title` + first-step `description` carry the seasoning
    bias — the CoderAgent prompt template incorporates both, so the
    bias propagates into the generated code without us needing to
    rewrite the agent's system prompt.
    """
    if seasoning == "performance" and mesh_alternative_summary:
        title = f"Optimise primarily for performance: {mesh_alternative_summary[:80]}"
        action_desc = (
            "Implement the chosen approach prioritising runtime + "
            "memory: avoid unnecessary allocations, prefer iterative "
            "loops over recursion, vectorise where possible. "
            f"{mesh_alternative_summary[:200]}"
        )
    elif seasoning == "edge_case" and mesh_alternative_summary:
        title = f"Optimise primarily for edge-case coverage: {mesh_alternative_summary[:80]}"
        action_desc = (
            "Implement the chosen approach prioritising correctness "
            "on adversarial input: empty / single-element / huge / "
            "duplicate / sorted / negative inputs all produce sane "
            f"results. {mesh_alternative_summary[:200]}"
        )
    else:
        title = (user_prompt or "Code task")[:100]
        action_desc = "Implement the chosen approach cleanly and correctly."
    return {
        "title": title,
        "language": language,
        "deliverable_type": "code_snippet",
        "task_type": "generation",
        "plan": [{
            "step": 1, "action": "implement",
            "agent": "coder", "description": action_desc,
            "depends_on": [],
        }],
        "seasoning": seasoning,
    }


class TournamentRunner:
    """One instance per session — stateless aside from injected deps."""

    def __init__(
        self,
        *,
        llm_call: LLMCall,
        sandbox: Any,
        benchmarker: PerformanceBenchmarker | None = None,
        property_runner: PropertyTestRunner | None = None,
        symbolic_analyzer: SymbolicComplexityAnalyzer | None = None,
        n_candidates: int = 3,
    ) -> None:
        self._llm = llm_call
        self._sandbox = sandbox
        self._benchmarker = benchmarker or PerformanceBenchmarker(sandbox)
        self._property_runner = property_runner or PropertyTestRunner(sandbox)
        self._symbolic = symbolic_analyzer or SymbolicComplexityAnalyzer()
        self._n = max(1, min(int(n_candidates), 5))

    async def run(
        self,
        *,
        user_prompt: str,
        language: str = "python",
        code_context: str | None = None,
        mesh_alternatives: dict[str, str] | None = None,
        invariants: list[Invariant] | None = None,
        claimed_complexity: str = "",
    ) -> TournamentBundle:
        if not user_prompt.strip():
            return TournamentBundle(
                failed=True, failure_reason="empty user_prompt",
            )

        # Build N candidate seasonings + plans.
        plans: list[tuple[str, str, dict[str, Any]]] = []
        for idx in range(self._n):
            seasoning = _SEASONINGS[idx % len(_SEASONINGS)]
            mesh_summary: str | None = None
            if mesh_alternatives:
                mesh_summary = mesh_alternatives.get(seasoning)
            plan = _make_plan(user_prompt, seasoning, mesh_summary, language)
            plans.append((_label_for(idx), seasoning, plan))

        # Generate candidates in parallel.
        gen_tasks = [
            asyncio.create_task(self._generate_one(
                label=label, seasoning=seasoning, plan=plan,
                user_prompt=user_prompt, language=language,
                code_context=code_context,
            ))
            for label, seasoning, plan in plans
        ]
        candidates: list[TournamentCandidate] = await asyncio.gather(*gen_tasks)

        # Verify each candidate (bench + symbolic + property) in parallel.
        # Symbolic is cheap and synchronous; bench + property each spend
        # one sandbox call. asyncio.gather lets the long-pole calls
        # share wall time.
        verify_tasks = [
            asyncio.create_task(self._verify_one(
                cand, language=language, claimed=claimed_complexity,
                invariants=invariants or [],
            ))
            for cand in candidates if cand.code
        ]
        if verify_tasks:
            await asyncio.gather(*verify_tasks)

        # Pareto election.
        return self._elect(candidates)

    # ─── per-candidate generation + verify ─────────────────────

    async def _generate_one(
        self, *,
        label: str, seasoning: str, plan: dict[str, Any],
        user_prompt: str, language: str, code_context: str | None,
    ) -> TournamentCandidate:
        ctx = AgentContext(
            user_prompt=user_prompt,
            code_context=code_context,
            plan=plan,
            language=language,
        )
        try:
            coder = CoderAgent(self._llm)
            out = await coder.run(ctx)
        except Exception as exc:
            logger.warning("tournament_coder_%s_failed: %s", label, exc)
            return TournamentCandidate(
                label=label, seasoning=seasoning, error=str(exc)[:300],
            )
        if out.error or not out.code:
            return TournamentCandidate(
                label=label, seasoning=seasoning,
                error=out.error or "coder produced no code",
            )
        return TournamentCandidate(
            label=label, seasoning=seasoning, code=out.code,
        )

    async def _verify_one(
        self, cand: TournamentCandidate, *,
        language: str, claimed: str, invariants: list[Invariant],
    ) -> None:
        if not cand.code:
            return
        # Symbolic is sync — run it directly.
        try:
            sym = self._symbolic.analyse(cand.code)
            cand.symbolic = sym.to_dict()
        except Exception as exc:
            logger.warning("tournament_symbolic_%s_failed: %s",
                           cand.label, exc)

        async def _bench():
            return await self._benchmarker.run(
                cand.code or "", language=language,
                claimed_label=claimed,
            )

        async def _props():
            if not invariants:
                return None
            return await self._property_runner.run(
                cand.code or "", invariants=invariants, language=language,
            )

        bench_res, prop_res = await asyncio.gather(_bench(), _props())
        if isinstance(bench_res, BenchmarkResult):
            cand.benchmark = bench_res.to_dict()
        if isinstance(prop_res, PropertyTestResult):
            cand.property_tests = prop_res.to_dict()

        cand.score = self._score(cand)

    def _score(self, cand: TournamentCandidate) -> CandidateScore:
        # Correctness: 1.0 - (failed_invariants / total_invariants).
        prop = cand.property_tests or {}
        outcomes = prop.get("outcomes") or []
        if outcomes:
            failed = sum(1 for o in outcomes if not o.get("passed"))
            correctness = max(0.0, 1.0 - (failed / max(1, len(outcomes))))
        else:
            correctness = 0.5  # unknown — neutral

        # Growth: from benchmarker fit, default to linear.
        bench = cand.benchmark or {}
        fit = bench.get("fit") or {}
        growth = float(fit.get("exponent") or 1.0)
        if growth <= 0:
            growth = 1.0

        # Memory: peak across all bench scales (kb).
        records = bench.get("records") or []
        memory_kb = max((int(r.get("peak_kb", 0) or 0) for r in records),
                        default=0)

        # Static issues — placeholder. The Reactor facade will fill
        # this from the per-function-complexity dict that
        # static_analysis now exposes; for now we use the symbolic
        # `total_loop_depth` as a coarse proxy.
        sym = cand.symbolic or {}
        static_issues = int(sym.get("total_loop_depth") or 0)

        composite = (
            0.5 * correctness
            - 0.3 * math.log10(max(growth, 1.0))
            - 0.15 * math.log10(memory_kb + 1)
            - 0.05 * (static_issues / 10.0)
        )
        return CandidateScore(
            correctness=correctness, growth_factor=growth,
            memory_kb=memory_kb, static_issues=static_issues,
            composite=composite,
        )

    # ─── election ───────────────────────────────────────────────

    def _elect(
        self, candidates: list[TournamentCandidate],
    ) -> TournamentBundle:
        if not candidates:
            return TournamentBundle(
                failed=True, failure_reason="no candidates generated",
            )

        # 1) Filter — eliminate hard failures (no code, generation error).
        survivors: list[TournamentCandidate] = []
        for c in candidates:
            if c.error or not c.code:
                c.elimination_reason = c.error or "no code produced"
                continue
            survivors.append(c)

        # 2) Property test gate — eliminate any candidate with failures.
        passed_property: list[TournamentCandidate] = []
        for c in survivors:
            prop = c.property_tests or {}
            if prop.get("all_passed"):
                passed_property.append(c)
            else:
                num_failed = int(prop.get("num_failed", 0) or 0)
                c.elimination_reason = (
                    f"property tests failed ({num_failed} invariant(s))"
                )

        winners_pool: list[TournamentCandidate]
        degraded = False
        if passed_property:
            winners_pool = passed_property
        elif survivors:
            # All-fail fallback — pick the least-broken candidate.
            survivors.sort(key=lambda c: int(
                (c.property_tests or {}).get("num_failed", 9_999) or 9_999
            ))
            best = survivors[0]
            best.elimination_reason = None  # un-eliminate the chosen
            winners_pool = [best]
            degraded = True
            for other in survivors[1:]:
                if not other.elimination_reason:
                    other.elimination_reason = (
                        "least-broken-elsewhere; lost to degraded fallback"
                    )
        else:
            return TournamentBundle(
                candidates=candidates,
                failed=True,
                failure_reason="every candidate failed to generate code",
                elimination_reasons={
                    c.label: c.elimination_reason or "?"
                    for c in candidates if c.elimination_reason
                },
            )

        # 3) Pareto — pick the highest composite. Ties broken by
        # higher correctness then lower growth.
        winners_pool.sort(
            key=lambda c: (-c.score.composite,
                           -c.score.correctness,
                           c.score.growth_factor),
        )
        winner = winners_pool[0]

        # Pareto front = candidates not dominated on every axis.
        front_labels: list[str] = []
        for c in winners_pool:
            dominated = False
            for d in winners_pool:
                if d is c:
                    continue
                if (d.score.correctness >= c.score.correctness
                        and d.score.growth_factor <= c.score.growth_factor
                        and d.score.memory_kb <= c.score.memory_kb
                        and d.score.static_issues <= c.score.static_issues
                        and (d.score.composite > c.score.composite)):
                    dominated = True
                    break
            if not dominated:
                front_labels.append(c.label)

        elimination_reasons = {
            c.label: c.elimination_reason
            for c in candidates if c.elimination_reason
        }

        return TournamentBundle(
            candidates=candidates,
            winner_label=winner.label,
            pareto_front=front_labels,
            elimination_reasons=elimination_reasons,
            degraded=degraded,
        )
