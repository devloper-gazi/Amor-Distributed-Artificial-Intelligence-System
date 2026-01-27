"""
MongoDB-backed chat persistence for the web UI.

This stores chat sessions and messages so that history is reliable across
reloads and container restarts (unlike in-memory state / browser localStorage).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid

from ..config.logging_config import logger
from ..infrastructure.storage import storage_manager


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

        self._indexes_ready = True
        logger.info("chat_store_indexes_ready")

    async def create_session(
        self,
        *,
        client_id: str,
        mode: str,
        title: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> ChatSession:
        await self.ensure_indexes()
        db = await self._db()
        sessions = db["chat_sessions"]

        now = _utcnow()
        sid = session_id or _new_session_id()

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

        await sessions.insert_one(doc)

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
    ) -> str:
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
        msg_doc = {
            "session_id": session_id,
            "role": role,
            "content": content,
            "format": format,
            "ai_type": ai_type,
            "extras": extras or {},
            "created_at": now,
        }
        inserted = await messages.insert_one(msg_doc)

        # Update session updated_at and auto-title on first user message
        update: Dict[str, Any] = {"updated_at": now}
        if role == "user":
            existing_title = session.get("title") or "Untitled Chat"
            if existing_title == "Untitled Chat" and content.strip():
                update["title"] = (content.strip()[:50] + ("..." if len(content.strip()) > 50 else ""))

        await sessions.update_one({"_id": session_id}, {"$set": update})

        return str(inserted.inserted_id)

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


# Global instance
chat_store = ChatStore()

