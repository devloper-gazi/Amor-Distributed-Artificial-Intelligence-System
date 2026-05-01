"""Unit tests for Subsystem C — prompt evolution."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from document_processor.sentinel.evolution.governance import (
    LedgerStore,
    load_immutable_constraints,
)
from document_processor.sentinel.evolution.prompt_evolution import (
    EvalCase,
    EvalResult,
    PromptEvolutionEngine,
    PromptStore,
    PromptVersion,
    adversarial_addendum,
    evaluate_prompt,
    few_shot_bootstrap,
    genetic_mutate,
    is_pareto_improvement,
    meets_acceptance_floor,
)


def _run(coro):
    return asyncio.run(coro)


# ─── PromptStore round-trip ─────────────────────────────────────────


def test_prompt_store_writes_and_reads_back(tmp_path: Path):
    store = PromptStore(tmp_path)
    v = PromptVersion(
        agent_name="auditor",
        version="v001",
        system_prompt="You are an expert security engineer.",
        mutation_method="manual",
        created_at=1.0,
    )
    path = store.write_version(v)
    assert path.exists()
    versions = store.list_versions("auditor")
    assert len(versions) == 1
    assert versions[0].system_prompt.startswith("You are")


def test_prompt_store_promote_demotes_previous_production(tmp_path: Path):
    store = PromptStore(tmp_path)
    v1 = PromptVersion(agent_name="auditor", version="v001",
                       system_prompt="A", status="production")
    v2 = PromptVersion(agent_name="auditor", version="v002",
                       system_prompt="B", status="staging")
    store.write_version(v1)
    store.write_version(v2)
    store.promote(v2)
    after = {v.version: v.status for v in store.list_versions("auditor")}
    assert after["v001"] == "archived"
    assert after["v002"] == "production"


# ─── Mutators ───────────────────────────────────────────────────────


class _FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self, prompt: str, system: str | None, max_tokens: int) -> str:
        self.calls += 1
        return self.responses.pop(0) if self.responses else ""


def test_genetic_mutate_drops_empty_returns():
    llm = _FakeLLM(["mutant 1", "", "mutant 3", "  ", "mutant 5"])
    out = _run(genetic_mutate(parent_prompt="P", llm=llm, n_mutants=5))
    # Empty + whitespace-only responses dropped.
    assert "mutant 1" in out
    assert "mutant 3" in out
    assert "mutant 5" in out
    assert "" not in out


def test_genetic_mutate_strips_fences():
    llm = _FakeLLM(["```\nmutant text\n```"])
    out = _run(genetic_mutate(parent_prompt="P", llm=llm, n_mutants=1))
    assert out == ["mutant text"]


def test_few_shot_bootstrap_emits_n_combos():
    pool = [
        {"input": f"in{i}", "output": f"out{i}"}
        for i in range(10)
    ]
    out = few_shot_bootstrap(
        parent_prompt="Parent.", pool=pool,
        sample_size=3, n_combos=4,
    )
    assert len(out) == 4
    for prompt, demos in out:
        assert "## Examples" in prompt
        assert len(demos) == 3


def test_few_shot_empty_pool_returns_empty():
    out = few_shot_bootstrap(parent_prompt="P", pool=[])
    assert out == []


def test_adversarial_addendum_appends_section():
    out = adversarial_addendum(
        parent_prompt="Parent.",
        failure_summary="watch out for crypto IV reuse",
    )
    assert "Adversarial cases" in out
    assert "crypto IV reuse" in out


def test_adversarial_addendum_no_summary_returns_parent():
    assert adversarial_addendum(parent_prompt="P", failure_summary="") == "P"


# ─── evaluate_prompt ────────────────────────────────────────────────


async def _scorer_returning(json_obj: dict) -> str:
    return json.dumps(json_obj)


def test_evaluate_prompt_perfect_score():
    cases = [
        EvalCase(input_prompt="x", expected_verdict="true_positive"),
        EvalCase(input_prompt="y", expected_verdict="true_positive"),
    ]

    async def scorer(_sys, _user, _max):
        return json.dumps({"verdict": "true_positive"})

    res = _run(evaluate_prompt(system_prompt="P", cases=cases, scorer=scorer))
    assert res.cases == 2
    assert res.precision == 1.0
    assert res.recall == 1.0
    assert res.f1 == 1.0


def test_evaluate_prompt_handles_unparseable():
    cases = [EvalCase(input_prompt="x", expected_verdict="false_positive")]

    async def scorer(_sys, _user, _max):
        return "this is not JSON or a verdict word"

    res = _run(evaluate_prompt(system_prompt="P", cases=cases, scorer=scorer))
    assert res.cases == 1
    # Precision/recall depend on how the verdict was extracted; the
    # scorer returned no positive class so neither tp nor fp counted.
    assert res.precision == 0.0


def test_evaluate_prompt_partial_correctness():
    cases = [
        EvalCase(input_prompt="a", expected_verdict="true_positive"),
        EvalCase(input_prompt="b", expected_verdict="true_positive"),
        EvalCase(input_prompt="c", expected_verdict="false_positive"),
    ]
    counter = {"i": 0}

    async def scorer(_sys, _user, _max):
        counter["i"] += 1
        # First two: correct true_positive.  Third: incorrect.
        verdict = "true_positive" if counter["i"] <= 2 else "true_positive"
        return json.dumps({"verdict": verdict})

    res = _run(evaluate_prompt(system_prompt="P", cases=cases, scorer=scorer))
    # tp=2 fp=1 fn=0 → precision 2/3, recall 1.0, f1 = 0.8
    assert res.precision == pytest.approx(2 / 3, rel=1e-3)
    assert res.recall == 1.0


# ─── Pareto + acceptance ────────────────────────────────────────────


def test_pareto_improvement_precision_winner():
    base = EvalResult(precision=0.6, recall=0.5)
    cand = EvalResult(precision=0.7, recall=0.5)
    assert is_pareto_improvement(cand, base, improvement_required=0.05)


def test_pareto_improvement_no_change_blocks():
    base = EvalResult(precision=0.6, recall=0.5)
    cand = EvalResult(precision=0.6, recall=0.5)
    assert not is_pareto_improvement(cand, base, improvement_required=0.05)


def test_pareto_improvement_recall_better_but_precision_worse_blocks():
    base = EvalResult(precision=0.7, recall=0.4)
    cand = EvalResult(precision=0.5, recall=0.9)  # worse precision
    assert not is_pareto_improvement(cand, base, improvement_required=0.05)


def test_meets_acceptance_floor_passes_at_floor():
    c = load_immutable_constraints()
    res = EvalResult(precision=c.precision_floor, recall=c.recall_floor)
    assert meets_acceptance_floor(res, c)


def test_meets_acceptance_floor_blocks_below():
    c = load_immutable_constraints()
    res = EvalResult(precision=0.0, recall=0.0)
    assert not meets_acceptance_floor(res, c)


# ─── PromptEvolutionEngine end-to-end ───────────────────────────────


def test_engine_run_generation_promotes_pareto_winner(tmp_path: Path):
    """End-to-end: parent scores poorly; one mutant scores
    strongly above floor + Pareto improving → promoted."""
    store = PromptStore(tmp_path)
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()

    parent = PromptVersion(
        agent_name="auditor",
        version="v001",
        system_prompt="Parent prompt — too vague.",
        status="production",
    )
    store.write_version(parent)

    # Mutator returns 1 mutant + dummies.
    class _Mutator:
        async def __call__(self, p, s, m):
            return "Better mutant prompt"

    # Scorer: parent → wrong verdict, mutant → right verdict.
    async def scorer(system_prompt: str, user: str, max_tokens: int) -> str:
        verdict = (
            "true_positive" if "Better mutant" in system_prompt
            else "false_positive"
        )
        return json.dumps({"verdict": verdict})

    cases = [
        EvalCase(input_prompt=f"case-{i}", expected_verdict="true_positive")
        for i in range(5)
    ]

    engine = PromptEvolutionEngine(
        store=store, ledger=ledger, constraints=constraints, rng_seed=0,
    )
    report = _run(engine.run_generation(
        agent_name="auditor",
        parent=parent,
        mutator=_Mutator(),
        scorer=scorer,
        eval_cases=cases,
        few_shot_pool=[],
        n_genetic=1,
        n_few_shot=0,
        improvement_required=0.05,
    ))
    assert report["promoted"] is not None
    promoted = report["promoted"]
    assert promoted["mutation_method"] == "genetic"
    # Ledger entry recorded.
    kinds = [e.kind for e in ledger.entries()]
    assert "prompt_promoted" in kinds


def test_engine_run_generation_rejects_when_no_improvement(tmp_path: Path):
    store = PromptStore(tmp_path)
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()

    parent = PromptVersion(
        agent_name="auditor", version="v001",
        system_prompt="Parent.", status="production",
    )
    store.write_version(parent)

    class _Mutator:
        async def __call__(self, p, s, m):
            return "Equivalent mutant"

    async def scorer(system_prompt, user, max_tokens):
        return json.dumps({"verdict": "true_positive"})

    cases = [
        EvalCase(input_prompt=f"c{i}", expected_verdict="true_positive")
        for i in range(3)
    ]
    engine = PromptEvolutionEngine(
        store=store, ledger=ledger, constraints=constraints,
    )
    report = _run(engine.run_generation(
        agent_name="auditor",
        parent=parent,
        mutator=_Mutator(),
        scorer=scorer,
        eval_cases=cases,
        few_shot_pool=[],
        n_genetic=1,
        n_few_shot=0,
        improvement_required=0.05,
    ))
    assert report["promoted"] is None
    kinds = [e.kind for e in ledger.entries()]
    assert "prompt_mutated" in kinds
    assert "prompt_promoted" not in kinds
