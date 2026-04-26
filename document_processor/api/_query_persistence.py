"""
Shared persistence + cancellation helpers for AI handler routes.

Phase C of fancy-swinging-karp.md. The three AI handler families
(chat_research_routes, thinking_routes, local_ai_routes_simple) all
need the same wrap-around logic on every long-running request:

1. Persist the user message to MongoDB (idempotent, defends against
   the frontend's parallel write).
2. Persist the assistant message after the AI returns (same idempotency
   guarantee).
3. Update the query_record's status / progress / result.
4. Track the in-flight asyncio.Task so a cancel endpoint can stop it.

Every helper here is a no-op when its inputs are missing — the legacy
flow (no chat_session_id, no query_record_id) works exactly as before
this phase landed. None of these helpers raise; they log and swallow,
so a Mongo blip can't poison the AI response itself.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from ..config.logging_config import logger
from ..infrastructure.chat_store import chat_store


# In-process registry of active AI tasks. Keyed by query_record_id so
# the matching POST /api/{mode}/cancel/{record_id} endpoint can cancel
# the right one. Populated by `register_active_task`, cleared by the
# context manager below.
#
# This is per-replica — cancellation across replicas relies on the
# query_record_id being uniformly addressable: any replica that ends
# up holding the live task will respond to the cancel; the OTHER
# replica's cancel call still records `status="cancelled"` in MongoDB,
# which the running pipeline can poll between phases.
_ACTIVE_TASKS: Dict[str, asyncio.Task] = {}


def register_active_task(query_record_id: str, task: asyncio.Task) -> None:
    """Track an in-flight task so the cancel endpoint can find it."""
    if not query_record_id:
        return
    _ACTIVE_TASKS[query_record_id] = task


def unregister_active_task(query_record_id: Optional[str]) -> None:
    """Clean up after a task completes (success, failure, or cancel)."""
    if not query_record_id:
        return
    _ACTIVE_TASKS.pop(query_record_id, None)


def cancel_active_task(query_record_id: str) -> bool:
    """Cancel the in-flight task for this record id. Returns True if a
    matching task was found AND cancellation was requested."""
    task = _ACTIVE_TASKS.get(query_record_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


async def persist_user_message(
    *,
    chat_session_id: Optional[str],
    user_id: Optional[str],
    client_id: Optional[str],
    prompt: str,
    idempotency_key: Optional[str],
) -> None:
    """
    Write the user message to MongoDB. Defense-in-depth alongside the
    frontend's `_persistChatMessage()`; the unique sparse index on
    `chat_messages.idempotency_key` collapses the two writes to one
    row when the same key is supplied (the frontend supplies the same
    key it sends here).

    Silent no-op if the chat_session_id is missing (back-compat with
    legacy calls that don't track a chat session).
    """
    if not chat_session_id or not prompt:
        return
    try:
        await chat_store.append_message(
            client_id=client_id or "",
            user_id=user_id,
            session_id=chat_session_id,
            role="user",
            content=prompt,
            format="text",
            idempotency_key=idempotency_key,
        )
    except KeyError:
        logger.warning(
            "persist_user_message_session_not_found",
            chat_session_id=chat_session_id,
        )
    except Exception as exc:
        logger.warning(
            "persist_user_message_failed",
            chat_session_id=chat_session_id,
            error=str(exc),
        )


async def persist_assistant_message(
    *,
    chat_session_id: Optional[str],
    user_id: Optional[str],
    client_id: Optional[str],
    content: str,
    ai_type: str,
    format: str = "text",
    extras: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> None:
    """Write the assistant message. Same dedupe + fail-open contract."""
    if not chat_session_id or not content:
        return
    try:
        await chat_store.append_message(
            client_id=client_id or "",
            user_id=user_id,
            session_id=chat_session_id,
            role="assistant",
            content=content,
            format=format,
            ai_type=ai_type,
            extras=extras or {},
            idempotency_key=idempotency_key,
        )
    except KeyError:
        logger.warning(
            "persist_assistant_message_session_not_found",
            chat_session_id=chat_session_id,
        )
    except Exception as exc:
        logger.warning(
            "persist_assistant_message_failed",
            chat_session_id=chat_session_id,
            error=str(exc),
        )


async def mark_query_completed(
    *,
    query_record_id: Optional[str],
    result_markdown: Optional[str] = None,
    sources: Optional[list] = None,
    tokens_used: Optional[int] = None,
) -> None:
    """Stamp the query record as completed. Silent no-op if no id."""
    if not query_record_id:
        return
    try:
        from datetime import datetime, timezone
        await chat_store.update_query_record(
            query_record_id,
            status="completed",
            progress=100,
            current_phase=None,
            current_task=None,
            result_markdown=result_markdown,
            sources=sources,
            tokens_used=tokens_used,
            completed_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.warning(
            "mark_query_completed_failed",
            query_record_id=query_record_id,
            error=str(exc),
        )


async def mark_query_failed(
    *,
    query_record_id: Optional[str],
    error: str,
) -> None:
    """Stamp the query record as failed."""
    if not query_record_id:
        return
    try:
        from datetime import datetime, timezone
        await chat_store.update_query_record(
            query_record_id,
            status="failed",
            error=error[:2000],
            completed_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.warning(
            "mark_query_failed_persist_failed",
            query_record_id=query_record_id,
            error=str(exc),
        )


async def mark_query_cancelled(
    *,
    query_record_id: Optional[str],
    reason: str = "Cancelled by user.",
) -> None:
    """Stamp the query record as cancelled."""
    if not query_record_id:
        return
    try:
        from datetime import datetime, timezone
        await chat_store.update_query_record(
            query_record_id,
            status="cancelled",
            error=reason[:2000],
            cancelled_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.warning(
            "mark_query_cancelled_persist_failed",
            query_record_id=query_record_id,
            error=str(exc),
        )
