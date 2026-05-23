"""
Cycle C Sprint 8 Day 1 — append-only ``Conversation`` event log.

Mirrors OpenHands V1's ``ConversationState``: an immutable list of
events the agent appends to as it thinks / acts / observes.  Two key
guarantees the rest of the agentic loop relies on:

* **Append-only** — every mutation is ``conv.append(event)``; the
  underlying list is never re-ordered or pruned (replay is trivial).
* **Typed iteration** — ``conv.iter_kind("action")`` filters by
  discriminator without leaking concrete subclasses.

Why not Pydantic?  The container itself is a hot-path mutable
collection; the *events* are immutable Pydantic.  Wrapping the
container in a frozen model would force a rebuild on every append.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, List, Optional

from .events import (
    ActionEvent,
    Event,
    EventKind,
    MessageEvent,
    ObservationEvent,
    ThoughtEvent,
)


@dataclass
class ConversationState:
    """Snapshot exposed to UI / API.  Built lazily from a Conversation."""

    session_id: str
    events: List[Event]
    iteration: int = 0
    finished: bool = False
    finish_reason: Optional[str] = None


class Conversation:
    """Append-only, replay-friendly event log.

    Construct once per agent run; pass a stable ``session_id`` so SSE
    fanout / replay can key on it.  ``snapshot()`` produces a
    ``ConversationState`` for the route layer.
    """

    def __init__(self, *, session_id: str) -> None:
        self.session_id = session_id
        self._events: List[Event] = []
        self._iteration = 0
        self._finished = False
        self._finish_reason: Optional[str] = None

    # ── append surface ────────────────────────────────────────────

    def append(self, event: Event) -> Event:
        """Append a single event.  Returns the event so callers can
        chain further state updates without re-fetching."""
        if self._finished:
            raise RuntimeError(
                f"Conversation {self.session_id} is finished — "
                "no further events accepted",
            )
        self._events.append(event)
        return event

    def append_message(self, role: str, text: str) -> MessageEvent:
        ev = MessageEvent(role=role, text=text)  # type: ignore[arg-type]
        self.append(ev)
        return ev

    def append_thought(self, text: str, *, iteration: Optional[int] = None) -> ThoughtEvent:
        ev = ThoughtEvent(text=text, iteration=iteration if iteration is not None else self._iteration)
        self.append(ev)
        return ev

    def append_action(
        self, *, tool: str, arguments: dict, iteration: Optional[int] = None,
    ) -> ActionEvent:
        ev = ActionEvent(
            tool=tool,
            arguments=dict(arguments),
            iteration=iteration if iteration is not None else self._iteration,
        )
        self.append(ev)
        return ev

    def append_observation(
        self,
        *,
        tool: str,
        arguments: dict,
        output,
        is_error: bool = False,
        error_message: Optional[str] = None,
        elapsed_ms: float = 0.0,
        iteration: Optional[int] = None,
    ) -> ObservationEvent:
        ev = ObservationEvent(
            tool=tool,
            arguments=dict(arguments),
            output=output,
            is_error=bool(is_error),
            error_message=error_message,
            elapsed_ms=float(elapsed_ms),
            iteration=iteration if iteration is not None else self._iteration,
        )
        self.append(ev)
        return ev

    # ── lifecycle ────────────────────────────────────────────────

    def start_iteration(self) -> int:
        """Advance the iteration counter and return its new value.
        ReAct loops call this once per think/act/observe cycle."""
        self._iteration += 1
        return self._iteration

    def finish(self, reason: str) -> None:
        if self._finished:
            return
        self._finished = True
        self._finish_reason = reason

    @property
    def iteration(self) -> int:
        return self._iteration

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def finish_reason(self) -> Optional[str]:
        return self._finish_reason

    # ── read surface ─────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    @property
    def events(self) -> List[Event]:
        """Defensive copy so callers can't break the append-only
        invariant by mutating the returned list."""
        return list(self._events)

    def iter_kind(self, kind: EventKind) -> Iterator[Event]:
        for ev in self._events:
            if ev.kind == kind:
                yield ev

    def last(self, kind: Optional[EventKind] = None) -> Optional[Event]:
        if kind is None:
            return self._events[-1] if self._events else None
        for ev in reversed(self._events):
            if ev.kind == kind:
                return ev
        return None

    def snapshot(self) -> ConversationState:
        return ConversationState(
            session_id=self.session_id,
            events=list(self._events),
            iteration=self._iteration,
            finished=self._finished,
            finish_reason=self._finish_reason,
        )

    # ── action / observation pair helpers (Sprint 8 Day 3 stuck
    #    detector eats these) ─────────────────────────────────────

    def action_observation_pairs(self) -> List[tuple[ActionEvent, ObservationEvent]]:
        """Return the (action, immediately-following observation)
        pairs in chronological order.  Pairs without observations
        (e.g. an action whose observation is still pending) are
        excluded."""
        pairs: List[tuple[ActionEvent, ObservationEvent]] = []
        pending: Optional[ActionEvent] = None
        for ev in self._events:
            if isinstance(ev, ActionEvent):
                pending = ev
            elif isinstance(ev, ObservationEvent) and pending is not None:
                pairs.append((pending, ev))
                pending = None
        return pairs
