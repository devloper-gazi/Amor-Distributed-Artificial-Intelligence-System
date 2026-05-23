"""
Cycle F Sprint 5 — approval-request bridge state.

In-memory + Redis-backed registry of open approval requests.
Engine threads call ``request_user_approval(...)``, which:

  1. Generates a request_id.
  2. Publishes an `approval_required` event on the session's SSE
     channel via `_publish`.
  3. Awaits a Future that the HTTP endpoint resolves.
  4. Returns True / False (or raises TimeoutError if the user
     doesn't respond inside the configured budget).

The Future lives in-process for the dispatch wait.  Cross-replica
resolution is handled via Redis pub/sub: when one replica receives
the POST, it broadcasts the decision; every replica's `_subscribe`
loop wakes any local Futures keyed by the same request_id.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import uuid4

logger = logging.getLogger(__name__)


_APPROVAL_REDIS_KEY_PREFIX = "amor:approval:req:"
_APPROVAL_REDIS_CHANNEL = "amor:approval:decision"


@dataclass
class AwaitingApproval:
    """One open approval request."""

    request_id: str
    session_id: str
    tool_name: str
    category: str
    arguments: Mapping[str, Any]
    actor_role: str | None
    created_at: float = field(default_factory=time.monotonic)
    future: asyncio.Future | None = None
    timeout_s: float = 90.0  # default — overridable per call

    def to_event(self) -> dict[str, Any]:
        return {
            "type": "approval_required",
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "category": self.category,
            "arguments": dict(self.arguments),
            "actor_role": self.actor_role,
            "timeout_s": self.timeout_s,
        }


# Process-local registry.  Cross-replica wakeups happen via the
# Redis subscription configured at app startup; the local registry
# only holds Futures actually being awaited on THIS replica.
_PENDING: dict[str, AwaitingApproval] = {}


def pending_count() -> int:
    return len(_PENDING)


def _get(request_id: str) -> AwaitingApproval | None:
    return _PENDING.get(request_id)


def _drop(request_id: str) -> AwaitingApproval | None:
    return _PENDING.pop(request_id, None)


async def _persist_to_redis(req: AwaitingApproval) -> None:
    """Record the request in Redis so a different replica can
    accept the POST and we can fan the decision back."""

    try:
        from ...infrastructure.cache import cache_manager  # noqa: PLC0415
        key = f"{_APPROVAL_REDIS_KEY_PREFIX}{req.request_id}"
        await cache_manager.set(
            key,
            req.to_event(),
            ttl=int(req.timeout_s) + 10,
        )
    except Exception as exc:  # pragma: no cover (defensive)
        logger.debug("approval_persist_to_redis_failed err=%s", exc)


async def _broadcast_decision(request_id: str, approved: bool) -> None:
    """Publish the decision to every replica via Redis pub/sub."""

    try:
        from ...infrastructure.cache import cache_manager  # noqa: PLC0415
        await cache_manager.publish_event(
            _APPROVAL_REDIS_CHANNEL,
            {"request_id": request_id, "approved": bool(approved)},
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("approval_broadcast_failed err=%s", exc)


# ─── Public entry points ────────────────────────────────────────────


async def request_user_approval(
    *,
    session_id: str,
    tool_name: str,
    category: str = "unclassified",
    arguments: Mapping[str, Any] | None = None,
    actor_role: str | None = None,
    timeout_s: float = 90.0,
    publish_fn: Any = None,
) -> bool:
    """Open an approval request + await the user's decision.

    Returns True (approved) / False (denied or timed out).

    `publish_fn(session_id, event)` is the SSE publish callable —
    accepted as a dependency so callers can wire it without
    creating an import cycle.  When None, the function attempts
    to import the code-intelligence `_publish` lazily.
    """

    arguments = dict(arguments or {})
    req_id = uuid4().hex
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    req = AwaitingApproval(
        request_id=req_id,
        session_id=session_id,
        tool_name=tool_name,
        category=category,
        arguments=arguments,
        actor_role=actor_role,
        future=fut,
        timeout_s=float(timeout_s),
    )
    _PENDING[req_id] = req

    await _persist_to_redis(req)

    if publish_fn is None:
        try:
            from ..code_intelligence_routes import _publish  # noqa: PLC0415
            publish_fn = _publish
        except Exception:
            publish_fn = None

    if publish_fn is not None:
        try:
            await publish_fn(session_id, req.to_event())
        except Exception as exc:  # pragma: no cover
            logger.warning("approval_publish_failed err=%s", exc)

    try:
        approved = await asyncio.wait_for(fut, timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.info(
            "approval_timeout request_id=%s tool=%s session=%s",
            req_id, tool_name, session_id,
        )
        approved = False
        # Notify subscribers that this request timed out so the UI
        # can clear the pending card.
        if publish_fn is not None:
            try:
                await publish_fn(session_id, {
                    "type": "approval_resolved",
                    "request_id": req_id,
                    "approved": False,
                    "reason": "timeout",
                })
            except Exception:
                pass
    finally:
        _drop(req_id)

    return bool(approved)


def resolve_approval(request_id: str, approved: bool) -> bool:
    """Resolve a pending approval future.  Returns True if a future
    was actually waiting on this replica (False if the request was
    registered on a different replica)."""

    req = _get(request_id)
    if req is None or req.future is None:
        return False
    if not req.future.done():
        req.future.set_result(bool(approved))
        return True
    return False


async def handle_cross_replica_decision(
    request_id: str, approved: bool,
) -> bool:
    """Called by the Redis subscription loop when a decision arrives
    from another replica.  Resolves the local Future if present."""

    resolved = resolve_approval(request_id, approved)
    if resolved:
        logger.info(
            "approval_resolved_via_redis request_id=%s approved=%s",
            request_id, approved,
        )
    return resolved


__all__ = [
    "AwaitingApproval",
    "handle_cross_replica_decision",
    "pending_count",
    "request_user_approval",
    "resolve_approval",
]
