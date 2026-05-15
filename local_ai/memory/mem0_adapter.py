"""
Cycle C Sprint 7 Day 1 — Mem0 OSS adapter.

Wraps `mem0ai` (the OSS Apache-2.0 release, NOT the hosted commercial
service) so the rest of AMOR's memory layer can target a unified
``Mem0Adapter`` interface regardless of whether ``mem0ai`` is
installed.

Why a thin adapter
------------------
* Mem0 is a heavyweight optional dep — pulling it in unconditionally
  inflates the cold-start image by ~120 MB (sentence-transformers,
  torch, neo4j-driver, ...).  We only want it when the operator
  actually opts in via ``AMOR_MEMORY_BACKEND=mem0``.
* Cycle C plan calls out Mem0's licensing caveat: graph-memory
  features require Neo4j.  Our default config disables those so the
  AMOR stack keeps a single Postgres + LanceDB + SQLite footprint.
* The existing 3-tier ``MemoryStore`` surface (read_core /
  append_recall / archive / search_*) stays the canonical entry
  point.  When Mem0 is enabled, this adapter fans the calls out
  through Mem0's ``add`` / ``search`` API and translates results
  back into the dataclass shapes the rest of the codebase expects.

Configuration (env)
-------------------
* ``AMOR_MEMORY_BACKEND``           — ``"mem0"`` | ``"native"``
                                      (empty/unset → ``"native"``)
* ``AMOR_MEMORY_DIR``               — root for Mem0's history-db /
                                      vector-store paths.  Default:
                                      ``data/amor_memory``.
* ``AMOR_MEMORY_VECTOR_STORE``      — ``"lancedb"`` | ``"qdrant"``
                                      (default ``"lancedb"`` per plan)
* ``AMOR_MEMORY_LLM_BASE_URL``      — OpenAI-compat base for
                                      fact-extraction LLM (defaults
                                      to ``AMOR_LLAMASWAP_URL``)
* ``AMOR_MEMORY_LLM_MODEL``         — model name to use for
                                      extraction (default
                                      ``"amor-architect"``)
* ``AMOR_MEMORY_NEO4J``             — ``"1"`` to enable graph memory
                                      (requires Neo4j).  Default off.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─── feature probe ────────────────────────────────────────────────


_MEM0_INSTALLED: Optional[bool] = None


def mem0_available() -> bool:
    """Cached probe — True iff ``import mem0`` succeeds.

    Checks ``sys.modules`` first so a test that injects a stub
    ``mem0`` module gets recognised; falls back to ``find_spec``
    for the production import-path.
    """
    global _MEM0_INSTALLED  # noqa: PLW0603
    if _MEM0_INSTALLED is not None:
        return _MEM0_INSTALLED
    try:
        import sys as _sys  # noqa: PLC0415
        if "mem0" in _sys.modules:
            _MEM0_INSTALLED = True
            return True
        import importlib  # noqa: PLC0415
        spec = importlib.util.find_spec("mem0")
        _MEM0_INSTALLED = spec is not None
    except Exception:  # pragma: no cover
        _MEM0_INSTALLED = False
    return _MEM0_INSTALLED


def mem0_enabled() -> bool:
    """True when the env asks for mem0 AND mem0 is importable."""
    requested = os.environ.get("AMOR_MEMORY_BACKEND", "").strip().lower()
    return requested == "mem0" and mem0_available()


# ─── dataclasses ───────────────────────────────────────────────────


@dataclass(frozen=True)
class MemoryRecord:
    """One Mem0 memory entry, normalised for AMOR's UI / API."""

    id: str
    user_id: str
    text: str
    score: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class AdapterStatus:
    """What ``GET /api/admin/memory/status`` returns + what the
    "Remembered" pill consults to decide whether to show."""

    backend: str            # "mem0" | "native"
    available: bool         # Mem0 importable / configured
    vector_store: str
    history_db: str
    llm_base_url: Optional[str]
    llm_model: Optional[str]
    graph_enabled: bool
    user_namespace: str


