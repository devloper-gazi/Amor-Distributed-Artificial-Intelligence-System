"""
MongoDB-backed chat persistence for the web UI.

This stores chat sessions and messages so that history is reliable across
reloads and container restarts (unlike in-memory state / browser localStorage).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar
import uuid

from ..config.logging_config import logger
from ..infrastructure.storage import storage_manager


_T = TypeVar("_T")

# Substrings that identify *transient* MongoDB errors worth retrying.
# Anything else (bad query, schema violation, auth) shouldn't be retried.
_TRANSIENT_MONGO_MARKERS = (
    "network",
    "timeout",
    "connection",
    "notprimary",
    "interrupted",
    "unreachable",
    "socket",
)


async def _write_with_retry(
    coro_factory: Callable[[], Awaitable[_T]],
    *,
    max_attempts: int = 3,
    op_name: str = "mongo_write",
) -> _T:
    """
    Run a MongoDB write coroutine with exponential-backoff retry.

    `coro_factory` must be a zero-arg callable that *creates* a fresh
    coroutine each time (so we can await it again on retry — coroutines
    can't be awaited twice). Pass a ``lambda`` that closes over the call
    arguments.

    Retries only on substrings indicating transient errors
    (``network|timeout|connection|notprimary|interrupted|unreachable|socket``).
    All other exceptions propagate immediately so callers see real
    schema/auth/logic errors.

    Between attempts, the storage manager's ``_mongo_connected`` flag is
    reset so the next call refreshes the live connection (see the new
    re-validation path in :func:`storage.StorageManager.connect_mongo`).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            err_text = str(exc).lower()
            transient = any(m in err_text for m in _TRANSIENT_MONGO_MARKERS)
            if not transient or attempt >= max_attempts - 1:
                logger.warning(
                    "mongo_write_failed",
                    op=op_name,
                    attempt=attempt + 1,
                    transient=transient,
                    error=str(exc),
                )
                raise
            wait = 0.5 * (2 ** attempt)
            logger.warning(
                "mongo_write_retry",
                op=op_name,
                attempt=attempt + 1,
                max_attempts=max_attempts,
                error=str(exc),
                retry_in=wait,
            )
            # Force the next call to re-validate (and reconnect on
            # failure) the underlying client.
            storage_manager._mongo_connected = False  # noqa: SLF001
            await asyncio.sleep(wait)
    # Unreachable — the loop either returns or raises — but mypy/static
    # checkers want a definitive code path.
    assert last_exc is not None
    raise last_exc


def _generate_title_from_query(query: str, max_chars: int = 60) -> str:
    """
    Build a chat title from the first user query.

    Replaces the previous 50-char raw truncation, which mid-word-cut
    arbitrary content and left raw markdown tokens (``**``, ``#`` etc.)
    in the sidebar. Strips HTML/markdown decoration, normalises
    whitespace, breaks at the last word boundary, and capitalises.
    """
    if not query:
        return "New Chat"
    text = re.sub(r"<[^>]+>", "", query)              # strip HTML tags
    text = re.sub(r"[*_`#~>\[\]()!]+", "", text)     # strip markdown noise
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "New Chat"
    if len(text) <= max_chars:
        return text[:1].upper() + text[1:]
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    # Only break at a word boundary if the cut leaves a usable prefix
    # (≥66% of max_chars). Otherwise keep the hard cut so we don't drop
    # half a sentence to a single word.
    if last_space > max_chars * 0.66:
        truncated = truncated[:last_space]
    return (truncated[:1].upper() + truncated[1:]).rstrip(".,;: ") + "…"


def _utcnow() -> datetime:
    # Use timezone-aware timestamps to avoid client-side timezone ambiguity.
    return datetime.now(timezone.utc)


def _new_session_id() -> str:
    return str(uuid.uuid4())


def _new_folder_id() -> str:
    return str(uuid.uuid4())


_UNSET = object()


@dataclass(frozen=True)
class ChatSession:
    id: str
    client_id: str
    mode: str
    title: str
    created_at: datetime
    updated_at: datetime
    user_id: Optional[str] = None
    archived: bool = False
    folder_id: Optional[str] = None
    pinned: bool = False


class ChatStore:
    """Persistence layer for chat sessions/messages (MongoDB)."""

    def __init__(self):
        self._indexes_ready = False

    async def _db(self):
        if not storage_manager._mongo_connected:  # noqa: SLF001 (internal flag)
            await storage_manager.connect_mongo()
        if storage_manager.mongo_db is None:
            raise RuntimeError("MongoDB not initialized")
        return storage_manager.mongo_db

    async def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return

        db = await self._db()
        sessions = db["chat_sessions"]
        messages = db["chat_messages"]
        folders = db["chat_folders"]

        # Sessions: quick lookup by client/mode sorted by updated time
        await sessions.create_index([("client_id", 1), ("mode", 1), ("updated_at", -1)])
        # Sessions: lookup across modes (folders are shared across modes)
        await sessions.create_index([("client_id", 1), ("updated_at", -1)])
        # Future auth support
        await sessions.create_index([("user_id", 1), ("mode", 1), ("updated_at", -1)], sparse=True)
        await sessions.create_index([("user_id", 1), ("updated_at", -1)], sparse=True)
        # Common filters
        await sessions.create_index([("client_id", 1), ("folder_id", 1), ("archived", 1), ("updated_at", -1)])
        await sessions.create_index([("user_id", 1), ("folder_id", 1), ("archived", 1), ("updated_at", -1)], sparse=True)
        # Pinned sorting (pinned first, then by updated_at)
        await sessions.create_index([("client_id", 1), ("pinned", -1), ("updated_at", -1)])
        await sessions.create_index([("user_id", 1), ("pinned", -1), ("updated_at", -1)], sparse=True)

        # Messages: ordered retrieval within a session
        await messages.create_index([("session_id", 1), ("created_at", 1)])

        # Folders: list by owner, stable name lookup
        await folders.create_index([("client_id", 1), ("updated_at", -1)])
        await folders.create_index([("user_id", 1), ("updated_at", -1)], sparse=True)
        await folders.create_index([("client_id", 1), ("name", 1)])
        await folders.create_index([("user_id", 1), ("name", 1)], sparse=True)
        # Folders: pinned sorting (pinned first, then recently updated)
        await folders.create_index([("client_id", 1), ("pinned", -1), ("updated_at", -1)])
        await folders.create_index([("user_id", 1), ("pinned", -1), ("updated_at", -1)], sparse=True)

        # Phase B/C — chat_messages: idempotency_key dedupe so frontend
        # and backend writes for the same logical message collapse to one
        # row. Sparse so existing rows without the field don't conflict.
        await messages.create_index(
            [("idempotency_key", 1)], sparse=True, unique=True
        )

        # Phase B — chat_sessions: idempotency_key dedupe so a double-
        # submit during first message can't create two Untitled Chats.
        await sessions.create_index(
            [("idempotency_key", 1)], sparse=True, unique=True
        )

        # Phase A3 — query_records collection. The durable bridge between
        # ephemeral Redis pipeline state (thinking/research session_ids)
        # and permanent chat history. Re-entry, cancellation, and
        # in-progress UI all read this collection.
        records = db["query_records"]
        # List all queries for one chat session, newest first.
        await records.create_index(
            [("chat_session_id", 1), ("started_at", -1)]
        )
        # "What's the active query for this user/session right now?" —
        # used by the resume banner.
        await records.create_index(
            [("user_id", 1), ("status", 1), ("started_at", -1)],
            sparse=True,
        )
        await records.create_index(
            [("client_id", 1), ("status", 1), ("started_at", -1)]
        )
        # Reverse-lookup from a Redis pipeline session_id → query_record.
        await records.create_index([("thinking_session_id", 1)], sparse=True)
        await records.create_index([("research_session_id", 1)], sparse=True)
        # Idempotency dedupe — POST /api/query-records must be safe to retry.
        await records.create_index(
            [("idempotency_key", 1)], sparse=True, unique=True
        )

        # Per-user model preferences (More settings → AI Model). Stored
        # under user_id when authenticated, client_id otherwise. The
        # "mode" value is one of "research" / "thinking" / "coding" /
        # "code" / "__all__" — the wildcard applies across modes when
        # no exact-mode preference exists.
        prefs = db["user_model_preferences"]
        await prefs.create_index(
            [("user_id", 1), ("mode", 1)],
            unique=True, sparse=True,
        )
        await prefs.create_index(
            [("client_id", 1), ("mode", 1)],
            unique=True, sparse=True,
        )

        # v3 — Per-user multi-model routing strategy. One doc per user
        # (or client when anonymous). Schema in `set_model_routing`.
        # The picker UI's "Single | Multi" toggle writes here; backend
        # callers read it to pick a per-role model when multi-model
        # mode is on.
        routing = db["user_model_routing"]
        await routing.create_index([("user_id", 1)], unique=True, sparse=True)
        await routing.create_index([("client_id", 1)], unique=True, sparse=True)

        self._indexes_ready = True
        logger.info("chat_store_indexes_ready")

    # ─────────────────────────────────────────────────────────────────────
    # User model preferences (More settings → AI Model)
    #
    # Optional override of the default Ollama tag, stored per-user (or
    # per-client when unauthenticated). The settings UI writes here; the
    # AI handlers read here at request time. Falling back to None means
    # the caller should auto-select.
    # ─────────────────────────────────────────────────────────────────────

    PREFERENCE_MODE_ALL = "__all__"

    @staticmethod
    def _pref_filter(
        user_id: Optional[str], client_id: str, mode: str,
    ) -> Dict[str, Any]:
        """Build the unique-key filter — user_id wins when set, else client_id."""
        if user_id:
            return {"user_id": str(user_id), "mode": mode}
        return {"user_id": None, "client_id": client_id, "mode": mode}

    async def set_model_preference(
        self,
        *,
        user_id: Optional[str],
        client_id: str,
        mode: str,
        model_tag: str,
        model_source: str = "ollama_registry",
        display_name: Optional[str] = None,
        profile: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Upsert a model preference for a specific mode (or "__all__").

        ``profile`` is the v3 advanced-options dict — sliders/inputs from
        the picker's Advanced expansion. Recognised keys:
          - ``temperature``     (float 0.0–2.0)
          - ``top_p``           (float 0.0–1.0)
          - ``top_k``           (int 1–200)
          - ``repeat_penalty``  (float 1.0–2.0)
          - ``num_ctx``         (int — context length in tokens)
          - ``num_gpu``         (int — Ollama GPU layer offload; 0 = CPU only)
          - ``num_thread``      (int — CPU thread budget)
          - ``seed``            (int — RNG seed for reproducibility, -1 = random)
          - ``system_prompt``   (str — prepended to every prompt)
          - ``mirostat``        (int 0/1/2)
          - ``stop``            (list[str] — stop sequences)

        The dict is stored verbatim; ModelManager's
        ``apply_profile_to_options`` whitelists known keys at request
        time so unknown fields are ignored without crashing Ollama.
        """
        await self.ensure_indexes()
        db = await self._db()
        coll = db["user_model_preferences"]
        flt = self._pref_filter(user_id, client_id, mode)
        now = _utcnow()
        update_set: Dict[str, Any] = {
            "model_tag": model_tag,
            "model_source": model_source,
            "display_name": display_name,
            "updated_at": now,
        }
        if profile is not None:
            # Storing None explicitly clears the profile; an empty dict
            # is treated the same as None for callers that want "use
            # Ollama defaults again".
            update_set["profile"] = profile or None
        await _write_with_retry(
            lambda: coll.update_one(
                flt,
                {
                    "$set": update_set,
                    "$setOnInsert": {**flt, "set_at": now},
                },
                upsert=True,
            ),
            op_name="set_model_preference",
        )

    async def get_model_preference_full(
        self,
        *,
        user_id: Optional[str],
        client_id: str,
        mode: str,
    ) -> Optional[Dict[str, Any]]:
        """Like ``get_model_preference`` but returns the full doc
        (tag + profile) rather than just the tag string. Same fallback
        chain: exact mode → __all__ wildcard → None."""
        await self.ensure_indexes()
        db = await self._db()
        coll = db["user_model_preferences"]
        try:
            exact = await coll.find_one(
                self._pref_filter(user_id, client_id, mode)
            )
            if exact and exact.get("model_tag"):
                return self._serialise_pref_doc(exact)
            wildcard = await coll.find_one(
                self._pref_filter(user_id, client_id, self.PREFERENCE_MODE_ALL)
            )
            if wildcard and wildcard.get("model_tag"):
                return self._serialise_pref_doc(wildcard)
        except Exception as exc:
            logger.warning("get_model_preference_full_failed: %s", exc)
        return None

    @staticmethod
    def _serialise_pref_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
        """Strip Mongo-specific keys; UI/JSON-friendly view."""
        return {
            "mode": doc.get("mode"),
            "model_tag": doc.get("model_tag"),
            "model_source": doc.get("model_source"),
            "display_name": doc.get("display_name"),
            "profile": doc.get("profile") or {},
            "set_at": doc.get("set_at"),
            "updated_at": doc.get("updated_at"),
        }

    async def get_model_preference(
        self,
        *,
        user_id: Optional[str],
        client_id: str,
        mode: str,
    ) -> Optional[str]:
        """
        Resolution order:
          1. Exact (user/client, mode)
          2. Wildcard (user/client, "__all__")
          3. None — caller should auto-select
        """
        await self.ensure_indexes()
        db = await self._db()
        coll = db["user_model_preferences"]
        try:
            exact = await coll.find_one(
                self._pref_filter(user_id, client_id, mode)
            )
            if exact and exact.get("model_tag"):
                return str(exact["model_tag"])
            wildcard = await coll.find_one(
                self._pref_filter(user_id, client_id, self.PREFERENCE_MODE_ALL)
            )
            if wildcard and wildcard.get("model_tag"):
                return str(wildcard["model_tag"])
        except Exception as exc:
            logger.warning("get_model_preference_failed: %s", exc)
        return None

    async def delete_model_preference(
        self,
        *,
        user_id: Optional[str],
        client_id: str,
        mode: str,
    ) -> bool:
        """Remove a preference; returns True if a row was deleted."""
        await self.ensure_indexes()
        db = await self._db()
        coll = db["user_model_preferences"]
        try:
            res = await _write_with_retry(
                lambda: coll.delete_one(self._pref_filter(user_id, client_id, mode)),
                op_name="delete_model_preference",
            )
            return bool(getattr(res, "deleted_count", 0))
        except Exception as exc:
            logger.warning("delete_model_preference_failed: %s", exc)
            return False

    async def get_all_model_preferences(
        self,
        *,
        user_id: Optional[str],
        client_id: str,
    ) -> Dict[str, Dict[str, Any]]:
        """Return {mode: {model_tag, model_source, display_name, profile, ...}}
        — used by the settings UI to populate the picker on open."""
        await self.ensure_indexes()
        db = await self._db()
        coll = db["user_model_preferences"]
        flt: Dict[str, Any] = (
            {"user_id": str(user_id)} if user_id
            else {"user_id": None, "client_id": client_id}
        )
        out: Dict[str, Dict[str, Any]] = {}
        try:
            async for doc in coll.find(flt):
                mode = doc.get("mode") or self.PREFERENCE_MODE_ALL
                out[mode] = {
                    "model_tag": doc.get("model_tag"),
                    "model_source": doc.get("model_source"),
                    "display_name": doc.get("display_name"),
                    "profile": doc.get("profile") or {},
                    "set_at": doc.get("set_at"),
                    "updated_at": doc.get("updated_at"),
                }
        except Exception as exc:
            logger.warning("get_all_model_preferences_failed: %s", exc)
        return out

    # ─────────────────────────────────────────────────────────────────────
    # v3 — Multi-model routing (Single | Per-mode | Per-role | Ensemble)
    #
    # The routing doc encodes how the system chooses a model when the
    # user enables "multi-model orchestration" in the picker. Schema:
    #
    #   {
    #     "user_id": "..." | null, "client_id": "...",
    #     "strategy": "single" | "per_mode" | "per_role" | "ensemble",
    #     "single_tag": "qwen2.5:7b" | null,
    #     "mode_routes": { "research": "tag", "thinking": "tag", "code": "tag" },
    #     "role_routes": { "planner": "tag", "coder": "tag", "critic": "tag", "reviewer": "tag" },
    #     "fallback_chain": ["primary:tag", "fallback:tag"],
    #     "hardware_pref": "auto" | "gpu" | "cpu" | "gpu_partial",
    #     "gpu_layers": 999,
    #     "ensemble": { "voting": "majority" | "weighted", "members": ["tag1", "tag2"] }
    #   }
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _routing_filter(
        user_id: Optional[str], client_id: str,
    ) -> Dict[str, Any]:
        if user_id:
            return {"user_id": str(user_id)}
        return {"user_id": None, "client_id": client_id}

    async def set_model_routing(
        self,
        *,
        user_id: Optional[str],
        client_id: str,
        routing: Dict[str, Any],
    ) -> None:
        """Upsert the user/client routing strategy. Whitelists known keys."""
        await self.ensure_indexes()
        db = await self._db()
        coll = db["user_model_routing"]
        flt = self._routing_filter(user_id, client_id)
        now = _utcnow()

        allowed = {
            "strategy", "single_tag", "mode_routes", "role_routes",
            "fallback_chain", "hardware_pref", "gpu_layers", "ensemble",
        }
        clean = {k: v for k, v in routing.items() if k in allowed}
        # Strategy must be one of the four canonical values; default
        # falls back to "single" so a malformed write doesn't lock the
        # user into nothing.
        strat = str(clean.get("strategy") or "single").lower()
        if strat not in {"single", "per_mode", "per_role", "ensemble"}:
            strat = "single"
        clean["strategy"] = strat

        await _write_with_retry(
            lambda: coll.update_one(
                flt,
                {
                    "$set": {**clean, "updated_at": now},
                    "$setOnInsert": {**flt, "set_at": now},
                },
                upsert=True,
            ),
            op_name="set_model_routing",
        )

    async def get_model_routing(
        self,
        *,
        user_id: Optional[str],
        client_id: str,
    ) -> Dict[str, Any]:
        """Return the user/client routing doc, or a default
        {strategy: "single", ...} when none is set."""
        await self.ensure_indexes()
        db = await self._db()
        coll = db["user_model_routing"]
        try:
            doc = await coll.find_one(self._routing_filter(user_id, client_id))
            if doc:
                return {
                    "strategy": doc.get("strategy") or "single",
                    "single_tag": doc.get("single_tag"),
                    "mode_routes": doc.get("mode_routes") or {},
                    "role_routes": doc.get("role_routes") or {},
                    "fallback_chain": doc.get("fallback_chain") or [],
                    "hardware_pref": doc.get("hardware_pref") or "auto",
                    "gpu_layers": doc.get("gpu_layers"),
                    "ensemble": doc.get("ensemble") or {},
                    "updated_at": doc.get("updated_at"),
                }
        except Exception as exc:
            logger.warning("get_model_routing_failed: %s", exc)
        return {
            "strategy": "single",
            "single_tag": None,
            "mode_routes": {},
            "role_routes": {},
            "fallback_chain": [],
            "hardware_pref": "auto",
            "gpu_layers": None,
            "ensemble": {},
        }

    async def delete_model_routing(
        self,
        *,
        user_id: Optional[str],
        client_id: str,
    ) -> bool:
        """Reset the user/client to default (single-model) behaviour."""
        await self.ensure_indexes()
        db = await self._db()
        coll = db["user_model_routing"]
        try:
            res = await _write_with_retry(
                lambda: coll.delete_one(self._routing_filter(user_id, client_id)),
                op_name="delete_model_routing",
            )
            return bool(getattr(res, "deleted_count", 0))
        except Exception as exc:
            logger.warning("delete_model_routing_failed: %s", exc)
            return False

    async def create_session(
        self,
        *,
        client_id: str,
        mode: str,
        title: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> ChatSession:
        """
        Create a chat session.

        When ``idempotency_key`` is supplied, this is an upsert keyed on
        the unique sparse ``idempotency_key`` index — two concurrent
        creates with the same key collapse to a single document, fixing
        the sub-100 ms double-submit race that produced ghost
        ``Untitled Chat`` rows. Without a key the call falls through to
        the original `insert_one` path (back-compat for legacy callers).
        """
        await self.ensure_indexes()
        db = await self._db()
        sessions = db["chat_sessions"]

        now = _utcnow()
        sid = session_id or _new_session_id()

        if idempotency_key:
            from pymongo import ReturnDocument
            insert_doc = {
                "_id": sid,
                "client_id": client_id,
                "user_id": user_id,
                "mode": mode,
                "title": title or "Untitled Chat",
                "created_at": now,
                "updated_at": now,
                "archived": False,
                "folder_id": None,
                "pinned": False,
                "idempotency_key": idempotency_key,
            }
            doc = await _write_with_retry(
                lambda: sessions.find_one_and_update(
                    {"idempotency_key": idempotency_key},
                    {"$setOnInsert": insert_doc},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                ),
                op_name="session_upsert",
            )
            return ChatSession(
                id=doc["_id"],
                client_id=doc["client_id"],
                user_id=doc.get("user_id"),
                mode=doc["mode"],
                title=doc["title"],
                created_at=doc["created_at"],
                updated_at=doc["updated_at"],
                archived=bool(doc.get("archived", False)),
                folder_id=doc.get("folder_id"),
                pinned=bool(doc.get("pinned", False)),
            )

        # Legacy path — no idempotency, plain insert.
        doc = {
            "_id": sid,
            "client_id": client_id,
            "user_id": user_id,
            "mode": mode,
            "title": title or "Untitled Chat",
            "created_at": now,
            "updated_at": now,
            "archived": False,
            "folder_id": None,
            "pinned": False,
        }
        await _write_with_retry(
            lambda: sessions.insert_one(doc),
            op_name="session_insert",
        )

        return ChatSession(
            id=sid,
            client_id=client_id,
            user_id=user_id,
            mode=mode,
            title=doc["title"],
            created_at=now,
            updated_at=now,
            archived=False,
            folder_id=None,
            pinned=False,
        )

    async def list_sessions(
        self,
        *,
        client_id: str,
        mode: str,
        user_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = True,
        folder_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        await self.ensure_indexes()
        db = await self._db()
        sessions = db["chat_sessions"]

        query: Dict[str, Any] = {"mode": mode}
        if user_id:
            query["user_id"] = user_id
        else:
            query["client_id"] = client_id
        if not include_archived:
            query["archived"] = {"$ne": True}
        if folder_id is not None:
            query["folder_id"] = folder_id

        cursor = (
            sessions.find(query)
            .sort([("pinned", -1), ("updated_at", -1)])
            .skip(max(offset, 0))
            .limit(min(max(limit, 1), 200))
        )

        results: List[Dict[str, Any]] = []
        async for doc in cursor:
            results.append(
                {
                    "id": doc["_id"],
                    "client_id": doc.get("client_id"),
                    "user_id": doc.get("user_id"),
                    "mode": doc.get("mode"),
                    "title": doc.get("title"),
                    "created_at": doc.get("created_at"),
                    "updated_at": doc.get("updated_at"),
                    "archived": bool(doc.get("archived", False)),
                    "folder_id": doc.get("folder_id"),
                    "pinned": bool(doc.get("pinned", False)),
                }
            )
        return results

    async def list_sessions_all(
        self,
        *,
        client_id: str,
        user_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
        folder_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        await self.ensure_indexes()
        db = await self._db()
        sessions = db["chat_sessions"]

        query: Dict[str, Any] = {}
        if user_id:
            query["user_id"] = user_id
        else:
            query["client_id"] = client_id

        if not include_archived:
            query["archived"] = {"$ne": True}
        if folder_id is not None:
            query["folder_id"] = folder_id

        cursor = (
            sessions.find(query)
            .sort([("pinned", -1), ("updated_at", -1)])
            .skip(max(offset, 0))
            .limit(min(max(limit, 1), 200))
        )

        results: List[Dict[str, Any]] = []
        async for doc in cursor:
            results.append(
                {
                    "id": doc["_id"],
                    "client_id": doc.get("client_id"),
                    "user_id": doc.get("user_id"),
                    "mode": doc.get("mode"),
                    "title": doc.get("title"),
                    "created_at": doc.get("created_at"),
                    "updated_at": doc.get("updated_at"),
                    "archived": bool(doc.get("archived", False)),
                    "folder_id": doc.get("folder_id"),
                    "pinned": bool(doc.get("pinned", False)),
                }
            )
        return results

    async def get_session(
        self,
        *,
        client_id: str,
        session_id: str,
        user_id: Optional[str] = None,
        include_messages: bool = True,
    ) -> Dict[str, Any]:
        await self.ensure_indexes()
        db = await self._db()
        sessions = db["chat_sessions"]
        messages = db["chat_messages"]

        session = await sessions.find_one({"_id": session_id})
        if not session:
            raise KeyError("session_not_found")

        # Access control (lightweight until auth exists)
        if user_id:
            if session.get("user_id") != user_id:
                raise KeyError("session_not_found")
        else:
            if session.get("client_id") != client_id:
                raise KeyError("session_not_found")

        result: Dict[str, Any] = {
            "id": session["_id"],
            "client_id": session.get("client_id"),
            "user_id": session.get("user_id"),
            "mode": session.get("mode"),
            "title": session.get("title"),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "archived": bool(session.get("archived", False)),
            "folder_id": session.get("folder_id"),
            "pinned": bool(session.get("pinned", False)),
        }

        if include_messages:
            cursor = messages.find({"session_id": session_id}).sort("created_at", 1)
            msgs: List[Dict[str, Any]] = []
            async for m in cursor:
                msgs.append(
                    {
                        "role": m.get("role"),
                        "content": m.get("content", ""),
                        "format": m.get("format", "text"),
                        "aiType": m.get("ai_type"),
                        "extras": m.get("extras") or {},
                        "createdAt": m.get("created_at"),
                    }
                )
            result["messages"] = msgs

        return result

    async def append_message(
        self,
        *,
        client_id: str,
        session_id: str,
        role: str,
        content: str,
        format: str = "text",
        ai_type: Optional[str] = None,
        extras: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """
        Append a message to a chat session.

        When `idempotency_key` is supplied, this is an upsert keyed on
        the unique sparse index `chat_messages.idempotency_key`. Both
        the frontend (after the AI returns) and the backend AI handlers
        (Phase C1) write the SAME logical message with the SAME key —
        the second write becomes a no-op, defending against double
        persistence under network retry or simultaneous client/server
        writes.

        Returns the canonical message id either way.
        """
        await self.ensure_indexes()
        db = await self._db()
        sessions = db["chat_sessions"]
        messages = db["chat_messages"]

        session = await sessions.find_one({"_id": session_id})
        if not session:
            raise KeyError("session_not_found")

        if user_id:
            if session.get("user_id") != user_id:
                raise KeyError("session_not_found")
        else:
            if session.get("client_id") != client_id:
                raise KeyError("session_not_found")

        now = _utcnow()
        message_id_str: str

        if idempotency_key:
            # Upsert path. We use the idempotency_key both to dedupe and
            # as the natural primary key for the row (str), which keeps
            # the API-returned `message_id` stable across retries.
            from pymongo import ReturnDocument
            insert_doc = {
                "_id": idempotency_key,
                "session_id": session_id,
                "role": role,
                "content": content,
                "format": format,
                "ai_type": ai_type,
                "extras": extras or {},
                "created_at": now,
                "idempotency_key": idempotency_key,
            }
            doc = await _write_with_retry(
                lambda: messages.find_one_and_update(
                    {"idempotency_key": idempotency_key},
                    {"$setOnInsert": insert_doc},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                ),
                op_name="message_upsert",
            )
            message_id_str = str(doc["_id"])
        else:
            # Legacy path — plain insert.
            msg_doc = {
                "session_id": session_id,
                "role": role,
                "content": content,
                "format": format,
                "ai_type": ai_type,
                "extras": extras or {},
                "created_at": now,
            }
            inserted = await _write_with_retry(
                lambda: messages.insert_one(msg_doc),
                op_name="message_insert",
            )
            message_id_str = str(inserted.inserted_id)

        # Update session updated_at + auto-title on first user message.
        # Phase B2: route through `_generate_title_from_query` so the
        # sidebar gets a clean, word-boundary, markdown-stripped title
        # instead of the previous mid-word raw 50-char cut.
        update: Dict[str, Any] = {"updated_at": now}
        if role == "user":
            existing_title = session.get("title") or "Untitled Chat"
            if existing_title in {"Untitled Chat", "New Chat"} and content.strip():
                update["title"] = _generate_title_from_query(content)

        await _write_with_retry(
            lambda: sessions.update_one({"_id": session_id}, {"$set": update}),
            op_name="session_touch",
        )

        return message_id_str

    async def update_session(
        self,
        *,
        client_id: str,
        session_id: str,
        user_id: Optional[str] = None,
        title: Optional[str] = None,
        mode: Optional[str] = None,
        archived: object = _UNSET,
        folder_id: object = _UNSET,
        pinned: object = _UNSET,
    ) -> None:
        await self.ensure_indexes()
        db = await self._db()
        sessions = db["chat_sessions"]

        session = await sessions.find_one({"_id": session_id})
        if not session:
            raise KeyError("session_not_found")

        if user_id:
            if session.get("user_id") != user_id:
                raise KeyError("session_not_found")
        else:
            if session.get("client_id") != client_id:
                raise KeyError("session_not_found")

        update: Dict[str, Any] = {"updated_at": _utcnow()}
        if title is not None:
            update["title"] = title
        if mode is not None:
            update["mode"] = mode
        if archived is not _UNSET:
            update["archived"] = bool(archived)
        if folder_id is not _UNSET:
            update["folder_id"] = folder_id
        if pinned is not _UNSET:
            update["pinned"] = bool(pinned)

        await sessions.update_one({"_id": session_id}, {"$set": update})

    async def update_session_title(
        self,
        *,
        client_id: str,
        session_id: str,
        title: str,
        user_id: Optional[str] = None,
    ) -> None:
        """Compatibility helper (per plan): update only the session title."""
        await self.update_session(
            client_id=client_id,
            session_id=session_id,
            user_id=user_id,
            title=title,
            mode=None,
        )

    async def delete_session(
        self,
        *,
        client_id: str,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> None:
        await self.ensure_indexes()
        db = await self._db()
        sessions = db["chat_sessions"]
        messages = db["chat_messages"]

        session = await sessions.find_one({"_id": session_id})
        if not session:
            return

        if user_id:
            if session.get("user_id") != user_id:
                return
        else:
            if session.get("client_id") != client_id:
                return

        await sessions.delete_one({"_id": session_id})
        await messages.delete_many({"session_id": session_id})

    # -------------------- Folders --------------------
    async def create_folder(
        self,
        *,
        client_id: str,
        name: str,
        user_id: Optional[str] = None,
        folder_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self.ensure_indexes()
        db = await self._db()
        folders = db["chat_folders"]

        n = (name or "").strip()
        if not n:
            raise ValueError("invalid_folder_name")

        now = _utcnow()
        fid = folder_id or _new_folder_id()
        doc = {
            "_id": fid,
            "client_id": client_id,
            "user_id": user_id,
            "name": n,
            "created_at": now,
            "updated_at": now,
            "pinned": False,
        }

        await folders.insert_one(doc)
        return {
            "id": fid,
            "name": n,
            "created_at": now,
            "updated_at": now,
            "pinned": False,
        }

    async def list_folders(
        self,
        *,
        client_id: str,
        user_id: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        await self.ensure_indexes()
        db = await self._db()
        folders = db["chat_folders"]

        query: Dict[str, Any] = {}
        if user_id:
            query["user_id"] = user_id
        else:
            query["client_id"] = client_id

        cursor = (
            folders.find(query)
            .sort([("pinned", -1), ("updated_at", -1)])
            .skip(max(offset, 0))
            .limit(min(max(limit, 1), 200))
        )

        results: List[Dict[str, Any]] = []
        async for doc in cursor:
            results.append(
                {
                    "id": doc["_id"],
                    "name": doc.get("name") or "",
                    "created_at": doc.get("created_at"),
                    "updated_at": doc.get("updated_at"),
                    "pinned": bool(doc.get("pinned", False)),
                }
            )
        return results

    async def update_folder(
        self,
        *,
        client_id: str,
        folder_id: str,
        user_id: Optional[str] = None,
        name: object = _UNSET,
        pinned: object = _UNSET,
    ) -> None:
        await self.ensure_indexes()
        db = await self._db()
        folders = db["chat_folders"]

        folder = await folders.find_one({"_id": folder_id})
        if not folder:
            raise KeyError("folder_not_found")

        if user_id:
            if folder.get("user_id") != user_id:
                raise KeyError("folder_not_found")
        else:
            if folder.get("client_id") != client_id:
                raise KeyError("folder_not_found")

        update: Dict[str, Any] = {"updated_at": _utcnow()}
        if name is not _UNSET:
            n = (name or "").strip()
            if not n:
                raise ValueError("invalid_folder_name")
            update["name"] = n
        if pinned is not _UNSET:
            update["pinned"] = bool(pinned)

        await folders.update_one({"_id": folder_id}, {"$set": update})

    async def rename_folder(
        self,
        *,
        client_id: str,
        folder_id: str,
        name: str,
        user_id: Optional[str] = None,
    ) -> None:
        await self.ensure_indexes()
        db = await self._db()
        folders = db["chat_folders"]

        folder = await folders.find_one({"_id": folder_id})
        if not folder:
            raise KeyError("folder_not_found")

        if user_id:
            if folder.get("user_id") != user_id:
                raise KeyError("folder_not_found")
        else:
            if folder.get("client_id") != client_id:
                raise KeyError("folder_not_found")

        n = (name or "").strip()
        if not n:
            raise ValueError("invalid_folder_name")

        await folders.update_one(
            {"_id": folder_id},
            {"$set": {"name": n, "updated_at": _utcnow()}},
        )

    async def delete_folder(
        self,
        *,
        client_id: str,
        folder_id: str,
        user_id: Optional[str] = None,
    ) -> None:
        await self.ensure_indexes()
        db = await self._db()
        folders = db["chat_folders"]
        sessions = db["chat_sessions"]

        folder = await folders.find_one({"_id": folder_id})
        if not folder:
            return

        if user_id:
            if folder.get("user_id") != user_id:
                return
            owner_filter: Dict[str, Any] = {"user_id": user_id}
        else:
            if folder.get("client_id") != client_id:
                return
            owner_filter = {"client_id": client_id}

        await folders.delete_one({"_id": folder_id})

        # Remove folder assignment from any sessions that referenced it.
        await sessions.update_many(
            {"folder_id": folder_id, **owner_filter},
            {"$set": {"folder_id": None, "updated_at": _utcnow()}},
        )

    # ─── Phase A3 / B4 / C / D — query_records ─────────────────────────
    #
    # The query_records collection is the **durable bridge** between
    # ephemeral pipeline state (thinking/research Redis session ids) and
    # permanent chat history (chat_messages, chat_sessions). Every long-
    # running query writes a record on creation, mutates it as progress
    # advances, and stamps a final status on completion / failure /
    # cancellation. The frontend reads it for the resume banner, inline
    # progress card, and history reload.

    async def create_query_record(
        self,
        *,
        chat_session_id: str,
        user_id: Optional[str],
        client_id: str,
        mode: str,
        query_text: str,
        provider: str,
        effort: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """
        Create (or, with idempotency_key, fetch the existing) query record.
        Returns the record id either way. Always durable (uses
        ``_write_with_retry``).
        """
        await self.ensure_indexes()
        db = await self._db()
        records = db["query_records"]
        now = _utcnow()

        if idempotency_key:
            # Upsert keyed on idempotency_key so two concurrent POSTs
            # with the same key collapse to a single document.
            doc_id = str(uuid.uuid4())
            insert_doc = {
                "_id": doc_id,
                "chat_session_id": chat_session_id,
                "user_id": user_id,
                "client_id": client_id,
                "mode": mode,
                "query_text": query_text,
                "provider": provider,
                "effort": effort,
                "status": "pending",
                "progress": 0,
                "current_phase": None,
                "current_task": None,
                "phases": [],
                "result_markdown": None,
                "result_html": None,
                "sources": None,
                "error": None,
                "thinking_session_id": None,
                "research_session_id": None,
                "tokens_used": None,
                "started_at": now,
                "updated_at": now,
                "completed_at": None,
                "cancelled_at": None,
                "idempotency_key": idempotency_key,
            }
            from pymongo import ReturnDocument
            existing = await _write_with_retry(
                lambda: records.find_one_and_update(
                    {"idempotency_key": idempotency_key},
                    {"$setOnInsert": insert_doc},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                ),
                op_name="query_record_upsert",
            )
            return existing["_id"]

        # Plain insert (no key) — caller accepts no dedupe guarantees.
        doc_id = str(uuid.uuid4())
        await _write_with_retry(
            lambda: records.insert_one({
                "_id": doc_id,
                "chat_session_id": chat_session_id,
                "user_id": user_id,
                "client_id": client_id,
                "mode": mode,
                "query_text": query_text,
                "provider": provider,
                "effort": effort,
                "status": "pending",
                "progress": 0,
                "current_phase": None,
                "current_task": None,
                "phases": [],
                "result_markdown": None,
                "result_html": None,
                "sources": None,
                "error": None,
                "thinking_session_id": None,
                "research_session_id": None,
                "tokens_used": None,
                "started_at": now,
                "updated_at": now,
                "completed_at": None,
                "cancelled_at": None,
            }),
            op_name="query_record_insert",
        )
        return doc_id

    async def update_query_record(
        self,
        record_id: str,
        **fields: Any,
    ) -> bool:
        """
        Atomic ``$set`` update. Always bumps ``updated_at``. Caller must
        not pass ``_id``. Returns True if a document was modified.
        """
        await self.ensure_indexes()
        db = await self._db()
        if "_id" in fields:
            fields.pop("_id")
        fields["updated_at"] = _utcnow()
        result = await _write_with_retry(
            lambda: db["query_records"].update_one(
                {"_id": record_id},
                {"$set": fields},
            ),
            op_name="query_record_update",
        )
        return bool(result.modified_count)

    async def get_query_record(
        self,
        record_id: str,
        *,
        user_id: Optional[str],
        client_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch a record with strict ownership check. Returns ``None`` if
        the record doesn't exist OR doesn't belong to the caller (the
        404-vs-403 distinction is intentionally collapsed to avoid
        information leakage).
        """
        db = await self._db()
        doc = await db["query_records"].find_one({"_id": record_id})
        if not doc:
            return None
        if user_id:
            if doc.get("user_id") != user_id:
                return None
        else:
            if doc.get("client_id") != client_id:
                return None
        return doc

    async def list_query_records_for_session(
        self,
        chat_session_id: str,
        *,
        user_id: Optional[str],
        client_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List query records for a session, newest first."""
        db = await self._db()
        ownership: Dict[str, Any] = {"chat_session_id": chat_session_id}
        if user_id:
            ownership["user_id"] = user_id
        else:
            ownership["client_id"] = client_id
        cursor = (
            db["query_records"]
            .find(ownership)
            .sort([("started_at", -1)])
            .limit(min(max(limit, 1), 200))
        )
        return [doc async for doc in cursor]

    async def get_active_query(
        self,
        chat_session_id: str,
        *,
        user_id: Optional[str],
        client_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Return the most-recent record for this session whose status is
        still ``pending`` or ``running``. Used by the resume banner.
        """
        db = await self._db()
        ownership: Dict[str, Any] = {
            "chat_session_id": chat_session_id,
            "status": {"$in": ["pending", "running"]},
        }
        if user_id:
            ownership["user_id"] = user_id
        else:
            ownership["client_id"] = client_id
        return await db["query_records"].find_one(
            ownership,
            sort=[("started_at", -1)],
        )

    async def cancel_query_record(
        self,
        record_id: str,
        *,
        user_id: Optional[str],
        client_id: str,
        reason: Optional[str] = None,
    ) -> bool:
        """
        Mark a record cancelled. Idempotent — already-terminal records
        are NOT modified (returns False), so re-cancelling a completed
        run doesn't rewrite history.
        """
        record = await self.get_query_record(
            record_id, user_id=user_id, client_id=client_id
        )
        if not record:
            return False
        if record.get("status") in {"completed", "failed", "cancelled"}:
            return False
        now = _utcnow()
        ok = await self.update_query_record(
            record_id,
            status="cancelled",
            cancelled_at=now,
            error=reason or "Cancelled by user.",
            current_task="Cancelled.",
        )
        return ok


# Global instance
chat_store = ChatStore()

