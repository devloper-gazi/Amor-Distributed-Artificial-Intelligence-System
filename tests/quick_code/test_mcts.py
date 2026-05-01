"""
Unit tests for ``document_processor/quick_code/mcts.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from document_processor.quick_code.contracts import CodeSnippet
from document_processor.quick_code.mcts import MCTSRunner


def _run(coro):
    return asyncio.run(coro)


def _snippets(scores: list[float]) -> list[CodeSnippet]:
    return [
        CodeSnippet(source=f"sn{i}", score=s, language="python")
        for i, s in enumerate(scores)
    ]


# ─────────────────────────────────────────────────────────────────────
# Empty input
# ─────────────────────────────────────────────────────────────────────


def test_empty_candidates_raises():
    runner = MCTSRunner(max_iters=4)

    async def scorer(_):
        return 1.0

    with pytest.raises(ValueError):
        _run(runner.select([], scorer))


# ─────────────────────────────────────────────────────────────────────
# Determinism + scoring
# ─────────────────────────────────────────────────────────────────────


def test_picks_higher_scored_leaf():
    candidates = _snippets([0.1, 0.2, 0.3, 0.9])
    runner = MCTSRunner(max_iters=20, seed=42)

    async def scorer(snippet: CodeSnippet) -> float:
        # Return the snippet's own attached score so the answer is
        # easily checkable.
        return snippet.score

    best, nodes = _run(runner.select(candidates, scorer))
    assert best.source == "sn3"
    assert any(n.visit_count > 0 for n in nodes)


def test_deterministic_with_seed():
    candidates = _snippets([0.5, 0.5, 0.5, 0.5])

    async def scorer(_):
        return 0.5

    runner_a = MCTSRunner(max_iters=8, seed=1)
    runner_b = MCTSRunner(max_iters=8, seed=1)
    _, nodes_a = _run(runner_a.select(candidates, scorer))
    _, nodes_b = _run(runner_b.select(candidates, scorer))
    assert [n.visit_count for n in nodes_a] == [n.visit_count for n in nodes_b]


def test_max_iters_cap_respected():
    candidates = _snippets([0.5, 0.5, 0.5])
    runner = MCTSRunner(max_iters=3, seed=0)

    calls = [0]

    async def scorer(_):
        calls[0] += 1
        return 0.5

    _run(runner.select(candidates, scorer))
    # Cold start gives each candidate 1 visit (3 calls); max_iters
    # 3 is exactly that, so no UCT iterations beyond the cold start.
    assert calls[0] == 3


# ─────────────────────────────────────────────────────────────────────
# Scorer error handling
# ─────────────────────────────────────────────────────────────────────


def test_scorer_exception_treated_as_zero():
    candidates = _snippets([1.0, 1.0])

    async def flaky(snippet: CodeSnippet) -> float:
        if snippet.source == "sn0":
            raise RuntimeError("oops")
        return 0.7

    runner = MCTSRunner(max_iters=4, seed=0)
    best, nodes = _run(runner.select(candidates, flaky))
    # sn1 scored 0.7 every time, sn0 scored 0.  Best should be sn1.
    assert best.source == "sn1"
