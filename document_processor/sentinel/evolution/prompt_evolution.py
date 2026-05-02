"""
Sentinel Evolution — Subsystem C: prompt evolution engine.

Three parallel mechanisms for evolving agent system prompts:

* **DSPy-lite optimisation** — bootstrap few-shot demos from past
  successful auditor verdicts; pick the demo combo that scores
  best on a hold-out set.
* **Genetic mutation** — a small LLM (default ``qwen2.5:1.5b``)
  produces paraphrased mutants of the parent prompt, each scored
  on the hold-out set.  Top-K survive into the next generation.
* **Adversarial prompt evolution** — RedTeam generates inputs that
  the parent prompt fails on; the failure pattern is summarised
  into an "watch out for X" addendum that the next version of the
  prompt absorbs.

Versioning + governance: every mutant is recorded in
``prompts/<agent>/<version>.yaml`` with metadata (parent_version,
mutation_method, eval_metrics, promotion_date).  Every promotion
flows through ``LedgerStore`` and is checked against
``ImmutableConstraints`` before the YAML hits disk.

License: MIT.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .governance import (
    HardConstraintViolation,
    ImmutableConstraints,
    LedgerStore,
)


logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]


# ─────────────────────────────────────────────────────────────────────
# Data shapes
# ─────────────────────────────────────────────────────────────────────


@dataclass
class PromptVersion:
    agent_name: str
    version: str                # "v003" / "v003_genetic_mutant"
    system_prompt: str
    parent_version: str = ""
    mutation_method: str = "manual"   # manual / dspy / genetic / adversarial
    few_shot_demos: list[dict[str, str]] = field(default_factory=list)
    eval_metrics: dict[str, float] = field(default_factory=dict)
    status: str = "staging"     # staging | production | archived | rejected
    created_at: float = 0.0
    promoted_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvalCase:
    """One hold-out evaluation case for the prompt scorer."""
    input_prompt: str
    expected_verdict: str       # true_positive / false_positive / etc.
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalResult:
    cases: int = 0
    correct: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    elapsed_ms: float = 0.0


# ─────────────────────────────────────────────────────────────────────
# PromptStore — versioned YAML files
# ─────────────────────────────────────────────────────────────────────


class PromptStore:
    """Disk layout: ``prompts/<agent>/<version>.yaml`` + ``current.txt``
    that holds the active version slug for fast lookup."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "prompts"
        self.root.mkdir(parents=True, exist_ok=True)

    def agent_dir(self, agent_name: str) -> Path:
        d = self.root / agent_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_version(self, version: PromptVersion) -> Path:
        try:
            import yaml  # type: ignore
        except Exception:
            yaml = None  # falls back to JSON dump
        d = self.agent_dir(version.agent_name)
        path = d / f"{version.version}.yaml"
        payload = version.to_dict()
        if yaml is not None:
            text = yaml.safe_dump(payload, sort_keys=True)
        else:
            text = json.dumps(payload, indent=2, sort_keys=True, default=str)
        path.write_text(text, encoding="utf-8")
        return path

    def list_versions(self, agent_name: str) -> list[PromptVersion]:
        d = self.agent_dir(agent_name)
        out: list[PromptVersion] = []
        for p in sorted(d.glob("*.yaml")):
            out.append(self._load(p))
        return out

    def get_production(self, agent_name: str) -> PromptVersion | None:
        for v in self.list_versions(agent_name):
            if v.status == "production":
                return v
        return None

    def promote(self, version: PromptVersion) -> None:
        # Demote any other production for this agent.
        for v in self.list_versions(version.agent_name):
            if v.status == "production" and v.version != version.version:
                v.status = "archived"
                self.write_version(v)
        version.status = "production"
        version.promoted_at = time.time()
        self.write_version(version)

    def archive(self, version: PromptVersion) -> None:
        version.status = "archived"
        self.write_version(version)

    def _load(self, path: Path) -> PromptVersion:
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        return PromptVersion(
            agent_name=str(data.get("agent_name") or ""),
            version=str(data.get("version") or path.stem),
            system_prompt=str(data.get("system_prompt") or ""),
            parent_version=str(data.get("parent_version") or ""),
            mutation_method=str(data.get("mutation_method") or "manual"),
            few_shot_demos=list(data.get("few_shot_demos") or []),
            eval_metrics=dict(data.get("eval_metrics") or {}),
            status=str(data.get("status") or "staging"),
            created_at=float(data.get("created_at") or 0.0),
            promoted_at=data.get("promoted_at"),
        )


