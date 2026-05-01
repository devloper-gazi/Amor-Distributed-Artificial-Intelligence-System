"""
SpecialistBandit — Thompson-sampling read-side learner over the
``mesh_metrics`` collection. Re-weights mesh specialists at reasoning
time toward roles that historically produced cleaner code.

α/β per (role, task_type):
  α = 1 + Σ(was_chosen ∧ verification_passed ∧ arbiter_verdict=="approve")
  β = 1 + Σ(was_chosen ∧ (¬verification_passed ∨ arbiter_verdict=="reject"))

Specialists NOT chosen contribute 0 — only "in-the-arena" data counts.

Cold-start: total observations < N → uniform weights.

Temperature: α' = 1 + (α-1)/T  (T>1 → flatter, more exploration).

Failure: Mongo offline → uniform weights, no error.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)


@dataclass
class BanditPosterior:
    alpha: float = 1.0
    beta: float = 1.0
    observations: int = 0

    def sample(self, rng: random.Random) -> float:
        """One Thompson draw."""
        return rng.betavariate(self.alpha, self.beta)


class SpecialistBandit:
    """Per-(role, task_type) Beta posterior + Thompson sampler."""

    def __init__(
        self,
        *,
        cold_start_threshold: int = 5,
        temperature: float = 1.0,
        rng: random.Random | None = None,
    ) -> None:
        self._cold_start = max(1, int(cold_start_threshold))
        self._temperature = max(0.05, float(temperature))
        self._rng = rng or random.Random()
        # Map (role, task_type) → BanditPosterior.
        self._posteriors: dict[tuple[str, str], BanditPosterior] = {}
        self._loaded_at_ts: float = 0.0

    @property
    def posteriors(self) -> dict[tuple[str, str], BanditPosterior]:
        return self._posteriors

    async def update_from_collection(self, collection: Any) -> int:
        """Rebuild posteriors from the mesh_metrics collection.
        Tolerates Mongo offline + non-collection objects (test shims).

        Tests can pass a list of dicts to bypass Mongo entirely.
        """
        rows: list[dict[str, Any]] = []
        try:
            if collection is None:
                return 0
            if isinstance(collection, list):
                rows = list(collection)
            elif hasattr(collection, "find"):
                cursor = collection.find({})
                async for doc in cursor:  # type: ignore[attr-defined]
                    rows.append(doc)
        except Exception as exc:
            logger.debug("bandit_update_collection_failed: %s", exc)
            return 0

        return self.update_from_rows(rows)

    def update_from_rows(self, rows: Iterable[dict[str, Any]]) -> int:
        """Build α/β from an iterable of metric rows. Returns the row count."""
        posteriors: dict[tuple[str, str], BanditPosterior] = {}
        count = 0
        for row in rows:
            count += 1
            phase = str(row.get("phase") or "")
            if phase != "reason":
                continue
            if not row.get("was_chosen"):
                continue
            role = str(row.get("role") or "")
            task = str(row.get("task_type") or "default")
            if not role:
                continue
            key = (role, task)
            post = posteriors.setdefault(key, BanditPosterior())
            post.observations += 1
            verification_passed = bool(row.get("verification_passed"))
            arbiter_ok = (row.get("arbiter_verdict") == "approve")
            if verification_passed and arbiter_ok:
                post.alpha += 1
            else:
                post.beta += 1
        self._posteriors = posteriors
        return count

    def weights(
        self,
        roles: list[str],
        *,
        task_type: str = "default",
    ) -> dict[str, float]:
        """Sample one Thompson draw per role; normalise to sum=1.

        Cold-start floor: when total observations for ``roles`` ×
        ``task_type`` is below the threshold, return uniform weights.
        """
        if not roles:
            return {}
        relevant = [self._posteriors.get((r, task_type)) for r in roles]
        total_obs = sum((p.observations if p else 0) for p in relevant)
        if total_obs < self._cold_start:
            uniform = 1.0 / len(roles)
            return {r: uniform for r in roles}

        T = self._temperature
        scaled: dict[str, float] = {}
        for role, post in zip(roles, relevant):
            if post is None:
                # No data for this role → uniform contribution.
                scaled[role] = 0.5
                continue
            alpha_prime = 1.0 + max(0.0, (post.alpha - 1.0) / T)
            beta_prime = 1.0 + max(0.0, (post.beta - 1.0) / T)
            try:
                scaled[role] = self._rng.betavariate(alpha_prime, beta_prime)
            except Exception:
                scaled[role] = 0.5
        # Normalise.
        total = sum(scaled.values()) or 1.0
        return {r: v / total for r, v in scaled.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "cold_start_threshold": self._cold_start,
            "temperature": self._temperature,
            "posteriors": {
                f"{r}|{t}": {"alpha": p.alpha, "beta": p.beta,
                              "observations": p.observations}
                for (r, t), p in self._posteriors.items()
            },
        }
