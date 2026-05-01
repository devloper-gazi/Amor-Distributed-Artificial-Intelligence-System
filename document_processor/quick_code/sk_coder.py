"""
QuickCode V2 — SkCoder: BM25 + cosine hybrid code retrieval.

Inspired by `Skeleton Code Coder <https://arxiv.org/abs/2310.13396>`_.
Given a ``TaskIR`` we retrieve the most relevant snippets from a
small in-process corpus, score each by a hybrid

    α = β * normalised_bm25 + (1 - β) * cosine

and return the top K.  When the best α falls below
``alpha_floor`` (default 0.35) we instead return an empty list plus
a single ``<FILL_HERE:...>`` placeholder so the coder template can
ask the LLM to fill in the gap rather than copy a poor match.

Design notes
------------

* We avoid a hard dependency on ``rank_bm25``: when it isn't
  installed the SkCoder falls back to a tiny inline implementation
  with the same API.  Production hosts already have ``rank_bm25``
  via ``code_intelligence/reactor/rag.py``, but the quick_code
  package itself shouldn't strictly require it for unit tests to
  run on minimal installations.
* Cosine reuses ``striatum.cosine`` to keep the math in one place.
* No content filters / refusal language — the retrieval prompt
  delivered to the coder is a plain technical hint, nothing more.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Awaitable, Callable, Iterable, Sequence

from .contracts import CodeSnippet, TaskIR
from .striatum import cosine, hash_embedder

logger = logging.getLogger(__name__)


Embedder = Callable[[str], Awaitable[Sequence[float]] | Sequence[float]]


# ─────────────────────────────────────────────────────────────────────
# Tokeniser
# ─────────────────────────────────────────────────────────────────────


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _tokenise(text: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(text or "")]


# ─────────────────────────────────────────────────────────────────────
# Tiny BM25 implementation (no rank_bm25 dependency required)
# ─────────────────────────────────────────────────────────────────────


class _BM25:
    """Bare-bones BM25 with the standard k1=1.5, b=0.75 defaults."""

    def __init__(
        self,
        documents: Iterable[Sequence[str]],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        docs = [list(d) for d in documents]
        self._docs = docs
        self._k1 = float(k1)
        self._b = float(b)
        self._n = len(docs)
        self._doc_lens = [len(d) for d in docs]
        self._avg_dl = (sum(self._doc_lens) / self._n) if self._n else 0.0
        # Document-frequency
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        self._idf = {
            term: math.log(1.0 + (self._n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def score(self, query: Sequence[str], doc_idx: int) -> float:
        if doc_idx >= self._n or not query:
            return 0.0
        d = self._docs[doc_idx]
        dl = self._doc_lens[doc_idx]
        if dl == 0:
            return 0.0
        tf = Counter(d)
        score = 0.0
        for term in query:
            if term not in tf:
                continue
            idf = self._idf.get(term, 0.0)
            f = tf[term]
            denom = f + self._k1 * (
                1.0 - self._b + self._b * dl / max(self._avg_dl, 1e-9)
            )
            score += idf * (f * (self._k1 + 1.0)) / denom
        return score

    def all_scores(self, query: Sequence[str]) -> list[float]:
        return [self.score(query, i) for i in range(self._n)]


# ─────────────────────────────────────────────────────────────────────
# Public class
# ─────────────────────────────────────────────────────────────────────


class SkCoder:
    """Hybrid BM25 + cosine retrieval with low-confidence fallback.

    Args:
        corpus: list of dicts with at least ``source`` (string code).
                Optional fields: ``source_path``, ``language``,
                ``embedding`` (precomputed).
        embedder: optional async or sync vector embedder.  Defaults
                  to ``striatum.hash_embedder`` for fully local /
                  test-friendly behaviour.
        alpha_floor: minimum hybrid score to include a snippet
                     (default 0.35).  Below this we return an empty
                     list plus a ``<FILL_HERE>`` hint.
        beta: BM25/cosine mix (1.0 = pure BM25, 0.0 = pure cosine).
        top_k: cap on returned snippets.
    """

    FILL_HERE_PREFIX = "<FILL_HERE:"

    def __init__(
        self,
        *,
        corpus: list[dict] | None = None,
        embedder: Embedder | None = None,
        alpha_floor: float = 0.35,
        beta: float = 0.5,
        top_k: int = 5,
    ) -> None:
        self._corpus_raw: list[dict] = list(corpus or [])
        self._embedder: Embedder = embedder or hash_embedder
        self._alpha_floor = max(0.0, min(1.0, float(alpha_floor)))
        self._beta = max(0.0, min(1.0, float(beta)))
        self._top_k = max(1, int(top_k))

        # Pre-tokenise + pre-embed once at construction.
        self._tokenised: list[list[str]] = []
        self._embeddings: list[Sequence[float]] = []
        for row in self._corpus_raw:
            text = self._row_text(row)
            self._tokenised.append(_tokenise(text))
            emb = row.get("embedding")
            if not emb:
                emb = hash_embedder(text)  # deterministic, sync
            self._embeddings.append(list(emb))
        self._bm25 = _BM25(self._tokenised) if self._tokenised else None

    # ─── Public API ─────────────────────────────────────────────────

    async def retrieve(self, ir: TaskIR) -> list[CodeSnippet]:
        """Return the top-K snippets above the α floor.  Empty list
        means *no good match*; the caller should fall through to
        ``retrieve_or_hint`` if it wants a placeholder."""
        snippets, _hint = await self.retrieve_or_hint(ir)
        return snippets

    async def retrieve_or_hint(
        self, ir: TaskIR
    ) -> tuple[list[CodeSnippet], str | None]:
        """Same as ``retrieve`` but also returns a ``<FILL_HERE:...>``
        hint when the best match is below the α floor.  Hint is
        ``None`` when the floor is met."""
        if not self._tokenised or self._bm25 is None:
            return [], self._fill_hint(ir, reason="empty corpus")

        prompt = (ir.prompt or "").strip()
        if not prompt:
            return [], self._fill_hint(ir, reason="empty prompt")

        # BM25 scores, normalised to [0, 1].
        query_tokens = _tokenise(prompt)
        bm25_raw = self._bm25.all_scores(query_tokens)
        bm25_max = max(bm25_raw) if bm25_raw else 0.0
        bm25 = [s / bm25_max if bm25_max > 0 else 0.0 for s in bm25_raw]

        # Cosine scores, clamped to [0, 1].
        try:
            res = self._embedder(prompt)
            if hasattr(res, "__await__"):
                res = await res  # type: ignore[assignment]
            query_vec = list(res or [])
        except Exception as exc:  # pragma: no cover - infra
            logger.debug("sk_coder embed failed: %s", exc)
            query_vec = []
        cos = [
            max(0.0, min(1.0, cosine(query_vec, e)))
            for e in self._embeddings
        ]

        # Hybrid α
        alpha = [
            self._beta * bm25[i] + (1.0 - self._beta) * cos[i]
            for i in range(len(bm25))
        ]
        ranked = sorted(
            range(len(alpha)),
            key=lambda i: alpha[i],
            reverse=True,
        )[: self._top_k]

        best = alpha[ranked[0]] if ranked else 0.0
        if best < self._alpha_floor:
            return [], self._fill_hint(
                ir,
                reason=f"alpha={best:.2f} below floor {self._alpha_floor:.2f}",
            )

        snippets: list[CodeSnippet] = []
        for i in ranked:
            row = self._corpus_raw[i]
            snippets.append(
                CodeSnippet(
                    source=str(row.get("source") or ""),
                    score=float(round(alpha[i], 4)),
                    source_path=str(row.get("source_path") or ""),
                    language=str(row.get("language") or "python"),
                    bm25_score=float(round(bm25[i], 4)),
                    cosine_score=float(round(cos[i], 4)),
                )
            )
        return snippets, None

    def __len__(self) -> int:
        return len(self._corpus_raw)

    # ─── Internals ──────────────────────────────────────────────────

    def _row_text(self, row: dict) -> str:
        # Prefer explicit "text" if provided (lets the corpus author
        # add a natural-language summary), otherwise concatenate
        # source + summary so BM25 sees both.
        if row.get("text"):
            return str(row["text"])
        bits: list[str] = []
        if row.get("summary"):
            bits.append(str(row["summary"]))
        if row.get("source"):
            bits.append(str(row["source"]))
        return "\n".join(bits)

    def _fill_hint(self, ir: TaskIR, *, reason: str) -> str:
        # Keep the hint short but specific enough that the coder
        # template can ask the LLM to fill in a focused gap.
        stripped = (ir.prompt or "").strip()
        head = stripped.splitlines()[0][:160] if stripped else "<unspecified>"
        return f"{self.FILL_HERE_PREFIX} {head} ({reason})>"


__all__ = ["SkCoder"]
