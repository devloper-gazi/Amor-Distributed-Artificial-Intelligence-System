"""
Archival memory tier — Phase 16 Commit F.

The archival tier is the long-term, vector-indexed scratchpad.
Phase 16 ships a SQLite-backed implementation with optional
embedding-based ranking; LanceDB / EpisodicMemoryStore wiring is
deferred until the first integration that needs it (Letta /
Code Intelligence MCP-tool flow).

Without an embedder injected, ``search`` falls back to a
substring-match ranking by recency.

License: MIT.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_archival (
    rowid          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope          TEXT    NOT NULL,
    text           TEXT    NOT NULL,
    metadata_json  TEXT,
    embedding_json TEXT,
    inserted_at    REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS memory_archival_scope_idx
    ON memory_archival(scope, inserted_at);
"""


# Embedder protocol — accepts (str | list[str]) returning list[list[float]].
EmbedderFn = Callable[[Any], Awaitable[list[list[float]]]]


@dataclass
class ArchivalEntry:
    rowid: int
    scope: str
    text: str
    metadata: dict[str, Any]
    score: float = 0.0
    inserted_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rowid": self.rowid,
            "scope": self.scope,
            "text": self.text,
            "metadata": dict(self.metadata or {}),
            "score": round(self.score, 4),
            "inserted_at": self.inserted_at,
        }


class ArchivalMemoryBackend:
    """Long-term archive.  Optional embedder injection lifts search
    quality; without one we fall back to substring + recency."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        embedder: Optional[EmbedderFn] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    # ─── public API ─────────────────────────────────────────────

    async def archive(
        self,
        text: str,
        *,
        scope: str = "default",
        metadata: dict[str, Any] | None = None,
    ) -> ArchivalEntry:
        ts = time.time()
        emb_json: Optional[str] = None
        if self._embedder is not None:
            try:
                vectors = await self._embedder(text)
                if vectors:
                    emb_json = json.dumps([float(v) for v in vectors[0]])
            except Exception:
                emb_json = None
        meta_json = (
            json.dumps(metadata, default=str) if metadata else None
        )
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO memory_archival "
                "(scope, text, metadata_json, embedding_json, inserted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope, text, meta_json, emb_json, ts),
            )
            rowid = cur.lastrowid
            conn.commit()
        return ArchivalEntry(
            rowid=int(rowid or 0),
            scope=scope, text=text, metadata=dict(metadata or {}),
            score=1.0, inserted_at=ts,
        )

    async def search(
        self,
        query: str,
        *,
        scope: str = "default",
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[ArchivalEntry]:
        """Vector similarity (when embedder set) + substring fallback.
        Returns top-``limit`` entries above ``min_score``."""
        if not query:
            return []

        # Try embedding-based first.
        if self._embedder is not None:
            try:
                qvecs = await self._embedder(query)
                qvec = qvecs[0] if qvecs else None
            except Exception:
                qvec = None
            if qvec:
                return self._vector_search(qvec, scope, limit, min_score)

        # Fallback — substring match ranked by recency.
        return self._substring_search(query, scope, limit)

    def count(self, *, scope: str = "default") -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_archival WHERE scope = ?",
                (scope,),
            ).fetchone()
            return int(row[0]) if row else 0

    def clear(self, *, scope: str = "default") -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM memory_archival WHERE scope = ?", (scope,),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def set_embedder(self, embedder: Optional[EmbedderFn]) -> None:
        self._embedder = embedder

    # ─── helpers ────────────────────────────────────────────────

    def _vector_search(
        self,
        qvec: list[float],
        scope: str,
        limit: int,
        min_score: float,
    ) -> list[ArchivalEntry]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT rowid, scope, text, metadata_json, "
                "embedding_json, inserted_at FROM memory_archival "
                "WHERE scope = ?",
                (scope,),
            ).fetchall()
        scored: list[tuple[float, sqlite3.Row]] = []
        for r in rows:
            emb_json = r[4]
            if not emb_json:
                continue
            try:
                vec = json.loads(emb_json)
            except Exception:
                continue
            score = _cosine(qvec, vec)
            if score < min_score:
                continue
            scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        out: list[ArchivalEntry] = []
        for score, r in scored[:limit]:
            try:
                meta = json.loads(r[3]) if r[3] else {}
            except Exception:
                meta = {}
            out.append(ArchivalEntry(
                rowid=r[0], scope=r[1], text=r[2],
                metadata=meta, score=score, inserted_at=r[5],
            ))
        return out

    def _substring_search(
        self, query: str, scope: str, limit: int,
    ) -> list[ArchivalEntry]:
        like = f"%{query}%"
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT rowid, scope, text, metadata_json, "
                "embedding_json, inserted_at FROM memory_archival "
                "WHERE scope = ? AND text LIKE ? "
                "ORDER BY inserted_at DESC LIMIT ?",
                (scope, like, int(limit)),
            ).fetchall()
        out: list[ArchivalEntry] = []
        # Score by inverse rank (recency proxy).
        for i, r in enumerate(rows):
            try:
                meta = json.loads(r[3]) if r[3] else {}
            except Exception:
                meta = {}
            out.append(ArchivalEntry(
                rowid=r[0], scope=r[1], text=r[2],
                metadata=meta,
                score=max(0.0, 1.0 - (i / max(1, len(rows)))),
                inserted_at=r[5],
            ))
        return out

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


__all__ = ["ArchivalEntry", "ArchivalMemoryBackend", "EmbedderFn"]