# ─── adapter ───────────────────────────────────────────────────────


class Mem0Adapter:
    """Thin wrapper around ``mem0.Memory``.

    The adapter is *always constructible*.  When mem0 is missing, every
    ``add`` / ``search`` becomes a no-op (returns empty list) — so
    callers can wire the adapter unconditionally and the operator
    flips it on by installing mem0 + setting the env flag.
    """

    def __init__(
        self,
        *,
        user_id: str = "local",
        root: Optional[Path] = None,
        vector_store: str = "lancedb",
        llm_base_url: Optional[str] = None,
        llm_model: str = "amor-architect",
        graph_enabled: bool = False,
    ) -> None:
        self.user_id = user_id
        self.root = root or Path(
            os.environ.get("AMOR_MEMORY_DIR", "data/amor_memory"),
        )
        self.vector_store = vector_store
        self.llm_base_url = llm_base_url or os.environ.get(
            "AMOR_LLAMASWAP_URL", ""
        ) or None
        self.llm_model = llm_model
        self.graph_enabled = graph_enabled
        self._client: Any | None = None
        self._init_error: Optional[str] = None

        if mem0_enabled():
            try:
                self._client = self._build_client()
                logger.info(
                    "Mem0 adapter ready (vector=%s, graph=%s, llm=%s)",
                    self.vector_store,
                    self.graph_enabled,
                    self.llm_model,
                )
            except Exception as exc:  # pragma: no cover — runtime dep
                self._init_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "Mem0 init failed (%s); adapter degraded to no-op",
                    self._init_error,
                )

    # ── construction ──────────────────────────────────────────────

    def _build_client(self) -> Any:
        from mem0 import Memory  # noqa: PLC0415

        self.root.mkdir(parents=True, exist_ok=True)
        history_db = self.root / "mem0_history.sqlite"
        vector_path = self.root / "mem0_lancedb"
        vector_path.mkdir(exist_ok=True)

        config: Dict[str, Any] = {
            "vector_store": {
                "provider": self.vector_store,
                "config": {
                    "collection_name": "amor_memory",
                    # Mem0's lancedb provider takes ``path`` directly.
                    "path": str(vector_path),
                },
            },
            "history_db_path": str(history_db),
        }
        # Plug in our local OpenAI-compat backend for fact extraction
        # (matches the Cycle C plan: amor-architect via OpenAI shim).
        if self.llm_base_url:
            config["llm"] = {
                "provider": "openai",
                "config": {
                    "model": self.llm_model,
                    "openai_base_url": self.llm_base_url.rstrip("/") + "/v1",
                    # Mem0's openai provider reads OPENAI_API_KEY from
                    # env; the local stack doesn't enforce auth, so a
                    # placeholder satisfies the SDK without leaking
                    # anything.
                    "api_key": os.environ.get("OPENAI_API_KEY", "amor-local"),
                },
            }
        # Graph memory is OFF by default — Cycle C caveat: Mem0 graph
        # requires Neo4j which we don't ship.
        if self.graph_enabled:
            config["graph_store"] = {
                "provider": "neo4j",
                "config": {
                    "url": os.environ.get("AMOR_MEMORY_NEO4J_URL", ""),
                    "username": os.environ.get("AMOR_MEMORY_NEO4J_USER", ""),
                    "password": os.environ.get("AMOR_MEMORY_NEO4J_PASSWORD", ""),
                },
            }
        return Memory.from_config(config)

    # ── status ────────────────────────────────────────────────────

    def status(self) -> AdapterStatus:
        return AdapterStatus(
            backend="mem0" if self._client is not None else "native",
            available=self._client is not None,
            vector_store=self.vector_store,
            history_db=str(self.root / "mem0_history.sqlite"),
            llm_base_url=self.llm_base_url,
            llm_model=self.llm_model if self._client is not None else None,
            graph_enabled=self.graph_enabled,
            user_namespace=self.user_id,
        )

    # ── public surface (always callable; no-op when degraded) ────

    def add(
        self,
        messages: List[Dict[str, str]] | str,
        *,
        user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[MemoryRecord]:
        """Persist message(s) as memory.  ``messages`` is either a
        single string (treated as the user's utterance) or the
        chat-style list ``[{"role":"user","content":"..."}]``.
        """
        if self._client is None:
            return []
        uid = user_id or self.user_id
        try:
            result = self._client.add(messages, user_id=uid, metadata=metadata or {})
        except Exception as exc:  # pragma: no cover — runtime
            logger.warning("mem0.add failed: %s", exc)
            return []
        return _normalise(result, fallback_user=uid)

    def search(
        self,
        query: str,
        *,
        user_id: Optional[str] = None,
        limit: int = 5,
    ) -> List[MemoryRecord]:
        if self._client is None:
            return []
        uid = user_id or self.user_id
        try:
            result = self._client.search(query, user_id=uid, limit=limit)
        except Exception as exc:  # pragma: no cover
            logger.warning("mem0.search failed: %s", exc)
            return []
        return _normalise(result, fallback_user=uid)

    def get_all(
        self,
        *,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[MemoryRecord]:
        if self._client is None:
            return []
        uid = user_id or self.user_id
        try:
            result = self._client.get_all(user_id=uid, limit=limit)
        except Exception as exc:  # pragma: no cover
            logger.warning("mem0.get_all failed: %s", exc)
            return []
        return _normalise(result, fallback_user=uid)

    def delete(self, memory_id: str) -> bool:
        if self._client is None:
            return False
        try:
            self._client.delete(memory_id=memory_id)
            return True
        except Exception as exc:  # pragma: no cover
            logger.warning("mem0.delete failed: %s", exc)
            return False


# ─── normaliser ───────────────────────────────────────────────────


def _normalise(raw: Any, *, fallback_user: str) -> List[MemoryRecord]:
    """Mem0's return shape varies by version + by call (``add`` returns
    ``{"results": [...]}``, ``search`` returns ``[{...}]`` or
    ``{"results": [...]}``).  Normalise into our flat dataclass."""
    if raw is None:
        return []
    if isinstance(raw, dict) and "results" in raw:
        items = raw["results"]
    else:
        items = raw if isinstance(raw, list) else [raw]

    out: List[MemoryRecord] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        out.append(
            MemoryRecord(
                id=str(entry.get("id") or entry.get("memory_id") or ""),
                user_id=str(entry.get("user_id") or fallback_user),
                text=str(entry.get("memory") or entry.get("text") or ""),
                score=_maybe_float(entry.get("score")),
                metadata=entry.get("metadata") or {},
                created_at=_maybe_str(entry.get("created_at")),
                updated_at=_maybe_str(entry.get("updated_at")),
            ),
        )
    return out


def _maybe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _maybe_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)


# ─── module-level singleton helper ────────────────────────────────


_GLOBAL_ADAPTER: Optional[Mem0Adapter] = None


def get_default_adapter() -> Mem0Adapter:
    """Return a process-wide Mem0Adapter, lazily constructed.  Callers
    that need a per-user namespace should build their own instance
    rather than reusing the global one (the global default uses
    ``user_id="local"``)."""
    global _GLOBAL_ADAPTER  # noqa: PLW0603
    if _GLOBAL_ADAPTER is None:
        _GLOBAL_ADAPTER = Mem0Adapter(
            user_id=os.environ.get("AMOR_MEMORY_USER", "local"),
            graph_enabled=os.environ.get("AMOR_MEMORY_NEO4J", "").strip() in ("1", "true", "yes"),
            llm_model=os.environ.get("AMOR_MEMORY_LLM_MODEL", "amor-architect"),
            vector_store=os.environ.get("AMOR_MEMORY_VECTOR_STORE", "lancedb"),
        )
    return _GLOBAL_ADAPTER


def reset_default_adapter() -> None:
    """Test hook — clears the cached global adapter so a monkeypatched
    env can rebuild it.  Production code shouldn't need this."""
    global _GLOBAL_ADAPTER  # noqa: PLW0603
    _GLOBAL_ADAPTER = None
