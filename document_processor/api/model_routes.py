"""
Unified model-management API — ``/api/models/*``.

This is the single surface the new "More settings → AI Model" UI talks
to. It wraps :class:`ModelManager` and the per-user preference store on
``ChatStore`` to provide:

  * GET    /api/models                    — installed + catalogue, decorated
  * GET    /api/models/auto-select        — preview the auto pick
  * GET    /api/models/preference         — current user's per-mode prefs
  * PUT    /api/models/preference         — set a per-mode pref
  * DELETE /api/models/preference/{mode}  — clear a pref
  * POST   /api/models/pull               — SSE: pull tag from Ollama Hub
  * POST   /api/models/upload             — multipart: GGUF → ollama create
  * DELETE /api/models/custom/{tag}       — owner-only delete

Auth posture
------------
Authentication is *optional* on every endpoint — the app supports both
authenticated users (JWT-bearing) and anonymous clients (X-Client-Id
header). When a JWT is present the preference is keyed by ``user_id``;
otherwise it falls back to ``client_id``. This mirrors the rest of the
app (chat sessions, folders, etc.) and lets the picker work on a fresh
incognito tab.

Why a path-encoded tag for delete
---------------------------------
Ollama tags use ``/`` and ``:`` (``custom/foo:abc123``). Routing them as
``/{encoded_tag}`` with ``:path`` semantics keeps the URL flat without
needing the client to URL-encode slashes; ``custom/`` is the only legal
prefix for delete (we can't let a user blow away an official tag).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth.dependencies import get_optional_user
from ..auth.models import User
from ..code_intelligence.model_registry import CODE_MODEL_CATALOGUE
from ..infrastructure.chat_store import chat_store
from ..services.model_manager import (
    MAX_UPLOAD_SIZE_BYTES,
    ModelManager,
    OLLAMA_MODEL_DEFAULT,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/models", tags=["models"])


# ─── tiny helpers ────────────────────────────────────────────────────────────


VALID_MODES = {"research", "thinking", "coding", "code", "__all__"}
VALID_EFFORTS = {"basic", "medium", "deep", "expert", "ultra"}


def _require_client_id(x_client_id: Optional[str]) -> str:
    if not x_client_id or not x_client_id.strip():
        raise HTTPException(status_code=400, detail="Missing X-Client-Id header")
    return x_client_id.strip()


def _normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m not in VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode (got {mode!r}); expected one of {sorted(VALID_MODES)}",
        )
    return m


def _normalize_effort(effort: str) -> str:
    e = (effort or "medium").strip().lower()
    if e not in VALID_EFFORTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid effort (got {effort!r}); expected one of {sorted(VALID_EFFORTS)}",
        )
    return e


def _get_manager(request: Request) -> ModelManager:
    """Pull the lifespan-attached singleton off ``app.state``."""
    mgr: Optional[ModelManager] = getattr(request.app.state, "model_manager", None)
    if mgr is None:
        # Fallback — happens in test harness where lifespan isn't run.
        mgr = ModelManager()
        request.app.state.model_manager = mgr
    return mgr


# ─── pydantic schemas ────────────────────────────────────────────────────────


class PreferenceWriteRequest(BaseModel):
    """Body for ``PUT /preference`` — write a per-mode override."""

    mode: str = Field(..., description="research | thinking | coding | code | __all__")
    model_tag: str = Field(..., min_length=1, max_length=160)
    model_source: str = Field(
        "ollama_registry",
        description="ollama_registry | gguf_upload | custom",
        max_length=40,
    )
    display_name: Optional[str] = Field(None, max_length=120)


class PullModelRequest(BaseModel):
    """Body for ``POST /pull`` — pull from Ollama Hub."""

    tag: str = Field(..., min_length=1, max_length=160)


# ─── 1. GET /api/models ──────────────────────────────────────────────────────


@router.get("")
async def list_models(
    request: Request,
    mode: Optional[str] = None,
    effort: str = "medium",
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: Optional[User] = Depends(get_optional_user),
):
    """
    List installed Ollama tags + the curated catalogue, decorated with:

      * ``is_preferred`` — current user has chosen this for the queried mode
      * ``is_auto_selected`` — would be picked by auto-select right now
      * ``ollama_available`` — whether Ollama is reachable at all

    Mode + effort are optional — when omitted, ``__all__`` and ``medium``
    are used (so the list page works for users who haven't picked a mode).
    """
    client_id = _require_client_id(x_client_id)
    mode_n = _normalize_mode(mode or "__all__")
    effort_n = _normalize_effort(effort)
    user_id = user.id if user else None

    manager = _get_manager(request)
    installed = await manager.list_installed()
    ollama_up = bool(installed) or await _ollama_reachable(manager)

    # Per-user preference for the queried mode + the wildcard.
    pref_for_mode: Optional[str] = None
    pref_wildcard: Optional[str] = None
    try:
        pref_for_mode = await chat_store.get_model_preference(
            user_id=user_id, client_id=client_id, mode=mode_n,
        )
        # `get_model_preference` already falls back to wildcard, but we
        # also want to surface the wildcard explicitly to the UI so the
        # "applied across all modes" hint can render.
        if mode_n != chat_store.PREFERENCE_MODE_ALL:
            pref_wildcard = await chat_store.get_model_preference(
                user_id=user_id,
                client_id=client_id,
                mode=chat_store.PREFERENCE_MODE_ALL,
            )
    except Exception as exc:
        logger.warning("model_list_pref_lookup_failed: %s", exc)

    auto_tag, auto_reason = await manager.auto_select(mode=mode_n, effort=effort_n)

    installed_tags = {m.tag.lower() for m in installed}

    # Decorate installed entries.
    installed_payload: list[dict[str, Any]] = []
    for m in installed:
        spec = m.spec
        installed_payload.append({
            "tag": m.tag,
            "display_name": m.display_name or m.tag,
            "size_bytes": m.size_bytes,
            "modified_at": m.modified_at,
            "is_custom": m.is_custom,
            "is_preferred": (pref_for_mode or "").lower() == m.tag.lower(),
            "is_auto_selected": (auto_tag or "").lower() == m.tag.lower(),
            "spec": spec.to_dict() if spec else None,
            "source": "gguf_upload" if m.is_custom else (
                "ollama_registry" if spec else "ollama_local"
            ),
        })

    # Catalogue entries — anything from CODE_MODEL_CATALOGUE the user
    # could pull but hasn't yet. Marked ``is_installed=False`` so the UI
    # can render a Pull button.
    catalogue_payload: list[dict[str, Any]] = []
    for spec in CODE_MODEL_CATALOGUE:
        is_installed = spec.ollama_tag.lower() in installed_tags
        catalogue_payload.append({
            "tag": spec.ollama_tag,
            "display_name": spec.display_name,
            "is_installed": is_installed,
            "is_preferred": (pref_for_mode or "").lower() == spec.ollama_tag.lower(),
            "is_auto_selected": (auto_tag or "").lower() == spec.ollama_tag.lower(),
            "spec": spec.to_dict(),
            "source": "ollama_registry",
        })

    return {
        "installed": installed_payload,
        "catalogue": catalogue_payload,
        "ollama_available": ollama_up,
        "default_tag": OLLAMA_MODEL_DEFAULT,
        "active_preference": {
            "mode": mode_n,
            "tag": pref_for_mode,
            "wildcard_tag": pref_wildcard,
        },
        "auto_select": {
            "mode": mode_n,
            "effort": effort_n,
            "tag": auto_tag,
            "reason": auto_reason,
        },
    }


async def _ollama_reachable(manager: ModelManager) -> bool:
    """Quick reachability probe — used when ``list_installed`` returns []
    so we can distinguish "Ollama up, no models" from "Ollama down"."""
    try:
        # Trip a fresh probe; failure is swallowed inside list_installed.
        await manager.list_installed(force_refresh=True)
        return True
    except Exception:
        return False


# ─── 2. GET /api/models/auto-select ──────────────────────────────────────────


@router.get("/auto-select")
async def preview_auto_select(
    request: Request,
    mode: str = "__all__",
    effort: str = "medium",
    user: Optional[User] = Depends(get_optional_user),
):
    """Return what auto-select would pick right now — UI uses this to
    fill the "Auto" tooltip so the user can see *why* and *what*."""
    mode_n = _normalize_mode(mode)
    effort_n = _normalize_effort(effort)
    manager = _get_manager(request)
    tag, reason = await manager.auto_select(mode=mode_n, effort=effort_n)
    return {
        "mode": mode_n,
        "effort": effort_n,
        "tag": tag,
        "reason": reason,
    }


# ─── 3. GET /api/models/preference ───────────────────────────────────────────


@router.get("/preference")
async def get_preferences(
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: Optional[User] = Depends(get_optional_user),
):
    """Return all per-mode preferences for this user/client."""
    client_id = _require_client_id(x_client_id)
    user_id = user.id if user else None
    prefs = await chat_store.get_all_model_preferences(
        user_id=user_id, client_id=client_id,
    )
    return {
        "preferences": prefs,
        "user_scoped": bool(user_id),
        "client_id": None if user_id else client_id,
    }


# ─── 4. PUT /api/models/preference ───────────────────────────────────────────


@router.put("/preference")
async def set_preference(
    body: PreferenceWriteRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: Optional[User] = Depends(get_optional_user),
):
    """Set a per-mode preference. ``mode="__all__"`` writes the wildcard."""
    client_id = _require_client_id(x_client_id)
    user_id = user.id if user else None
    mode_n = _normalize_mode(body.mode)

    await chat_store.set_model_preference(
        user_id=user_id,
        client_id=client_id,
        mode=mode_n,
        model_tag=body.model_tag.strip(),
        model_source=body.model_source,
        display_name=body.display_name,
    )
    logger.info(
        "model_preference_set user_scoped=%s mode=%s tag=%s",
        bool(user_id), mode_n, body.model_tag,
    )
    return {"ok": True, "mode": mode_n, "model_tag": body.model_tag}


# ─── 5. DELETE /api/models/preference/{mode} ─────────────────────────────────


@router.delete("/preference/{mode}")
async def delete_preference(
    mode: str,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: Optional[User] = Depends(get_optional_user),
):
    """Clear the preference for one mode (or "__all__" to clear the wildcard)."""
    client_id = _require_client_id(x_client_id)
    user_id = user.id if user else None
    mode_n = _normalize_mode(mode)

    deleted = await chat_store.delete_model_preference(
        user_id=user_id, client_id=client_id, mode=mode_n,
    )
    return {"ok": True, "mode": mode_n, "deleted": deleted}


# ─── 6. POST /api/models/pull ────────────────────────────────────────────────


@router.post("/pull")
async def pull_model(
    body: PullModelRequest,
    request: Request,
    user: Optional[User] = Depends(get_optional_user),
):
    """Stream Ollama pull progress as SSE.

    Reads the tag from the request body (not the URL) so it works for
    arbitrary-shaped tags (``custom/foo:bar``, ``library/qwen2.5:7b``,
    ``hf.co/Org/Repo:Q4_K_M``) without URL-encoding gymnastics.
    """
    manager = _get_manager(request)
    tag = body.tag.strip()
    if not tag:
        raise HTTPException(status_code=400, detail="tag is required")

    async def event_stream():
        try:
            async for event in manager.pull_model_stream(tag):
                yield f"data: {json.dumps(event)}\n\n"
                # Stop reading the AsyncIterator if the client hung up —
                # otherwise httpx happily drains the whole pull into the
                # void.
                if await request.is_disconnected():
                    logger.info("model_pull_client_disconnected tag=%s", tag)
                    return
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as exc:
            logger.exception("model_pull_stream_failed tag=%s", tag)
            yield (
                f"data: {json.dumps({'type': 'pull_error', 'tag': tag, 'error': str(exc)})}\n\n"
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── 7. POST /api/models/upload ──────────────────────────────────────────────


@router.post("/upload")
async def upload_gguf(
    request: Request,
    file: UploadFile = File(..., description="GGUF model weights"),
    display_name: Optional[str] = Form(None),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: Optional[User] = Depends(get_optional_user),
):
    """
    Accept a multipart ``.gguf`` upload, validate the magic bytes, write
    it under ``CUSTOM_MODELS_DIR/<owner>/``, generate a ``Modelfile``,
    and call ``ollama create`` to register a ``custom/<name>:<hash>`` tag.

    Pre-checks
    ----------
    * ``Content-Length`` must be present and ≤ ``MAX_UPLOAD_SIZE_BYTES``
      (default 50 GB) — we 413 *before* draining the stream so a
      misbehaving uploader can't tie up the worker for hours.
    * Filename must end in ``.gguf`` (case-insensitive).
    """
    client_id = _require_client_id(x_client_id)
    user_id = user.id if user else None
    manager = _get_manager(request)

    # Cheap precheck — refuse before reading the body.
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Upload too large ({int(cl) / 1024**3:.1f} GB); limit is "
                f"{MAX_UPLOAD_SIZE_BYTES // (1024**3)} GB."
            ),
        )

    filename = (file.filename or "model.gguf").strip()
    if not filename.lower().endswith(".gguf"):
        raise HTTPException(
            status_code=400,
            detail="File must be a .gguf model weights file.",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file was empty.")

    try:
        result = await manager.import_gguf(
            user_id=user_id,
            client_id=client_id,
            filename=filename,
            file_bytes=file_bytes,
            display_name=display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.warning("model_upload_create_failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info(
        "model_upload_ok user_scoped=%s tag=%s size_mb=%.1f",
        bool(user_id), result["tag"], len(file_bytes) / 1024**2,
    )
    return {
        "ok": True,
        "tag": result["tag"],
        "display_name": result["display_name"],
        "size_bytes": len(file_bytes),
    }


# ─── 8. DELETE /api/models/custom/{tag:path} ─────────────────────────────────


@router.delete("/custom/{tag:path}")
async def delete_custom_model(
    tag: str,
    request: Request,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: Optional[User] = Depends(get_optional_user),
):
    """Owner-only deletion of a previously uploaded GGUF model."""
    client_id = _require_client_id(x_client_id)
    user_id = user.id if user else None
    manager = _get_manager(request)

    if not tag.lower().startswith("custom/"):
        raise HTTPException(
            status_code=400,
            detail="Only custom-uploaded models (custom/...) can be deleted here.",
        )

    try:
        await manager.delete_custom_model(
            tag=tag, user_id=user_id, client_id=client_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("model_delete_failed tag=%s", tag)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Also clear any preference rows that pointed at this now-gone tag,
    # so the UI doesn't keep showing it as "active".
    try:
        all_prefs = await chat_store.get_all_model_preferences(
            user_id=user_id, client_id=client_id,
        )
        for mode, doc in all_prefs.items():
            if (doc.get("model_tag") or "").lower() == tag.lower():
                await chat_store.delete_model_preference(
                    user_id=user_id, client_id=client_id, mode=mode,
                )
    except Exception as exc:  # pragma: no cover
        logger.warning("model_delete_pref_cleanup_failed: %s", exc)

    return {"ok": True, "tag": tag}
