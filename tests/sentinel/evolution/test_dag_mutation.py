"""Unit tests for Subsystem H — DAG mutation."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest

from document_processor.sentinel.evolution.dag_mutation import (
    DAG,
    DAGMutator,
    DAGNode,
    DAGStore,
    DEFAULT_DAG,
    ReplayCase,
    ReplayMetric,
    add_edge,
    add_node,
    bypass_node,
    is_pareto_dag_improvement,
    parallelise,
    run_replay,
    swap,
)
from document_processor.sentinel.evolution.governance import (
    LedgerStore,
    load_immutable_constraints,
)


def _run(coro):
    return asyncio.run(coro)


# ─── DAG model invariants ──────────────────────────────────────────


def test_default_dag_is_acyclic():
    assert DEFAULT_DAG.has_cycle() is False
    order = DEFAULT_DAG.topological_order()
    # Every node appears exactly once.
    assert len(order) == len(DEFAULT_DAG.nodes)
    # static_swarm must precede aggregate.
    assert order.index("static_swarm") < order.index("aggregate")


def test_dag_round_trip_dict():
    d = DEFAULT_DAG.to_dict()
    restored = DAG.from_dict(d)
    assert restored.version == DEFAULT_DAG.version
    assert len(restored.nodes) == len(DEFAULT_DAG.nodes)


# ─── Mutation operators ────────────────────────────────────────────


def test_add_edge_succeeds():
    # auditor → reasoner is a valid forward edge (both already
    # appear before patcher in the topological order).
    mut = add_edge(DEFAULT_DAG, "auditor", "reasoner")
    assert mut is not None
    assert ("auditor", "reasoner") in mut.edges


def test_add_edge_self_loop_blocked():
    assert add_edge(DEFAULT_DAG, "patcher", "patcher") is None


def test_add_edge_unknown_node_blocked():
    assert add_edge(DEFAULT_DAG, "patcher", "fictional_stage") is None


def test_add_edge_creating_cycle_blocked():
    # report → static_swarm would create a cycle.
    assert add_edge(DEFAULT_DAG, "report", "static_swarm") is None


def test_add_node_inserts_after():
    mut = add_node(DEFAULT_DAG, "pre_filter", "static_swarm")
    assert mut is not None
    assert mut.has_node("pre_filter")
    # static_swarm now points to pre_filter, not directly to aggregate.
    assert ("static_swarm", "pre_filter") in mut.edges
    assert ("pre_filter", "aggregate") in mut.edges
    assert ("static_swarm", "aggregate") not in mut.edges


def test_add_node_existing_label_blocked():
    assert add_node(DEFAULT_DAG, "auditor", "static_swarm") is None


def test_bypass_node_redteam():
    mut = bypass_node(DEFAULT_DAG, "redteam")
    assert mut is not None
    assert not mut.has_node("redteam")
    # Redteam's incoming (rag_enrich) now flows directly to its
    # outgoing (patcher).
    assert ("rag_enrich", "patcher") in mut.edges


def test_bypass_node_with_no_neighbours_blocked():
    # `report` has no outgoing edges; bypass should fail.
    assert bypass_node(DEFAULT_DAG, "report") is None


def test_parallelise_increases_factor():
    mut = parallelise(DEFAULT_DAG, "auditor", 5)
    assert mut is not None
    auditor = mut.find_node("auditor")
    assert auditor is not None
    assert auditor.parallel_factor == 5


def test_parallelise_below_two_blocked():
    assert parallelise(DEFAULT_DAG, "auditor", 1) is None


def test_swap_only_when_neighbourhood_identical():
    # auditor / reasoner / redteam share the same neighbourhood
    # (in: rag_enrich, out: patcher) per DEFAULT_DAG.
    mut = swap(DEFAULT_DAG, "reasoner", "redteam")
    assert mut is not None
    labels = [n.label for n in mut.nodes]
    assert labels.index("redteam") < labels.index("reasoner")


def test_swap_blocks_distinct_neighbourhoods():
    # auditor and patcher don't share the same neighbours.
    assert swap(DEFAULT_DAG, "auditor", "patcher") is None


# ─── Replay + Pareto ───────────────────────────────────────────────


def test_run_replay_perfect_score():
    cases = [
        ReplayCase(scan_id=f"s{i}", input_payload={},
                   expected_verdict="true_positive")
        for i in range(4)
    ]

    async def scorer(dag, case):
        return ("true_positive", "high", 10.0)

    res = _run(run_replay(dag=DEFAULT_DAG, cases=cases, scorer=scorer))
    assert res.cases == 4
    assert res.precision == 1.0
    assert res.recall == 1.0


def test_pareto_improvement_better_precision_same_recall():
    base = ReplayMetric(precision=0.6, recall=0.5, elapsed_ms=100)
    cand = ReplayMetric(precision=0.8, recall=0.5, elapsed_ms=100)
    assert is_pareto_dag_improvement(cand, base)


def test_pareto_improvement_better_latency_same_others():
    base = ReplayMetric(precision=0.6, recall=0.5, elapsed_ms=200)
    cand = ReplayMetric(precision=0.6, recall=0.5, elapsed_ms=120)
    assert is_pareto_dag_improvement(cand, base)


def test_pareto_improvement_blocks_when_metric_regresses():
    base = ReplayMetric(precision=0.6, recall=0.5, elapsed_ms=100)
    cand = ReplayMetric(precision=0.7, recall=0.4, elapsed_ms=100)
    assert not is_pareto_dag_improvement(cand, base)


def test_pareto_improvement_latency_tolerance_band():
    base = ReplayMetric(precision=0.6, recall=0.5, elapsed_ms=100)
    cand = ReplayMetric(precision=0.7, recall=0.5, elapsed_ms=108)
    # Latency 8% worse; tolerance band default 10% → still passes.
    assert is_pareto_dag_improvement(cand, base)


# ─── DAGMutator end-to-end ─────────────────────────────────────────


def test_mutator_runs_replay_and_records_ledger(tmp_path: Path):
    store = DAGStore(tmp_path)
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    mutator = DAGMutator(store=store, ledger=ledger, constraints=constraints)
    parent = store.get_production() or DEFAULT_DAG

    cases = [
        ReplayCase(scan_id=f"s{i}", input_payload={},
                   expected_verdict="true_positive")
        for i in range(3)
    ]
    state = {"calls": 0}

    async def scorer(dag, case):
        # Mutant always strictly faster than parent: easy Pareto win.
        state["calls"] += 1
        latency = 50.0 if "_par_" in dag.version else 200.0
        return ("true_positive", "high", latency)

    proposals = _run(mutator.propose_generation(
        parent=parent,
        replay_cases=cases,
        scorer=scorer,
        operators=[("parallelise", {"label": "auditor", "n": 5})],
    ))
    assert proposals
    p = proposals[0]
    assert p.eval_metrics.cases == 3
    # Ledger entry for the mutation.
    kinds = [e.kind for e in ledger.entries()]
    assert "dag_mutated" in kinds
    # Stays in staging; promotion requires user approval.
    proposal_versions = {p.candidate.version for p in proposals}
    statuses = {dag.version: status for status, dag in store.list()}
    for v in proposal_versions:
        assert statuses[v] == "staging"


def test_mutator_promote_records_ledger(tmp_path: Path):
    store = DAGStore(tmp_path)
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    mutator = DAGMutator(store=store, ledger=ledger, constraints=constraints)
    parent = store.get_production() or DEFAULT_DAG

    cases = [ReplayCase(scan_id="s1", input_payload={},
                        expected_verdict="true_positive")]

    async def scorer(dag, case):
        return ("true_positive", "high", 50.0)

    proposals = _run(mutator.propose_generation(
        parent=parent, replay_cases=cases, scorer=scorer,
        operators=[("parallelise", {"label": "auditor", "n": 5})],
    ))
    assert proposals
    _run(mutator.promote(proposals[0]))
    statuses = {dag.version: status for status, dag in store.list()}
    promoted = proposals[0].candidate.version
    assert statuses[promoted] == "production"
    kinds = [e.kind for e in ledger.entries()]
    assert "dag_promoted" in kinds


def test_default_dagstore_writes_default_on_init(tmp_path: Path):
    store = DAGStore(tmp_path)
    prod = store.get_production()
    assert prod is not None
    assert prod.version == DEFAULT_DAG.version
