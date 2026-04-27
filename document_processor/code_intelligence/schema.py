"""
VersionedModel — Charter §6 Mandate 6.

Every Pydantic / dataclass model that crosses a persistence boundary
(MongoDB document, Redis cache value, exported telemetry) carries a
``schema_version: int`` field. The default is ``1``; bumps coincide
with backwards-incompatible changes. Loaders branch on the field
when migrations are needed.

This module provides:

- ``VersionedModel`` — Pydantic ``BaseModel`` subclass with
  ``schema_version: int = 1``.
- ``CURRENT_SCHEMA_VERSIONS`` — dict mapping payload kind →
  current version, exposed so a future migration tool can
  enumerate them.
- ``ensure_schema_version(payload, kind)`` — helper that adds the
  field to a plain dict on the way out (for non-Pydantic payloads
  like the engine's ``code_session`` Redis dict).

Why an int and not semver?
--------------------------
Migrations are easier to reason about as a totally-ordered chain.
Semver implies independent breaking and feature changes per axis;
that's overkill for in-band schema evolution where a single sequence
captures every breaking change.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Current schema versions per payload kind
# ─────────────────────────────────────────────────────────────────────────────


CURRENT_SCHEMA_VERSIONS: dict[str, int] = {
    "code_session": 1,
    "capability_record": 1,
    "adversarial_event": 1,
    "trace_span": 1,
    "query_record": 1,
}


# ─────────────────────────────────────────────────────────────────────────────
# VersionedModel
# ─────────────────────────────────────────────────────────────────────────────


class VersionedModel(BaseModel):
    """
    Pydantic base for any payload crossing a persistence boundary.

    Subclasses set their kind via the ``__schema_kind__`` class
    attribute; ``schema_version`` is then keyed off
    ``CURRENT_SCHEMA_VERSIONS`` automatically.

    Example::

        class CodeSessionPayload(VersionedModel):
            __schema_kind__ = "code_session"
            session_id: str
            ...
    """

    schema_version: int = Field(
        default=1,
        description="Schema version for this payload kind. "
        "Bumped on backwards-incompatible changes; loaders "
        "branch on this field.",
    )

    # Pydantic v2 — allow `__schema_kind__` to be a class attr without
    # being mistaken for a field.
    model_config = {"arbitrary_types_allowed": True}

    __schema_kind__: str = ""  # subclasses set this

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        kind = getattr(cls, "__schema_kind__", "")
        if kind and kind in CURRENT_SCHEMA_VERSIONS:
            current = CURRENT_SCHEMA_VERSIONS[kind]
            # Only override if the subclass hasn't explicitly set a
            # different default — otherwise tests (or migrations)
            # could need a specific value.
            cls.model_fields["schema_version"].default = current


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for non-Pydantic payloads
# ─────────────────────────────────────────────────────────────────────────────


def ensure_schema_version(
    payload: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    """
    Idempotently add ``schema_version`` to a plain dict payload.

    Returns the same dict, mutated in place if needed, so callers can
    chain or ignore the return value. The current version comes from
    ``CURRENT_SCHEMA_VERSIONS[kind]``; unknown kinds default to 1 with
    a logged warning so the caller learns about the missing entry.
    """
    if not isinstance(payload, dict):
        return payload
    if kind not in CURRENT_SCHEMA_VERSIONS:
        logger.warning(
            "schema_version_unknown_kind kind=%s defaulting=1",
            kind,
        )
        version = 1
    else:
        version = CURRENT_SCHEMA_VERSIONS[kind]
    payload.setdefault("schema_version", version)
    return payload


def schema_version_of(payload: dict[str, Any], default: int = 1) -> int:
    """Read the schema_version of a payload, falling back to ``default``.
    Useful for migration loaders that need to branch on it."""
    if not isinstance(payload, dict):
        return default
    raw = payload.get("schema_version", default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default
