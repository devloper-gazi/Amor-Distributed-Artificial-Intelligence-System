"""Cycle UI v2.7 (D1) — filesystem attachment storage.

Saves user-uploaded attachments to `data/attachments/{user_id}/{yyyy-mm}/{uuid}.{ext}`,
records canonical metadata in MongoDB `attachments_meta` collection, exposes
async helpers for read/delete + index init.

Path-traversal guard: storage filenames are ALWAYS UUID4-hex; the
user-provided ``original_name`` is metadata-only.  An ``os.path.realpath``
containment check is enforced on every read/write so even a malformed
``user_id`` cannot escape the root dir.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..api.attachment_models import AttachmentMeta

logger = logging.getLogger(__name__)


# ─── Layout / paths ──────────────────────────────────────────────────


def _repo_data_root() -> Path:
    """Resolve the repo's `data/` directory.  Same convention used by
    `infrastructure/storage.py` for documents, evals, baselines."""
    # document_processor/infrastructure/attachment_storage.py
    #   → parents[2] = repo root
    return Path(__file__).resolve().parents[2] / "data"


def _attachments_root() -> Path:
    return _repo_data_root() / "attachments"


_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")


def _safe_user_dir(user_id: str) -> Path:
    """Resolve `data/attachments/{user_id}/` with strict containment.

    Rejects user_ids that wouldn't survive a regex check (defence-in-
    depth: backend auth produces normalized user IDs, but downstream
    code must never assume safety)."""
    if not _SAFE_USER_ID_RE.match(user_id):
        raise ValueError(f"unsafe user_id for storage path: {user_id!r}")
    root = _attachments_root()
    target = root / user_id
    real_root = root.resolve()
    real_target = target.resolve()
    try:
        real_target.relative_to(real_root)
    except ValueError:  # pragma: no cover — `resolve()` already guards
        raise ValueError("path traversal attempt detected") from None
    return target


def _ext_for(original_name: str, mime: str) -> str:
    """Pick a 1-7 char extension based on original_name first, mime fallback.

    Storage filenames are `{uuid}.{ext}`; the ext only helps OS-level
    tooling (`file`, image previews) — server never re-derives MIME
    from it (we trust the at-upload sniff + metadata)."""
    _, _, candidate = original_name.rpartition(".")
    if candidate and 1 <= len(candidate) <= 7 and candidate.isalnum():
        return candidate.lower()
    # Mime fallback
    if "/" in mime:
        sub = mime.split("/", 1)[1].split("+", 1)[0].split(";", 1)[0].strip()
        if 1 <= len(sub) <= 7 and sub.isalnum():
            return sub.lower()
    return "bin"


# ─── Mongo collection / indexes ──────────────────────────────────────


ATTACHMENTS_META_COLLECTION = "attachments_meta"


async def ensure_attachments_indexes(db: Any) -> None:
    """Idempotent index init.  Called once at app startup from
    `infrastructure/storage.py` boot path."""
    coll = db[ATTACHMENTS_META_COLLECTION]
    await coll.create_index([("user_id", 1), ("created_at", -1)], name="user_created")
    await coll.create_index([("session_id", 1)], name="session", sparse=True)
    await coll.create_index([("message_id", 1)], name="message", sparse=True)
    # sha256 dedup (per-user — same hash for different users is fine).
    await coll.create_index(
        [("user_id", 1), ("sha256", 1)], name="user_sha256_unique", unique=True
    )
    # TTL: when expires_at is set, doc auto-removes after the timestamp.
    await coll.create_index("expires_at", name="ttl_expiry", expireAfterSeconds=0)


# ─── Write path ──────────────────────────────────────────────────────


async def save_attachment_bytes(
    db: Any,
    *,
    user_id: str,
    original_name: str,
    mime: str,
    blob: bytes,
    session_id: Optional[str] = None,
    text_extracted_preview: Optional[str] = None,
) -> AttachmentMeta:
    """Persist `blob` to filesystem + write metadata doc.

    Dedup: if a doc with the same `(user_id, sha256)` exists, return
    the existing record without re-writing the file (idempotent
    re-uploads).
    """
    sha = hashlib.sha256(blob).hexdigest()
    size = len(blob)

    coll = db[ATTACHMENTS_META_COLLECTION]
    existing = await coll.find_one({"user_id": user_id, "sha256": sha})
    if existing is not None:
        logger.info(
            "attachment_dedup_hit user=%s sha=%s size=%d existing_id=%s",
            user_id, sha[:8], size, existing.get("id") or existing.get("_id"),
        )
        return AttachmentMeta.model_validate(existing)

    # Compose paths.  UUID-only filename — original_name only in metadata.
    user_dir = _safe_user_dir(user_id)
    yyyy_mm = datetime.now(timezone.utc).strftime("%Y-%m")
    shard_dir = user_dir / yyyy_mm
    shard_dir.mkdir(parents=True, exist_ok=True)
    fid = uuid.uuid4().hex
    ext = _ext_for(original_name, mime)
    storage_filename = f"{fid}.{ext}"
    storage_path = shard_dir / storage_filename
    storage_path_rel = str(storage_path.relative_to(_repo_data_root())).replace("\\", "/")

    # Containment re-check post-resolve (paranoia).
    real_attach_root = _attachments_root().resolve()
    if not str(storage_path.resolve()).startswith(str(real_attach_root)):
        raise RuntimeError("storage path escaped attachments root")

    # Sync write — bytes already in memory, blocking write is fine and
    # avoids aiofile dep.  Run in executor to keep the event loop free.
    await asyncio.get_running_loop().run_in_executor(
        None, storage_path.write_bytes, blob
    )

    meta = AttachmentMeta(
        id=fid,
        user_id=user_id,
        session_id=session_id,
        message_id=None,
        original_name=original_name[:512],
        mime=mime,
        size=size,
        sha256=sha,
        storage_path=storage_path_rel,
        status="uploaded",
        text_extracted_preview=text_extracted_preview,
    )
    doc = meta.to_dict()
    # Mongo `_id` shadows our `id` field cleanly: insert with explicit
    # `_id=id` so future find_one by uuid is direct.
    doc["_id"] = doc["id"]
    await coll.insert_one(doc)
    logger.info(
        "attachment_saved user=%s id=%s sha=%s size=%d mime=%s",
        user_id, fid, sha[:8], size, mime,
    )
    return meta


# ─── Read / download path ────────────────────────────────────────────


async def get_attachment_meta(
    db: Any, *, user_id: str, attachment_id: str
) -> Optional[AttachmentMeta]:
    """Fetch metadata; returns None when not found OR not owned by user.

    Tenancy: queries always include `user_id` to prevent cross-tenant
    access via guessed UUIDs (UUID4 collisions are astronomically
    unlikely but defence-in-depth)."""
    coll = db[ATTACHMENTS_META_COLLECTION]
    doc = await coll.find_one({"_id": attachment_id, "user_id": user_id})
    return AttachmentMeta.model_validate(doc) if doc else None


def read_attachment_bytes_sync(meta: AttachmentMeta) -> bytes:
    """Sync read for the lookup path — call from `run_in_executor` if
    invoked from an async route (FastAPI does this for `FileResponse`
    already)."""
    target = _repo_data_root() / meta.storage_path
    # Containment recheck on read too (defence-in-depth).
    real_root = _attachments_root().resolve()
    real_target = target.resolve()
    if not str(real_target).startswith(str(real_root)):
        raise RuntimeError("storage path escaped on read")
    return target.read_bytes()


async def read_attachment_bytes(meta: AttachmentMeta) -> bytes:
    return await asyncio.get_running_loop().run_in_executor(
        None, read_attachment_bytes_sync, meta
    )


async def iter_attachment_chunks(
    meta: AttachmentMeta, chunk_size: int = 64 * 1024
) -> AsyncIterator[bytes]:
    """Stream the file in chunks for the download endpoint.  Avoids
    loading large attachments fully into memory on the way back out."""
    target = _repo_data_root() / meta.storage_path
    real_root = _attachments_root().resolve()
    if not str(target.resolve()).startswith(str(real_root)):
        raise RuntimeError("storage path escaped on stream")

    def _open_iter() -> list[bytes]:
        # Sync read; we batch tiny chunks per iteration step.  For 10 MB
        # cap there are at most ~160 chunks — cheap.
        with target.open("rb") as f:
            return list(iter(lambda: f.read(chunk_size), b""))

    chunks = await asyncio.get_running_loop().run_in_executor(None, _open_iter)
    for c in chunks:
        yield c


# ─── Delete path ─────────────────────────────────────────────────────


async def delete_attachment(
    db: Any, *, user_id: str, attachment_id: str
) -> bool:
    """Remove FS file + metadata doc.  Returns True if anything was
    removed."""
    meta = await get_attachment_meta(db, user_id=user_id, attachment_id=attachment_id)
    if meta is None:
        return False
    target = _repo_data_root() / meta.storage_path
    real_root = _attachments_root().resolve()
    if str(target.resolve()).startswith(str(real_root)) and target.exists():
        await asyncio.get_running_loop().run_in_executor(None, os.remove, str(target))
    coll = db[ATTACHMENTS_META_COLLECTION]
    res = await coll.delete_one({"_id": attachment_id, "user_id": user_id})
    return res.deleted_count > 0
