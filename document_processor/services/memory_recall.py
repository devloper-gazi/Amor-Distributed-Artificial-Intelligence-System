"""
Cycle C Sprint 7 Day 4 — chat pipeline memory recall hook.

Thin glue between AMOR's pipeline phases (engine.py / chat-research)
and the Mem0 OSS adapter.  Pure functions — no FastAPI surface, no
DB writes; the route layer at ``api/admin_memory_routes.py`` is the
single ingress for HTTP-driven memory ops.

Public surface
--------------
* ``RecallResult``               — return value (count + snippets)
* ``recall_for_prompt(prompt, user_id, *, limit)``
                                  — async helper that fetches up to
                                  ``limit`` Mem0 memories scoped to
                                  ``user_id`` and returns a normalised
                                  ``RecallResult`` (always succeeds —
                                  empty when Mem0 is degraded).
* ``format_recall_block(memories)``
                                  — render the memories as a Markdown
                                  block the engine prepends to the
                                  triage system message.

Privacy
-------
* Per-user namespacing via ``user_id`` (UUID).  An anonymous prompt
  uses the ``"local"`` namespace so system-level memories surface
  for un-authenticated public flows.
* Errors are silenced — memory recall is advisory.  A misbehaving
  Mem0 client must NEVER take down a chat turn.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecallResult:
    count: int
    snippets: List[str] = field(default_factory=list)
    backend: str = "native"  # "mem0" | "native"
    available: bool = False


def recall_disabled() -> RecallResult:
    return RecallResult(count=0, snippets=[], backend="native", available=False)


async def recall_for_prompt(
    prompt: str,
    *,
    user_id: str = "local",
    limit: int = 5,
) -> RecallResult:
    """Look up up to ``limit`` memories for ``user_id`` matching ``prompt``.

    Always succeeds.  Returns an empty :class:`RecallResult` when:
    * Mem0 isn't installed or ``AMOR_MEMORY_BACKEND != "mem0"``
    * The lookup raises (logged at warning, not propagated)
    * The prompt is empty or whitespace-only
    """
    text = (prompt or "").strip()
    if not text:
        return recall_disabled()

    try:
        from local_ai.memory.mem0_adapter import Mem0Adapter, mem0_enabled  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Mem0 adapter import failed: %s", exc)
        return recall_disabled()

    if not mem0_enabled():
        return recall_disabled()

    # Per-user adapter so two users never bleed into each other.
    try:
        adapter = Mem0Adapter(user_id=user_id)
    except Exception as exc:  # pragma: no cover
        logger.warning("Mem0Adapter construction failed: %s", exc)
        return recall_disabled()

    if not adapter.status().available:
        return recall_disabled()

    try:
        records = adapter.search(text, user_id=user_id, limit=int(limit))
    except Exception as exc:  # pragma: no cover
        logger.warning("Mem0 search failed: %s", exc)
        return recall_disabled()

    snippets = [_truncate(r.text, 240) for r in records if r.text]
    return RecallResult(
        count=len(snippets),
        snippets=snippets,
        backend="mem0",
        available=True,
    )


def format_recall_block(result: RecallResult) -> Optional[str]:
    """Render a Markdown block to prepend to the triage system message.

    Returns ``None`` when there are no memories to show, so callers can
    pattern-match on truthiness.
    """
    if result.count == 0 or not result.snippets:
        return None
    bullets = "\n".join(f"- {s}" for s in result.snippets)
    return (
        "## Recalled memory\n"
        "These prior facts about the user were retrieved by Mem0:\n\n"
        f"{bullets}\n"
    )


def _truncate(text: str, n: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def memory_recall_enabled_in_engine() -> bool:
    """Cheap env probe — used by ``engine.py`` to short-circuit the
    Mem0 import + adapter construction when the operator hasn't
    enabled the feature."""
    if os.environ.get("AMOR_MEMORY_RECALL_ENABLED", "").strip().lower() in {
        "0",
        "false",
        "no",
    }:
        return False
    backend = os.environ.get("AMOR_MEMORY_BACKEND", "").strip().lower()
    return backend == "mem0"
