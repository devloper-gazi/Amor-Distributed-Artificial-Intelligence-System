"""Cycle H Phase A.3 — verifier-reward signal coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.training import verifier_rewards as vr


# ─── Weights table sanity ──────────────────────────────────────────


def test_weights_sum_to_one():
    """If anyone tweaks WEIGHTS, the sum check at import time
    raises; this test pins the assertion explicitly."""
    assert abs(sum(vr.WEIGHTS.values()) - 1.0) < 1e-9


def test_weights_version_pinned():
    """Bumping WEIGHTS_VERSION is the contract for retiring old GRPO
    checkpoints.  Test pins current value; future bump → bump here too."""
    assert vr.WEIGHTS_VERSION == "v1.0"


# ─── Single-slot score functions ───────────────────────────────────


def test_execution_pass_scores_1():
    obs = vr.VerifierObservation(execution_exit_code=0)
    assert vr._slot_score_execution(obs) == 1.0


def test_execution_fail_scores_0():
    obs = vr.VerifierObservation(execution_exit_code=2)
    assert vr._slot_score_execution(obs) == 0.0


def test_execution_timeout_scores_0():
    obs = vr.VerifierObservation(execution_exit_code=0, execution_timed_out=True)
    assert vr._slot_score_execution(obs) == 0.0


def test_execution_skipped_scores_neutral_0_5():
    """Non-codegen tasks (docs/explanation) have no execution phase →
    neutral 0.5, not punished."""
    obs = vr.VerifierObservation()
    assert vr._slot_score_execution(obs) == 0.5


def test_test_pass_scores_1():
    obs = vr.VerifierObservation(test_exit_code=0)
    assert vr._slot_score_test(obs) == 1.0


def test_test_skipped_scores_neutral():
    obs = vr.VerifierObservation(test_exit_code=0, test_skipped=True)
    assert vr._slot_score_test(obs) == 0.5


def test_static_clean_scores_1():
    obs = vr.VerifierObservation(static_error_count=0, static_warning_count=0)
    assert vr._slot_score_static(obs) == 1.0


def test_static_one_error_subtracts_0_1():
    obs = vr.VerifierObservation(static_error_count=1)
    assert vr._slot_score_static(obs) == 0.9


def test_static_many_errors_floor_at_0():
    obs = vr.VerifierObservation(static_error_count=20)
    assert vr._slot_score_static(obs) == 0.0


def test_static_warning_counts_as_fifth_of_error():
    obs = vr.VerifierObservation(static_error_count=0, static_warning_count=5)
    # 5 warnings = 1.0 err-unit → 0.9
    assert abs(vr._slot_score_static(obs) - 0.9) < 0.001


def test_critic_maps_to_unit_interval():
    assert vr._slot_score_critic(vr.VerifierObservation(critic_score=85.0)) == 0.85
    assert vr._slot_score_critic(vr.VerifierObservation(critic_score=0)) == 0.0
    assert vr._slot_score_critic(vr.VerifierObservation(critic_score=100)) == 1.0


def test_critic_missing_scores_neutral():
    assert vr._slot_score_critic(vr.VerifierObservation()) == 0.5


# ─── Bonus + penalty ───────────────────────────────────────────────


def test_bonus_property_tests():
    obs = vr.VerifierObservation(property_tests_present=True)
    assert vr._bonus(obs) > 0


def test_bonus_branch_coverage_above_80():
    obs = vr.VerifierObservation(branch_coverage_ratio=0.95)
    # 0.05 * (0.95 - 0.80) = 0.0075
    assert vr._bonus(obs) > 0.005


def test_bonus_branch_coverage_at_or_below_80_no_bonus():
    obs = vr.VerifierObservation(branch_coverage_ratio=0.80)
    assert vr._bonus(obs) == 0


def test_bonus_mutation_score():
    obs = vr.VerifierObservation(mutation_score=0.5)
    # 0.05 * 0.5 = 0.025
    assert abs(vr._bonus(obs) - 0.025) < 0.001


def test_bonus_capped_at_010():
    """Stack every bonus — capped at BONUS_CAP (0.10).  Sum of
    components at maximum: 0.03 + 0.05 * (1.0 - 0.80) + 0.05 = 0.09.
    Cap is defensive — anyone bumping individual weights past their
    current values will hit the cap.  Test asserts BOTH that the
    bonus reaches the design ceiling AND that the cap functions."""
    obs = vr.VerifierObservation(
        property_tests_present=True,
        branch_coverage_ratio=1.0,
        mutation_score=1.0,
    )
    bonus = vr._bonus(obs)
    assert bonus <= 0.10
    # At today's weights, max-stacked bonus is 0.09 (3 + 1 + 5 cents)
    assert abs(bonus - 0.09) < 0.001


def test_penalty_surviving_mutants():
    obs = vr.VerifierObservation(surviving_mutants=5)
    # 0.05 * min(5/10, 1.0) = 0.025
    assert abs(vr._penalty(obs) - 0.025) < 0.001


def test_penalty_capped():
    obs = vr.VerifierObservation(surviving_mutants=100, missed_branches=20)
    # capped at PENALTY_CAP=0.15
    assert vr._penalty(obs) <= 0.15


# ─── compute_reward_scalar end-to-end ──────────────────────────────


def test_perfect_pass_scores_near_one():
    """Every signal optimal."""
    obs = vr.VerifierObservation(
        execution_exit_code=0,
        test_exit_code=0,
        static_error_count=0,
        critic_score=95.0,
        property_tests_present=True,
        branch_coverage_ratio=0.95,
        mutation_score=0.9,
    )
    reward = vr.compute_reward_scalar(obs)
    assert reward >= 0.95


def test_hard_fail_scores_low():
    """All hard signals off / fail."""
    obs = vr.VerifierObservation(
        execution_exit_code=1,
        execution_timed_out=True,
        test_exit_code=1,
        static_error_count=5,
        critic_score=10.0,
    )
    reward = vr.compute_reward_scalar(obs)
    assert reward < 0.15


def test_neutral_unscored_session_in_mid_range():
    """Non-codegen session with no execution/test/critic info →
    neutral middle reward, NOT zero."""
    obs = vr.VerifierObservation()
    reward = vr.compute_reward_scalar(obs)
    # All neutrals: 0.35*0.5 + 0.25*0.5 + 0.15*1.0 + 0.25*0.5
    #             = 0.175 + 0.125 + 0.15 + 0.125 = 0.575
    assert 0.55 <= reward <= 0.60


def test_reward_bounded_in_unit_interval():
    """Reward must always be in [0, 1] regardless of inputs."""
    extreme_neg = vr.VerifierObservation(
        execution_exit_code=1, execution_timed_out=True,
        test_exit_code=1, static_error_count=100, critic_score=0,
        surviving_mutants=100, missed_branches=20,
    )
    extreme_pos = vr.VerifierObservation(
        execution_exit_code=0, test_exit_code=0,
        static_error_count=0, critic_score=100,
        property_tests_present=True, branch_coverage_ratio=1.0,
        mutation_score=1.0,
    )
    r_neg = vr.compute_reward_scalar(extreme_neg)
    r_pos = vr.compute_reward_scalar(extreme_pos)
    assert 0.0 <= r_neg <= 1.0
    assert 0.0 <= r_pos <= 1.0
    assert r_neg < r_pos


# ─── Breakdown shape ───────────────────────────────────────────────


def test_breakdown_keys_and_sum():
    obs = vr.VerifierObservation(
        execution_exit_code=0, test_exit_code=0,
        static_error_count=1, critic_score=80,
    )
    bd = vr.compute_reward_breakdown(obs)
    for key in ("execution", "test", "static", "critic",
                "bonus", "penalty", "total", "weights_version"):
        assert key in bd
    # Sum check: execution + test + static + critic + bonus + penalty = total
    parts = bd["execution"] + bd["test"] + bd["static"] + bd["critic"] \
            + bd["bonus"] + bd["penalty"]
    assert abs(parts - bd["total"]) < 0.01


# ─── VerifierObservation.from_session ──────────────────────────────


def test_from_session_handles_full_engine_snapshot():
    """Engine.snapshot() output shape — verifies the mapping picks
    up every signal."""
    session = {
        "execution_results": [
            {"exit_code": 0, "timed_out": False, "stdout": "ok", "stderr": ""},
        ],
        "test_execution_result": {"exit_code": 0, "skipped": False},
        "static_analysis": {
            "severity_counts": {"error": 0, "warning": 2, "info": 1},
        },
        "review": {"score": 88, "verdict": "approved"},
        "coverage_report": {
            "branch_coverage_ratio": 0.92,
            "missed_branches": [],
        },
        "mutation_result": {"score": 0.75, "survived": 2, "total": 8},
        "test_metadata": {"property_tests_present": True},
    }
    obs = vr.VerifierObservation.from_session(session)
    assert obs.execution_exit_code == 0
    assert obs.test_exit_code == 0
    assert obs.static_warning_count == 2
    assert obs.critic_score == 88
    assert obs.branch_coverage_ratio == 0.92
    assert obs.mutation_score == 0.75
    assert obs.surviving_mutants == 2
    assert obs.property_tests_present is True


def test_from_session_handles_empty_snapshot():
    """Tasks that skip phases (docs/explanation) have empty session
    dicts — must not raise."""
    obs = vr.VerifierObservation.from_session({})
    assert obs.execution_exit_code is None
    assert obs.critic_score is None
    assert vr.compute_reward_scalar(obs) > 0.4    # neutral mid-range


def test_from_session_tolerates_missing_test_metadata():
    """Some sessions populate breakdown.property_tests instead of
    test_metadata.property_tests_present — handle both shapes."""
    session = {
        "score": {
            "breakdown": {"property_tests": True}
        },
        "review": {"score": 75},
    }
    obs = vr.VerifierObservation.from_session(session)
    assert obs.property_tests_present is True


# ─── Pair annotation ───────────────────────────────────────────────


def test_annotate_pair_basic():
    pair = {"prompt": "p", "chosen": "c", "rejected": "r"}
    chosen_obs = vr.VerifierObservation(execution_exit_code=0, critic_score=85)
    rejected_obs = vr.VerifierObservation(execution_exit_code=1, critic_score=40)
    annotated = vr.annotate_pair_with_rewards(pair, chosen_obs, rejected_obs)
    assert "reward_chosen" in annotated
    assert "reward_rejected" in annotated
    # Chosen should score higher than rejected (the whole point)
    assert annotated["reward_chosen"] > annotated["reward_rejected"]
    # Original fields untouched
    assert annotated["prompt"] == "p"
    assert annotated["chosen"] == "c"
    assert annotated["rejected"] == "r"


def test_annotate_pair_skips_when_observations_none():
    """When the harness has no verifier data, leave the pair
    un-annotated (ORPO can still consume it)."""
    pair = {"prompt": "p", "chosen": "c", "rejected": "r"}
    out = vr.annotate_pair_with_rewards(pair, None, None)
    assert "reward_chosen" not in out
    assert "reward_rejected" not in out


# ─── JSONL batch annotation ────────────────────────────────────────


def test_annotate_jsonl_file_passthrough_when_no_observations(tmp_path):
    """No observations → JSONL rows pass through unchanged but the
    file is still rewritten (so the orpo_weekly_cron pipeline always
    has a valid output)."""
    src = tmp_path / "in.jsonl"
    dst = tmp_path / "out.jsonl"
    src.write_text(
        '{"id": "p1", "prompt": "a", "chosen": "b", "rejected": "c"}\n'
        '{"id": "p2", "prompt": "x", "chosen": "y", "rejected": "z"}\n',
        encoding="utf-8",
    )
    stats = vr.annotate_jsonl_file(src, dst)
    assert stats["rows_total"] == 2
    assert stats["rows_annotated_chosen"] == 0
    assert stats["rows_annotated_rejected"] == 0
    rows = [json.loads(l) for l in dst.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert "reward_chosen" not in rows[0]


def test_annotate_jsonl_file_with_observation_lookup(tmp_path):
    """Observations keyed `<id>:chosen` and `<id>:rejected` get
    looked up + annotated."""
    src = tmp_path / "in.jsonl"
    dst = tmp_path / "out.jsonl"
    src.write_text(
        '{"id": "p1", "prompt": "a", "chosen": "b", "rejected": "c"}\n',
        encoding="utf-8",
    )
    lookup = {
        "p1:chosen": vr.VerifierObservation(execution_exit_code=0, critic_score=90),
        "p1:rejected": vr.VerifierObservation(execution_exit_code=1, critic_score=30),
    }
    stats = vr.annotate_jsonl_file(src, dst, observation_lookup=lookup)
    assert stats["rows_total"] == 1
    assert stats["rows_annotated_chosen"] == 1
    assert stats["rows_annotated_rejected"] == 1
    row = json.loads(dst.read_text(encoding="utf-8").splitlines()[0])
    assert "reward_chosen" in row
    assert row["reward_chosen"] > row["reward_rejected"]


def test_annotate_jsonl_file_skips_malformed_lines(tmp_path):
    src = tmp_path / "in.jsonl"
    dst = tmp_path / "out.jsonl"
    src.write_text(
        '{"id": "p1", "prompt": "a", "chosen": "b", "rejected": "c"}\n'
        'not-json\n'
        '   \n'
        '{"id": "p2", "prompt": "x", "chosen": "y", "rejected": "z"}\n',
        encoding="utf-8",
    )
    stats = vr.annotate_jsonl_file(src, dst)
    assert stats["rows_total"] == 2  # malformed + empty skipped
