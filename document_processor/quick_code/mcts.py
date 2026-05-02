"""
QuickCode V2 — MCTSRunner: UCT tree search over candidate code.

Bandit / Monte Carlo Tree Search picker for Pro-mode runs.  Given a
list of candidate ``CodeSnippet`` objects and an async ``scorer``,
the runner repeatedly picks a candidate using the UCT formula

    UCT(i) = mean_score(i) + c * sqrt( log(N) / n_i )

scores it, updates the bookkeeping, and returns the candidate with
the highest visit count (the standard MCTS recommendation).

Why a flat list rather than a deep tree?  In V2 candidates are
already produced by the reasoner / SkCoder / mesh, so the tree
collapses to depth-1.  We keep the UCT statistics + an explicit
``MCTSNode`` per candidate so observers can audit the run after
the fact.

Deterministic with ``seed`` so tests can pin a specific selection
order.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import Awaitable, Callable

from .contracts import CodeSnippet, MCTSNode

logger = logging.getLogger(__name__)


Scorer = Callable[[CodeSnippet], Awaitable[float] | float]


class MCTSRunner:
    """UCT picker over a flat candidate list."""

    def __init__(
        self,
        *,
        c: float = 1.41,
        max_iters: int = 16,
        seed: int | None = None,
        cold_start: int = 1,
    ) -> None:
        self._c = max(0.0, float(c))
        self._max_iters = max(1, int(max_iters))
        self._cold_start = max(1, int(cold_start))
        self._rng = random.Random(seed)

    @property
    def max_iters(self) -> int:
        return self._max_iters

    @property
    def c(self) -> float:
        return self._c

    # ─── Public API ─────────────────────────────────────────────────

    async def select(
        self,
        candidates: list[CodeSnippet],
        scorer: Scorer,
    ) -> tuple[CodeSnippet, list[MCTSNode]]:
        """Run UCT for at most ``max_iters`` iterations and return
        the best candidate plus the audit trail of nodes."""
        if not candidates:
            raise ValueError("MCTSRunner.select requires at least one candidate")
        nodes: list[MCTSNode] = [
            MCTSNode(id=f"n{i}", code=c.source, depth=1)
            for i, c in enumerate(candidates)
        ]

        # Cold-start: every candidate gets at least ``cold_start``
        # visits before UCT takes over.
        for _ in range(self._cold_start):
            for i, snippet in enumerate(candidates):
                if len(nodes[i].metadata.get("scores", [])) >= self._cold_start:
                    continue
                await self._score_and_record(snippet, nodes[i], scorer)

        remaining = max(0, self._max_iters - sum(n.visit_count for n in nodes))

        for _ in range(remaining):
            idx = self._select_uct(nodes)
            await self._score_and_record(candidates[idx], nodes[idx], scorer)

        # Pick the candidate with the highest visit count; break ties
        # by mean score, then by stable index.
        def _rank_key(i: int) -> tuple[int, float, int]:
            n = nodes[i]
            mean = (n.score / n.visit_count) if n.visit_count else 0.0
            return (n.visit_count, mean, -i)

        best = max(range(len(candidates)), key=_rank_key)
        return candidates[best], nodes

    # ─── Internals ──────────────────────────────────────────────────

    def _select_uct(self, nodes: list[MCTSNode]) -> int:
        # Any unvisited node wins immediately.
        unvisited = [i for i, n in enumerate(nodes) if n.visit_count == 0]
        if unvisited:
            return self._rng.choice(unvisited)

        total = sum(n.visit_count for n in nodes)
        log_n = math.log(max(1, total))

        best_score = -math.inf
        best_idx = 0
        # Iterate in fixed order so the rng's tie-break is the only
        # source of nondeterminism.
        for i, n in enumerate(nodes):
            mean = n.score / n.visit_count
            ucb = mean + self._c * math.sqrt(log_n / n.visit_count)
            if ucb > best_score:
                best_score = ucb
                best_idx = i
            elif ucb == best_score and self._rng.random() < 0.5:
                best_idx = i
        return best_idx

    async def _score_and_record(
        self,
        snippet: CodeSnippet,
        node: MCTSNode,
        scorer: Scorer,
    ) -> None:
        try:
            res = scorer(snippet)
            if asyncio.iscoroutine(res):
                value = float(await res)
            else:
                value = float(res)  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover
            logger.debug("mcts scorer raised: %s", exc)
            value = 0.0
        node.visit_count += 1
        node.score += value
        scores = node.metadata.setdefault("scores", [])
        scores.append(value)


__all__ = ["MCTSRunner", "Scorer"]
