"""
Server-side model resolution helper.

Single function the route handlers call when they need to know "which
Ollama tag should this run use?". Resolution order matches the spec:

  1. ``request.preferred_model`` — explicit override on the wire
     (the picker JS writes here when the user selected a model)
  2. ``chat_store.get_model_preference(mode)`` — per-user/client mode pref
     (already falls back internally to the ``__all__`` wildcard pref)
  3. ``ModelManager.auto_select(mode, effort)`` — best installed model

A return of ``None`` means "no override — let ``call_ollama`` use
``OLLAMA_MODEL``". The caller stashes the resolved tag onto the session
payload so background workers can read it via the ``_ACTIVE_MODEL``
ContextVar without re-querying.

Why a separate module
---------------------
Three route files (research / thinking / code) need this same logic,
and they all already import from both ``services/model_manager`` and
``infrastructure/chat_store`` — putting the helper here keeps the import
graph flat (no route → route imports).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Request

from ..infrastructure.chat_store import chat_store
from .model_manager import ModelManager

logger = logging.getLogger(__name__)


async def resolve_request_model(
    *,
    request: Request,
    requested_model: Optional[str],
    user_id: Optional[str],
    client_id: str,
    mode: str,
    effort: str = "medium",
) -> tuple[Optional[str], str]:
    """
    Resolve the effective Ollama tag for a request.

    Returns ``(tag, reason)``. ``tag`` is ``None`` only when no
    preference exists *and* auto-select chose the env-default — in
    which case the call sites can let ``call_ollama_with(None, …)``
    read ``OLLAMA_MODEL`` like they always have.
    """
    # 1) Direct request override always wins (the user just clicked
    #    "Pull + Use" or similar in the picker).
    if requested_model and requested_model.strip():
        return requested_model.strip(), "request override"

    # 2) Look up persisted user/client preference for this exact mode
    #    (the chat_store helper already falls back to the wildcard).
    try:
        pref = await chat_store.get_model_preference(
            user_id=user_id, client_id=client_id, mode=mode,
        )
        if pref:
            return pref, f"user preference ({mode})"
    except Exception as exc:
        logger.warning("resolve_request_model_pref_lookup_failed: %s", exc)

    # 3) Auto-select via ModelManager (singleton on app.state).
    manager: Optional[ModelManager] = getattr(
        request.app.state, "model_manager", None,
    )
    if manager is None:
        # In tests / minimal harness lifespan may not have run.
        manager = ModelManager()
        request.app.state.model_manager = manager

    try:
        tag, reason = await manager.auto_select(mode=mode, effort=effort)
        return tag, reason
    except Exception as exc:
        logger.warning("resolve_request_model_auto_select_failed: %s", exc)
        return None, "fallback to OLLAMA_MODEL"


async def resolve_request_model_full(
    *,
    request: Request,
    requested_model: Optional[str],
    user_id: Optional[str],
    client_id: str,
    mode: str,
    effort: str = "medium",
) -> tuple[Optional[str], Optional[dict], str]:
    """
    v3 — like ``resolve_request_model`` but also returns the saved
    advanced-options profile when one is bound to the resolved tag.

    Returns ``(tag, profile, reason)``. ``profile`` is ``None`` unless
    the resolved tag came from a stored user preference *and* that
    preference has a non-empty profile dict.
    """
    # 1) Request override — no profile from MongoDB; the picker would
    # have written the profile alongside before sending the request.
    if requested_model and requested_model.strip():
        return requested_model.strip(), None, "request override"

    # 2) Persisted preference (full doc, including profile).
    try:
        full = await chat_store.get_model_preference_full(
            user_id=user_id, client_id=client_id, mode=mode,
        )
        if full and full.get("model_tag"):
            return (
                str(full["model_tag"]),
                full.get("profile") or None,
                f"user preference ({full.get('mode') or mode})",
            )
    except Exception as exc:
        logger.warning("resolve_request_model_full_pref_lookup_failed: %s", exc)

    # 3) Fall through to auto-select (no stored profile).
    manager: Optional[ModelManager] = getattr(
        request.app.state, "model_manager", None,
    )
    if manager is None:
        manager = ModelManager()
        request.app.state.model_manager = manager

    try:
        tag, reason = await manager.auto_select(mode=mode, effort=effort)
        return tag, None, reason
    except Exception as exc:
        logger.warning("resolve_request_model_auto_select_failed: %s", exc)
        return None, None, "fallback to OLLAMA_MODEL"


def header_client_id(headers) -> Optional[str]:
    """Extract X-Client-Id from a Starlette/FastAPI Headers object."""
    cid = headers.get("X-Client-Id") or headers.get("x-client-id")
    return cid.strip() if cid and cid.strip() else None
