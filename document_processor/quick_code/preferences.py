"""
QuickCode V2 — ORPO preference-pair exporter.

When the engine refines a failing piece of code into a passing one
we have everything an ORPO trainer needs:

* the original prompt (chosen + rejected share it),
* a *rejected* candidate (the failing version),
* a *chosen* candidate (the patch that finally passed),
* a *reward delta* derived from sandbox / reactor / RLEF telemetry.

This module turns those triples into ``PreferencePair`` rows and
sinks them into MongoDB (and, optionally, Kafka).  Built on top of
the existing ``RLEFCollector`` Mongo connection so we don't open a
second client just for preferences.

Sink semantics
--------------

* When ``enabled=False`` ``export()`` is a no-op that returns an
  empty list.  Keeps the flag-matrix tests trivial.
* When ``mongo_collection`` is set but no Mongo client is configured
  the call still returns the constructed ``PreferencePair`` list —
  the caller can decide whether to retry later.
* Failures during persistence are swallowed (logged at DEBUG).  We
  never block the engine on training-data plumbing.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .contracts import PreferencePair

logger = logging.getLogger(__name__)


class ORPOExporter:
    """Convert (rejected, chosen) pairs into ``PreferencePair`` rows."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        mongo_collection: str = "orpo_pairs",
        mongo_db: Any | None = None,
        kafka_topic: str | None = None,
        kafka_producer: Any | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._mongo_collection = str(mongo_collection or "orpo_pairs")
        self._mongo_db = mongo_db
        self._kafka_topic = kafka_topic or ""
        self._kafka_producer = kafka_producer

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ─── Public API ─────────────────────────────────────────────────

    async def export(
        self,
        *,
        prompt: str,
        rejected: str,
        chosen: str,
        reward_chosen: float = 1.0,
        reward_rejected: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> list[PreferencePair]:
        """Build + persist a single preference pair.  Returns the
        list (always size 0 or 1) so callers can audit what was
        recorded."""
        if not self._enabled:
            return []
        if not (prompt or "").strip() or not chosen or not rejected:
            return []
        if chosen.strip() == rejected.strip():
            return []  # nothing to learn from

        pair = PreferencePair(
            prompt=prompt,
            chosen=chosen,
            rejected=rejected,
            reward_delta=float(reward_chosen) - float(reward_rejected),
            metadata={
                "ts": time.time(),
                "reward_chosen": float(reward_chosen),
                "reward_rejected": float(reward_rejected),
                **(metadata or {}),
            },
        )

        await self._persist_mongo(pair)
        await self._publish_kafka(pair)
        return [pair]

    async def export_from_bundle(
        self,
        bundle: Any,
        *,
        prompt: str,
        rejected: str,
        chosen: str,
    ) -> list[PreferencePair]:
        """Convenience helper that pulls reward + metadata from a
        bundle dict produced by ``QuickCodeBundle.to_dict()``."""
        meta = {}
        chosen_reward = 1.0
        rejected_reward = 0.0
        if isinstance(bundle, dict):
            rlef = bundle.get("rlef_reward") or {}
            if isinstance(rlef, dict):
                chosen_reward = float(rlef.get("composite") or 1.0)
                meta["rlef"] = rlef.get("breakdown") or {}
            verification = bundle.get("verification") or {}
            if isinstance(verification, dict):
                meta["verification_score"] = verification.get("score")
            meta["session_id"] = bundle.get("session_id")
        return await self.export(
            prompt=prompt,
            rejected=rejected,
            chosen=chosen,
            reward_chosen=chosen_reward,
            reward_rejected=rejected_reward,
            metadata=meta,
        )

    # ─── Sinks ──────────────────────────────────────────────────────

    async def _persist_mongo(self, pair: PreferencePair) -> None:
        if self._mongo_db is None:
            return
        try:
            collection = self._mongo_db[self._mongo_collection]
            doc = pair.model_dump()
            res = collection.insert_one(doc)
            if hasattr(res, "__await__"):
                await res  # type: ignore[func-returns-value]
        except Exception as exc:  # pragma: no cover - infra
            logger.debug("orpo mongo persist failed: %s", exc)

    async def _publish_kafka(self, pair: PreferencePair) -> None:
        if not self._kafka_topic or self._kafka_producer is None:
            return
        try:
            payload = pair.model_dump_json().encode("utf-8")
            send = self._kafka_producer.send(self._kafka_topic, value=payload)
            if hasattr(send, "__await__"):
                await send  # type: ignore[func-returns-value]
        except Exception as exc:  # pragma: no cover - infra
            logger.debug("orpo kafka publish failed: %s", exc)


__all__ = ["ORPOExporter"]
