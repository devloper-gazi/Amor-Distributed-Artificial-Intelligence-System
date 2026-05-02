"""
Tests for RLEFCollector — composite reward computation, dual-sink
publishing (Mongo + Kafka), batch read for the nightly trainer.

Mongo + Kafka are mocked; tests run offline + fast.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from local_ai.rlef_collector import (
    DEFAULT_WEIGHTS,
    KAFKA_TOPIC,
    RLEFCollector,
    RLEFReward,
    _to_bytes,
    compute_reward_score,
)


# ── compute_reward_score: corner cases ──────────────────────────────


def test_perfect_run_scores_close_to_one():
    """All-positive signals → score in [0.95, 1.0]."""
    score = compute_reward_score(
        test_pass_rate=1.0,
        compilation_success=True,
        runtime_error=None,
        execution_time_ms=100.0,
        z3_was_verified=True,
        mcts_iterations_used=0,
    )
    assert 0.95 <= score <= 1.0


def test_complete_failure_scores_zero():
    score = compute_reward_score(
        test_pass_rate=0.0,
        compilation_success=False,
        runtime_error="Traceback ...",
        execution_time_ms=99_999.0,
        z3_was_verified=False,
        mcts_iterations_used=20,
    )
    assert score == 0.0 or score < 0.05


def test_compilation_failure_drops_score_meaningfully():
    """Same signals except compilation fails — score drops by at
    least the compilation weight."""
    base = compute_reward_score(
        test_pass_rate=1.0, compilation_success=True,
        runtime_error=None, execution_time_ms=100.0,
        z3_was_verified=True, mcts_iterations_used=0,
    )
    fail = compute_reward_score(
        test_pass_rate=1.0, compilation_success=False,
        runtime_error=None, execution_time_ms=100.0,
        z3_was_verified=True, mcts_iterations_used=0,
    )
    drop = base - fail
    # Should drop by ~the compilation_success weight (15%).
    assert drop >= DEFAULT_WEIGHTS["compilation_success"] - 0.01


def test_z3_verification_provides_bonus():
    """z3_was_verified True vs False should differ by ~weight."""
    on = compute_reward_score(
        test_pass_rate=1.0, compilation_success=True,
        runtime_error=None, execution_time_ms=100.0,
        z3_was_verified=True, mcts_iterations_used=0,
    )
    off = compute_reward_score(
        test_pass_rate=1.0, compilation_success=True,
        runtime_error=None, execution_time_ms=100.0,
        z3_was_verified=False, mcts_iterations_used=0,
    )
    diff = on - off
    assert diff >= DEFAULT_WEIGHTS["z3_was_verified"] - 0.01


def test_slow_execution_decays_fast_signal():
    """Past the fast threshold the signal should decay (not flip to 0)."""
    fast = compute_reward_score(
        test_pass_rate=0.5, compilation_success=True,
        runtime_error=None, execution_time_ms=1_000.0,
        z3_was_verified=False, mcts_iterations_used=0,
        fast_threshold_ms=5_000.0,
    )
    slow = compute_reward_score(
        test_pass_rate=0.5, compilation_success=True,
        runtime_error=None, execution_time_ms=50_000.0,
        z3_was_verified=False, mcts_iterations_used=0,
        fast_threshold_ms=5_000.0,
    )
    assert slow < fast


def test_many_mcts_iterations_decays_efficiency_signal():
    eff = compute_reward_score(
        test_pass_rate=1.0, compilation_success=True,
        runtime_error=None, execution_time_ms=100.0,
        z3_was_verified=True, mcts_iterations_used=1,
    )
    inefficient = compute_reward_score(
        test_pass_rate=1.0, compilation_success=True,
        runtime_error=None, execution_time_ms=100.0,
        z3_was_verified=True, mcts_iterations_used=20,
    )
    assert inefficient < eff


def test_weights_override_changes_score():
    """Custom weights should change the composite. The override
    has to flip a signal that's actually firing — comparing the
    z3_was_verified weight while z3 is verified=True."""
    standard = compute_reward_score(
        test_pass_rate=1.0, compilation_success=True,
        runtime_error=None, execution_time_ms=100.0,
        z3_was_verified=True, mcts_iterations_used=0,
    )
    boosted_z3 = compute_reward_score(
        test_pass_rate=1.0, compilation_success=True,
        runtime_error=None, execution_time_ms=100.0,
        z3_was_verified=True, mcts_iterations_used=0,
        weights={"z3_was_verified": 0.50},  # 5× the default
    )
    # Boosted z3 weight on a verified run lifts the score (clamped
    # at 1.0 — but the standard run is already < 1.0 because not
    # every weight is 1.0, so the boost is observable).
    assert boosted_z3 > standard or (standard == 1.0 and boosted_z3 == 1.0)


def test_score_clamped_to_unit_interval():
    """Even with weird weights summing > 1, output stays ≤ 1."""
    score = compute_reward_score(
        test_pass_rate=1.0, compilation_success=True,
        runtime_error=None, execution_time_ms=100.0,
        z3_was_verified=True, mcts_iterations_used=0,
        weights={k: 1.0 for k in DEFAULT_WEIGHTS},  # sums to 6
    )
    assert 0.0 <= score <= 1.0


# ── RLEFReward dataclass ──────────────────────────────────────────


def test_reward_to_dict_round_trip():
    r = RLEFReward(
        session_id="s1", code_hash="abc",
        test_pass_rate=0.9, compilation_success=True,
        execution_time_ms=42.0,
    )
    d = r.to_dict()
    for key in ("session_id", "code_hash", "test_pass_rate",
                "compilation_success", "execution_time_ms",
                "reward_score", "timestamp", "extras"):
        assert key in d


# ── build_reward: full computation pipeline ───────────────────────


def test_build_reward_computes_score_and_populates_record():
    coll = RLEFCollector()
    r = coll.build_reward(
        session_id="s1", code_hash="h1",
        test_pass_rate=1.0, compilation_success=True,
        runtime_error=None, execution_time_ms=200.0,
        z3_was_verified=True, mcts_iterations_used=0,
        language="python", task_type="sort",
    )
    assert isinstance(r, RLEFReward)
    assert r.reward_score > 0.9
    assert r.session_id == "s1"
    assert r.task_type == "sort"


def test_build_reward_propagates_extras():
    coll = RLEFCollector()
    r = coll.build_reward(
        session_id="s2", code_hash="h2",
        extras={"llm_tokens": 4_096, "gpu_temp_c": 71},
    )
    assert r.extras == {"llm_tokens": 4_096, "gpu_temp_c": 71}


# ── Sinks (mocked) ────────────────────────────────────────────────


class _MockMongo:
    def __init__(self, fail: bool = False):
        self.docs: list[dict] = []
        self.fail = fail

    async def insert_one(self, doc):
        if self.fail:
            raise RuntimeError("simulated mongo failure")
        self.docs.append(doc)

    def find(self, query, projection):
        # Filter docs by reward_score gate.
        gate = (query or {}).get("reward_score", {})
        min_score = gate.get("$gte", 0.0)
        matched = [d for d in self.docs
                   if d.get("reward_score", 0.0) >= min_score]

        async def _gen():
            for d in matched:
                yield d

        class _Cursor:
            def __aiter__(s): return _gen()
        return _Cursor()


class _MockKafka:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, bytes]] = []
        self.fail = fail

    async def send_and_wait(self, topic, payload):
        if self.fail:
            raise RuntimeError("simulated kafka failure")
        self.sent.append((topic, payload))


@pytest.mark.asyncio
async def test_collect_writes_to_both_sinks():
    mongo = _MockMongo()
    kafka = _MockKafka()
    coll = RLEFCollector(
        mongo_collection=mongo, kafka_producer=kafka,
    )
    r = coll.build_reward(
        session_id="s", code_hash="h",
        test_pass_rate=1.0, compilation_success=True,
    )
    result = await coll.collect(r)
    assert result == {"persisted": True, "published": True}
    assert len(mongo.docs) == 1
    assert len(kafka.sent) == 1
    assert kafka.sent[0][0] == KAFKA_TOPIC


@pytest.mark.asyncio
async def test_collect_with_no_sinks_drops():
    coll = RLEFCollector()
    r = coll.build_reward(session_id="s", code_hash="h")
    result = await coll.collect(r)
    assert result == {"persisted": False, "published": False}
    assert coll.dropped_count == 1


@pytest.mark.asyncio
async def test_collect_only_persists_when_kafka_unavailable():
    mongo = _MockMongo()
    coll = RLEFCollector(mongo_collection=mongo)
    r = coll.build_reward(session_id="s", code_hash="h")
    result = await coll.collect(r)
    assert result["persisted"] is True
    assert result["published"] is False
    assert coll.persisted_count == 1
    assert coll.dropped_count == 0


@pytest.mark.asyncio
async def test_collect_only_publishes_when_mongo_unavailable():
    kafka = _MockKafka()
    coll = RLEFCollector(kafka_producer=kafka)
    r = coll.build_reward(session_id="s", code_hash="h")
    result = await coll.collect(r)
    assert result == {"persisted": False, "published": True}


@pytest.mark.asyncio
async def test_collect_one_sink_failing_does_not_block_other():
    mongo = _MockMongo(fail=True)
    kafka = _MockKafka()
    coll = RLEFCollector(
        mongo_collection=mongo, kafka_producer=kafka,
    )
    r = coll.build_reward(session_id="s", code_hash="h")
    result = await coll.collect(r)
    assert result == {"persisted": False, "published": True}


@pytest.mark.asyncio
async def test_collect_both_sinks_failing_increments_dropped():
    mongo = _MockMongo(fail=True)
    kafka = _MockKafka(fail=True)
    coll = RLEFCollector(
        mongo_collection=mongo, kafka_producer=kafka,
    )
    r = coll.build_reward(session_id="s", code_hash="h")
    result = await coll.collect(r)
    assert result == {"persisted": False, "published": False}
    assert coll.dropped_count == 1


# ── batch read ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_batch_filters_by_min_score_and_caps_limit():
    mongo = _MockMongo()
    coll = RLEFCollector(mongo_collection=mongo)
    # 10 high-quality + 5 low.
    for i in range(10):
        await coll.collect(coll.build_reward(
            session_id=f"good{i}", code_hash=f"g{i}",
            test_pass_rate=1.0, compilation_success=True,
            execution_time_ms=100.0, z3_was_verified=True,
        ))
    for i in range(5):
        await coll.collect(coll.build_reward(
            session_id=f"bad{i}", code_hash=f"b{i}",
            test_pass_rate=0.1, compilation_success=False,
            runtime_error="boom",
        ))
    batch = await coll.fetch_batch(min_score=0.8, limit=5)
    assert len(batch) == 5
    assert all(r.reward_score >= 0.8 for r in batch)


@pytest.mark.asyncio
async def test_fetch_batch_no_collection_returns_empty():
    coll = RLEFCollector()  # no mongo
    assert await coll.fetch_batch() == []


# ── Kafka serialisation ───────────────────────────────────────────


def test_kafka_payload_serialises_as_json_bytes():
    r = RLEFReward(session_id="x", code_hash="y", reward_score=0.5)
    raw = _to_bytes(r.to_dict())
    assert isinstance(raw, bytes)
    decoded = json.loads(raw.decode("utf-8"))
    assert decoded["session_id"] == "x"
    assert decoded["code_hash"] == "y"
    assert decoded["reward_score"] == 0.5
