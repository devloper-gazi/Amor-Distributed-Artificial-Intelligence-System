"""Cycle UI v2.7 (D6) — attachment-id → prompt context block resolver.

Called at the head of every chat `/start` endpoint that inherits
`AttachmentBearingRequest`.  Loads metadata + bytes for each id,
classifies the inclusion strategy (inline_text / image_ref /
filename_only) and returns:

* `enriched_prompt`: original prompt prefixed with one or more
  ``<!-- AMOR-ATTACH:START --> ... <!-- AMOR-ATTACH:END -->`` blocks.
* `image_refs`: list of (mime, bytes) tuples a multimodal LLM
  backend can forward to the model directly.  Empty when no images
  attached OR no vision-capable model active.
* `message_attachments`: list[MessageAttachmentRef] to persist on the
  user_message doc.

Per-file inline cap: 32 KB (`INLINE_TEXT_CAP_BYTES`).
Total inline budget: 96 KB (`MAX_TOTAL_INLINE_BYTES`) — once exceeded,
remaining files fall back to ``filename_only`` so the prompt budget
stays predictable.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .attachment_models import (
    INLINE_PREVIEW_CHARS,
    INLINE_TEXT_CAP_BYTES,
    MAX_TOTAL_INLINE_BYTES,
    IMAGE_MIME_PREFIXES,
    TEXT_INLINE_MIME_PREFIXES,
    DOC_MIMES,
    CODE_EXT_FALLBACK,
    MessageAttachmentRef,
    AttachmentMeta,
)
from ..infrastructure.attachment_storage import (
    get_attachment_meta,
    read_attachment_bytes,
)

logger = logging.getLogger(__name__)


# ─── Inclusion classifier ────────────────────────────────────────────


def _is_text_inline(meta: AttachmentMeta) -> bool:
    if any(meta.mime.startswith(p) for p in TEXT_INLINE_MIME_PREFIXES):
        return True
    # Extension fallback for `application/octet-stream` from drag-drop.
    name = meta.original_name.lower()
    for ext in CODE_EXT_FALLBACK:
        if name.endswith(ext):
            return True
    return False


def _is_image(meta: AttachmentMeta) -> bool:
    return any(meta.mime.startswith(p) for p in IMAGE_MIME_PREFIXES)


def _is_pdf(meta: AttachmentMeta) -> bool:
    return meta.mime in DOC_MIMES


# ─── Per-file content extraction ─────────────────────────────────────


def _safe_decode(blob: bytes) -> str:
    """Try utf-8 then latin-1 fallback.  Never raises — binary files
    fall back to repr-style escape so the prompt still renders sane
    even if the model can't parse it."""
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return blob.decode("latin-1")
        except UnicodeDecodeError:
            return blob.decode("utf-8", errors="replace")


def _truncate_inline(text: str, cap: int) -> tuple[str, int]:
    """Return (truncated_text, dropped_bytes)."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= cap:
        return text, 0
    head = encoded[:cap]
    # Try to land on a UTF-8 codepoint boundary so the truncation doesn't
    # produce mojibake.
    for back in range(0, 4):
        try:
            return head[: len(head) - back].decode("utf-8"), len(encoded) - len(head) + back
        except UnicodeDecodeError:
            continue
    return head.decode("utf-8", errors="replace"), len(encoded) - len(head)


async def _extract_pdf_text(blob: bytes, max_pages: int = 20) -> Optional[str]:
    """Lazy-import pdfplumber; returns extracted text or None if the
    lib isn't installed.  Caps at first N pages so a 500-page PDF
    doesn't blow the prompt budget."""
    try:
        import pdfplumber  # type: ignore  # noqa: PLC0415
    except ImportError:
        logger.info("attachment_pdf_skip pdfplumber_not_installed=true")
        return None
    import io
    try:
        pages_text: list[str] = []
        with pdfplumber.open(io.BytesIO(blob)) as pdf:
            for i, page in enumerate(pdf.pages[:max_pages]):
                pages_text.append(page.extract_text() or "")
        return "\n\n".join(pages_text).strip()
    except Exception as exc:
        logger.warning("attachment_pdf_extract_failed error=%s", exc)
        return None


