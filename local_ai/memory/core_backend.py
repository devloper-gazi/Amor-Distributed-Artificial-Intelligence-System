"""
Core memory tier — Phase 16 Commit F.

The core tier is the always-in-context slice every Sentinel /
Consortium agent sees on every call.  Capped at ~2 KB so it can
sit at the top of the prompt without crowding out the user
question.

Storage shape: a single-row SQLite table per ``MemoryStore``
instance, keyed by ``scope`` (default ``"default"``) so a process
running multiple personas can keep their core scratchpads
separate.

License: MIT.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_core (
    scope        TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at   REAL NOT NULL,
    bytes        INTEGER NOT NULL
);
"""


class CoreMemoryBackend:
    """Single-row JSON blob, byte-capped, per scope."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_bytes: int = 2048,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    # ─── public API ─────────────────────────────────────────────

    def read(self, scope: str = "default") -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM memory_core WHERE scope = ?",
                (scope,),
            ).fetchone()
            if row is None:
                return {}
            try:
                return dict(json.loads(row[0]))
            except Exception:
                return {}

    def write(
        self,
        payload: dict[str, Any],
        *,
        scope: str = "default",
    ) -> tuple[bool, int]:
        """Persist ``payload``.  Returns ``(ok, bytes)``.  When the
        serialised payload exceeds ``max_bytes`` the write is
        rejected (``ok=False``) and the existing row is left
        untouched — a future LRU/compaction pass is up to the
        orchestrator."""
        text = json.dumps(payload, separators=(",", ":"), default=str)
        size = len(text.encode("utf-8"))
        if size > self.max_bytes:
            return (False, size)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_core "
                "(scope, payload_json, updated_at, bytes) VALUES (?, ?, ?, ?)",
                (scope, text, time.time(), size),
            )
            conn.commit()
        return (True, size)

    def patch(
        self,
        updates: dict[str, Any],
        *,
        scope: str = "default",
    ) -> tuple[bool, int]:
        """Read-modify-write helper — merges ``updates`` into the
        current core blob and rewrites.  Atomic per-scope (held
        under the instance lock)."""
        with self._lock:
            current = self.read(scope=scope)
            current.update(updates)
            return self.write(current, scope=scope)

    def clear(self, scope: str = "default") -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM memory_core WHERE scope = ?", (scope,))
            conn.commit()

    # ─── helpers ────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


__all__ = ["CoreMemoryBackend"]
