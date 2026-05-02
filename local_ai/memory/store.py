"""
MemoryStore — Phase 16 Commit F.

Letta / MemGPT-inspired three-tier memory hierarchy:

* **Core** (~2 KB, always-in-context) — agent identity, persona,
  current task.  ``CoreMemoryBackend`` (single-row SQLite blob).
* **Recall** (last N=50 messages, recent-window) —
  ``RecallMemoryBackend`` (SQLite ring buffer).
* **Archival** (long-term, vector-indexed) —
  ``ArchivalMemoryBackend`` (SQLite + optional embedder).

Every write optionally appends a ``memory_write`` ledger entry via
the Phase 15 ``LedgerStore`` so the immutable trail covers
conversation history too.

License: MIT.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .archival_backend import (
    ArchivalEntry,
    ArchivalMemoryBackend,
    EmbedderFn,
)
from .core_backend import CoreMemoryBackend
from .recall_backend import RecallEntry, RecallMemoryBackend


logger = logging.getLogger(__name__)


# Ledger-audit hook — accepts (kind, payload) and returns optional id.
LedgerHookFn = Callable[[str, dict], Awaitable[Optional[str]] | Optional[str]]


@dataclass
class MemoryStats:
    core_bytes: int
    recall_count: int
    archival_count: int


class MemoryStore:
    """3-tier memory orchestrator.

    Construct once per agent / session; ``read_core`` / ``write_core``
    / ``append_recall`` / ``search_recall`` / ``archive`` /
    ``search_archival`` are the canonical Letta-pattern surface.
    """

    def __init__(
        self,
        *,
        root: str | Path,
        core_max_bytes: int = 2048,
        recall_window: int = 50,
        embedder: Optional[EmbedderFn] = None,
        scope: str = "default",
        ledger_hook: Optional[LedgerHookFn] = None,
        audit_enabled: bool = True,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.scope = scope
        self.audit_enabled = bool(audit_enabled)
        self._ledger_hook = ledger_hook

        self._core = CoreMemoryBackend(
            self.root / "memory_core.sqlite",
            max_bytes=int(core_max_bytes),
        )
        self._recall = RecallMemoryBackend(
            self.root / "memory_recall.sqlite",
            window_size=int(recall_window),
        )
        self._archival = ArchivalMemoryBackend(
            self.root / "memory_archival.sqlite",
            embedder=embedder,
        )

    # ─── Core tier ──────────────────────────────────────────────

    def read_core(self) -> dict[str, Any]:
        return self._core.read(scope=self.scope)

    async def write_core(
        self, payload: dict[str, Any],
    ) -> tuple[bool, int]:
        ok, size = self._core.write(payload, scope=self.scope)
        await self._maybe_audit(
            "memory_core_written",
            {"scope": self.scope, "ok": ok, "bytes": size},
        )
        return ok, size

    async def patch_core(
        self, updates: dict[str, Any],
    ) -> tuple[bool, int]:
        ok, size = self._core.patch(updates, scope=self.scope)
        await self._maybe_audit(
            "memory_core_patched",
            {"scope": self.scope, "ok": ok, "bytes": size,
             "keys": sorted(updates.keys())},
        )
        return ok, size

    # ─── Recall tier ────────────────────────────────────────────

    async def append_recall(
        self,
        role: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> RecallEntry:
        entry = self._recall.append(
            role, content, scope=self.scope, metadata=metadata,
        )
        await self._maybe_audit(
            "memory_recall_appended",
            {"scope": self.scope, "rowid": entry.rowid, "role": role,
             "bytes": len(content)},
        )
        return entry

    def latest_recall(self, n: Optional[int] = None) -> list[RecallEntry]:
        return self._recall.latest(n, scope=self.scope)

    def search_recall(
        self, query: str, *, limit: int = 20,
    ) -> list[RecallEntry]:
        return self._recall.search(query, scope=self.scope, limit=limit)

    # ─── Archival tier ──────────────────────────────────────────

    async def archive(
        self,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ArchivalEntry:
        entry = await self._archival.archive(
            text, scope=self.scope, metadata=metadata,
        )
        await self._maybe_audit(
            "memory_archival_written",
            {"scope": self.scope, "rowid": entry.rowid, "bytes": len(text)},
        )
        return entry

    async def search_archival(
        self,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[ArchivalEntry]:
        return await self._archival.search(
            query, scope=self.scope, limit=limit, min_score=min_score,
        )

    # ─── Stats + cleanup ───────────────────────────────────────

    def stats(self) -> MemoryStats:
        core = self._core.read(scope=self.scope)
        core_bytes = len(str(core).encode("utf-8")) if core else 0
        return MemoryStats(
            core_bytes=core_bytes,
            recall_count=self._recall.count(scope=self.scope),
            archival_count=self._archival.count(scope=self.scope),
        )

    def clear_all(self) -> None:
        self._core.clear(scope=self.scope)
        self._recall.clear(scope=self.scope)
        self._archival.clear(scope=self.scope)

    def set_embedder(self, embedder: Optional[EmbedderFn]) -> None:
        self._archival.set_embedder(embedder)

    # ─── audit helper ──────────────────────────────────────────

    async def _maybe_audit(self, kind: str, payload: dict) -> None:
        if not self.audit_enabled or self._ledger_hook is None:
            return
        try:
            result = self._ledger_hook(kind, payload)
            if hasattr(result, "__await__"):
                await result  # type: ignore[func-returns-value]
        except Exception as exc:  # pragma: no cover
            logger.debug("memory ledger hook failed: %s", exc)


def make_no_op_store() -> "MemoryStore":
    """Return a stand-in store that uses a temporary directory.

    Useful for the ``_BaseAgent._default_memory()`` lazy fallback
    so agents constructed without an explicit ``memory=`` argument
    still get a functional (process-local) memory hierarchy."""
    import tempfile  # noqa: PLC0415
    tmp = Path(tempfile.mkdtemp(prefix="amor_memory_default_"))
    return MemoryStore(
        root=tmp,
        audit_enabled=False,
    )


__all__ = [
    "MemoryStore",
    "MemoryStats",
    "LedgerHookFn",
    "make_no_op_store",
]
