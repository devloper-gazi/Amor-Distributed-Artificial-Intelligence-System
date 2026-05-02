"""
Sentinel Evolution — Subsystem H: DAG architecture mutation.

The Sentinel pipeline DAG (Phase 2) gets to mutate itself.

We model the production DAG as a tiny graph (nodes + directed
edges) and define five mutation operators:

* **add_edge(src, dst)** — add a feedback / fan-in edge.
* **add_node(label, after)** — insert a new processing node after
  ``after``.
* **bypass_node(label)** — short-circuit a node so its inputs
  flow directly to its outputs (used to skip optional stages).
* **parallelise(label, n)** — turn a single node into N parallel
  variants whose outputs are voted.
* **swap(a, b)** — swap two adjacent nodes (topological reorder).

Every mutant DAG is replay-tested against the last K scans
(``DAGReplayer.run_replay``) — we already store finding inputs as
preferences, so we replay them through the proposed graph and
collect precision / recall / latency.  A mutant is **Pareto-
improving** when at least one metric is better and no metric is
worse than the production DAG.

License: MIT.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Literal


from .governance import (
    HardConstraintViolation,
    ImmutableConstraints,
    LedgerStore,
)


logger = logging.getLogger(__name__)


MutationOperator = Literal[
    "add_edge", "add_node", "bypass_node", "parallelise", "swap",
]


# ─────────────────────────────────────────────────────────────────────
# DAG model
# ─────────────────────────────────────────────────────────────────────


@dataclass
class DAGNode:
    label: str
    parallel_factor: int = 1   # 1 = single instance; N = voted ensemble


@dataclass
class DAG:
    version: str = "v001"
    nodes: list[DAGNode] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)  # (src, dst)
    metadata: dict[str, Any] = field(default_factory=dict)

    def has_node(self, label: str) -> bool:
        return any(n.label == label for n in self.nodes)

    def find_node(self, label: str) -> DAGNode | None:
        for n in self.nodes:
            if n.label == label:
                return n
        return None

    def topological_order(self) -> list[str]:
        """Kahn's algorithm.  Falls back to insertion order on cycle
        (which we never expect; mutation operators reject cycles)."""
        in_deg: dict[str, int] = {n.label: 0 for n in self.nodes}
        for src, dst in self.edges:
            if dst in in_deg:
                in_deg[dst] += 1
        queue = [n.label for n in self.nodes if in_deg.get(n.label, 0) == 0]
        out: list[str] = []
        while queue:
            cur = queue.pop(0)
            out.append(cur)
            for src, dst in self.edges:
                if src == cur:
                    in_deg[dst] -= 1
                    if in_deg[dst] == 0:
                        queue.append(dst)
        if len(out) < len(self.nodes):
            # Cycle — bail to insertion order.
            return [n.label for n in self.nodes]
        return out

    def has_cycle(self) -> bool:
        in_deg = {n.label: 0 for n in self.nodes}
        for src, dst in self.edges:
            if dst in in_deg:
                in_deg[dst] += 1
        queue = [n for n, d in in_deg.items() if d == 0]
        seen = 0
        while queue:
            cur = queue.pop(0)
            seen += 1
            for src, dst in self.edges:
                if src == cur:
                    in_deg[dst] -= 1
                    if in_deg[dst] == 0:
                        queue.append(dst)
        return seen != len(self.nodes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "nodes": [asdict(n) for n in self.nodes],
            "edges": list(self.edges),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DAG":
        nodes = [
            DAGNode(
                label=str(d.get("label") or ""),
                parallel_factor=int(d.get("parallel_factor") or 1),
            )
            for d in (data.get("nodes") or [])
        ]
        edges = [
            (str(e[0]), str(e[1]))
            for e in (data.get("edges") or [])
            if isinstance(e, (list, tuple)) and len(e) == 2
        ]
        return cls(
            version=str(data.get("version") or "v001"),
            nodes=nodes, edges=edges,
            metadata=dict(data.get("metadata") or {}),
        )

    def clone(self, *, new_version: str | None = None) -> "DAG":
        return DAG(
            version=new_version or self.version,
            nodes=[DAGNode(label=n.label, parallel_factor=n.parallel_factor)
                   for n in self.nodes],
            edges=list(self.edges),
            metadata=dict(self.metadata),
        )


# Default Sentinel V1 DAG.
DEFAULT_DAG = DAG(
    version="v001",
    nodes=[
        DAGNode(label="static_swarm"),
        DAGNode(label="ml_pipeline"),
        DAGNode(label="aggregate"),
        DAGNode(label="rag_enrich"),
        DAGNode(label="auditor", parallel_factor=3),
        DAGNode(label="reasoner"),
        DAGNode(label="redteam"),
        DAGNode(label="patcher"),
        DAGNode(label="critic_loop"),
        DAGNode(label="judge"),
        DAGNode(label="score"),
        DAGNode(label="report"),
    ],
    edges=[
        ("static_swarm", "aggregate"),
        ("ml_pipeline", "aggregate"),
        ("aggregate", "rag_enrich"),
        ("rag_enrich", "auditor"),
        ("rag_enrich", "reasoner"),
        ("rag_enrich", "redteam"),
        ("auditor", "patcher"),
        ("reasoner", "patcher"),
        ("redteam", "patcher"),
        ("patcher", "critic_loop"),
        ("critic_loop", "judge"),
        ("judge", "score"),
        ("score", "report"),
    ],
)


# ─────────────────────────────────────────────────────────────────────
# DAGStore
# ─────────────────────────────────────────────────────────────────────


class DAGStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "architecture"
        self.root.mkdir(parents=True, exist_ok=True)
        # Bootstrap with the default DAG if the dir is empty.
        if not any(self.root.glob("*.yaml")):
            self.write(DEFAULT_DAG, status="production")

    def write(self, dag: DAG, *, status: str = "staging") -> Path:
        path = self.root / f"dag_{dag.version}.yaml"
        payload = dag.to_dict()
        payload["status"] = status
        try:
            import yaml  # type: ignore
            text = yaml.safe_dump(payload, sort_keys=True)
        except Exception:
            text = json.dumps(payload, indent=2, default=str)
        path.write_text(text, encoding="utf-8")
        return path

    def list(self) -> list[tuple[str, DAG]]:
        out: list[tuple[str, DAG]] = []
        for p in sorted(self.root.glob("*.yaml")):
            try:
                import yaml  # type: ignore
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            status = str(data.get("status") or "staging")
            out.append((status, DAG.from_dict(data)))
        return out

    def get_production(self) -> DAG | None:
        for status, dag in self.list():
            if status == "production":
                return dag
        return None

    def promote(self, dag: DAG) -> None:
        # Demote current production.
        for status, other in self.list():
            if status == "production" and other.version != dag.version:
                self.write(other, status="archived")
        self.write(dag, status="production")


# ─────────────────────────────────────────────────────────────────────
# Mutation operators
# ─────────────────────────────────────────────────────────────────────


def _next_version(parent: DAG, suffix: str) -> str:
    return f"{parent.version}_{suffix}_{int(time.time())}"


def add_edge(parent: DAG, src: str, dst: str) -> DAG | None:
    if src == dst:
        return None
    if not parent.has_node(src) or not parent.has_node(dst):
        return None
    if (src, dst) in parent.edges:
        return None
    mutant = parent.clone(new_version=_next_version(parent, "edge"))
    mutant.edges.append((src, dst))
    if mutant.has_cycle():
        return None
    mutant.metadata.setdefault("mutations", []).append({
        "op": "add_edge", "src": src, "dst": dst,
    })
    return mutant


def add_node(parent: DAG, label: str, after: str) -> DAG | None:
    if parent.has_node(label):
        return None
    if not parent.has_node(after):
        return None
    mutant = parent.clone(new_version=_next_version(parent, "node"))
    mutant.nodes.append(DAGNode(label=label))
    # Re-route the outgoing edges of `after` through the new node.
    new_edges: list[tuple[str, str]] = []
    rerouted = False
    for src, dst in mutant.edges:
        if src == after:
            new_edges.append((label, dst))
            rerouted = True
        else:
            new_edges.append((src, dst))
    if not rerouted:
        # `after` had no outgoing edges; just add (after, label).
        new_edges.append((after, label))
    else:
        new_edges.append((after, label))
    mutant.edges = new_edges
    if mutant.has_cycle():
        return None
    mutant.metadata.setdefault("mutations", []).append({
        "op": "add_node", "label": label, "after": after,
    })
    return mutant


def bypass_node(parent: DAG, label: str) -> DAG | None:
    if not parent.has_node(label):
        return None
    incoming = [src for src, dst in parent.edges if dst == label]
    outgoing = [dst for src, dst in parent.edges if src == label]
    if not incoming or not outgoing:
        return None
    mutant = parent.clone(new_version=_next_version(parent, "bypass"))
    mutant.nodes = [n for n in mutant.nodes if n.label != label]
    new_edges: list[tuple[str, str]] = []
    for src, dst in mutant.edges:
        if src == label or dst == label:
            continue
        new_edges.append((src, dst))
    for src in incoming:
        for dst in outgoing:
            if (src, dst) not in new_edges:
                new_edges.append((src, dst))
    mutant.edges = new_edges
    if mutant.has_cycle():
        return None
    mutant.metadata.setdefault("mutations", []).append({
        "op": "bypass_node", "label": label,
    })
    return mutant


def parallelise(parent: DAG, label: str, n: int) -> DAG | None:
    if not parent.has_node(label) or n < 2:
        return None
    mutant = parent.clone(new_version=_next_version(parent, "par"))
    target = mutant.find_node(label)
    if target is None:
        return None
    target.parallel_factor = max(target.parallel_factor, int(n))
    mutant.metadata.setdefault("mutations", []).append({
        "op": "parallelise", "label": label, "factor": int(n),
    })
    return mutant


def swap(parent: DAG, a: str, b: str) -> DAG | None:
    """Swap nodes a and b only when they share the same neighbourhood."""
    if a == b or not parent.has_node(a) or not parent.has_node(b):
        return None
    inc_a = sorted({src for src, dst in parent.edges if dst == a})
    inc_b = sorted({src for src, dst in parent.edges if dst == b})
    out_a = sorted({dst for src, dst in parent.edges if src == a})
    out_b = sorted({dst for src, dst in parent.edges if src == b})
    if inc_a != inc_b or out_a != out_b:
        # Only safe when the two nodes have identical neighbourhoods.
        return None
    mutant = parent.clone(new_version=_next_version(parent, "swap"))
    # Reorder the nodes list.
    idx_a = next(i for i, n in enumerate(mutant.nodes) if n.label == a)
    idx_b = next(i for i, n in enumerate(mutant.nodes) if n.label == b)
    mutant.nodes[idx_a], mutant.nodes[idx_b] = mutant.nodes[idx_b], mutant.nodes[idx_a]
    mutant.metadata.setdefault("mutations", []).append({
        "op": "swap", "a": a, "b": b,
    })
    return mutant


# ─────────────────────────────────────────────────────────────────────
# Replay tester
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ReplayCase:
    """One historical scan input + its known correct verdict."""
    scan_id: str
    input_payload: dict[str, Any]
    expected_verdict: str
    expected_severity: str = "low"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplayMetric:
    cases: int = 0
    correct: int = 0
    precision: float = 0.0
    recall: float = 0.0
    elapsed_ms: float = 0.0


# Replay scorer signature: takes ``(dag, case)`` and returns
# ``(verdict, severity, latency_ms)``.  The orchestrator uses this
# to feed each candidate graph through the same payloads.
ReplayScorer = Callable[[DAG, ReplayCase], Awaitable[tuple[str, str, float]]]


async def run_replay(
    *,
    dag: DAG,
    cases: list[ReplayCase],
    scorer: ReplayScorer,
) -> ReplayMetric:
    if not cases:
        return ReplayMetric()
    start = time.monotonic()
    correct = 0
    tp = fp = fn = 0
    positive = {"true_positive", "exploitable", "approved"}
    elapsed_total = 0.0
    for c in cases:
        try:
            verdict, _sev, latency_ms = await scorer(dag, c)
        except Exception:
            verdict, _sev, latency_ms = "", "", 0.0
        elapsed_total += float(latency_ms or 0.0)
        verdict = (verdict or "").strip().lower()
        expected = (c.expected_verdict or "").strip().lower()
        if verdict == expected:
            correct += 1
        is_pred_pos = verdict in positive
        is_exp_pos = expected in positive
        if is_pred_pos and is_exp_pos:
            tp += 1
        elif is_pred_pos and not is_exp_pos:
            fp += 1
        elif not is_pred_pos and is_exp_pos:
            fn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return ReplayMetric(
        cases=len(cases),
        correct=correct,
        precision=round(precision, 4),
        recall=round(recall, 4),
        elapsed_ms=elapsed_total or ((time.monotonic() - start) * 1000.0),
    )


def is_pareto_dag_improvement(
    candidate: ReplayMetric,
    baseline: ReplayMetric,
    *,
    latency_tolerance_ratio: float = 0.10,
) -> bool:
    """DAG mutation Pareto rule: at least one of {precision, recall,
    latency} strictly better; no metric strictly worse beyond
    ``latency_tolerance_ratio`` (10%) for latency."""
    eps = 1e-9
    precision_better = candidate.precision > baseline.precision + eps
    precision_same_or_better = candidate.precision >= baseline.precision - eps
    recall_better = candidate.recall > baseline.recall + eps
    recall_same_or_better = candidate.recall >= baseline.recall - eps
    latency_floor = baseline.elapsed_ms * (1.0 + latency_tolerance_ratio)
    latency_better = candidate.elapsed_ms < baseline.elapsed_ms - eps
    latency_within_tolerance = candidate.elapsed_ms <= latency_floor + eps

    if precision_better and recall_same_or_better and latency_within_tolerance:
        return True
    if recall_better and precision_same_or_better and latency_within_tolerance:
        return True
    if (latency_better
            and precision_same_or_better
            and recall_same_or_better):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────


@dataclass
class DAGMutationProposal:
    candidate: DAG
    operator: MutationOperator
    eval_metrics: ReplayMetric
    pareto_improvement: bool
    requires_user_consent: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "operator": self.operator,
            "metrics": {
                "cases": self.eval_metrics.cases,
                "correct": self.eval_metrics.correct,
                "precision": self.eval_metrics.precision,
                "recall": self.eval_metrics.recall,
                "elapsed_ms": self.eval_metrics.elapsed_ms,
            },
            "pareto_improvement": self.pareto_improvement,
            "requires_user_consent": self.requires_user_consent,
        }


class DAGMutator:
    """Generates + evaluates DAG mutants, stages winners for human
    review (DAG mutation never auto-promotes per the spec)."""

    def __init__(
        self,
        *,
        store: DAGStore,
        ledger: LedgerStore,
        constraints: ImmutableConstraints,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.constraints = constraints

    async def propose_generation(
        self,
        *,
        parent: DAG,
        replay_cases: list[ReplayCase],
        scorer: ReplayScorer,
        operators: Iterable[tuple[MutationOperator, dict[str, Any]]] | None = None,
        rng: random.Random | None = None,
    ) -> list[DAGMutationProposal]:
        """Apply each ``(operator, args)`` pair → score → return a
        list of proposals.  The caller (route layer) hands proposals
        to the user for explicit approval — this method never
        promotes."""
        rng = rng or random.Random()
        operators = list(
            operators
            or self._default_operator_suggestions(parent, rng=rng),
        )

        # 1. Score baseline once.
        baseline = await run_replay(
            dag=parent, cases=replay_cases, scorer=scorer,
        )

        proposals: list[DAGMutationProposal] = []
        for op, args in operators:
            mutant = self._apply(parent, op, args)
            if mutant is None:
                continue
            try:
                self.constraints.check({
                    "version": mutant.version,
                    "nodes": [n.label for n in mutant.nodes],
                    "edges": list(mutant.edges),
                })
            except HardConstraintViolation as exc:
                self.ledger.append(
                    actor="dag_mutation",
                    kind="constraint_check_failed",
                    payload={
                        "operator": op,
                        "version": mutant.version,
                        "reason": str(exc),
                    },
                )
                continue
            metrics = await run_replay(
                dag=mutant, cases=replay_cases, scorer=scorer,
            )
            improving = is_pareto_dag_improvement(metrics, baseline)
            self.store.write(mutant, status="staging")
            self.ledger.append(
                actor="dag_mutation",
                kind="dag_mutated",
                payload={
                    "operator": op,
                    "version": mutant.version,
                    "metrics": {
                        "precision": metrics.precision,
                        "recall": metrics.recall,
                        "elapsed_ms": metrics.elapsed_ms,
                    },
                    "baseline": {
                        "precision": baseline.precision,
                        "recall": baseline.recall,
                        "elapsed_ms": baseline.elapsed_ms,
                    },
                    "pareto_improvement": improving,
                },
            )
            proposals.append(DAGMutationProposal(
                candidate=mutant,
                operator=op,                          # type: ignore[arg-type]
                eval_metrics=metrics,
                pareto_improvement=improving,
            ))
        return proposals

    async def promote(self, proposal: DAGMutationProposal) -> None:
        """Promote a user-approved proposal to production.  Records
        a dag_promoted ledger entry."""
        self.store.promote(proposal.candidate)
        self.ledger.append(
            actor="dag_mutation",
            kind="dag_promoted",
            payload={
                "version": proposal.candidate.version,
                "operator": proposal.operator,
                "metrics": {
                    "precision": proposal.eval_metrics.precision,
                    "recall": proposal.eval_metrics.recall,
                    "elapsed_ms": proposal.eval_metrics.elapsed_ms,
                },
            },
        )

    # ─── Internals ──────────────────────────────────────────────

    def _apply(
        self,
        parent: DAG,
        op: MutationOperator,
        args: dict[str, Any],
    ) -> DAG | None:
        if op == "add_edge":
            return add_edge(parent, str(args.get("src") or ""),
                            str(args.get("dst") or ""))
        if op == "add_node":
            return add_node(parent, str(args.get("label") or ""),
                            str(args.get("after") or ""))
        if op == "bypass_node":
            return bypass_node(parent, str(args.get("label") or ""))
        if op == "parallelise":
            return parallelise(parent, str(args.get("label") or ""),
                               int(args.get("n") or 1))
        if op == "swap":
            return swap(parent, str(args.get("a") or ""),
                        str(args.get("b") or ""))
        return None

    def _default_operator_suggestions(
        self,
        parent: DAG,
        *,
        rng: random.Random,
    ) -> list[tuple[MutationOperator, dict[str, Any]]]:
        out: list[tuple[MutationOperator, dict[str, Any]]] = []
        labels = [n.label for n in parent.nodes]
        # 1) Patcher → Reasoner feedback edge.
        if "patcher" in labels and "reasoner" in labels:
            out.append(("add_edge", {"src": "patcher", "dst": "reasoner"}))
        # 2) Bypass redteam for fast-path.
        if "redteam" in labels:
            out.append(("bypass_node", {"label": "redteam"}))
        # 3) Parallelise auditor 5x.
        if "auditor" in labels:
            out.append(("parallelise", {"label": "auditor", "n": 5}))
        return out


__all__ = [
    "DAG",
    "DAGMutationProposal",
    "DAGMutator",
    "DAGNode",
    "DAGStore",
    "DEFAULT_DAG",
    "MutationOperator",
    "ReplayCase",
    "ReplayMetric",
    "ReplayScorer",
    "add_edge",
    "add_node",
    "bypass_node",
    "is_pareto_dag_improvement",
    "parallelise",
    "run_replay",
    "swap",
]
