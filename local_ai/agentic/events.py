"""
Cycle C Sprint 8 Day 1 — typed event taxonomy.

Mirrors OpenHands V1's typed-event model (arXiv 2511.03690 §3.1):

* :class:`MessageEvent`       — user / assistant utterance
* :class:`ThoughtEvent`       — agent's <thought> bubble
* :class:`ActionEvent`        — typed "I want to call tool X with args Y"
* :class:`ObservationEvent`   — typed "tool X returned Z"

Every event is immutable Pydantic and carries:

* ``id`` — ULID (lexicographically sortable; replay-friendly)
* ``ts_iso`` — UTC timestamp
* ``kind`` — discriminator string (so ``Conversation`` can ``isinstance``-
  switch without leaking concrete types)
* ``meta`` — opaque dict for adapters

Frontend integration: :func:`Event.to_tool_stream` projects each event
into the canonical Vercel AI SDK 5 envelope from Sprint 4 Day 4 so
``ToolCallCard`` already understands the wire shape.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ─── id helpers ────────────────────────────────────────────────────


_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    """Generate a 26-char Crockford-base32 ULID without pulling in
    a dep.  Time-prefixed so events sort chronologically; tail is
    cryptographically random so concurrent appends don't collide."""
    millis = int(time.time() * 1000) & ((1 << 48) - 1)
    head = ""
    for _ in range(10):
        head = _BASE32[millis & 0x1F] + head
        millis >>= 5
    tail = "".join(_BASE32[secrets.randbelow(32)] for _ in range(16))
    return head + tail


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ─── kinds ────────────────────────────────────────────────────────


EventKind = Literal["message", "thought", "action", "observation"]


# ─── base ─────────────────────────────────────────────────────────


class Event(BaseModel):
    """Common fields for every event type.  Pydantic config locks the
    model immutable + extras-forbidden so a typo in a producer doesn't
    silently land on the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=_ulid)
    ts_iso: str = Field(default_factory=_now_iso)
    kind: EventKind
    meta: Dict[str, Any] = Field(default_factory=dict)

    def to_tool_stream(self) -> List[Dict[str, Any]]:
        """Project to the Sprint 4 Day 4 SSE envelope.  The base
        implementation returns an empty list — subclasses override."""
        return []


class MessageEvent(Event):
    kind: Literal["message"] = "message"
    role: Literal["user", "assistant"]
    text: str

    def to_tool_stream(self) -> List[Dict[str, Any]]:
        # Plain messages don't need a tool envelope; the frontend
        # already handles them via the chat-message stream.
        return []


class ThoughtEvent(Event):
    """The ``<thought>`` block emitted between actions in the ReAct
    loop.  The frontend may render thoughts collapsed by default."""

    kind: Literal["thought"] = "thought"
    text: str
    iteration: int = 0

    def to_tool_stream(self) -> List[Dict[str, Any]]:
        return []


class ActionEvent(Event):
    """The agent's resolved tool call.  Mirrors the Vercel AI SDK
    ``tool-input-start`` + ``tool-input-available`` pair."""

    kind: Literal["action"] = "action"
    iteration: int = 0
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ObservationEvent(Event):
    """The tool's return value.  Mirrors ``tool-output-available``."""

    kind: Literal["observation"] = "observation"
    iteration: int = 0
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    is_error: bool = False
    error_message: Optional[str] = None
    elapsed_ms: float = 0.0


# Re-declare the two methods on the right classes — Pydantic doesn't
# inherit `to_tool_stream` overrides cleanly when frozen so we attach
# bound functions explicitly.


def _action_to_stream(self: "ActionEvent") -> List[Dict[str, Any]]:
    cid = f"{self.tool}-{self.iteration}"
    return [
        {
            "type": "tool-input-start",
            "toolCallId": cid,
            "tool": self.tool,
            "meta": {"iteration": self.iteration, **(self.meta or {})},
        },
        {
            "type": "tool-input-available",
            "toolCallId": cid,
            "input": self.arguments,
        },
    ]


def _observation_to_stream(self: "ObservationEvent") -> List[Dict[str, Any]]:
    cid = f"{self.tool}-{self.iteration}"
    if self.is_error:
        return [
            {
                "type": "tool-output-available",
                "toolCallId": cid,
                "output": {"error": self.error_message, "elapsed_ms": self.elapsed_ms},
                "isError": True,
            },
            {
                "type": "tool-error",
                "toolCallId": cid,
                "message": self.error_message or "tool error",
            },
        ]
    return [
        {
            "type": "tool-output-available",
            "toolCallId": cid,
            "output": self.output,
            "isError": False,
        },
    ]


ActionEvent.to_tool_stream = _action_to_stream  # type: ignore[assignment]
ObservationEvent.to_tool_stream = _observation_to_stream  # type: ignore[assignment]
