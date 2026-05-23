"""Cycle UI v2.7 — Pydantic models + constants for the attachment system.

Shared across:
* `attachments_routes.py` — upload/get/delete endpoint request/response shapes.
* `attachment_resolver.py` — id → prompt context block resolution.
* 6× chat `/start` endpoints — `AttachmentBearingRequest` mixin.
* `chat_sessions_routes.py` — message append/persist path.

Filesystem layout (D1, locked):
    data/attachments/{user_id}/{yyyy-mm}/{uuid}.{ext}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ─── Size limits (D5, locked) ────────────────────────────────────────

ATTACH_MAX_SINGLE_BYTES = 10 * 1024 * 1024          # 10 MB single file
ATTACH_MAX_SUBMISSION_BYTES = 25 * 1024 * 1024      # 25 MB per submission
ATTACH_MAX_SESSION_BYTES = 200 * 1024 * 1024        # 200 MB per session
ATTACH_APPROVAL_THRESHOLD_BYTES = 5 * 1024 * 1024   # >5 MB → approval policy
ATTACH_MAX_FILES_PER_MESSAGE = 10                    # message-level cap

# ─── LLM context-window guards (D6) ──────────────────────────────────

INLINE_TEXT_CAP_BYTES = 32 * 1024       # per-file inline text cap
MAX_TOTAL_INLINE_BYTES = 96 * 1024      # total per-prompt inline budget
INLINE_PREVIEW_CHARS = 1024              # metadata preview persisted to Mongo

# ─── MIME whitelist (D4) ─────────────────────────────────────────────

# Text/code MIMEs handled inline (`inclusion: "inline_text"`).
TEXT_INLINE_MIME_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
    "application/typescript",
)

# Code by-extension fallback when ``Content-Type`` is generic
# (``application/octet-stream`` from drag-drop or some clients).
CODE_EXT_FALLBACK = {
    ".py", ".pyi", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx",
    ".java", ".kt", ".scala", ".rb", ".php", ".sh", ".bash",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".m", ".mm",
    ".html", ".css", ".scss", ".vue", ".svelte",
    ".sql", ".graphql", ".proto", ".thrift",
    ".md", ".markdown", ".rst", ".txt", ".log",
    ".toml", ".ini", ".cfg", ".conf", ".env",
    ".csv", ".tsv",
}

# Image MIMEs (D7 gated fallback — text-only unless vision model
# capability detected at resolver time).
IMAGE_MIME_PREFIXES = ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/svg+xml")

# Document (PDF) — first N pages text-extracted via pdfplumber when
# the lib is available; falls back to filename-only otherwise.
DOC_MIMES = ("application/pdf",)

# Hard reject — never accept executable / archive / blob types.  These
# would either bypass MIME sniffing or carry latent payloads.
REJECT_MIME_PREFIXES = (
    "application/x-msdownload",
    "application/x-executable",
    "application/x-msdos-program",
    "application/x-msi",
    "application/x-elf",
    "application/zip",
    "application/x-rar-compressed",
    "application/x-7z-compressed",
    "application/x-tar",
    "application/x-gzip",
)


# ─── Pydantic models ─────────────────────────────────────────────────


class AttachmentMeta(BaseModel):
    """Canonical attachments_meta doc shape (MongoDB).

    Stored once at upload time; referenced by ``attachment_id`` from
    chat_messages.attachments[] (denormalized name/mime/size cached
    there for replay safety).
    """

    id: str = Field(description="UUID4 hex — also the on-disk filename stem.")
    user_id: str
    session_id: Optional[str] = None
    message_id: Optional[str] = None
    original_name: str
    mime: str
    size: int
    sha256: str
    storage_path: str = Field(description="Relative to repo `data/` root.")
    status: Literal["uploaded", "scanned", "expired"] = "uploaded"
    text_extracted_preview: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        # Motor expects naive-aware datetimes; we already produce UTC.
        d = self.model_dump(mode="json")
        d["created_at"] = self.created_at
        if self.expires_at is not None:
            d["expires_at"] = self.expires_at
        return d


class AttachmentUploadResponse(BaseModel):
    """Returned to the browser after `POST /api/attachments/upload`."""

    attachment_id: str
    mime: str
    size: int
    sha256: str
    status: Literal["uploaded", "scanned", "expired"]
    text_extracted_preview: Optional[str] = None
    original_name: str


class MessageAttachmentRef(BaseModel):
    """Cycle UI v2.7 (D3) — `chat_messages.attachments[]` element shape.

    Denormalized fields (name/mime/size) so message replay stays
    truthful even if the underlying attachments_meta doc is TTL-pruned
    or migrated.  ``inclusion`` records HOW the LLM saw the file —
    critical for audit / debug ("did the model actually receive the
    CSV content or only the filename?").
    """

    attachment_id: str
    name: str
    mime: str
    size: int
    role: Literal["user_attached", "model_emitted"] = "user_attached"
    inclusion: Literal["inline_text", "image_ref", "filename_only"] = "filename_only"
    inline_preview: Optional[str] = Field(default=None, description="ilk 1024 char (D6)")


class AttachmentBearingRequest(BaseModel):
    """Cycle UI v2.7 (D9) — mixin for the 6 chat /start endpoints.

    Each endpoint's request model multiplies inherits this; the
    `attachment_resolver.resolve_and_inject(ids, prompt)` helper is
    invoked at endpoint head to enrich the prompt before dispatch.

    Backward compatibility: default `[]` so legacy clients work
    untouched.
    """

    attachment_ids: list[str] = Field(default_factory=list)
