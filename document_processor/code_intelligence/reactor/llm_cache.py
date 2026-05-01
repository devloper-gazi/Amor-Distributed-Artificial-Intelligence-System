"""
SemanticLLMCache — embedding-keyed Redis cache wrapping any LLM call.

Cache key = (role, sha256(system_prompt), max_tokens_bucket,
             temp_bucket, corpus_version) → bucket of stored entries
            keyed by quantised embedding signature.

Lookup: embed user_prompt → cosine over the bucket → HIT if any
stored entry has cosine >= threshold (default 0.92), else MISS.

Failure mode: any exception (Redis offline, embedder unavailable,
serialisation error) yields a MISS — the engine keeps running, just
without cache acceleration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)


Embedder = Callable[[str], Awaitable[Sequence[float]] | Sequence[float]]


@dataclass
class CacheKey:
    """Components of the cache key, before final hashing."""

    role: str
    system_prompt_hash: str
    max_tokens_bucket: int
    corpus_version: int

    def bucket_id(self) -> str:
        """8-bit prefix → up to ~50 candidates per bucket."""
        h = hashlib.sha256(
            f"{self.role}|{self.system_prompt_hash}|"
            f"{self.max_tokens_bucket}|{self.corpus_version}".encode()
        ).hexdigest()
        return h[:2]  # 8-bit hex prefix

    def full_hash(self) -> str:
        h = hashlib.sha256(
            f"{self.role}|{self.system_prompt_hash}|"
            f"{self.max_tokens_bucket}|{self.corpus_version}".encode()
        ).hexdigest()
        return h


def _quantise_embedding(vec: Sequence[float], decimals: int = 4) -> list[float]:
    """Round to N decimals so duplicates collapse + storage shrinks."""
    return [round(float(v), decimals) for v in vec]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _hash_system_prompt(system_prompt: str | None) -> str:
    return hashlib.sha256((system_prompt or "").encode()).hexdigest()[:16]


class SemanticLLMCache:
    """Drop-in cache layer for any async ``llm_call(prompt, system, max_tokens)``.

    Constructor args:
      cache : object with `async get_json(key)` + `async set_json(key, val, ttl=...)`
              (the project's existing CacheManager satisfies this).
      embedder : callable(str) → 768-d vector (sync or async).
      cosine_threshold : minimum similarity to count as HIT.
      ttl_s : Redis TTL.
      corpus_version : monotonic salt; bump to invalidate everything.
    """

    def __init__(
        self,
        *,
        cache: Any,
        embedder: Embedder,
        cosine_threshold: float = 0.92,
        ttl_s: int = 86_400,
        corpus_version: int = 1,
        max_bucket_size: int = 50,
    ) -> None:
        self._cache = cache
        self._embed = embedder
        self._cosine_threshold = float(cosine_threshold)
        self._ttl = int(ttl_s)
        self._corpus_version = int(corpus_version)
        self._max_bucket = int(max_bucket_size)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _max_tokens_bucket(n: int) -> int:
        return max(0, (int(n or 0) // 256) * 256)

    def _make_key(self, role: str, system: str | None, max_tokens: int) -> CacheKey:
        return CacheKey(
            role=str(role or ""),
            system_prompt_hash=_hash_system_prompt(system),
            max_tokens_bucket=self._max_tokens_bucket(max_tokens),
            corpus_version=self._corpus_version,
        )

    async def _embed_async(self, text: str) -> list[float]:
        """Coerce sync/async embedder into a list[float]."""
        try:
            res = self._embed(text)
            if hasattr(res, "__await__"):
                res = await res  # type: ignore[assignment]
        except Exception as exc:
            logger.debug("llm_cache_embed_failed: %s", exc)
            return []
        return [float(v) for v in (res or [])]

    async def lookup(
        self,
        *,
        role: str,
        system_prompt: str | None,
        user_prompt: str,
        max_tokens: int,
    ) -> str | None:
        """Returns a cached response when cosine ≥ threshold, else None."""
        key = self._make_key(role, system_prompt, max_tokens)
        try:
            embedding = await self._embed_async(user_prompt)
            if not embedding:
                self.misses += 1
                return None
            redis_key = f"reactor:llm_cache:{key.bucket_id()}:{key.full_hash()[:16]}"
            bucket = await self._cache.get_json(redis_key) or []
            if not isinstance(bucket, list):
                self.misses += 1
                return None
            best_score = 0.0
            best_response: str | None = None
            for entry in bucket:
                if not isinstance(entry, dict):
                    continue
                stored_emb = entry.get("e") or []
                score = _cosine(embedding, stored_emb)
                if score > best_score:
                    best_score = score
                    best_response = entry.get("r")
            if best_response is not None and best_score >= self._cosine_threshold:
                self.hits += 1
                return str(best_response)
        except Exception as exc:
            logger.debug("llm_cache_lookup_failed: %s", exc)
        self.misses += 1
        return None

    async def store(
        self,
        *,
        role: str,
        system_prompt: str | None,
        user_prompt: str,
        max_tokens: int,
        response: str,
    ) -> None:
        """Add response to its bucket. Negative results (empty / error
        markers) are stored with shorter TTL via store_negative()."""
        if not (response or "").strip():
            return
        key = self._make_key(role, system_prompt, max_tokens)
        try:
            embedding = await self._embed_async(user_prompt)
            if not embedding:
                return
            redis_key = f"reactor:llm_cache:{key.bucket_id()}:{key.full_hash()[:16]}"
            bucket = await self._cache.get_json(redis_key) or []
            if not isinstance(bucket, list):
                bucket = []
            bucket.append({
                "e": _quantise_embedding(embedding),
                "r": response,
                "t": int(time.time()),
            })
            # Cap the bucket; oldest first.
            if len(bucket) > self._max_bucket:
                bucket = bucket[-self._max_bucket:]
            await self._cache.set_json(redis_key, bucket, ttl=self._ttl)
        except Exception as exc:
            logger.debug("llm_cache_store_failed: %s", exc)


def wrap_llm_call(
    inner: Callable[[str, str | None, int], Awaitable[str]],
    cache: SemanticLLMCache,
    *,
    role_getter: Callable[[], str] | None = None,
) -> Callable[[str, str | None, int], Awaitable[str]]:
    """Return a new llm_call that consults the cache first.

    ``role_getter`` lets the caller plug in the existing _ACTIVE_ROLE
    ContextVar so cache keys partition by per-role bindings without
    explicit threading."""
    async def cached(prompt: str, system: str | None, max_tokens: int) -> str:
        role = (role_getter() if role_getter else "") or "default"
        hit = await cache.lookup(
            role=role, system_prompt=system,
            user_prompt=prompt, max_tokens=max_tokens,
        )
        if hit is not None:
            return hit
        result = await inner(prompt, system, max_tokens)
        await cache.store(
            role=role, system_prompt=system,
            user_prompt=prompt, max_tokens=max_tokens,
            response=result,
        )
        return result
    return cached
