"""
QuickCode V2 — Striatum (cosine fast-path procedural cache).

Inspired by the basal-ganglia striatum's role in habit formation:
once a prompt has been answered well, the same family of prompts
should hit a fast cache instead of running the full pipeline.

Mechanics
---------

* On lookup, embed the prompt and compare cosine similarity against
  the most recent ``max_entries`` cached entries.  If the best match
  scores ``≥ threshold`` (default 0.95), return the cached bundle.
* On store, embed once, append a new entry, trim to ``max_entries``,
  and refresh the Redis TTL.

Design notes
------------

* The Redis layer is optional — pass ``cache=None`` and the store
  becomes a process-local in-memory list.  Tests use this path.
* Embeddings can be ``async`` or sync — same contract as
  ``code_intelligence/reactor/rag.py`` ``Embedder``.
* The ``salt`` field combines with ``settings.code_reactor_cache_salt``
  in the Redis key so a prompt-template change can invalidate every
  cached entry by bumping either constant.
* No content filters / refusal language anywhere — the user
  explicitly asked for that.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)


Embedder = Callable[[str], Awaitable[Sequence[float]] | Sequence[float]]


# ─────────────────────────────────────────────────────────────────────
# Default deterministic embedder (64-d sketch).
#
# Used when the caller does not supply one — keeps Striatum useful in
# tests without dragging in sentence-transformers.  The sketch is a
# stable hash-buckets-into-64-dim approach: every word's md5 gets
# folded into one bucket.  Cosine over these sketches preserves
# *exact match* and rough lexical overlap, which is sufficient for a
# 0.95-threshold fast path.  Real production wiring should pass
# Ollama's ``nomic-embed-text`` for stronger semantics.
# ─────────────────────────────────────────────────────────────────────


_SKETCH_DIM = 64


def hash_embedder(text: str) -> list[float]:
    """Deterministic 64-dim sketch.  Same input → same vector."""
    if not text:
        return [0.0] * _SKETCH_DIM
    vec = [0.0] * _SKETCH_DIM
    for token in text.lower().split():
        digest = hashlib.md5(token.encode("utf-8")).digest()
        idx = digest[0] % _SKETCH_DIM
        sign = 1.0 if (digest[1] & 1) else -1.0
        vec[idx] += sign
    return vec


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1].  Returns 0 on empty/zero input."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        ai = float(a[i])
        bi = float(b[i])
        dot += ai * bi
        na += ai * ai
        nb += bi * bi
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ─────────────────────────────────────────────────────────────────────
# Striatum
# ─────────────────────────────────────────────────────────────────────


class Striatum:
    """Cosine ≥ threshold fast-path cache."""

    DEFAULT_KEY = "amor:quick_code:striatum:v1"

    def __init__(
        self,
        *,
        cache: Any | None = None,
        embedder: Embedder | None = None,
        threshold: float = 0.95,
        ttl_s: int = 86_400,
        max_entries: int = 512,
        salt: int = 1,
        key: str | None = None,
    ) -> None:
        self._cache = cache
        self._embedder: Embedder = embedder or hash_embedder
        self._threshold = max(0.0, min(1.0, float(threshold)))
        self._ttl_s = max(60, int(ttl_s))
        self._max_entries = max(1, int(max_entries))
        self._salt = int(salt)
        self._key = key or self.DEFAULT_KEY
        # In-memory fallback when Redis is unavailable.  Process-local
        # only — fine for tests and degraded-mode hosts.
        self._memory_entries: list[dict[str, Any]] = []

    # ─── Public API ─────────────────────────────────────────────────

    async def lookup(self, prompt: str) -> dict[str, Any] | None:
        """Return the cached bundle dict if cosine ≥ threshold."""
        if not (prompt or "").strip():
            return None
        try:
            query_vec = await self._embed(prompt)
        except Exception as exc:  # pragma: no cover - infra
            logger.debug("striatum embed failed: %s", exc)
            return None
        if not query_vec:
            return None

        entries = await self._read_entries()
        if not entries:
            return None

        best_score = -1.0
        best_entry: dict[str, Any] | None = None
        for entry in entries:
            try:
                vec = entry.get("vec") or []
                score = cosine(query_vec, vec)
            except Exception:  # pragma: no cover
                continue
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry is None or best_score < self._threshold:
            return None

        # Defensive deep-copy so callers cannot mutate cached state
        # through the returned bundle.  Then attach a striatum_meta
        # block so the engine can tell hits from cold-runs.
        bundle = _deep_copy(best_entry.get("bundle") or {})
        bundle.setdefault("striatum_meta", {}).update({
            "score": round(best_score, 4),
            "threshold": self._threshold,
            "stored_at": best_entry.get("stored_at"),
        })
        return bundle

    async def store(self, prompt: str, bundle: dict[str, Any]) -> None:
        """Append a new entry, trim, and write back."""
        if not (prompt or "").strip() or not bundle:
            return
        try:
            vec = await self._embed(prompt)
        except Exception as exc:  # pragma: no cover - infra
            logger.debug("striatum embed failed during store: %s", exc)
            return
        if not vec:
            return

        entries = await self._read_entries()
        entries.append({
            "vec": list(vec),
            "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "bundle": _deep_copy(bundle),
            "stored_at": time.time(),
        })
        # Trim oldest first.
        if len(entries) > self._max_entries:
            entries = entries[-self._max_entries:]
        await self._write_entries(entries)

    async def clear(self) -> None:
        """Drop everything.  Used by tests + the eviction route."""
        self._memory_entries = []
        if self._cache is not None:
            try:
                await self._cache.delete(self._scoped_key())
            except Exception as exc:  # pragma: no cover - infra
                logger.debug("striatum clear failed: %s", exc)

    async def stats(self) -> dict[str, Any]:
        entries = await self._read_entries()
        return {
            "size": len(entries),
            "max_entries": self._max_entries,
            "threshold": self._threshold,
            "ttl_s": self._ttl_s,
            "salt": self._salt,
            "key": self._scoped_key(),
        }

    # ─── Internals ──────────────────────────────────────────────────

    async def _embed(self, text: str) -> list[float]:
        try:
            res = self._embedder(text)
            if hasattr(res, "__await__"):
                res = await res  # type: ignore[assignment]
            return list(res or [])
        except Exception as exc:
            logger.debug("striatum embedder raised: %s", exc)
            return []

    def _scoped_key(self) -> str:
        return f"{self._key}:salt={self._salt}"

    async def _read_entries(self) -> list[dict[str, Any]]:
        if self._cache is None:
            return list(self._memory_entries)
        try:
            raw = await self._cache.get(self._scoped_key())
        except Exception as exc:  # pragma: no cover - infra
            logger.debug("striatum redis get failed: %s", exc)
            return list(self._memory_entries)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception:  # pragma: no cover
            return []
        if not isinstance(data, list):
            return []
        return [e for e in data if isinstance(e, dict)]

    async def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        if self._cache is None:
            self._memory_entries = entries
            return
        try:
            await self._cache.set(
                self._scoped_key(),
                json.dumps(entries, default=str),
                ttl=self._ttl_s,
            )
        except Exception as exc:  # pragma: no cover - infra
            logger.debug("striatum redis set failed: %s", exc)
            self._memory_entries = entries


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _deep_copy(d: dict[str, Any]) -> dict[str, Any]:
    """Defensive deep copy via JSON round-trip so callers cannot
    mutate cached state through the returned dict.  Slow only when a
    bundle has tens of thousands of fields, which it never does."""
    return json.loads(json.dumps(d, default=str))


__all__ = [
    "Striatum",
    "Embedder",
    "hash_embedder",
    "cosine",
]
