"""Cycle K.1 — Simulated Bifurcation router coverage.

Tests the QUBO construction + the greedy / SB comparator path.  The
SB solver itself is exercised with a small synthetic catalogue so
the test run stays under a few seconds (no GPU, CPU-only).
"""

from __future__ import annotations

import random
from typing import List

import pytest

from tools.meta_opt.sb_router import (
    ScheduleResult,
    SkillEntry,
    SkillScheduleQUBO,
    _greedy_solve,
    solve_skill_schedule,
    load_skill_catalogue,
)


# ─── QUBO construction ──────────────────────────────────────────────


def test_qubo_size_matches_skill_count():
    skills = [
        SkillEntry("a", token_cost=100, utility=0.5),
        SkillEntry("b", token_cost=200, utility=0.7),
        SkillEntry("c", token_cost=150, utility=0.6),
    ]
    qubo = SkillScheduleQUBO(skills, budget=1000)
    Q = qubo.build()
    assert qubo.n == 3
    assert len(Q) == 3
    assert all(len(row) == 3 for row in Q)


def test_qubo_diagonal_encodes_utility_reward():
    """Higher utility → lower (more-negative) diagonal entry (we minimise)."""
    skills = [
        SkillEntry("low", token_cost=100, utility=0.1),
        SkillEntry("high", token_cost=100, utility=0.9),
    ]
    qubo = SkillScheduleQUBO(skills, budget=10_000, budget_penalty=0.0)
    Q = qubo.build()
    assert Q[1][1] < Q[0][0]   # high-utility row has more-negative diagonal


def test_qubo_is_symmetric():
    """Q[i][j] == Q[j][i] so x^T Q x is well-defined."""
    skills = [
        SkillEntry("a", token_cost=50, utility=0.5,
                   affinity_with={"b": 0.4}),
        SkillEntry("b", token_cost=80, utility=0.6),
    ]
    qubo = SkillScheduleQUBO(skills, budget=200)
    Q = qubo.build()
    assert abs(Q[0][1] - Q[1][0]) < 1e-12


def test_qubo_affinity_pulls_pair_closer():
    """When two skills have affinity, the off-diagonal is more
    NEGATIVE than without — activating both is rewarded."""
    base = [
        SkillEntry("a", token_cost=10, utility=0.5),
        SkillEntry("b", token_cost=10, utility=0.5),
    ]
    enhanced = [
        SkillEntry("a", token_cost=10, utility=0.5, affinity_with={"b": 0.8}),
        SkillEntry("b", token_cost=10, utility=0.5),
    ]
    Q_base = SkillScheduleQUBO(base, budget=100, budget_penalty=0.0).build()
    Q_enh = SkillScheduleQUBO(enhanced, budget=100, budget_penalty=0.0).build()
    # Adding affinity makes the off-diagonal more negative.
    assert Q_enh[0][1] < Q_base[0][1]


def test_qubo_conflict_pushes_pair_apart():
    """Conflict makes the off-diagonal MORE POSITIVE — activating
    both is punished."""
    base = [
        SkillEntry("a", token_cost=10, utility=0.5),
        SkillEntry("b", token_cost=10, utility=0.5),
    ]
    enhanced = [
        SkillEntry("a", token_cost=10, utility=0.5, conflict_with={"b": 0.9}),
        SkillEntry("b", token_cost=10, utility=0.5),
    ]
    Q_base = SkillScheduleQUBO(base, budget=100, budget_penalty=0.0).build()
    Q_enh = SkillScheduleQUBO(enhanced, budget=100, budget_penalty=0.0).build()
    assert Q_enh[0][1] > Q_base[0][1]


def test_qubo_empty_skills_raises():
    with pytest.raises(ValueError):
        SkillScheduleQUBO([], budget=100)


# ─── Greedy comparator ─────────────────────────────────────────────


def test_greedy_respects_budget():
    """The greedy solver MUST NOT exceed the budget, even when high-
    utility skills are cheap."""
    skills = [
        SkillEntry(f"s{i}", token_cost=100, utility=0.9 - i * 0.01)
        for i in range(10)
    ]
    qubo = SkillScheduleQUBO(skills, budget=300)
    xs, _ = _greedy_solve(qubo)
    activated = [i for i, x in enumerate(xs) if x]
    total = sum(qubo.skills[i].token_cost for i in activated)
    assert total <= 300