# ─────────────────────────────────────────────────────────────────────
# Mutation operators
# ─────────────────────────────────────────────────────────────────────


GENETIC_SYSTEM_PROMPT = (
    "You are a prompt engineer. Given a parent system prompt, "
    "produce a single short paraphrase of it that preserves intent "
    "but rephrases instructions. Keep the JSON contract identical.\n"
    "Return only the new prompt text, no commentary, no fences."
)


async def genetic_mutate(
    *,
    parent_prompt: str,
    llm: LLMCall,
    n_mutants: int = 5,
    max_tokens: int = 800,
) -> list[str]:
    """Ask a small LLM for ``n_mutants`` paraphrased variants of the
    parent prompt.  Failures or empty returns are silently dropped."""
    out: list[str] = []
    for i in range(max(1, int(n_mutants))):
        seed_hint = f"Variant #{i + 1}: change a few synonyms; do not invert any rule."
        try:
            text = await llm(
                f"{seed_hint}\n\n# Parent prompt\n{parent_prompt[:6000]}",
                GENETIC_SYSTEM_PROMPT,
                max_tokens,
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("genetic_mutate llm failed: %s", exc)
            continue
        text = (text or "").strip()
        if not text:
            continue
        # Strip markdown fences if any.
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("text"):
                text = text[4:].lstrip()
        out.append(text)
    return out


def few_shot_bootstrap(
    *,
    parent_prompt: str,
    pool: list[dict[str, str]],
    sample_size: int = 3,
    n_combos: int = 5,
    rng: random.Random | None = None,
) -> list[tuple[str, list[dict[str, str]]]]:
    """DSPy-lite: pick ``n_combos`` random subsets of size
    ``sample_size`` from ``pool`` (each entry must have
    ``input``+``output`` keys) and produce candidate prompts that
    embed the demos in a "## Examples" appendix.
    Returns list of ``(rendered_prompt, demos_used)``."""
    rng = rng or random.Random()
    out: list[tuple[str, list[dict[str, str]]]] = []
    if not pool:
        return out
    sample_size = max(1, min(sample_size, len(pool)))
    for _ in range(max(1, int(n_combos))):
        demos = rng.sample(pool, sample_size)
        rows = ["", "## Examples", ""]
        for d in demos:
            rows.append(f"INPUT: {str(d.get('input', ''))[:600]}")
            rows.append(f"OUTPUT: {str(d.get('output', ''))[:600]}")
            rows.append("")
        rendered = parent_prompt.rstrip() + "\n" + "\n".join(rows)
        out.append((rendered, demos))
    return out


def adversarial_addendum(
    *,
    parent_prompt: str,
    failure_summary: str,
    max_addendum_chars: int = 600,
) -> str:
    """Append a short "watch out for X" reminder.  Used by the
    RedTeam-driven adversarial loop."""
    summary = (failure_summary or "").strip()[:max_addendum_chars]
    if not summary:
        return parent_prompt
    addendum = (
        "\n\n## Adversarial cases to watch (auto-added by self-play)\n"
        f"{summary}\n"
    )
    return parent_prompt.rstrip() + addendum


# ─────────────────────────────────────────────────────────────────────
# EvalHarness
# ─────────────────────────────────────────────────────────────────────


# Async scorer signature: takes (system_prompt, user_prompt, max_tokens)
# and returns the model's verdict string.
ScorerCall = Callable[[str, str, int], Awaitable[str]]


def _verdict_from_text(text: str) -> str:
    """Extract a verdict slug from raw text.  Tolerant to
    JSON-fenced output."""
    if not text:
        return ""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").strip()
    try:
        data = json.loads(candidate)
        if isinstance(data, dict) and "verdict" in data:
            return str(data["verdict"]).strip().lower()
    except Exception:
        pass
    # Fallback: look for the first verdict keyword in the text.
    for kw in (
        "true_positive", "false_positive", "needs_more_context",
        "exploitable", "not_exploitable", "approved", "rejected",
    ):
        if kw in candidate.lower():
            return kw
    return candidate.lower()[:40]


async def evaluate_prompt(
    *,
    system_prompt: str,
    cases: list[EvalCase],
    scorer: ScorerCall,
    max_tokens: int = 400,
) -> EvalResult:
    """Score `system_prompt` against every case.  Returns precision
    + recall + F1 against the expected_verdicts.  Loops sequentially
    so the small LLM doesn't OOM the GPU."""
    if not cases:
        return EvalResult()
    start = time.monotonic()
    correct = 0
    tp = fp = fn = 0
    positive_classes = {"true_positive", "exploitable", "approved"}
    for case in cases:
        try:
            raw = await scorer(system_prompt, case.input_prompt, max_tokens)
        except Exception as exc:  # pragma: no cover - infra
            logger.debug("evaluate_prompt scorer raised: %s", exc)
            raw = ""
        predicted = _verdict_from_text(raw)
        expected = (case.expected_verdict or "").strip().lower()
        if predicted == expected:
            correct += 1
        # Binary precision/recall over "positive verdict".
        is_pred_pos = predicted in positive_classes
        is_exp_pos = expected in positive_classes
        if is_pred_pos and is_exp_pos:
            tp += 1
        elif is_pred_pos and not is_exp_pos:
            fp += 1
        elif not is_pred_pos and is_exp_pos:
            fn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 0.0 if (precision + recall) == 0 else (
        2 * precision * recall / (precision + recall)
    )
    return EvalResult(
        cases=len(cases),
        correct=correct,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        elapsed_ms=(time.monotonic() - start) * 1000.0,
    )


# ─────────────────────────────────────────────────────────────────────
# Promotion gating
# ─────────────────────────────────────────────────────────────────────


def is_pareto_improvement(
    candidate: EvalResult,
    baseline: EvalResult,
    *,
    improvement_required: float = 0.05,
) -> bool:
    """Pareto rule: candidate must beat baseline by at least
    ``improvement_required`` on at least one axis AND not regress
    on the other.  Equality (no change) is NOT an improvement."""
    eps = 1e-9
    precision_better = (
        candidate.precision - baseline.precision
        >= improvement_required - eps
    )
    recall_better = (
        candidate.recall - baseline.recall
        >= improvement_required - eps
    )
    if precision_better:
        return candidate.recall >= baseline.recall - eps
    if recall_better:
        return candidate.precision >= baseline.precision - eps
    return False


def meets_acceptance_floor(
    result: EvalResult,
    constraints: ImmutableConstraints,
) -> bool:
    return (
        result.precision >= constraints.precision_floor - 1e-6
        and result.recall >= constraints.recall_floor - 1e-6
    )


# ─────────────────────────────────────────────────────────────────────
# PromptEvolutionEngine — orchestrator
# ─────────────────────────────────────────────────────────────────────


class PromptEvolutionEngine:
    """Coordinates DSPy-lite + genetic mutation + adversarial
    addendum on top of ``PromptStore``."""

    def __init__(
        self,
        *,
        store: PromptStore,
        ledger: LedgerStore,
        constraints: ImmutableConstraints,
        rng_seed: int | None = None,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.constraints = constraints
        self._rng = random.Random(rng_seed)

    async def run_generation(
        self,
        *,
        agent_name: str,
        parent: PromptVersion,
        mutator: LLMCall,
        scorer: ScorerCall,
        eval_cases: list[EvalCase],
        few_shot_pool: list[dict[str, str]],
        n_genetic: int = 5,
        n_few_shot: int = 5,
        adversarial_failure_summary: str = "",
        improvement_required: float = 0.05,
    ) -> dict[str, Any]:
        """Produce + evaluate a single generation of mutants and
        promote the best Pareto-improving one (if any).  Returns a
        report dict with the candidates + their metrics + the
        promoted version (if any)."""
        # 0. Constraint check on the parent prompt itself (sanity).
        self.constraints.check({"prompt": parent.system_prompt})

        # 1. Score the parent on the eval set as our baseline.
        baseline = await evaluate_prompt(
            system_prompt=parent.system_prompt,
            cases=eval_cases,
            scorer=scorer,
        )

        # 2. Build candidates.
        candidates: list[PromptVersion] = []

        # 2a. Genetic paraphrases.
        for i, mutant_prompt in enumerate(
            await genetic_mutate(
                parent_prompt=parent.system_prompt,
                llm=mutator,
                n_mutants=n_genetic,
            ),
            start=1,
        ):
            self.constraints.check({"prompt": mutant_prompt})
            candidates.append(PromptVersion(
                agent_name=agent_name,
                version=f"{parent.version}_g{i}_{int(time.time())}",
                system_prompt=mutant_prompt,
                parent_version=parent.version,
                mutation_method="genetic",
                created_at=time.time(),
            ))

        # 2b. DSPy-lite few-shot bootstrap.
        for i, (prompt_text, demos) in enumerate(
            few_shot_bootstrap(
                parent_prompt=parent.system_prompt,
                pool=few_shot_pool,
                rng=self._rng,
                n_combos=n_few_shot,
            ),
            start=1,
        ):
            self.constraints.check({"prompt": prompt_text, "demos": demos})
            candidates.append(PromptVersion(
                agent_name=agent_name,
                version=f"{parent.version}_d{i}_{int(time.time())}",
                system_prompt=prompt_text,
                parent_version=parent.version,
                mutation_method="dspy_few_shot",
                few_shot_demos=demos,
                created_at=time.time(),
            ))

        # 2c. Adversarial addendum (one variant).
        if adversarial_failure_summary.strip():
            adv_prompt = adversarial_addendum(
                parent_prompt=parent.system_prompt,
                failure_summary=adversarial_failure_summary,
            )
            self.constraints.check({"prompt": adv_prompt})
            candidates.append(PromptVersion(
                agent_name=agent_name,
                version=f"{parent.version}_a_{int(time.time())}",
                system_prompt=adv_prompt,
                parent_version=parent.version,
                mutation_method="adversarial",
                created_at=time.time(),
            ))

        # 3. Score every candidate, persist as staging.
        for c in candidates:
            res = await evaluate_prompt(
                system_prompt=c.system_prompt,
                cases=eval_cases,
                scorer=scorer,
            )
            c.eval_metrics = {
                "precision": res.precision,
                "recall": res.recall,
                "f1": res.f1,
                "cases": res.cases,
                "correct": res.correct,
                "elapsed_ms": res.elapsed_ms,
            }
            c.status = "staging"
            self.store.write_version(c)

        # 4. Pick the best Pareto-improving candidate.
        best: PromptVersion | None = None
        best_result: EvalResult | None = None
        for c in candidates:
            res = EvalResult(
                cases=int(c.eval_metrics.get("cases") or 0),
                correct=int(c.eval_metrics.get("correct") or 0),
                precision=float(c.eval_metrics.get("precision") or 0.0),
                recall=float(c.eval_metrics.get("recall") or 0.0),
                f1=float(c.eval_metrics.get("f1") or 0.0),
            )
            if not meets_acceptance_floor(res, self.constraints):
                continue
            if not is_pareto_improvement(
                res, baseline, improvement_required=improvement_required
            ):
                continue
            if (best is None
                    or (res.f1 > (best_result.f1 if best_result else 0))):
                best = c
                best_result = res

        promoted: PromptVersion | None = None
        if best is not None:
            self.store.promote(best)
            self.ledger.append(
                actor="prompt_evolution",
                kind="prompt_promoted",
                payload={
                    "agent_name": agent_name,
                    "version": best.version,
                    "parent_version": best.parent_version,
                    "mutation_method": best.mutation_method,
                    "metrics": best.eval_metrics,
                    "baseline_metrics": {
                        "precision": baseline.precision,
                        "recall": baseline.recall,
                        "f1": baseline.f1,
                    },
                },
            )
            promoted = best
        else:
            # No promotion — record the attempt for audit.
            self.ledger.append(
                actor="prompt_evolution",
                kind="prompt_mutated",
                payload={
                    "agent_name": agent_name,
                    "parent_version": parent.version,
                    "candidates_evaluated": len(candidates),
                    "baseline_metrics": {
                        "precision": baseline.precision,
                        "recall": baseline.recall,
                        "f1": baseline.f1,
                    },
                    "outcome": "no_pareto_improvement",
                },
            )

        return {
            "agent_name": agent_name,
            "parent_version": parent.version,
            "candidates_evaluated": len(candidates),
            "promoted": promoted.to_dict() if promoted else None,
            "baseline": {
                "precision": baseline.precision,
                "recall": baseline.recall,
                "f1": baseline.f1,
            },
        }


__all__ = [
    "EvalCase",
    "EvalResult",
    "PromptEvolutionEngine",
    "PromptStore",
    "PromptVersion",
    "adversarial_addendum",
    "evaluate_prompt",
    "few_shot_bootstrap",
    "genetic_mutate",
    "is_pareto_improvement",
    "meets_acceptance_floor",
]
