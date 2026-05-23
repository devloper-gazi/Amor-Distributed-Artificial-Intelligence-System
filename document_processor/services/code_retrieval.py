"""
Cycle C Sprint 3 Day 3 — code retrieval: BM25 over identifiers + BGE
reranker over the cache layer from Day 1/2.

Pipeline
--------
query → BM25 top-25 (over symbol names + scope_text) →
BGE-reranker-v2-m3 (CPU) top-5 → return TagWithScore[].

LanceDB dense retrieval is deliberately NOT in Day 3 — see Day 3.5
note below for the rationale.

BM25 rationale
--------------
The repo-map cache (Sprint 3 Day 1) already stores ~3300 tags.  An
in-memory BM25 over those tags' (name + scope_text) lands queries
in <50 ms with ZERO new heavyweight deps.  The textbook BM25 fits
in ~50 lines of Python — no need for ``rank_bm25`` or Postgres FTS
at this scale.  Switch to one of those if the index grows past
~50K tags or we need persistence.

BGE reranker rationale
----------------------
``sentence-transformers`` is already installed (see requirements).
``CrossEncoder`` wraps any Hugging Face cross-encoder; pointing it
at ``BAAI/bge-reranker-v2-m3`` gives us the same model
``FlagEmbedding.FlagReranker`` would load — without the
``FlagEmbedding`` dep (which pins specific transformer versions
that conflict with ours).

The reranker model is downloaded on first use (~280 MB) and cached
under HF_HUB cache.  CPU inference: ~150 ms for 25 candidates per
the May 2026 markaicode benchmark.

Day 3.5 (LanceDB integration)
-----------------------------
LanceDB dense retrieval needs:
1. An embedding model (BGE-M3 / nomic-embed) running on CPU
2. The existing AMOR RAG index already populated (chunks of
   conversations, NOT code symbols)
3. RRF fusion of dense + BM25 results

Today's hybrid retrieval is therefore BM25-only — it's still
"hybrid" in the sense that BM25 ranks → reranker re-ranks.  The
plan-targeted +1 judge-point lift can come from this alone for
intra-repo code queries; LanceDB matters when the user prompt
references content outside the indexed codebase.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from .repo_map import RepoMap, Tag

logger = logging.getLogger(__name__)


# ─── tokenisation ──────────────────────────────────────────────────


# Identifier-aware tokenisation: split on non-word + camelCase + snake_case.
# We index BOTH the original token and its lower-case variant so the BM25
# matcher tolerates AppShell ↔ appshell.  CamelCase splitting catches
# AppShell → ['app','shell','appshell'].
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_NON_WORD_RE = re.compile(r"[^A-Za-z0-9_]+")


def _tokenise(text: str) -> List[str]:
    """Lower-case tokens + camel/snake decomposed forms."""
    if not text:
        return []
    out: List[str] = []
    for chunk in _NON_WORD_RE.split(text):
        if not chunk:
            continue
        out.append(chunk.lower())
        # Decompose camelCase / snake_case for richer match.
        parts = chunk.split("_") if "_" in chunk else _CAMEL_RE.findall(chunk)
        for part in parts:
            if part and part.lower() != chunk.lower():
                out.append(part.lower())
    return out


# ─── BM25 ──────────────────────────────────────────────────────────


@dataclass
class _Doc:
    tag: Tag
    tokens: List[str]
    tf: Counter         # token → frequency in this doc
    length: int


class BM25Index:
    """Textbook Okapi BM25.  In-memory; rebuilt from the RepoMap cache
    on each construction (the cache is fast — ~70 ms for 3K tags)."""

    def __init__(
        self,
        docs: Sequence[_Doc],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.docs = list(docs)
        self.k1 = k1
        self.b = b
        self.avgdl = (
            sum(d.length for d in self.docs) / max(1, len(self.docs))
        )
        # IDF per token: log((N - df + 0.5) / (df + 0.5) + 1).  Smoothed.
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(set(d.tokens))
        n = max(1, len(self.docs))
        self.idf = {
            tok: math.log((n - count + 0.5) / (count + 0.5) + 1.0)
            for tok, count in df.items()
        }

    def search(self, query: str, *, top_k: int = 25) -> List[Tuple[Tag, float]]:
        q_tokens = _tokenise(query)
        if not q_tokens or not self.docs:
            return []
        scores: List[Tuple[int, float]] = []
        for i, doc in enumerate(self.docs):
            score = 0.0
            for tok in q_tokens:
                idf = self.idf.get(tok)
                if idf is None:
                    continue
                tf = doc.tf.get(tok, 0)
                if tf == 0:
                    continue
                norm = 1 - self.b + self.b * (doc.length / max(self.avgdl, 1.0))
                score += idf * (tf * (self.k1 + 1)) / (tf + self.k1 * norm)
            if score > 0:
                scores.append((i, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return [(self.docs[i].tag, s) for i, s in scores[:top_k]]

    @classmethod
    def from_repomap(cls, rm: "RepoMap") -> "BM25Index":
        docs: List[_Doc] = []
        for tag in rm.all_tags():
            # Doc text = name (heaviest weight via repetition) + scope_text.
            # Repeating the name 3× tilts BM25 toward identifier-exact match.
            text = (tag.name + " ") * 3 + (tag.scope_text or "")
            tokens = _tokenise(text)
            docs.append(
                _Doc(
                    tag=tag,
                    tokens=tokens,
                    tf=Counter(tokens),
                    length=len(tokens),
                ),
            )
        return cls(docs)


# ─── BGE reranker (lazy-loaded) ────────────────────────────────────


_RERANKER_MODEL_NAME = os.environ.get(
    "AMOR_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3",
)
_RERANKER_CACHE: dict[str, "Any"] = {}  # type: ignore[name-defined]


def _get_reranker():
    """Lazy-init the cross-encoder.  Cached per-process; first call
    downloads the model (~280 MB) under HF_HUB cache."""
    if "model" in _RERANKER_CACHE:
        return _RERANKER_CACHE["model"]
    try:
        from sentence_transformers import CrossEncoder  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "sentence-transformers missing — install it to use the BGE "
            "reranker (already in requirements as of Sprint 3)",
        ) from exc
    logger.info("loading BGE reranker: %s", _RERANKER_MODEL_NAME)
    model = CrossEncoder(_RERANKER_MODEL_NAME, max_length=512)
    _RERANKER_CACHE["model"] = model
    return model


def rerank(
    query: str,
    candidates: Sequence[Tag],
    *,
    top_k: int = 5,
) -> List[Tuple[Tag, float]]:
    """Score (query, candidate.scope_text) pairs with BGE-reranker-v2-m3.
    Returns top-k by score, descending."""
    if not candidates:
        return []
    model = _get_reranker()
    pairs = [
        (
            query,
            f"{tag.rel_path}::{tag.name}\n{tag.scope_text or ''}",
        )
        for tag in candidates
    ]
    scores = model.predict(pairs, show_progress_bar=False)
    ranked = sorted(
        zip(candidates, scores),
        key=lambda x: float(x[1]),
        reverse=True,
    )
    return [(tag, float(score)) for tag, score in ranked[:top_k]]


# ─── public hybrid retrieval ───────────────────────────────────────


@dataclass(frozen=True)
class RetrievalResult:
    tag: Tag
    bm25_score: float
    rerank_score: Optional[float]


def hybrid_retrieve(
    query: str,
    *,
    repo_root: Optional[Path] = None,
    top_k_bm25: int = 25,
    top_k_final: int = 5,
    use_reranker: bool = True,
    rescan: bool = True,
) -> List[RetrievalResult]:
    """Day 3 public entry — BM25 top-25 → BGE rerank top-5.

    ``use_reranker=False`` returns BM25 raw top-N (useful for smoke
    tests that don't want to download the 280 MB model).  Plan
    contract: BGE-reranker-v2-m3 CPU latency P95 <250 ms for top-25
    → top-5 (markaicode May 2026 benchmark).
    """
    rm = RepoMap(repo_root or Path.cwd())
    if rescan:
        rm.scan()
    index = BM25Index.from_repomap(rm)
    started = time.perf_counter()
    bm25_hits = index.search(query, top_k=top_k_bm25)
    bm25_ms = (time.perf_counter() - started) * 1000.0
    if not bm25_hits:
        return []

    if not use_reranker:
        return [
            RetrievalResult(tag=t, bm25_score=s, rerank_score=None)
            for t, s in bm25_hits[:top_k_final]
        ]

    started = time.perf_counter()
    reranked = rerank(query, [t for t, _ in bm25_hits], top_k=top_k_final)
    rerank_ms = (time.perf_counter() - started) * 1000.0
    logger.info(
        "hybrid_retrieve: bm25=%d (%.1f ms), rerank=%d (%.1f ms)",
        len(bm25_hits), bm25_ms, len(reranked), rerank_ms,
    )

    bm25_lookup = {id(tag): score for tag, score in bm25_hits}
    return [
        RetrievalResult(
            tag=tag,
            bm25_score=bm25_lookup.get(id(tag), 0.0),
            rerank_score=score,
        )
        for tag, score in reranked
    ]


__all__ = [
    "BM25Index",
    "RetrievalResult",
    "hybrid_retrieve",
    "rerank",
]
