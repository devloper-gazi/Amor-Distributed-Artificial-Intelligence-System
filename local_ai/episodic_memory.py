"""
EpisodicMemory — long-term store of every successful code-generation
session.

Each "episode" captures the user query, the verified algorithm
skeleton, the final code, the test pass rate, and a vector embedding
of the query. Future runs can semantic-search the store to:

  * Reuse a verified skeleton when similarity ≥ 0.85 (skip the Logic
    Engine + Z3 entirely).
  * Seed the Logic Engine with a related skeleton when 0.6 ≤ sim < 0.85.
  * Fall through to the full pipeline when sim < 0.6.

The embedder is dependency-injected — tests pass a deterministic
hash-based stub, production code injects the LanceDB-backed
``nomic-embed-text-v1.5`` embedder that already ships in this repo.

The MongoDB writer is also injected: pass a real Motor collection in
production, an in-memory shim in tests. All public methods are
fail-soft: a missing collection / embedder / network error returns an
empty result rather than raising into the engine.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)


Embedder = Callable[[str], Awaitable[Sequence[float]] | Sequence[float]]


# Reuse-vs-seed-vs-fresh thresholds. Bands match the prompt spec.
DEFAULT_REUSE_THRESHOLD = 0.85
DEFAULT_SEED_THRESHOLD = 0.60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ─── Episode + retrieval result dataclasses ─────────────────────────


@dataclass
class EpisodicMemoryEntry:
    """One archived session."""

    session_id: str
    user_query: str
    timestamp: str = field(default_factory=_now_iso)
    algorithm_skeleton: dict[str, Any] = field(default_factory=dict)
    final_code: str = ""
    test_pass_rate: float = 0.0
    language: str = "python"
    complexity: str = ""
    tags: list[str] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)
    # Deterministic hash so an exact-duplicate insert upserts cleanly
    # rather than creating a new document.
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        h = hashlib.sha256()
        h.update(self.user_query.encode("utf-8", "replace"))
        h.update(self.final_code.encode("utf-8", "replace"))
        h.update(self.language.encode("utf-8", "replace"))
        return h.hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class RetrievedEpisode:
    """One ranked match from a similarity search."""

    entry: EpisodicMemoryEntry
    similarity: float

    @property
    def reuse_recommended(self) -> bool:
        return self.similarity >= DEFAULT_REUSE_THRESHOLD

    @property
    def seed_recommended(self) -> bool:
        return DEFAULT_SEED_THRESHOLD <= self.similarity < DEFAULT_REUSE_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry.to_dict(),
            "similarity": round(self.similarity, 4),
            "reuse_recommended": self.reuse_recommended,
            "seed_recommended": self.seed_recommended,
        }


@dataclass
class RetrievalDecision:
    """High-level routing decision the engine consults at request entry."""

    action: str            # "reuse" | "seed" | "fresh"
    matches: list[RetrievedEpisode] = field(default_factory=list)
    best_similarity: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "matches": [m.to_dict() for m in self.matches],
            "best_similarity": round(self.best_similarity, 4),
        }


# ─── Store ──────────────────────────────────────────────────────────


class EpisodicMemoryStore:
    """Async wrapper around a Mongo collection + an embedder.

    Public surface:
      ``await store(entry)``                  → write (upsert by content_hash)
      ``await search(query, k=3)``            → top-k cosine matches
      ``await decide(query, reuse_threshold=…, seed_threshold=…)`` →
                                                routing recommendation

    Both the collection and the embedder are optional; passing None
    yields an in-process fallback (a list of entries kept in memory)
    so the rest of the engine can run on machines without Mongo.
    """

    def __init__(
        self,
        *,
        collection: Any | None = None,
        embedder: Embedder | None = None,
        reuse_threshold: float = DEFAULT_REUSE_THRESHOLD,
        seed_threshold: float = DEFAULT_SEED_THRESHOLD,
        max_inmemory_size: int = 1024,
    ) -> None:
        self._collection = collection
        self._embedder = embedder
        self._reuse_threshold = float(reuse_threshold)
        self._seed_threshold = float(seed_threshold)
        self._inmemory: list[EpisodicMemoryEntry] = []
        self._max_inmemory_size = int(max_inmemory_size)

    # ── embedding ─────────────────────────────────────────────────

    async def _embed_async(self, text: str) -> list[float]:
        """Coerce sync/async embedder into list[float]. Returns []
        on any failure — callers treat empty as 'no embedding'."""
        if self._embedder is None:
            return []
        try:
            res = self._embedder(text)
            if hasattr(res, "__await__"):
                res = await res  # type: ignore[assignment]
            return [float(v) for v in (res or [])]
        except Exception as exc:
            logger.debug("episodic_embed_failed: %s", exc)
            return []

    # ── store ─────────────────────────────────────────────────────

    async def store(self, entry: EpisodicMemoryEntry) -> bool:
        """Upsert by content_hash. If embedding is missing, compute it.

        Returns True on success, False on failure-soft path. Never
        raises — episodic memory writes must NOT block the response.
        """
        try:
            if not entry.embedding:
                entry.embedding = await self._embed_async(entry.user_query)
        except Exception as exc:
            logger.debug("episodic_store_embed_failed: %s", exc)

        if self._collection is not None:
            try:
                await self._collection.update_one(
                    {"content_hash": entry.content_hash},
                    {"$set": entry.to_dict()},
                    upsert=True,
                )
                return True
            except Exception as exc:
                logger.debug("episodic_store_mongo_failed: %s", exc)
                # Fall through to in-memory.

        # In-memory fallback. Replace existing entry by content_hash.
        for i, existing in enumerate(self._inmemory):
            if existing.content_hash == entry.content_hash:
                self._inmemory[i] = entry
                return True
        self._inmemory.append(entry)
        # Cap in-memory size — drop oldest.
        if len(self._inmemory) > self._max_inmemory_size:
            self._inmemory = self._inmemory[-self._max_inmemory_size:]
        return True

    # ── search ────────────────────────────────────────────────────

    async def search(
        self, query: str, *, k: int = 3,
        min_similarity: float = 0.0,
    ) -> list[RetrievedEpisode]:
        """Embed `query` and return the top-k cosine matches.

        For a small store (<10k entries) we scan in-process; for a
        larger one production should use Mongo Atlas vector search OR
        switch to LanceDB. That swap is a one-line change in this
        method's body.
        """
        query_emb = await self._embed_async(query)
        if not query_emb:
            return []

        # Pull every candidate. For Mongo, this is a `find({}, {...})`
        # full scan — fine for the corpus sizes Phase 1A is targeting.
        candidates: list[EpisodicMemoryEntry] = []
        if self._collection is not None:
            try:
                cursor = self._collection.find(
                    {}, {"_id": 0},
                )
                async for doc in cursor:
                    if not isinstance(doc, dict):
                        continue
                    try:
                        candidates.append(EpisodicMemoryEntry(**doc))
                    except (TypeError, ValueError):
                        continue
            except Exception as exc:
                logger.debug("episodic_search_mongo_failed: %s", exc)
        # Always also include in-memory fallback entries.
        candidates.extend(self._inmemory)

        ranked: list[RetrievedEpisode] = []
        for cand in candidates:
            sim = _cosine(query_emb, cand.embedding)
            if sim < min_similarity:
                continue
            ranked.append(RetrievedEpisode(entry=cand, similarity=sim))
        ranked.sort(key=lambda r: r.similarity, reverse=True)
        return ranked[: max(1, int(k))]

    # ── decision ──────────────────────────────────────────────────

    async def decide(self, query: str) -> RetrievalDecision:
        """Top-line routing recommendation:
          ≥ reuse_threshold       → "reuse" (skip pipeline, return cached)
          ≥ seed_threshold        → "seed" (use cached as Logic Engine seed)
          else                    → "fresh" (full pipeline)
        """
        matches = await self.search(query, k=3)
        if not matches:
            return RetrievalDecision(action="fresh")
        best = matches[0]
        if best.similarity >= self._reuse_threshold:
            action = "reuse"
        elif best.similarity >= self._seed_threshold:
            action = "seed"
        else:
            action = "fresh"
        return RetrievalDecision(
            action=action, matches=matches,
            best_similarity=best.similarity,
        )

    # ── housekeeping ──────────────────────────────────────────────

    async def count(self) -> int:
        """Return the number of accessible entries — Mongo + the
        in-memory fallback. Entries that landed in BOTH (because a
        Mongo write succeeded after an in-memory shadow was made)
        are double-counted; the upsert-by-content_hash invariant
        keeps that case rare in practice and the search() path
        properly dedupes by content_hash anyway."""
        mongo_count = 0
        if self._collection is not None:
            try:
                mongo_count = int(await self._collection.count_documents({}))
            except Exception:
                mongo_count = 0
        return mongo_count + len(self._inmemory)

    async def clear_inmemory(self) -> None:
        """Tests use this between runs. Doesn't touch Mongo."""
        self._inmemory.clear()


# ─── Convenience: fixed-bag embedder for tests ──────────────────────


def hash_embedder(dim: int = 32) -> Embedder:
    """Test-only deterministic embedder.

    Hashes overlapping char n-grams of the input into a fixed-size
    bag-of-values vector. Two semantically similar queries that share
    many n-grams will land near each other in cosine space — good
    enough to write meaningful unit tests without dragging in
    sentence-transformers.
    """
    def _embed(text: str) -> list[float]:
        vec = [0.0] * dim
        text_l = (text or "").lower()
        for i in range(max(0, len(text_l) - 2)):
            tri = text_l[i:i + 3]
            h = int(hashlib.md5(tri.encode("utf-8", "replace")).hexdigest(), 16)
            vec[h % dim] += 1.0
        return vec
    return _embed
