"""Cycle I.2 — Titans test-time predictive memory (MAC variant).

Sapienza reimpl (arXiv 2510.09551) adapted to AMOR's session traces.
Following Plan-agent's "no gradient through verifier" simplification:
Titans here is a SIMILARITY-BASED RECALL layer over recent session
content; the surprise-gradient + chunking variants from the paper
are deliberately deferred (chunking sometimes hurts retrieval
recall per the original abstract).

Layered on top of the existing ``local_ai.memory`` archival store,
this module adds:

  1. **Bounded rolling window** of the last N (timestamp, content,
     embedding) tuples.  Default 200 — Plan-agent locked: 100M
     trainable parameters in the original paper roughly maps to a
     ~200-entry context window at AMOR's session size.
  2. **Cosine-similarity recall** — given a query, return the top-K
     most similar past entries.  Reuses the existing embedder (BGE-M3
     or whatever the operator wired) so we don't load a second model
     into the VRAM budget.
  3. **Engine hook** — ``_titans_recall_for_plan(prompt)`` returns a
     short markdown block that the planner prompt prepends as
     "Recalled context from past sessions".

Settings (in ``document_processor.config.settings``):
  * ``code_titans_enabled: bool = False``  (Plan-agent locked default OFF)
  * ``code_titans_recall_k: int = 3``
  * ``code_titans_max_window: int = 200``

Rollback: drop the engine hook + flag flip to disable.  No
persistent state lives outside the in-process deque; the archival
backend writes are append-only and read-only from Titans' POV.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Deque, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ─── Types ──────────────────────────────────────────────────────────


#: Embedder signature — matches `local_ai.memory.store.EmbedderFn`
#: so AMOR's existing BGE-M3 or sentence-transformers wrapper drops in.
EmbedderFn = Callable[[str], Awaitable[Sequence[float]]]


@dataclass
class TitansEntry:
    """One past session-trace slice ready for similarity recall.

    ``content`` is the canonical text we hashed/embedded.  ``role`` is
    a free-text tag (e.g. ``"user_prompt"``, ``"plan_summary"``,
    ``"verifier_outcome"``) so the recall renderer can label snippets.
    ``embedding`` is normalised so cosine sim is just dot product.
    """
    content: str
    role: str
    timestamp_utc: str
    embedding: Tuple[float, ...]
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "role": self.role,
            "timestamp_utc": self.timestamp_utc,
            "extra": dict(self.extra),
            # Embedding intentionally omitted from the dict shape —
            # callers don't need the floats, and shipping 1024 dims
            # in every event payload would bloat the SSE stream.
        }


# ─── Math primitives ────────────────────────────────────────────────


def _l2_normalise(vec: Sequence[float]) -> Tuple[float, ...]:
    """Return ``vec`` rescaled to unit length.  Zero-vector → zero
    (no division by zero; recall just won't find it)."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0:
        return tuple(0.0 for _ in vec)
    inv = 1.0 / norm
    return tuple(x * inv for x in vec)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two vectors.

    Operates on RAW vectors; callers store normalised embeddings so
    the inner-product shortcut works, but this helper is robust to
    either form (re-normalises internally).
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (na * nb)


# ─── Predictive memory ──────────────────────────────────────────────


class TitansPredictiveMemory:
    """Bounded rolling-window similarity-based recall.

    Thread-safety: the underlying ``deque`` is appended/iterated from
    a single asyncio loop in practice (the engine's coroutine).  If
    you need multi-thread access, add a lock externally.
    """

    def __init__(
        self,
        *,
        embedder: EmbedderFn,
        max_window: int = 200,
        recall_k: int = 3,
        min_score: float = 0.20,
    ) -> None:
        self._embedder: EmbedderFn = embedder
        self._max_window = int(max(1, max_window))
        self._recall_k = int(max(1, recall_k))
        self._min_score = float(min_score)
        self._entries: Deque[TitansEntry] = deque(maxlen=self._max_window)

    # ─── lifecycle ──────────────────────────────────────────────────

    @property
    def size(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        """Operator/test escape hatch — empties the in-memory window."""
        self._entries.clear()

    # ─── ingestion ──────────────────────────────────────────────────

    async def record(
        self,
        content: str,
        *,
        role: str = "session",
        extra: Optional[dict] = None,
        timestamp_utc: Optional[str] = None,
    ) -> TitansEntry:
        """Embed ``content`` + append to the rolling window.

        Returns the materialised entry so callers can inspect / log.
        If embedding fails, the entry is still appended with an empty
        embedding tuple — searches just won't find it (defensive: a
        failed write shouldn't crash the engine).
        """
        try:
            raw_emb = await self._embedder(content or "")
            emb = _l2_normalise(tuple(float(x) for x in raw_emb))
        except Exception as exc:
            logger.warning(
                "titans.embed_failed role=%s err=%s — storing without embedding",
                role, exc,
            )
            emb = tuple()
        entry = TitansEntry(
            content=content,
            role=role,
            timestamp_utc=timestamp_utc or datetime.now(timezone.utc).isoformat(),
            embedding=emb,
            extra=dict(extra or {}),
        )
        self._entries.append(entry)
        return entry

    # ─── recall ─────────────────────────────────────────────────────

    async def recall(
        self,
        query: str,
        *,
        k: Optional[int] = None,
        min_score: Optional[float] = None,
    ) -> List[Tuple[TitansEntry, float]]:
        """Return the top-K entries ranked by cosine similarity to
        ``query``'s embedding.  Entries below ``min_score`` are
        dropped — better to return fewer high-quality matches than
        flood the planner prompt with weak ones.

        Returns ``[]`` when the window is empty / embedding fails /
        nothing clears the threshold.  Plan-agent locked: recall MUST
        NEVER raise — the engine's _phase_plan call site treats this
        as best-effort context injection.
        """
        if not self._entries:
            return []
        try:
            raw = await self._embedder(query or "")
            qvec = _l2_normalise(tuple(float(x) for x in raw))
        except Exception as exc:
            logger.warning("titans.recall_embed_failed err=%s", exc)
            return []
        if not qvec or all(x == 0.0 for x in qvec):
            return []

        k_eff = int(k) if k is not None else self._recall_k
        threshold = float(min_score) if min_score is not None else self._min_score

        scored: List[Tuple[TitansEntry, float]] = []
        for ent in self._entries:
            if not ent.embedding:
                continue
            # Both vectors are normalised — dot product = cosine.
            score = sum(a * b for a, b in zip(qvec, ent.embedding))
            if score >= threshold:
                scored.append((ent, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k_eff]

    async def recall_as_markdown(
        self,
        query: str,
        *,
        k: Optional[int] = None,
        per_entry_chars: int = 320,
    ) -> str:
        """Render the recall result as a short markdown block ready
        to prepend to the planner prompt.  Returns ``""`` when nothing
        clears the threshold (caller should skip the injection)."""
        hits = await self.recall(query, k=k)
        if not hits:
            return ""
        lines: List[str] = ["**Recalled context from past sessions:**"]
        for i, (ent, score) in enumerate(hits, start=1):
            snippet = ent.content[:per_entry_chars].replace("\n", " ").strip()
            lines.append(
                f"  {i}. _({ent.role}, sim={score:.2f})_ {snippet}",
            )
        return "\n".join(lines) + "\n"


# ─── Convenience factories ──────────────────────────────────────────


def make_memory_from_settings(
    embedder: EmbedderFn,
) -> TitansPredictiveMemory:
    """Build a TitansPredictiveMemory using current Pydantic settings.

    Lazy-imports settings so the module stays testable without the
    full config tree.  Falls back to sane defaults if settings raises.
    """
    try:
        from ..config.settings import settings  # noqa: PLC0415
        return TitansPredictiveMemory(
            embedder=embedder,
            max_window=int(getattr(settings, "code_titans_max_window", 200)),
            recall_k=int(getattr(settings, "code_titans_recall_k", 3)),
            min_score=float(getattr(settings, "code_titans_min_score", 0.20)),
        )
    except Exception as exc:
        logger.warning("titans.config_fallback err=%s", exc)
        return TitansPredictiveMemory(embedder=embedder)
