"""
PhaseHooks — Charter §6 Mandate 3.

The engine exposes a ``PhaseHooks`` protocol with optional
``before_phase(name, state)`` and ``after_phase(name, state, result)``
callbacks. Hooks are registered at engine construction and called
synchronously at every phase boundary. This is how observability,
custom telemetry, and human-in-the-loop interrupts attach without
modifying engine code.

Two implementations ship:

  NoopHooks            — does nothing; the engine's default.
  ChainedHooks         — composes a list of hooks; useful when the
                          routes layer wants telemetry hooks
                          alongside an interrupt-checker.

Custom implementations subclass ``PhaseHooks`` (or just satisfy its
duck-typed interface) and are passed to ``CodeIntelligenceEngine(...,
hooks=...)``.

Failure-quiet by design: a misbehaving hook MUST NOT take down the
pipeline. ``ChainedHooks`` traps every per-hook exception and logs it.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Protocol
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class PhaseHooks(Protocol):
    """
    Protocol for engine phase-boundary hooks.

    All methods are async and return None. Implementations MAY raise
    ``asyncio.CancelledError`` to halt the engine cleanly (which the
    engine's existing cancellation path catches and propagates).

    Implementations MUST NOT raise other exceptions in production
    code; if they do, ``ChainedHooks`` swallows + logs, but a
    standalone hook taking down the engine is a Charter Discipline 8
    violation (no silent failures).
    """

    async def before_phase(self, name: str, state: dict[str, Any]) -> None:
        """Called immediately before ``runner`` starts for phase ``name``.

        ``state`` is a read-only-by-convention snapshot of the
        engine's accumulated state (plan, code, execution_results,
        ...). Mutating it from a hook is allowed but discouraged —
        the engine's _merge_phase_result is the canonical write path.
        """
        ...

    async def after_phase(
        self,
        name: str,
        state: dict[str, Any],
        result: dict[str, Any] | None,
    ) -> None:
        """Called immediately after ``runner`` returns for phase
        ``name``. ``result`` is the runner's return value (may be
        ``None`` if the phase failed)."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Concrete defaults
# ─────────────────────────────────────────────────────────────────────────────


class NoopHooks:
    """Default implementation — does nothing. The engine wires this
    automatically when no ``hooks=`` is passed."""

    async def before_phase(self, name: str, state: dict[str, Any]) -> None:
        return None

    async def after_phase(
        self,
        name: str,
        state: dict[str, Any],
        result: dict[str, Any] | None,
    ) -> None:
        return None


class ChainedHooks:
    """
    Compose a list of hooks. Calls flow in registration order for
    ``before_phase`` and the reverse for ``after_phase`` (matching
    the typical decorator/middleware ordering).

    Per-hook exceptions are caught and logged so a buggy custom hook
    cannot poison the pipeline.
    """

    def __init__(self, *hooks: PhaseHooks) -> None:
        self._hooks: list[PhaseHooks] = [h for h in hooks if h is not None]

    def add(self, hook: PhaseHooks) -> None:
        self._hooks.append(hook)

    @property
    def count(self) -> int:
        return len(self._hooks)

    async def before_phase(self, name: str, state: dict[str, Any]) -> None:
        for h in self._hooks:
            try:
                await h.before_phase(name, state)
            except Exception as exc:
                logger.warning(
                    "phase_hook_before_failed hook=%s phase=%s error=%s",
                    h.__class__.__name__,
                    name,
                    exc,
                )

    async def after_phase(
        self,
        name: str,
        state: dict[str, Any],
        result: dict[str, Any] | None,
    ) -> None:
        # Reverse order — matches the common "set up forward, tear
        # down reverse" middleware idiom.
        for h in reversed(self._hooks):
            try:
                await h.after_phase(name, state, result)
            except Exception as exc:
                logger.warning(
                    "phase_hook_after_failed hook=%s phase=%s error=%s",
                    h.__class__.__name__,
                    name,
                    exc,
                )


# ─────────────────────────────────────────────────────────────────────────────
# A useful built-in: telemetry hook → emits an observability event
# ─────────────────────────────────────────────────────────────────────────────


class TelemetryHooks:
    """
    Built-in hook that emits a free-standing observability event per
    phase boundary. Wires into the existing ``observability.emit_event``
    helper so the JSONL / Langfuse export already gets phase timings.
    """

    async def before_phase(self, name: str, state: dict[str, Any]) -> None:
        from .observability import emit_event

        emit_event("engine.phase", "phase_start", phase=name)

    async def after_phase(
        self,
        name: str,
        state: dict[str, Any],
        result: dict[str, Any] | None,
    ) -> None:
        from .observability import emit_event

        ok = result is not None
        emit_event(
            "engine.phase",
            "phase_complete" if ok else "phase_failed",
            phase=name,
            ok=ok,
        )
