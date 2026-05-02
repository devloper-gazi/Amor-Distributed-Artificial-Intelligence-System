"""
Late chunking — Phase 16 Commit D2.

Late-chunking (Jina, arXiv:2409.04701) embeds the *full* document
context first and propagates the global signal into each chunk so
small chunks carry the document's semantic neighbourhood.  The
canonical implementation (token-level pooling over a wide context
window) requires direct access to the encoder's per-token output,
which sentence-transformers doesn't expose for arbitrary checkpoints.

Phase 16 ships a pragmatic approximation that's both effective and
embedder-agnostic:

* ``LateChunker.chunk_with_context(text)`` returns
  ``LateChunk`` records — each carries the chunk text, the
  document's leading window (``contextual_text``), the
  byte-offset span, and an explicit ``contextual_payload``
  string the caller embeds in lieu of the bare chunk.
* The vector store layer can then call
  ``embedder.encode(chunk.contextual_payload)`` and store the
  resulting vector under the chunk's ID — same wire shape as the
  naive path, just better recall on short chunks.

When the upstream embedder gains true late-chunking support
(BGE-M3 with the ``--use-late-chunking`` flag, nomic-embed-text-v2,
…) the caller can swap to the model-native API; the dataclass
remains the same.

License: MIT.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional


_SENTENCE_TERMINATORS = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")


@dataclass
class LateChunk:
    """A single chunk with its document context payload attached."""

    text: str
    contextual_text: str          # the leading-window summary
    contextual_payload: str       # what the caller embeds
    start: int                    # byte offset in source
    end: int                      # byte offset in source (exclusive)
    chunk_index: int              # zero-based index in the document


class LateChunker:
    """Chunk a document and attach a leading-window context payload
    to each chunk for late-chunking-style embedding.

    Args:
        chunk_size: Target chunk size in characters.
        overlap: Character overlap between adjacent chunks.
        window_chars: Maximum length of the document context that
            gets prepended to each chunk's payload.  Defaults to
            ``settings.rag_late_chunking_window`` (8192).
        snap_to_sentence: If True, attempt to break on sentence
            boundaries within ±``snap_window`` chars of ``chunk_size``.
        snap_window: Characters of slack to find a sentence boundary.
        context_template: ``str.format`` template applied to each
            ``(contextual_text, chunk_text)`` pair.  Default places
            the context first, separated by a blank line.
    """

    DEFAULT_TEMPLATE = "{context}\n\n{chunk}"

    def __init__(
        self,
        *,
        chunk_size: int = 1000,
        overlap: int = 200,
        window_chars: Optional[int] = None,
        snap_to_sentence: bool = True,
        snap_window: int = 200,
        context_template: Optional[str] = None,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if overlap < 0 or overlap >= chunk_size:
            raise ValueError("overlap must be in [0, chunk_size)")
        self.chunk_size = int(chunk_size)
        self.overlap = int(overlap)
        self.window_chars = (
            int(window_chars)
            if window_chars is not None
            else self._settings_value("rag_late_chunking_window", 8192)
        )
        self.snap_to_sentence = bool(snap_to_sentence)
        self.snap_window = int(snap_window)
        self.context_template = context_template or self.DEFAULT_TEMPLATE

    # ─── public API ────────────────────────────────────────────

    def chunk_with_context(self, text: str) -> List[LateChunk]:
        """Split ``text`` and attach the leading-window context to
        every chunk's ``contextual_payload``.  Returns an empty list
        on empty / whitespace-only input."""
        if text is None:
            return []
        body = str(text)
        if not body.strip():
            return []

        context = self._derive_context(body)
        chunks = self._split(body)
        out: List[LateChunk] = []
        for i, (start, end) in enumerate(chunks):
            chunk_text = body[start:end]
            payload = self.context_template.format(
                context=context, chunk=chunk_text,
            )
            out.append(LateChunk(
                text=chunk_text,
                contextual_text=context,
                contextual_payload=payload,
                start=start,
                end=end,
                chunk_index=i,
            ))
        return out

    # ─── helpers ───────────────────────────────────────────────

    def _derive_context(self, text: str) -> str:
        """Leading window — first ``window_chars`` of the document.
        We snap on a sentence boundary within the last 200 chars so
        the context never ends mid-word."""
        if len(text) <= self.window_chars:
            return text
        window = text[: self.window_chars]
        if not self.snap_to_sentence:
            return window
        # Find the last sentence terminator inside the window.
        matches = list(_SENTENCE_TERMINATORS.finditer(window))
        if matches:
            return window[: matches[-1].end()]
        return window

    def _split(self, text: str) -> List[tuple[int, int]]:
        """Char-window chunks with optional sentence-boundary snap.
        Returns ``[(start, end), …]``."""
        spans: List[tuple[int, int]] = []
        i = 0
        n = len(text)
        while i < n:
            target = min(i + self.chunk_size, n)
            end = target
            if self.snap_to_sentence and target < n:
                window_lo = max(target - self.snap_window, i + 1)
                window_hi = min(target + self.snap_window, n)
                snippet = text[window_lo:window_hi]
                matches = list(_SENTENCE_TERMINATORS.finditer(snippet))
                if matches:
                    # Pick the boundary closest to ``target``.
                    best = min(
                        matches,
                        key=lambda m: abs(window_lo + m.end() - target),
                    )
                    end = window_lo + best.end()
            spans.append((i, end))
            if end >= n:
                break
            i = max(end - self.overlap, i + 1)
        return spans

    @staticmethod
    def _settings_value(name: str, default: int) -> int:
        try:
            from document_processor.config.settings import (  # noqa: PLC0415
                settings as _s,
            )
            v = getattr(_s, name, default)
            return int(v) if v else default
        except Exception:
            return default


def iter_payloads(chunks: Iterable[LateChunk]) -> List[str]:
    """Convenience: extract the ``contextual_payload`` of each
    chunk so callers can hand the list straight to
    ``embedder.encode``."""
    return [c.contextual_payload for c in chunks]


__all__ = ["LateChunk", "LateChunker", "iter_payloads"]
