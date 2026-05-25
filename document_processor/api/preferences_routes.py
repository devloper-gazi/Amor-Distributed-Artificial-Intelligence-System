"""Cycle UI v2.8.7 — user preferences API.

Cross-device persistence of UI flags (currently just ``auto_mode``).
Stored in MongoDB collection ``user_preferences``, keyed on
``user_id``.  Frontend localStorage holds the same value for instant
read; this endpoint syncs across devices.

Endpoints:
    GET   /api/preferences  -> returns the user's stored prefs
                                (auto_mode default = True if missing)
    PATCH /api/preferences  -> upsert one or more keys

The collection is lazy-created on first write; readers without an
existing document get the all-defaults shape so first-time users
don't see a 404.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from document_processor.auth.dependencies import get_current_user
from document_processor.infrastructure.storage import storage_manager


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/preferences", tags=["preferences"])


# ─── Pydantic models ──────────────────────────────────────────────


class PreferencesResponse(BaseModel):
    """All preference keys with their current value for this user.

    New keys must always have a server-side default so a frontend
    upgrade doesn't 404 on a missing field."""

    model_config = ConfigDict(extra="ignore")

    auto_mode: bool = Field(
        default=True,
        description=(
            "When true the composer's mode is auto-detected on every "
            "message; the user doesn't see/use the ModePicker."
        ),
    )


class PreferencesPatch(BaseModel):
    """Partial update — every field optional.  PATCH semantics: only
    fields present in the body are written; the rest are untouched."""

    model_config = ConfigDict(extra="ignore")

    auto_mode: bool | None = None


# ─── Mongo collection accessor ────────────────────────────────────


_COLLECTION_NAME = "user_preferences"


async def _collection() -> Any:
    """Return the Motor collection.  ``storage_manager.mongo_db`` is
    the process-wide async Motor handle initialised at FastAPI
    startup; we reuse it without per-request opens."""
    db = storage_manager.mongo_db
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB not available",
        )
    return db[_COLLECTION_NAME]


# ─── Routes ────────────────────────────────────────────────────────


@router.get("", response_model=PreferencesResponse)
async def get_preferences(
    user: Any = Depends(get_current_user),
) -> PreferencesResponse:
    """Return the user's stored prefs (or defaults if no document)."""
    col = await _collection()
    doc = await col.find_one({"user_id": str(user.id)})
    if doc is None:
        return PreferencesResponse()  # all defaults
    # Strip Mongo metadata + cast through Pydantic so any new key
    # added later auto-defaults instead of leaking _id / user_id.
    doc.pop("_id", None)
    doc.pop("user_id", None)
    return PreferencesResponse(**doc)


@router.patch("", response_model=PreferencesResponse)
async def patch_preferences(
    patch: PreferencesPatch,
    user: Any = Depends(get_current_user),
) -> PreferencesResponse:
    """Upsert the user's prefs with the supplied delta.

    Only fields explicitly present in the request body are written.
    Returns the FULL post-patch document so the frontend can
    overwrite its local state without a follow-up GET."""
    set_fields: Dict[str, Any] = {}
    for key, value in patch.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        set_fields[key] = value

    if not set_fields:
        # No-op patch: just return current state.
        return await get_preferences(user=user)

    col = await _collection()
    await col.update_one(
        {"user_id": str(user.id)},
        {"$set": {**set_fields, "user_id": str(user.id)}},
        upsert=True,
    )
    logger.debug(
        "preferences_patched user=%s keys=%s",
        getattr(user, "id", "?"),
        list(set_fields.keys()),
    )
    return await get_preferences(user=user)
