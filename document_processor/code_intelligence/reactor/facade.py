"""
CodeSynthesisReactor — single facade Quick + Pro both call into.

Wires together every v10 capability and exposes three high-level
methods the engines use:

  - ``fetch_rag_refs(prompt)``         → CorpusPattern[] | None
  - ``specialist_weights(roles, task)`` → dict[str, float] | None
  - ``verify_implementation(code, …)`` → ReactorBundle

Each subsystem (RAG / cache / bandit / benchmarker / symbolic /
property tests / tournament) is constructed lazily and is fail-soft:
a missing dependency (Hypothesis, LanceDB, Redis, Mongo) yields a
no-op subsystem rather than an exception. The facade ALWAYS returns
a valid envelope so engine code can rely on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .benchmarker import BenchmarkResult, PerformanceBenchmarker
from .config import ReactorConfig
from .property_tests import (
    Invariant,
    PropertyTestGenerator,
    PropertyTestResult,
    PropertyTestRunner,
)
from .rag import CodeCorpusRAG, RetrievalResult
from .symbolic_complexity import (
    SymbolicComplexity,
    SymbolicComplexityAnalyzer,
)
from .tournament import TournamentBundle, TournamentRunner

logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]


@dataclass
class ReactorBundle:
    """The envelope the facade returns from verify_implementation()."""

    symbolic: dict[str, Any] | None = None
    benchmark: dict[str, Any] | None = None
    property_tests: dict[str, Any] | None = None
    tournament: dict[str, Any] | None = None
    rag_refs: list[dict[str, Any]] = field(default_factory=list)
    bandit_weights: dict[str, float] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbolic": self.symbolic,
            "benchmark": self.benchmark,
            "property_tests": self.property_tests,
            "tournament": self.tournament,
            "rag_refs": list(self.rag_refs),
            "bandit_weights": dict(self.bandit_weights),
            "findings": list(self.findings),
            "config": dict(self.config),
        }


class CodeSynthesisReactor:
    """One per session. Holds every reactor subsystem; engines call
    its three methods as cross-cutting hooks."""

    def __init__(
        self,
        *,
        config: ReactorConfig | None = None,
        llm_call: LLMCall | None = None,
        sandbox: Any | None = None,
        embedder: Any | None = None,
        cache: Any | None = None,
        vector_store: Any | None = None,
        metrics_collection: Any | None = None,
        role_setter: Callable[[str | None], Any] | None = None,
    ) -> None:
        self.config = (config or ReactorConfig.from_settings()).normalised()
        self._llm = llm_call
        self._sandbox = sandbox
        self._embedder = embedder
        self._cache = cache
        self._vector_store = vector_store
        self._metrics_collection = metrics_collection
        self._role_setter = role_setter

        # Lazily-built subsystems. Build only when actually needed.
        self._benchmarker: PerformanceBenchmarker | None = None
        self._symbolic = SymbolicComplexityAnalyzer()
        self._property_gen = PropertyTestGenerator()
        self._property_runner: PropertyTestRunner | None = None
        self._tournament: TournamentRunner | None = None
        self._rag: CodeCorpusRAG | None = None
        self._bandit_loaded = False
        self._bandit: Any | None = None
        self._llm_cache: Any | None = None

    # ─── lazy builders ────────────────────────────────────────────

    def _get_benchmarker(self) -> PerformanceBenchmarker | None:
        if not self.config.is_feature_enabled("benchmarker"):
            return None
        if self._sandbox is None:
            return None
        if self._benchmarker is None:
            self._benchmarker = PerformanceBenchmarker(
                self._sandbox,
                scales=self.config.bench_scales,
                timeout_per_scale_s=self.config.bench_timeout_per_scale_s,
            )
        return self._benchmarker

    def _get_property_runner(self) -> PropertyTestRunner | None:
        if not self.config.is_feature_enabled("property_tests"):
            return None
        if self._sandbox is None:
            return None
        if self._property_runner is None:
            self._property_runner = PropertyTestRunner(self._sandbox)
        return self._property_runner

    def _get_tournament(self) -> TournamentRunner | None:
        if not self.config.is_feature_enabled("tournament"):
            return None
        if self._llm is None or self._sandbox is None:
            return None
        if self._tournament is None:
            self._tournament = TournamentRunner(
                llm_call=self._llm, sandbox=self._sandbox,
                benchmarker=self._get_benchmarker(),
                property_runner=self._get_property_runner(),
                symbolic_analyzer=self._symbolic,
                n_candidates=self.config.tournament_n,
            )
        return self._tournament

    def _get_rag(self) -> CodeCorpusRAG | None:
        if not self.config.is_feature_enabled("rag"):
            return None
        if self._vector_store is None or self._embedder is None:
            return None
        if self._rag is None:
            self._rag = CodeCorpusRAG(
                vector_store=self._vector_store,
                embedder=self._embedder,
                top_k=self.config.rag_top_k,
                similarity_floor=self.config.rag_similarity_floor,
            )
        return self._rag

    async def _get_bandit(self) -> Any | None:
        if not self.config.is_feature_enabled("bandit"):
            return None
        if self._bandit_loaded:
            return self._bandit
        try:
            from .bandit import SpecialistBandit  # noqa: PLC0415
            bandit = SpecialistBandit(
                cold_start_threshold=self.config.bandit_cold_start_threshold,
                temperature=self.config.bandit_temperature,
            )
            await bandit.update_from_collection(self._metrics_collection)
            self._bandit = bandit
        except Exception as exc:  # pragma: no cover
            logger.debug("reactor_bandit_init_failed: %s", exc)
            self._bandit = None
        self._bandit_loaded = True
        return self._bandit

    # ─── public API ─────────────────────────────────────────────

    async def fetch_rag_refs(self, user_prompt: str) -> RetrievalResult | None:
        rag = self._get_rag()
        if rag is None:
            return None
        try:
            return await rag.retrieve(user_prompt)
        except Exception as exc:
            logger.debug("reactor_rag_failed: %s", exc)
            return None

    async def specialist_weights(
        self,
        roles: list[str],
        *,
        task_type: str = "default",
    ) -> dict[str, float] | None:
        bandit = await self._get_bandit()
        if bandit is None:
            return None
        try:
            return bandit.weights(roles, task_type=task_type)
        except Exception as exc:
            logger.debug("reactor_bandit_weights_failed: %s", exc)
            return None

    async def generate_invariants(
        self,
        *,
        triage: dict | None,
        user_prompt: str,
        code: str | None = None,
        llm_call: LLMCall | None = None,
    ) -> list[Invariant]:
        invariants = list(
            self._property_gen.for_triage(triage, user_prompt)
        )
        if (self.config.is_feature_enabled("property_tests")
                and self.config.property_tests_llm_suggest
                and llm_call is not None):
            try:
                more = await self._property_gen.suggest(
                    llm_call, user_prompt=user_prompt, code=code,
                )
                invariants.extend(more)
            except Exception as exc:
                logger.debug("reactor_invariant_suggest_failed: %s", exc)
        return invariants

    async def verify_implementation(
        self,
        *,
        code: str | None,
        tests: str | None = None,
        user_prompt: str,
        triage: dict | None = None,
        claimed_complexity: str = "",
        language: str = "python",
        invariants: list[Invariant] | None = None,
    ) -> ReactorBundle:
        """The post-implement reactor pass — runs symbolic + bench +
        property tests on the SINGLE generated code. Tournament-N=3
        is a separate path (see run_tournament)."""
        bundle = ReactorBundle(config=self.config.to_dict())

        if not (code or "").strip():
            bundle.findings.append("no code to verify")
            return bundle

        # 1. Symbolic complexity (cheapest, deterministic, sync).
        if self.config.is_feature_enabled("symbolic_complexity"):
            try:
                sym = self._symbolic.analyse(code or "")
                bundle.symbolic = sym.to_dict()
                if claimed_complexity:
                    cmp = SymbolicComplexity.compare_bounds(
                        claimed_complexity, sym.worst_bound,
                    )
                    if cmp == -1:
                        bundle.findings.append(
                            f"claimed {claimed_complexity} but symbolic "
                            f"upper bound is {sym.worst_bound}"
                        )
            except Exception as exc:
                logger.debug("reactor_symbolic_failed: %s", exc)

        # 2. Performance benchmark (sandbox call).
        bm = self._get_benchmarker()
        if bm is not None:
            try:
                bench: BenchmarkResult = await bm.run(
                    code or "", language=language,
                    claimed_label=claimed_complexity,
                )
                bundle.benchmark = bench.to_dict()
                if bench.claim_vs_measured == -1:
                    bundle.findings.append(
                        f"benchmark exposes O({bench.fit.measured_label}) — "
                        f"worse than claimed {bench.claimed_label}"
                    )
            except Exception as exc:
                logger.debug("reactor_benchmarker_failed: %s", exc)

        # 3. Property tests (sandbox call).
        runner = self._get_property_runner()
        if runner is not None:
            invs = invariants
            if invs is None:
                invs = await self.generate_invariants(
                    triage=triage, user_prompt=user_prompt, code=code,
                    llm_call=self._llm,
                )
            if invs:
                try:
                    res: PropertyTestResult = await runner.run(
                        code or "", invariants=invs, language=language,
                    )
                    bundle.property_tests = res.to_dict()
                    if res.num_failed:
                        bundle.findings.append(
                            f"{res.num_failed} property invariant(s) failed"
                        )
                except Exception as exc:
                    logger.debug("reactor_property_runner_failed: %s", exc)

        return bundle

    async def run_tournament(
        self,
        *,
        user_prompt: str,
        language: str = "python",
        code_context: str | None = None,
        mesh_alternatives: dict[str, str] | None = None,
        triage: dict | None = None,
        claimed_complexity: str = "",
    ) -> TournamentBundle | None:
        tour = self._get_tournament()
        if tour is None:
            return None
        try:
            invs = await self.generate_invariants(
                triage=triage, user_prompt=user_prompt,
                llm_call=self._llm,
            )
            return await tour.run(
                user_prompt=user_prompt, language=language,
                code_context=code_context,
                mesh_alternatives=mesh_alternatives,
                invariants=invs,
                claimed_complexity=claimed_complexity,
            )
        except Exception as exc:
            logger.warning("reactor_tournament_failed: %s", exc)
            return None
