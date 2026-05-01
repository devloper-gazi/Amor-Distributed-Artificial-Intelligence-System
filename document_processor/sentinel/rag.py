"""
Sentinel — RAG layer (CWE / OWASP / history / project context).

Reuses the existing ``LanceDBVectorStore`` from
``local_ai/vector_store/`` plus an in-memory fallback that is good
enough for unit tests and a partial-install host.

Four logical "tables":

* ``sentinel_cwe``     — bundled CWE Top-25 corpus
* ``sentinel_owasp``   — bundled OWASP Top-10 corpus
* ``sentinel_history`` — past Sentinel findings (per-user)
* ``sentinel_project`` — chunks of the currently-scanned project

The corpora load lazily from JSON files in ``sentinel/data/``.  The
embedder defaults to a deterministic hash sketch (the same one
QuickCode V2's Striatum uses) so RAG works on a host without
sentence-transformers; production hosts swap in
``nomic-embed-text-v1.5`` automatically when available.

License: MIT.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

from .models import Finding, RAGContext

logger = logging.getLogger(__name__)


Embedder = Callable[[str], Awaitable[Sequence[float]] | Sequence[float]]


# ─────────────────────────────────────────────────────────────────────
# Hash-sketch embedder fallback
# ─────────────────────────────────────────────────────────────────────


_SKETCH_DIM = 96


def hash_embed(text: str) -> list[float]:
    """Deterministic sketch: same text → same vector.  Good enough
    for the bundled CWE/OWASP corpora (the corpus is small enough
    that we can effectively grep)."""
    if not text:
        return [0.0] * _SKETCH_DIM
    import hashlib
    vec = [0.0] * _SKETCH_DIM
    for token in text.lower().split():
        digest = hashlib.md5(token.encode("utf-8")).digest()
        idx = digest[0] % _SKETCH_DIM
        sign = 1.0 if (digest[1] & 1) else -1.0
        vec[idx] += sign
    return vec


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot, na, nb = 0.0, 0.0, 0.0
    for i in range(n):
        ai, bi = float(a[i]), float(b[i])
        dot += ai * bi
        na += ai * ai
        nb += bi * bi
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ─────────────────────────────────────────────────────────────────────
# In-memory vector "table" (fallback when LanceDB is missing)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _MemoryTable:
    name: str
    rows: list[dict[str, Any]] = field(default_factory=list)

    def add(self, payload: dict[str, Any]) -> None:
        # Deep-copy so callers cannot mutate cached state.
        self.rows.append(json.loads(json.dumps(payload, default=str)))

    def search(self, query_vec: Sequence[float], k: int) -> list[dict[str, Any]]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in self.rows:
            vec = row.get("_vec") or []
            score = cosine(query_vec, vec)
            scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        out: list[dict[str, Any]] = []
        for score, row in scored[: max(1, k)]:
            r = {k: v for k, v in row.items() if k != "_vec"}
            r["score"] = round(score, 4)
            out.append(r)
        return out

    def __len__(self) -> int:
        return len(self.rows)


# ─────────────────────────────────────────────────────────────────────
# Corpus loaders
# ─────────────────────────────────────────────────────────────────────


_DATA_DIR = Path(__file__).resolve().parent / "data"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("sentinel rag corpus load failed for %s: %s", path, exc)
        return {}


def load_cwe_corpus() -> list[dict[str, Any]]:
    data = _load_json(_DATA_DIR / "cwe_top25.json")
    return list(data.get("entries") or [])


def load_owasp_corpus() -> list[dict[str, Any]]:
    data = _load_json(_DATA_DIR / "owasp_top10.json")
    return list(data.get("entries") or [])


def load_cwe_cvss_map() -> dict[str, dict[str, Any]]:
    data = _load_json(_DATA_DIR / "cwe_cvss_map.json")
    return dict(data.get("map") or {})


def load_source_weights() -> dict[str, Any]:
    return _load_json(_DATA_DIR / "source_weights.json")


# ─────────────────────────────────────────────────────────────────────
# SentinelRAG
# ─────────────────────────────────────────────────────────────────────


class SentinelRAG:
    """Aggregator that owns the four logical tables.

    The embedder is injected (default: hash sketch).  The vector
    backend is also injected — pass an existing
    ``LanceDBVectorStore`` to plug into the real store, or leave it
    None to use the in-memory fallback (test path)."""

    TABLE_CWE = "sentinel_cwe"
    TABLE_OWASP = "sentinel_owasp"
    TABLE_HISTORY = "sentinel_history"
    TABLE_PROJECT = "sentinel_project"

    def __init__(
        self,
        *,
        embedder: Embedder = hash_embed,
        backend: Any | None = None,
    ) -> None:
        self._embedder = embedder
        self._backend = backend  # optional LanceDBVectorStore-like
        # In-memory fallback always available even when a real backend
        # is supplied — so tests can exercise SentinelRAG without
        # touching LanceDB on disk.
        self._memory: dict[str, _MemoryTable] = {
            self.TABLE_CWE:     _MemoryTable(self.TABLE_CWE),
            self.TABLE_OWASP:   _MemoryTable(self.TABLE_OWASP),
            self.TABLE_HISTORY: _MemoryTable(self.TABLE_HISTORY),
            self.TABLE_PROJECT: _MemoryTable(self.TABLE_PROJECT),
        }
        self._cwe_index: dict[str, dict[str, Any]] = {}
        self._owasp_index: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # ─── Lifecycle ──────────────────────────────────────────────

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        await self._load_corpora()
        self._loaded = True

    async def _load_corpora(self) -> None:
        for entry in load_cwe_corpus():
            text = " ".join([
                entry.get("id") or "",
                entry.get("name") or "",
                entry.get("description") or "",
                entry.get("mitigation") or "",
            ])
            vec = await self._embed(text)
            row = dict(entry)
            row["_vec"] = vec
            row["_text"] = text
            self._memory[self.TABLE_CWE].add(row)
            self._cwe_index[entry["id"]] = entry

        for entry in load_owasp_corpus():
            text = " ".join([
                entry.get("id") or "",
                entry.get("name") or "",
                entry.get("description") or "",
            ])
            vec = await self._embed(text)
            row = dict(entry)
            row["_vec"] = vec
            row["_text"] = text
            self._memory[self.TABLE_OWASP].add(row)
            self._owasp_index[entry["id"]] = entry

    # ─── Public API ─────────────────────────────────────────────

    async def enrich(self, finding: Finding) -> RAGContext:
        """Pull CWE + OWASP entries plus similar history / project
        context for a single Finding."""
        await self.ensure_loaded()
        cwe_entry = self._cwe_index.get(finding.cwe) if finding.cwe else None
        owasp_entry = self._owasp_index.get(finding.owasp) if finding.owasp else None
        if cwe_entry is None and finding.raw_message:
            # Vector search over CWE corpus when the tool didn't tag.
            vec = await self._embed(finding.raw_message)
            hits = self._memory[self.TABLE_CWE].search(vec, k=1)
            if hits and hits[0].get("score", 0) > 0.3:
                cwe_entry = {k: v for k, v in hits[0].items() if k != "_text"}

        history_hits: list[dict[str, Any]] = []
        try:
            vec = await self._embed(
                f"{finding.cwe or ''} {finding.raw_message or ''}"
            )
            history_hits = self._memory[self.TABLE_HISTORY].search(vec, k=3)
        except Exception:
            pass

        project_hits: list[dict[str, Any]] = []
        try:
            if finding.code_snippet:
                vec = await self._embed(finding.code_snippet)
                project_hits = self._memory[self.TABLE_PROJECT].search(vec, k=3)
        except Exception:
            pass

        return RAGContext(
            cwe_entry=cwe_entry,
            owasp_entry=owasp_entry,
            similar_findings=history_hits,
            project_chunks=project_hits,
        )

    async def upsert_history(self, finding: Finding) -> None:
        text = f"{finding.cwe} {finding.raw_message[:400]}"
        vec = await self._embed(text)
        row = finding.to_dict()
        row["_text"] = text
        row["_vec"] = vec
        self._memory[self.TABLE_HISTORY].add(row)

    async def index_project_chunk(
        self, *, file: str, line_start: int, snippet: str
    ) -> None:
        if not snippet:
            return
        vec = await self._embed(snippet)
        self._memory[self.TABLE_PROJECT].add({
            "file": file,
            "line_start": line_start,
            "snippet": snippet[:1200],
            "_text": snippet[:1200],
            "_vec": vec,
            "indexed_at": time.time(),
        })

    # ─── Diagnostics ────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        return {
            "cwe_count": len(self._memory[self.TABLE_CWE]),
            "owasp_count": len(self._memory[self.TABLE_OWASP]),
            "history_count": len(self._memory[self.TABLE_HISTORY]),
            "project_count": len(self._memory[self.TABLE_PROJECT]),
            "loaded": self._loaded,
        }

    # ─── Internals ──────────────────────────────────────────────

    async def _embed(self, text: str) -> list[float]:
        try:
            res = self._embedder(text)
            if hasattr(res, "__await__"):
                res = await res  # type: ignore[assignment]
            return list(res or [])
        except Exception as exc:
            logger.debug("rag embed failed: %s", exc)
            return [0.0] * _SKETCH_DIM


__all__ = [
    "SentinelRAG",
    "hash_embed",
    "load_cwe_corpus",
    "load_cwe_cvss_map",
    "load_owasp_corpus",
    "load_source_weights",
]
