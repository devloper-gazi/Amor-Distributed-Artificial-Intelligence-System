"""
RLEFCollector — Reinforcement Learning from Execution Feedback.

Every sandbox execution emits a `RLEFReward` capturing:

  * test pass rate
  * compilation success
  * runtime errors (if any)
  * execution wall time
  * whether Z3 verified the skeleton (boost)
  * whether the run came out of an MCTS / tournament (penalty for
    high-variance paths the engine had to explore through)

The composite ``reward_score`` ∈ [0, 1] becomes the training signal
for nightly LoRA fine-tuning. We persist every reward to MongoDB
(``rlef_rewards`` collection) AND publish to a Kafka topic so a
trainer microservice can consume it without polling Mongo.

Both sinks are fail-soft: a missing Mongo / Kafka client just drops
the reward (logged at debug). The engine layer must NEVER block on
a reward write.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


KAFKA_TOPIC = "task.rlef_reward"
DEFAULT_COLLECTION = "rlef_rewards"


# Composite-score weights. Tuned empirically:
#   correctness dominates (40%); wall time is a soft penalty (10%);
#   formal verification + bounded search win small bonuses.
DEFAULT_WEIGHTS: dict[str, float] = {
    "test_pass_rate":        0.40,
    "compilation_success":   0.15,
    "no_runtime_error":      0.15,
    "fast_enough":           0.10,
    "z3_was_verified":       0.10,
    "search_was_efficient":  0.10,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Reward dataclass ───────────────────────────────────────────────


@dataclass
class RLEFReward:
    """One sandbox execution's full reward record."""

    session_id: str
    code_hash: str
    test_pass_rate: float = 0.0
    compilation_success: bool = False
    runtime_error: str | None = None
    execution_time_ms: float = 0.0
    z3_was_verified: bool = False
    mcts_iterations_used: int = 0
    timestamp: str = field(default_factory=_now_iso)
    language: str = "python"
    task_type: str = ""
    # Composite computed by `_compute_reward`. Persisted alongside
    # the raw signals so a future learner can re-weight without
    # re-collecting.
    reward_score: float = 0.0
    # Free-form bag for future signals (LLM token cost, GPU temp …).
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── Score calculator ──────────────────────────────────────────────


