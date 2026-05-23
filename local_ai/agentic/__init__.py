"""
Cycle C Sprint 8 — MCP agentic loop.

Mirrors the OpenHands SDK V1 architecture (arXiv 2511.03690) without
importing it:

* :class:`Event` (typed Pydantic models)         — events.py
* :class:`Conversation` (append-only event log)  — conversation.py
* :class:`ReActAgent` (think → act → observe)    — agent.py
* :class:`StuckDetector` (3+ identical pairs)    — agent.py
* Workspace = the existing AMOR ``ExecutionSandbox`` (Sprint 5
  hardened, see ``document_processor/code_intelligence/sandbox.py``).

Tool calls dispatch to ``local_ai.tools.DEFAULT_REGISTRY`` so the
agent can use every tool the existing MCP facade already exposes.
"""

from .events import (
    ActionEvent,
    Event,
    EventKind,
    MessageEvent,
    ObservationEvent,
    ThoughtEvent,
)
from .conversation import Conversation, ConversationState
from .agent import (
    AgentConfig,
    AgentRunResult,
    ReActAgent,
    StuckDetector,
    default_tool_dispatcher,
)
from .prompt import parse_react, render_prompt

__all__ = [
    "ActionEvent",
    "AgentConfig",
    "AgentRunResult",
    "Conversation",
    "ConversationState",
    "Event",
    "EventKind",
    "MessageEvent",
    "ObservationEvent",
    "ReActAgent",
    "StuckDetector",
    "ThoughtEvent",
    "default_tool_dispatcher",
    "parse_react",
    "render_prompt",
]