# ─── Context block builder ───────────────────────────────────────────


def _format_block(name: str, mime: str, size: int, body: str, dropped: int = 0) -> str:
    """Render one ``<!-- AMOR-ATTACH:START --> ... <!-- AMOR-ATTACH:END -->``
    block.  Sentinel markers chosen to be unambiguous + hard for a
    naive prompt-injection to forge."""
    size_kb = round(size / 1024, 1)
    footer = f"[...truncated, {dropped} bytes omitted]\n" if dropped > 0 else ""
    return (
        f"<!-- AMOR-ATTACH:START -->\n"
        f"File: {name} ({mime}, {size_kb} KB)\n"
        f"-----\n"
        f"{body}\n"
        f"{footer}"
        f"<!-- AMOR-ATTACH:END -->\n"
    )


def _format_image_block(name: str, mime: str, size: int, has_vision: bool) -> str:
    note = "vision: available" if has_vision else "vision: unavailable — filename + metadata only"
    return _format_block(name, mime, size, f"[binary attachment — {note}]\n")


def _format_filename_block(name: str, mime: str, size: int, reason: str) -> str:
    return _format_block(name, mime, size, f"[filename + metadata only — {reason}]\n")


# ─── Main entry point ────────────────────────────────────────────────


async def resolve_and_inject(
    db: Any,
    *,
    user_id: str,
    attachment_ids: list[str],
    prompt: str,
    has_vision_model: bool = False,
) -> tuple[str, list[tuple[str, str]], list[MessageAttachmentRef]]:
    """Resolve every id, build the enriched prompt + message refs.

    Args:
        db: Mongo handle (`chat_store._db()` result).
        user_id: Auth'd user — every fetch is tenant-scoped.
        attachment_ids: as sent by the frontend (UUIDs).
        prompt: original user prompt.
        has_vision_model: when True, image attachments become
            ``image_ref`` (binary forwarded to LLM via image_refs);
            else ``filename_only`` (banner text in prompt).

    Returns:
        enriched_prompt — prompt with one block per attachment prepended.
        image_refs — list of (mime, bytes) for vision-capable LLMs.
        message_attachments — refs to persist on user_message doc.
    """
    if not attachment_ids:
        return prompt, [], []

    blocks: list[str] = []
    # Cycle UI v2.7.2 (D7) — Ollama's `messages[i].images` expects a
    # base64-encoded string list (NO data: prefix), matching the
    # OpenAI vision spec via openai_compat backend.  We encode at
    # resolve time so the caller (chat /start endpoint or engine
    # forwarding step) can splat the list directly onto
    # `ChatMessage.images`.
    import base64 as _b64  # noqa: PLC0415 — local keeps non-vision path cold
    image_refs: list[tuple[str, str]] = []
    msg_refs: list[MessageAttachmentRef] = []
    inline_budget = MAX_TOTAL_INLINE_BYTES

    for aid in attachment_ids:
        meta = await get_attachment_meta(db, user_id=user_id, attachment_id=aid)
        if meta is None:
            # Stale id (user removed file between upload and submit).
            # Don't fail the whole prompt — just skip with a log.
            logger.info("attachment_resolve_miss user=%s id=%s", user_id, aid)
            continue

        # Read blob once for inline-eligible files; images can be
        # forwarded as-is without decode.
        blob: Optional[bytes] = None
        try:
            blob = await read_attachment_bytes(meta)
        except Exception as exc:
            logger.warning("attachment_read_failed id=%s error=%s", aid, exc)
            blocks.append(
                _format_filename_block(
                    meta.original_name, meta.mime, meta.size, "read failed"
                )
            )
            msg_refs.append(MessageAttachmentRef(
                attachment_id=aid, name=meta.original_name, mime=meta.mime,
                size=meta.size, role="user_attached", inclusion="filename_only",
            ))
            continue

        # 1) IMAGE — gated by `has_vision_model`.
        if _is_image(meta):
            blocks.append(_format_image_block(
                meta.original_name, meta.mime, meta.size, has_vision_model
            ))
            inclusion: str = "image_ref" if has_vision_model else "filename_only"
            if has_vision_model:
                # Base64 — Ollama + OpenAI vision both consume this shape.
                image_refs.append((meta.mime, _b64.b64encode(blob).decode("ascii")))
            msg_refs.append(MessageAttachmentRef(
                attachment_id=aid, name=meta.original_name, mime=meta.mime,
                size=meta.size, role="user_attached",
                inclusion=inclusion,  # type: ignore[arg-type]
            ))
            continue

        # 2) PDF — first N pages text-extracted via pdfplumber when
        #    available.  Fall back to filename_only otherwise.
        if _is_pdf(meta):
            extracted = await _extract_pdf_text(blob)
            if extracted is None:
                blocks.append(_format_filename_block(
                    meta.original_name, meta.mime, meta.size,
                    "PDF extract unavailable (install pdfplumber)",
                ))
                msg_refs.append(MessageAttachmentRef(
                    attachment_id=aid, name=meta.original_name, mime=meta.mime,
                    size=meta.size, role="user_attached", inclusion="filename_only",
                ))
                continue
            # Treat extracted text as inline (with cap).
            per_file_cap = min(INLINE_TEXT_CAP_BYTES, inline_budget)
            truncated, dropped = _truncate_inline(extracted, per_file_cap)
            inline_budget -= len(truncated.encode("utf-8", errors="replace"))
            blocks.append(_format_block(
                meta.original_name, meta.mime, meta.size, truncated, dropped
            ))
            msg_refs.append(MessageAttachmentRef(
                attachment_id=aid, name=meta.original_name, mime=meta.mime,
                size=meta.size, role="user_attached", inclusion="inline_text",
                inline_preview=truncated[:INLINE_PREVIEW_CHARS],
            ))
            continue

        # 3) TEXT / CODE inline.
        if _is_text_inline(meta):
            if inline_budget <= 0:
                blocks.append(_format_filename_block(
                    meta.original_name, meta.mime, meta.size,
                    "inline budget exhausted",
                ))
                msg_refs.append(MessageAttachmentRef(
                    attachment_id=aid, name=meta.original_name, mime=meta.mime,
                    size=meta.size, role="user_attached", inclusion="filename_only",
                ))
                continue
            text = _safe_decode(blob)
            per_file_cap = min(INLINE_TEXT_CAP_BYTES, inline_budget)
            truncated, dropped = _truncate_inline(text, per_file_cap)
            inline_budget -= len(truncated.encode("utf-8", errors="replace"))
            blocks.append(_format_block(
                meta.original_name, meta.mime, meta.size, truncated, dropped
            ))
            msg_refs.append(MessageAttachmentRef(
                attachment_id=aid, name=meta.original_name, mime=meta.mime,
                size=meta.size, role="user_attached", inclusion="inline_text",
                inline_preview=truncated[:INLINE_PREVIEW_CHARS],
            ))
            continue

        # 4) UNKNOWN — filename-only fallback.
        blocks.append(_format_filename_block(
            meta.original_name, meta.mime, meta.size, "unsupported MIME"
        ))
        msg_refs.append(MessageAttachmentRef(
            attachment_id=aid, name=meta.original_name, mime=meta.mime,
            size=meta.size, role="user_attached", inclusion="filename_only",
        ))

    enriched_prompt = "".join(blocks) + ("\n" if blocks else "") + prompt
    logger.info(
        "attachment_resolved user=%s n=%d inline_used=%d image_refs=%d",
        user_id, len(msg_refs), MAX_TOTAL_INLINE_BYTES - inline_budget, len(image_refs),
    )
    return enriched_prompt, image_refs, msg_refs
