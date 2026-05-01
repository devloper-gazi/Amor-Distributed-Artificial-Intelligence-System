"""
CodeCorpusRAG — retrieves top-K reference patterns from a vector
store and formats them for injection into specialist system prompts.

Vector store contract (so tests can shim without LanceDB):

    class VectorStore(Protocol):
        async def search(query_vec: list[float], k: int) -> list[dict]:
            # each dict has at least: pattern, code, complexity_claim, summary, score (cosine)

The real LanceDBVectorStore wrapper lives in ``local_ai/vector_store/``;
this module accepts any object with a `search` method, so tests pass
an in-memory list-shim with a deterministic ``search``.

Retrieval: embed user prompt via the injected embedder → top-K from
store → filter by similarity floor → return list[Pattern].

Format: "PROVEN PATTERNS — RIFF, DON'T COPY. Cite by pattern name in
your rationale." prefix + per-pattern summary + claimed complexity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)


Embedder = Callable[[str], Awaitable[Sequence[float]] | Sequence[float]]


@dataclass
class CorpusPattern:
    """One retrieved corpus row."""

    pattern: str
    code: str = ""
    complexity_claim: str = ""
    summary: str = ""
    score: float = 0.0
    topic: str = ""
    language: str = "python"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CorpusPattern":
        return cls(
            pattern=str(d.get("pattern") or ""),
            code=str(d.get("code") or ""),
            complexity_claim=str(d.get("complexity_claim") or ""),
            summary=str(d.get("summary") or ""),
            score=float(d.get("score") or 0.0),
            topic=str(d.get("topic") or ""),
            language=str(d.get("language") or "python"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "code": self.code,
            "complexity_claim": self.complexity_claim,
            "summary": self.summary,
            "score": round(self.score, 4),
            "topic": self.topic,
            "language": self.language,
        }


@dataclass
class RetrievalResult:
    patterns: list[CorpusPattern] = field(default_factory=list)
    failed: bool = False
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "patterns": [p.to_dict() for p in self.patterns],
            "failed": self.failed,
            "failure_reason": self.failure_reason,
        }


class CodeCorpusRAG:
    """Retrieve + format reference patterns for the reasoner mesh."""

    def __init__(
        self,
        *,
        vector_store: Any,
        embedder: Embedder,
        top_k: int = 3,
        similarity_floor: float = 0.55,
    ) -> None:
        self._store = vector_store
        self._embed = embedder
        self._top_k = max(1, int(top_k))
        self._floor = max(0.0, min(1.0, float(similarity_floor)))

    async def retrieve(self, user_prompt: str) -> RetrievalResult:
        if not (user_prompt or "").strip():
            return RetrievalResult(
                failed=True, failure_reason="empty user prompt",
            )
        try:
            res = self._embed(user_prompt)
            if hasattr(res, "__await__"):
                res = await res  # type: ignore[assignment]
            embedding = list(res or [])
        except Exception as exc:
            logger.debug("rag_embed_failed: %s", exc)
            return RetrievalResult(
                failed=True, failure_reason=f"embed failed: {exc}"[:200],
            )
        if not embedding:
            return RetrievalResult(
                failed=True, failure_reason="empty embedding",
            )

        try:
            raw = self._store.search(embedding, self._top_k)
            if hasattr(raw, "__await__"):
                raw = await raw  # type: ignore[assignment]
            rows = list(raw or [])
        except Exception as exc:
            logger.debug("rag_search_failed: %s", exc)
            return RetrievalResult(
                failed=True, failure_reason=f"search failed: {exc}"[:200],
            )

        patterns: list[CorpusPattern] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            p = CorpusPattern.from_dict(row)
            if p.score >= self._floor and p.pattern:
                patterns.append(p)

        return RetrievalResult(patterns=patterns)

    @staticmethod
    def format_for_prompt(patterns: list[CorpusPattern]) -> str:
        """Render the retrieved patterns as a system-prompt-injectable
        reference block. The deliberate "RIFF, DON'T COPY" framing is
        an anti-overfit measure."""
        if not patterns:
            return ""
        rows: list[str] = [
            "PROVEN PATTERNS — RIFF, DON'T COPY. "
            "Cite by pattern name in your rationale.",
        ]
        for p in patterns:
            rows.append(
                f"\n## Pattern: {p.pattern}  (complexity: {p.complexity_claim or '?'})"
            )
            if p.summary:
                rows.append(p.summary[:300])
            if p.code:
                rows.append("```python")
                rows.append(p.code[:600])
                rows.append("```")
        return "\n".join(rows)
