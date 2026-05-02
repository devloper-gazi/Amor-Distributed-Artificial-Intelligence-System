"""
Recall memory tier — Phase 16 Commit F.

The recall tier is the recent-window of conversation turns / agent
events.  Backed by a SQLite ring buffer so reads are O(window) and
writes are O(1).  Trimming runs on every insert; capped by
``window_size`` (default 50, see ``settings.memory_recall_window``).

License: MIT.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_recall (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    metadata_json TEXT,
    inserted_at REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS memory_recall_scope_idx
    ON memory_recall(scope, inserted_at);
"""


@dataclass
class RecallEntry:
    rowid: int
    scope: str
    role: str
    content: str
    metadata: dict[str, Any]
    inserted_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "rowid": self.rowid,
            "scope": self.scope,
            "role": self.role,
            "content": self.content,
            "metadata": dict(self.metadata or {}),
            "inserted_at": self.inserted_at,
        }


class RecallMemoryBackend:
    """Per-scope ring buffer."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        window_size: int = 50,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.window_size = int(window_size)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    # ─── public API ─────────────────────────────────────────────

    def append(
        self,
        role: str,
        content: str,
        *,
        scope: str = "default",
        metadata: dict[str, Any] | None = None,
    ) -> RecallEntry:
        meta_json = (
            json.dumps(metadata, default=str) if metadata else None
        )
        ts = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO memory_recall "
                "(scope, role, content, metadata_json, inserted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope, role, content, meta_json, ts),
            )
            rowid = cur.lastrowid
            self._trim_locked(conn, scope)
            conn.commit()
        return RecallEntry(
            rowid=int(rowid or 0),
            scope=scope, role=role, content=content,
            metadata=dict(metadata or {}), inserted_at=ts,
        )

    def latest(
        self,
        n: int | None = None,
        *,
        scope: str = "default",
    ) -> list[RecallEntry]:
        n = self.window_size if n is None else int(n)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT rowid, scope, role, content, metadata_json, "
                "inserted_at FROM memory_recall WHERE scope = ? "
                "ORDER BY inserted_at DESC LIMIT ?",
                (scope, n),
            ).fetchall()
        out: list[RecallEntry] = []
        for r in rows:
            try:
                meta = json.loads(r[4]) if r[4] else {}
            except Exception:
                meta = {}
            out.append(RecallEntry(
                rowid=r[0], scope=r[1], role=r[2], content=r[3],
                metadata=meta, inserted_at=r[5],
            ))
        # Caller usually wants chronological order — newest last.
        out.reverse()
        return out

    def search(
        self,
        query: str,
        *,
        scope: str = "default",
        limit: int = 20,
    ) -> list[RecallEntry]:
        """Substring match.  Recall is small enough that BM25 is
        overkill; archival tier handles the heavy lifting."""
        if not query:
            return []
        like = f"%{query}%"
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT rowid, scope, role, content, metadata_json, "
                "inserted_at FROM memory_recall WHERE scope = ? "
                "AND (content LIKE ? OR role LIKE ?) "
                "ORDER BY inserted_at DESC LIMIT ?",
                (scope, like, like, int(limit)),
            ).fetchall()
        out: list[RecallEntry] = []
        for r in rows:
            try:
                meta = json.loads(r[4]) if r[4] else {}
            except Exception:
                meta = {}
            out.append(RecallEntry(
                rowid=r[0], scope=r[1], role=r[2], content=r[3],
                metadata=meta, inserted_at=r[5],
            ))
        return out

    def clear(self, scope: str = "default") -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM memory_recall WHERE scope = ?", (scope,),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def count(self, *, scope: str = "default") -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_recall WHERE scope = ?",
                (scope,),
            ).fetchone()
            return int(row[0]) if row else 0

    # ─── helpers ────────────────────────────────────────────────

    def _trim_locked(
        self, conn: sqlite3.Connection, scope: str,
    ) -> None:
        """Drop oldest entries beyond ``window_size`` for ``scope``."""
        row = conn.execute(
            "SELECT COUNT(*) FROM memory_recall WHERE scope = ?",
            (scope,),
        ).fetchone()
        excess = max(0, int(row[0]) - self.window_size)
        if excess <= 0:
            return
        conn.execute(
            "DELETE FROM memory_recall WHERE rowid IN ("
            "SELECT rowid FROM memory_recall WHERE scope = ? "
            "ORDER BY inserted_at ASC LIMIT ?)",
            (scope, excess),
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


__all__ = ["RecallEntry", "RecallMemoryBackend"]