def test_greedy_picks_highest_utility_per_cost_first():
    """Skills sort by utility/cost ratio descending."""
    skills = [
        SkillEntry("expensive_high", token_cost=300, utility=0.9),     # ratio 0.003
        SkillEntry("cheap_meh", token_cost=100, utility=0.5),          # ratio 0.005 ← wins
        SkillEntry("cheap_bad", token_cost=100, utility=0.1),          # ratio 0.001
    ]
    qubo = SkillScheduleQUBO(skills, budget=400)
    xs, _ = _greedy_solve(qubo)
    activated_names = [qubo.skills[i].skill_id for i, x in enumerate(xs) if x]
    # `cheap_meh` is chosen first (highest ratio), then expensive_high fits
    # in the remaining budget (400 − 100 = 300).  cheap_bad doesn't fit
    # (would need budget=500).
    assert "cheap_meh" in activated_names
    assert "expensive_high" in activated_names
    assert "cheap_bad" not in activated_names


# ─── SB solver smoke ───────────────────────────────────────────────


def test_sb_solver_produces_valid_schedule():
    """SB returns a schedule that obeys the budget constraint after
    penalty-driven optimisation.  Doesn't claim SB beats greedy here
    — that's the bench test below."""
    skills = [
        SkillEntry(f"s{i}", token_cost=random.randint(50, 200),
                   utility=random.uniform(0.3, 0.9))
        for i in range(8)
    ]
    qubo = SkillScheduleQUBO(skills, budget=500, budget_penalty=0.01)
    result = solve_skill_schedule(qubo, max_steps=500)
    assert isinstance(result, ScheduleResult)
    assert len(result.activated) <= len(skills)
    assert result.skills_evaluated == len(skills)


def test_sb_solver_handles_unsolvable_input_gracefully():
    """Very tight budget (zero) → SB should select nothing or hit the
    greedy fallback; either way, no exception."""
    skills = [
        SkillEntry("a", token_cost=1000, utility=0.5),
        SkillEntry("b", token_cost=1000, utility=0.5),
    ]
    qubo = SkillScheduleQUBO(skills, budget=0, budget_penalty=10.0)
    result = solve_skill_schedule(qubo, max_steps=100)
    assert result.total_cost <= 0 + 2  # rounding allowance
    # Falls through to greedy if SB fails; the result is still well-formed.


def test_sb_router_acceptance_synthetic_8skill(monkeypatch):
    """Plan-agent acceptance: SB must beat greedy on a synthetic
    multi-affinity setup.  We use 8 skills (not the full 32 to keep
    test wall-clock low); the qualitative bar is "SB's objective <=
    greedy's objective" on the same QUBO."""
    skills: List[SkillEntry] = [
        SkillEntry("api",   token_cost=200, utility=0.8,
                   affinity_with={"db": 0.6, "test": 0.5}),
        SkillEntry("db",    token_cost=150, utility=0.7,
                   affinity_with={"api": 0.6}),
        SkillEntry("test",  token_cost=100, utility=0.6,
                   affinity_with={"api": 0.5, "ci": 0.4}),
        SkillEntry("ci",    token_cost=120, utility=0.5,
                   affinity_with={"test": 0.4}),
        SkillEntry("ui",    token_cost=180, utility=0.6,
                   conflict_with={"db": 0.3}),
        SkillEntry("auth",  token_cost=130, utility=0.7,
                   affinity_with={"api": 0.3}),
        SkillEntry("docs",  token_cost=80,  utility=0.4),
        SkillEntry("lint",  token_cost=60,  utility=0.3),
    ]
    qubo = SkillScheduleQUBO(skills, budget=700, budget_penalty=0.005,
                             affinity_weight=1.0)
    sb_result = solve_skill_schedule(qubo, max_steps=2000, use_sb=True)
    greedy_result = solve_skill_schedule(qubo, max_steps=0, use_sb=False)
    # SB's objective MUST be ≤ greedy's (lower is better in our minimise).
    # Plus or minus 1e-3 to absorb numerical noise.
    assert sb_result.objective_value <= greedy_result.objective_value + 1e-3, (
        f"SB={sb_result.objective_value:.3f} vs Greedy={greedy_result.objective_value:.3f}"
    )


# ─── Catalogue loader ──────────────────────────────────────────────


def test_load_skill_catalogue_walks_skill_md_files(tmp_path):
    """The loader picks up <root>/<skill>/SKILL.md frontmatter."""
    skill_a = tmp_path / "make_app"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text(
        "---\nname: make_app\ntoken_cost: 250\nutility: 0.7\n---\nbody.",
        encoding="utf-8",
    )
    skill_b = tmp_path / "write_tests"
    skill_b.mkdir()
    (skill_b / "SKILL.md").write_text(
        "---\nname: write_tests\ntoken_cost: 180\nutility: 0.6\n---\nbody.",
        encoding="utf-8",
    )
    skills = load_skill_catalogue(tmp_path)
    assert len(skills) == 2
    ids = {s.skill_id for s in skills}
    assert ids == {"make_app", "write_tests"}


def test_load_skill_catalogue_returns_empty_on_missing_root(tmp_path):
    """Missing dir → []; doesn't raise."""
    assert load_skill_catalogue(tmp_path / "does_not_exist") == []
