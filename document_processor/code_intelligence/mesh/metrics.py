"""
Self-evolution metrics for the Multi-ML Mesh.

Every mesh run emits one document per role describing what each
specialist contributed and how the downstream pipeline judged it.
The collection looks like::

    {
      "session_id": "...",
      "ts": "2026-04-29T16:34:00+00:00",
      "engine": "quick_code",
      "role": "math",                       # or "performance" / "edge_case" / "general"
      "phase": "reason" | "audit" | "arbiter",
      "model_tag": "qwen2.5:7b",            # whichever the router picked
      "had_error": false,
      "alt_count": 3,                        # for reason phase
      "was_chosen": true,                    # this role's pick survived aggregation
      "verdict": "approve",                  # auditor / arbiter only
      "confidence": 0.82,                    # auditor / arbiter only
      "verification_passed": true,           # whole-session signal
      "arbiter_verdict": "approve",          # whole-session signal
      "arbiter_confidence": 0.91,
      "production_readiness": 92.0,
    }

A future learner reads this collection and weights ensemble votes
toward roles that historically produced cleaner code (high arbiter
confidence + verification passed). In this round we only *write* the
collection — the read-side learner is a follow-up.

Failure-quiet: if Mongo is unreachable the recorder logs a debug
message and drops the metric. The mesh never blocks on metrics.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


COLLECTION_NAME = "mesh_metrics"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MetricRow:
    """One row written to the mesh_metrics collection."""

    session_id: str
    role: str
    phase: str
    engine: str = "quick_code"
    ts: str = field(default_factory=_now_iso)
    model_tag: str | None = None
    had_error: bool = False
    alt_count: int | None = None
    was_chosen: bool | None = None
    verdict: str | None = None
    confidence: float | None = None
    verification_passed: bool | None = None
    arbiter_verdict: str | None = None
    arbiter_confidence: float | None = None
    production_readiness: float | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


class MeshMetricsRecorder:
    """Thin async wrapper around the Mongo collection.

    Pass an explicit ``mongo_client`` (motor.motor_asyncio.AsyncIOMotorClient)
    or let the recorder discover the configured app client via the
    standard service helper. The recorder NEVER blocks the pipeline:
    failure to write is logged at debug and silently swallowed.
    """

    def __init__(
        self,
        *,
        mongo_client: Any | None = None,
        database: str = "documents",
        collection: str = COLLECTION_NAME,
    ) -> None:
        self._client = mongo_client
        self._database = database
        self._collection = collection

    async def _get_collection(self) -> Any | None:
        client = self._client
        if client is None:
            try:
                # Late import + best-effort discovery via the
                # StorageManager singleton on the FastAPI app state.
                # Failure is silent — metrics never block the pipeline.
                from ...infrastructure.storage import storage_manager  # noqa: PLC0415
                client = getattr(storage_manager, "mongo_client", None)
            except Exception:  # pragma: no cover
                client = None
        if client is None:
            return None
        try:
            return client[self._database][self._collection]
        except Exception as exc:  # pragma: no cover
            logger.debug("mesh_metrics_collection_lookup_failed: %s", exc)
            return None

    async def write_many(self, rows: list[MetricRow]) -> int:
        """Insert a batch. Returns the count actually written, 0 on
        any failure path (Mongo offline, etc.)."""
        if not rows:
            return 0
        coll = await self._get_collection()
        if coll is None:
            logger.debug(
                "mesh_metrics_skipped: no Mongo client (rows=%d)", len(rows),
            )
            return 0
        docs = [r.to_dict() for r in rows]
        try:
            res = await coll.insert_many(docs, ordered=False)
            return len(res.inserted_ids)
        except Exception as exc:
            logger.debug("mesh_metrics_insert_failed: %s", exc)
            return 0


async def record_session_metrics(
    *,
    session_id: str,
    engine: str,
    aggregated_per_specialist_picks: dict[str, str],
    aggregated_specialist_errors: dict[str, str],
    aggregated_specialist_alt_counts: dict[str, int],
    chosen_label: str,
    audit_outputs: list[dict[str, Any]] | None,
    arbiter_verdict: str | None,
    arbiter_confidence: float | None,
    production_readiness: float | None,
    verification_passed: bool | None,
    role_models: dict[str, str] | None = None,
    recorder: MeshMetricsRecorder | None = None,
) -> int:
    """Build + write a per-session batch of metric rows.

    Each row carries one role's contribution + the whole-session
    outcome (so a later analyser can join role to outcome without
    needing a second query).

    Returns the number of rows actually inserted (0 if Mongo is offline
    or the recorder couldn't be obtained).
    """
    rec = recorder or MeshMetricsRecorder()
    role_models = role_models or {}
    rows: list[MetricRow] = []

    # Reason phase rows — one per specialist that contributed.
    seen_roles = set(aggregated_specialist_alt_counts.keys()) | set(aggregated_specialist_errors.keys())
    for role in seen_roles:
        was_chosen = bool(
            aggregated_per_specialist_picks.get(role) == chosen_label
        )
        rows.append(MetricRow(
            session_id=session_id,
            engine=engine,
            role=role,
            phase="reason",
            model_tag=role_models.get(role),
            had_error=role in aggregated_specialist_errors,
            alt_count=aggregated_specialist_alt_counts.get(role),
            was_chosen=was_chosen,
            verification_passed=verification_passed,
            arbiter_verdict=arbiter_verdict,
            arbiter_confidence=arbiter_confidence,
            production_readiness=production_readiness,
            notes=(
                aggregated_specialist_errors.get(role) or None
            ),
        ))

    # Audit phase rows.
    for audit in (audit_outputs or []):
        if not isinstance(audit, dict):
            continue
        role = str(audit.get("role") or "unknown")
        rows.append(MetricRow(
            session_id=session_id,
            engine=engine,
            role=role,
            phase="audit",
            model_tag=role_models.get(role),
            had_error=bool(audit.get("error")),
            verdict=audit.get("verdict"),
            confidence=audit.get("confidence"),
            verification_passed=verification_passed,
            arbiter_verdict=arbiter_verdict,
            arbiter_confidence=arbiter_confidence,
            production_readiness=production_readiness,
        ))

    # Arbiter row.
    if arbiter_verdict is not None:
        rows.append(MetricRow(
            session_id=session_id,
            engine=engine,
            role="meta_arbiter",
            phase="arbiter",
            model_tag=role_models.get("meta_arbiter"),
            had_error=False,
            verdict=arbiter_verdict,
            confidence=arbiter_confidence,
            verification_passed=verification_passed,
            arbiter_verdict=arbiter_verdict,
            arbiter_confidence=arbiter_confidence,
            production_readiness=production_readiness,
        ))

    return await rec.write_many(rows)
