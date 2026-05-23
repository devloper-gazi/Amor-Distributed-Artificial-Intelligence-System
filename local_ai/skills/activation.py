"""
Cycle F Sprint 4 — `_ACTIVE_SKILL` ContextVar + activation accessors.

Mirrors the `_ACTIVE_ROLE` plumbing from Cycle B / Sprint 3 LoRA.
The pattern: each phase that wants to thread state into the next
LLM call sets a ContextVar; the call layer reads it and adapts the
body / system prompt without changing engine signatures.

Public surface:

  _ACTIVE_SKILL                # ContextVar[str | None]
  set_active_skill(name)       # for use by code_intelligence_routes
                               # when load_skill() fires
  active_skill_name()          # for use by prompts.py / engine.py
                               # when assembling the next system prompt
"""

from __future__ import annotations

import contextvars
from typing import Optional

# Default is None — Sprint 3 deploys behave unchanged.
_ACTIVE_SKILL: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "amor_active_skill", default=None,
)


def set_active_skill(name: Optional[str]) -> contextvars.Token:
    """Set the active skill name in this task's ContextVar scope.

    Returns the token so the caller can ``reset`` it once the
    skill-activation scope ends — mirrors `_ACTIVE_ROLE.set` from
    `local_ai_routes_simple.py`.

    Pass ``None`` to clear without resetting the token.
    """

    return _ACTIVE_SKILL.set(name or None)


def active_skill_name() -> Optional[str]:
    """Return the currently-active skill name, or None when no
    skill has been activated for this task."""

    return _ACTIVE_SKILL.get()


__all__ = ["_ACTIVE_SKILL", "active_skill_name", "set_active_skill"]
