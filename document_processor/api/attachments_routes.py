"""Cycle UI v2.7 (D2) — attachment upload/download/delete endpoints.

POST `/api/attachments/upload` — multipart/form-data, single file per
request.  Returns canonical metadata so the browser can stash the
`attachment_id` against the composer state.

GET  `/api/attachments/{id}` — stream the file back (Inline disposition,
auth-guarded by user_id tenancy on the metadata).

DELETE `/api/attachments/{id}` — orphan cleanup (called when the user
removes a chip BEFORE submitting; persisted-into-message attachments
are managed by their host message and never deleted here).

Bound at app start: `app.include_router(attachments_router)` in
`document_processor/main.py` (see `_PEND` patch).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from ..auth.dependencies import get_current_user, User
from ..infrastructure.attachment_storage import (
    delete_attachment,
    get_attachment_meta,
    iter_attachment_chunks,
    save_attachment_bytes,
)
from ..infrastructure.chat_store import chat_store
from .attachment_models import (
    ATTACH_MAX_SINGLE_BYTES,
    AttachmentUploadResponse,
    DOC_MIMES,
    IMAGE_MIME_PREFIXES,
    REJECT_MIME_PREFIXES,
    TEXT_INLINE_MIME_PREFIXES,
    CODE_EXT_FALLBACK,
    INLINE_PREVIEW_CHARS,
)

logger = logging.getLogger(__name__)

attachments_router = APIRouter(prefix="/api/attachments", tags=["attachments"])


# ─── Helpers ─────────────────────────────────────────────────────────


def _mime_allowed(mime: str, filename: str) -> tuple[bool, str]:
    """Return (allowed, reason).  Defence-in-depth — frontend has the
    same whitelist but we re-verify server-side."""
    if not mime:
        mime = "application/octet-stream"
    if any(mime.startswith(p) for p in REJECT_MIME_PREFIXES):
        return False, f"MIME {mime} rejected (executable/archive)"
    if any(mime.startswith(p) for p in TEXT_INLINE_MIME_PREFIXES):
        return True, "text"
    if any(mime.startswith(p) for p in IMAGE_MIME_PREFIXES):
        return True, "image"
    if mime in DOC_MIMES:
        return True, "doc"
    # Extension fallback when client sends generic octet-stream.
    name_lower = filename.lower() if filename else ""
    for ext in CODE_EXT_FALLBACK:
        if name_lower.endswith(ext):
            return True, "code-by-ext"
    return False, f"MIME {mime} not in whitelist"


def _safe_preview(blob: bytes, mime: str) -> Optional[str]:
    """First N chars of utf-8 decoded blob for inline preview.  Only
    populated for text-ish MIMEs; binary/image preview always None."""
    if not any(mime.startswith(p) for p in TEXT_INLINE_MIME_PREFIXES):
        return None
    try:
        return blob.decode("utf-8", errors="replace")[:INLINE_PREVIEW_CHARS]
    except Exception:
        return None


# ─── POST /api/attachments/upload ────────────────────────────────────


@attachments_router.post("/upload", response_model=AttachmentUploadResponse)
async def upload_attachment(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(default=None),
    user: User = Depends(get_current_user),
) -> AttachmentUploadResponse:
    """Persist one user-uploaded file.  Single-file per request so the
    browser can fire `Promise.all([upload(f1), upload(f2)])` for
    parallel progress UI."""
    filename = (file.filename or "untitled").strip()
    mime = file.content_type or "application/octet-stream"
    user_id = str(user.id) if hasattr(user, "id") else str(user)

    # 1) MIME whitelist
    ok, reason = _mime_allowed(mime, filename)
    if not ok:
        logger.info("attachment_reject_mime user=%s mime=%s reason=%s", user_id, mime, reason)
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {reason}")

    # 2) Read fully into memory (10 MB cap → cheap).  Streaming chunked
    #    upload deferred to v2.9.
    blob = await file.read()
    if len(blob) == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(blob) > ATTACH_MAX_SINGLE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(blob)} bytes, max {ATTACH_MAX_SINGLE_BYTES})",
        )

    # 3) Persist (FS write + dedup by sha256 + Mongo metadata insert).
    db = await chat_store._db()
    preview = _safe_preview(blob, mime)
    meta = await save_attachment_bytes(
        db,
        user_id=user_id,
        original_name=filename,
        mime=mime,
        blob=blob,
        session_id=session_id,
        text_extracted_preview=preview,
    )

    return AttachmentUploadResponse(
        attachment_id=meta.id,
        mime=meta.mime,
        size=meta.size,
        sha256=meta.sha256,
        status=meta.status,
        text_extracted_preview=meta.text_extracted_preview,
        original_name=meta.original_name,
    )


# ─── GET /api/attachments/{id} ───────────────────────────────────────


@attachments_router.get("/{attachment_id}")
async def download_attachment(
    attachment_id: str,
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Download / preview.  Tenancy enforced by `get_attachment_meta`
    (returns None when not owned by `user_id`).  Inline disposition
    so browser previews images/PDFs directly when possible."""
    user_id = str(user.id) if hasattr(user, "id") else str(user)
    db = await chat_store._db()
    meta = await get_attachment_meta(db, user_id=user_id, attachment_id=attachment_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    headers = {
        # Inline so images/PDFs preview; download triggered by FE link.
        "Content-Disposition": f'inline; filename="{meta.original_name}"',
        "Content-Length": str(meta.size),
        "Cache-Control": "private, max-age=300",
    }
    return StreamingResponse(
        iter_attachment_chunks(meta),
        media_type=meta.mime,
        headers=headers,
    )


# ─── DELETE /api/attachments/{id} ────────────────────────────────────


@attachments_router.delete("/{attachment_id}")
async def remove_attachment(
    attachment_id: str,
    user: User = Depends(get_current_user),
) -> dict:
    """Best-effort cleanup (chip × button when user un-attaches before
    submit).  Never errors on missing — idempotent."""
    user_id = str(user.id) if hasattr(user, "id") else str(user)
    db = await chat_store._db()
    deleted = await delete_attachment(
        db, user_id=user_id, attachment_id=attachment_id
    )
    return {"deleted": deleted, "attachment_id": attachment_id}