def compute_reward_score(
    *,
    test_pass_rate: float,
    compilation_success: bool,
    runtime_error: str | None,
    execution_time_ms: float,
    z3_was_verified: bool,
    mcts_iterations_used: int,
    fast_threshold_ms: float = 5_000.0,
    efficient_iter_threshold: int = 3,
    weights: dict[str, float] | None = None,
) -> float:
    """Pure function — same inputs always produce the same score.

    Returns a value in [0, 1]. Intentionally NOT clipped to [0, 1] in
    intermediate math so a future weight override can stretch the
    range; the final clamp is applied at the very end.
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    pass_signal = max(0.0, min(1.0, float(test_pass_rate)))
    compile_signal = 1.0 if compilation_success else 0.0
    no_error_signal = 1.0 if not runtime_error else 0.0
    fast_signal = 1.0 if execution_time_ms <= fast_threshold_ms else (
        max(0.0, fast_threshold_ms / max(1.0, execution_time_ms))
    )
    z3_signal = 1.0 if z3_was_verified else 0.0
    # MCTS signal: 1.0 when no MCTS was needed (efficient path), drops
    # as iterations grow. 0.0 by `mcts_iterations_used == 2 * threshold`.
    if mcts_iterations_used <= 0:
        mcts_signal = 1.0
    else:
        ratio = float(mcts_iterations_used) / max(1, efficient_iter_threshold)
        mcts_signal = max(0.0, 1.0 - 0.5 * (ratio - 1.0))
        mcts_signal = max(0.0, min(1.0, mcts_signal))

    score = (
        w.get("test_pass_rate", 0)       * pass_signal +
        w.get("compilation_success", 0)  * compile_signal +
        w.get("no_runtime_error", 0)     * no_error_signal +
        w.get("fast_enough", 0)          * fast_signal +
        w.get("z3_was_verified", 0)      * z3_signal +
        w.get("search_was_efficient", 0) * mcts_signal
    )
    return max(0.0, min(1.0, round(score, 4)))


# ─── Collector ──────────────────────────────────────────────────────


class RLEFCollector:
    """Async wrapper around the dual sink (Mongo + Kafka).

    Both sinks are optional — pass ``None`` to disable either. The
    typical production wiring:

        collector = RLEFCollector(
            mongo_collection=storage_manager.mongo_db.rlef_rewards,
            kafka_producer=kafka_producer,
        )
    """

    def __init__(
        self,
        *,
        mongo_collection: Any | None = None,
        kafka_producer: Any | None = None,
        kafka_topic: str = KAFKA_TOPIC,
        weights: dict[str, float] | None = None,
        fast_threshold_ms: float = 5_000.0,
        efficient_iter_threshold: int = 3,
    ) -> None:
        self._collection = mongo_collection
        self._producer = kafka_producer
        self._topic = kafka_topic
        self._weights = weights
        self._fast_threshold = float(fast_threshold_ms)
        self._efficient_iter = int(efficient_iter_threshold)
        # Stats so monitors / tests can confirm rewards landed.
        self.published_count = 0
        self.persisted_count = 0
        self.dropped_count = 0

    def build_reward(
        self,
        *,
        session_id: str,
        code_hash: str,
        test_pass_rate: float = 0.0,
        compilation_success: bool = False,
        runtime_error: str | None = None,
        execution_time_ms: float = 0.0,
        z3_was_verified: bool = False,
        mcts_iterations_used: int = 0,
        language: str = "python",
        task_type: str = "",
        extras: dict[str, Any] | None = None,
    ) -> RLEFReward:
        """Compute the composite score AND wrap into an RLEFReward.

        Caller passes raw signals; the collector handles the math so
        every sink sees a fully-populated record.
        """
        score = compute_reward_score(
            test_pass_rate=test_pass_rate,
            compilation_success=compilation_success,
            runtime_error=runtime_error,
            execution_time_ms=execution_time_ms,
            z3_was_verified=z3_was_verified,
            mcts_iterations_used=mcts_iterations_used,
            fast_threshold_ms=self._fast_threshold,
            efficient_iter_threshold=self._efficient_iter,
            weights=self._weights,
        )
        return RLEFReward(
            session_id=session_id,
            code_hash=code_hash,
            test_pass_rate=float(test_pass_rate),
            compilation_success=bool(compilation_success),
            runtime_error=runtime_error,
            execution_time_ms=float(execution_time_ms),
            z3_was_verified=bool(z3_was_verified),
            mcts_iterations_used=int(mcts_iterations_used),
            language=language or "python",
            task_type=task_type or "",
            reward_score=score,
            extras=dict(extras or {}),
        )

    # ── sinks ─────────────────────────────────────────────────────

    async def collect(self, reward: RLEFReward) -> dict[str, bool]:
        """Send a reward to both sinks. Returns
        ``{"persisted": bool, "published": bool}`` so callers can
        log the path that took.

        Each sink is independently fail-soft: one failing won't stop
        the other.
        """
        persisted = await self._persist(reward)
        published = await self._publish(reward)
        if not (persisted or published):
            self.dropped_count += 1
        return {"persisted": persisted, "published": published}

    async def _persist(self, reward: RLEFReward) -> bool:
        if self._collection is None:
            return False
        try:
            await self._collection.insert_one(reward.to_dict())
            self.persisted_count += 1
            return True
        except Exception as exc:
            logger.debug("rlef_mongo_persist_failed: %s", exc)
            return False

    async def _publish(self, reward: RLEFReward) -> bool:
        if self._producer is None:
            return False
        try:
            payload = reward.to_dict()
            send = self._producer.send_and_wait
            await send(self._topic, _to_bytes(payload))
            self.published_count += 1
            return True
        except Exception as exc:
            logger.debug("rlef_kafka_publish_failed: %s", exc)
            return False

    # ── batch read (for nightly trainer) ──────────────────────────

    async def fetch_batch(
        self,
        *,
        min_score: float = 0.8,
        limit: int = 1_000,
    ) -> list[RLEFReward]:
        """Pull a high-quality batch for the nightly LoRA trainer.

        Filters by `reward_score >= min_score`. Returns at most
        `limit` records. Empty list if Mongo is unavailable.
        """
        if self._collection is None:
            return []
        out: list[RLEFReward] = []
        try:
            cursor = self._collection.find(
                {"reward_score": {"$gte": float(min_score)}},
                {"_id": 0},
            )
            count = 0
            async for doc in cursor:
                if not isinstance(doc, dict):
                    continue
                try:
                    out.append(RLEFReward(**doc))
                except (TypeError, ValueError):
                    continue
                count += 1
                if count >= limit:
                    break
        except Exception as exc:
            logger.debug("rlef_fetch_batch_failed: %s", exc)
        return out


# ─── helpers ────────────────────────────────────────────────────────


def _to_bytes(payload: dict[str, Any]) -> bytes:
    """Kafka producer wants bytes; canonicalise via JSON."""
    import json
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
