"""
Cycle C Sprint 8 Day 3 — ReActAgent + StuckDetector loop tests.

Drives the agent against scripted LLM stubs so each termination
condition is exercised deterministically.  The real LLM + the real
tool registry are NOT touched — both are injected.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from local_ai.agentic.agent import (
    AgentConfig,
    ReActAgent,
    StuckDetector,
)
from local_ai.agentic.conversation import Conversation
from local_ai.agentic.events import (
    ActionEvent,
    Event,
    ObservationEvent,
)


# ─── helpers ────────────────────────────────────────────────────


class ScriptedLLM:
    """Returns a sequence of completions in order; raises after the
    last one is consumed (so a leaky test surfaces obviously)."""

    def __init__(self, completions: List[str]) -> None:
        self._q = list(completions)
        self.calls: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self._q:
            raise RuntimeError("ScriptedLLM exhausted")
        return self._q.pop(0)


class StubTools:
    """Records every dispatch + returns canned outputs keyed on tool name."""

    def __init__(self, by_name: Dict[str, Any] | None = None, *, ok: bool = True) -> None:
        self.by_name = by_name or {}
        self.ok = ok
        self.calls: List[tuple[str, dict]] = []

    async def __call__(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, dict(args)))
        return {
            "name": name,
            "ok": self.ok,
            "output": self.by_name.get(name, f"stub:{name}"),
            "error": None if self.ok else "stub error",
            "elapsed_ms": 1.0,
        }


def _tools_catalogue():
    return [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "echoes args back",
                "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "stub search",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        },
    ]


# ─── termination: finish ───────────────────────────────────────


@pytest.mark.asyncio
async def test_finish_short_circuits_loop():
    """When the agent emits ``finish``, the loop stops at that
    iteration and the answer is preserved."""
    completions = [
        '<thought>I already know.</thought>\n<action>{"tool":"finish","arguments":{"answer":"42"}}</action>',
    ]
    conv = Conversation(session_id="t-finish")
    agent = ReActAgent(
        conversation=conv,
        llm_caller=ScriptedLLM(completions),
        tools_catalogue=_tools_catalogue(),
        tool_dispatcher=StubTools(),
    )
    result = await agent.run(user_task="anything")
    assert result.reason == "finish"
    assert result.answer == "42"
    assert result.iterations == 1
    assert conv.finished is True


# ─── termination: max-iterations ───────────────────────────────


@pytest.mark.asyncio
async def test_max_iterations_terminates_loop():
    """Agent that never emits ``finish`` must hit the iteration cap."""
    # Each completion emits a fresh action so the stuck detector
    # doesn't trip first.
    completions = [
        f'<thought>step {i}</thought>\n<action>{{"tool":"echo","arguments":{{"x":{i}}}}}</action>'
        for i in range(20)
    ]
    conv = Conversation(session_id="t-max")
    cfg = AgentConfig(max_iterations=4)
    agent = ReActAgent(
        conversation=conv,
        llm_caller=ScriptedLLM(completions),
        tools_catalogue=_tools_catalogue(),
        tool_dispatcher=StubTools(),
        config=cfg,
    )
    result = await agent.run(user_task="loop forever")
    assert result.reason == "max-iterations"
    assert result.iterations == 4


# ─── termination: stuck ────────────────────────────────────────


@pytest.mark.asyncio
async def test_stuck_detector_terminates_loop():
    """3 identical action+observation pairs ⇒ agent stops."""
    same = '<thought>retry</thought>\n<action>{"tool":"echo","arguments":{"x":1}}</action>'
    completions = [same] * 5  # plenty
    conv = Conversation(session_id="t-stuck")
    agent = ReActAgent(
        conversation=conv,
        llm_caller=ScriptedLLM(completions),
        tools_catalogue=_tools_catalogue(),
        tool_dispatcher=StubTools(by_name={"echo": "always-1"}),
        config=AgentConfig(max_iterations=10, stuck_window=3),
    )
    result = await agent.run(user_task="loop")
    assert result.reason == "stuck"
    assert result.iterations == 3


# ─── termination: parse-failure ────────────────────────────────


@pytest.mark.asyncio
async def test_parse_failure_terminates_loop():
    """3 unparseable completions in a row ⇒ stop with parse-failure."""
    completions = [
        "no tags at all just prose",
        "still no tags",
        "still nothing",
    ]
    conv = Conversation(session_id="t-parse")
    agent = ReActAgent(
        conversation=conv,
        llm_caller=ScriptedLLM(completions),
        tools_catalogue=_tools_catalogue(),
        tool_dispatcher=StubTools(),
        config=AgentConfig(max_iterations=10, max_parse_retries=3),
    )
    result = await agent.run(user_task="x")
    assert result.reason == "parse-failure"
    assert result.iterations >= 3


@pytest.mark.asyncio
async def test_parse_failure_resets_after_clean_parse():
    """A clean parse in the middle of the run must reset the parse
    failure counter so we don't trip the gate prematurely."""
    completions = [
        "garbage 1",
        "garbage 2",
        '<thought>ok</thought>\n<action>{"tool":"echo","arguments":{"x":1}}</action>',
        "garbage 3",  # one bad again — but counter was reset
        '<thought>done</thought>\n<action>{"tool":"finish","arguments":{"answer":"ok"}}</action>',
    ]
    conv = Conversation(session_id="t-parse-reset")
    agent = ReActAgent(
        conversation=conv,
        llm_caller=ScriptedLLM(completions),
        tools_catalogue=_tools_catalogue(),
        tool_dispatcher=StubTools(),
        config=AgentConfig(max_iterations=10, max_parse_retries=3),
    )
    result = await agent.run(user_task="resilient run")
    assert result.reason == "finish"
    assert result.answer == "ok"


# ─── tool dispatch surface ─────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_dispatcher_receives_args():
    completions = [
        '<thought>search.</thought>\n<action>{"tool":"search","arguments":{"q":"mango"}}</action>',
        '<thought>done.</thought>\n<action>{"tool":"finish","arguments":{"answer":"ok"}}</action>',
    ]
    tools = StubTools(by_name={"search": ["m1", "m2"]})
    conv = Conversation(session_id="t-tools")
    agent = ReActAgent(
        conversation=conv,
        llm_caller=ScriptedLLM(completions),
        tools_catalogue=_tools_catalogue(),
        tool_dispatcher=tools,
    )
    result = await agent.run(user_task="find mangoes")
    assert result.reason == "finish"
    assert tools.calls == [("search", {"q": "mango"})]


@pytest.mark.asyncio
async def test_tool_error_records_observation_and_continues():
    completions = [
        '<thought>try.</thought>\n<action>{"tool":"echo","arguments":{"x":1}}</action>',
        '<thought>recover.</thought>\n<action>{"tool":"finish","arguments":{"answer":"ok"}}</action>',
    ]
    conv = Conversation(session_id="t-err")
    agent = ReActAgent(
        conversation=conv,
        llm_caller=ScriptedLLM(completions),
        tools_catalogue=_tools_catalogue(),
        tool_dispatcher=StubTools(ok=False),
    )
    result = await agent.run(user_task="recover from error")
    assert result.reason == "finish"
    obs = [e for e in conv.events if isinstance(e, ObservationEvent)]
    assert any(o.is_error for o in obs)


# ─── on_event hook ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_event_invoked_for_every_event():
    completions = [
        '<thought>plan</thought>\n<action>{"tool":"echo","arguments":{"x":1}}</action>',
        '<thought>done</thought>\n<action>{"tool":"finish","arguments":{"answer":"y"}}</action>',
    ]
    conv = Conversation(session_id="t-hook")
    seen: List[Event] = []

    async def hook(ev: Event):
        seen.append(ev)

    agent = ReActAgent(
        conversation=conv,
        llm_caller=ScriptedLLM(completions),
        tools_catalogue=_tools_catalogue(),
        tool_dispatcher=StubTools(),
        on_event=hook,
    )
    await agent.run(user_task="check hook")
    kinds = [e.kind for e in seen]
    assert "message" in kinds
    assert "thought" in kinds
    assert "action" in kinds
    assert "observation" in kinds


# ─── stuck detector unit tests ─────────────────────────────────


def test_stuck_detector_passes_with_under_window_pairs():
    conv = Conversation(session_id="s")
    conv.append_action(tool="a", arguments={})
    conv.append_observation(tool="a", arguments={}, output=1)
    conv.append_action(tool="a", arguments={})
    conv.append_observation(tool="a", arguments={}, output=1)
    sd = StuckDetector(window=3)
    assert sd.is_stuck(conv) is False


def test_stuck_detector_trips_with_three_identical_pairs():
    conv = Conversation(session_id="s")
    for _ in range(3):
        conv.append_action(tool="a", arguments={"x": 1})
        conv.append_observation(tool="a", arguments={"x": 1}, output={"v": 1})
    sd = StuckDetector(window=3)
    assert sd.is_stuck(conv) is True


def test_stuck_detector_ignores_argument_jitter():
    conv = Conversation(session_id="s")
    for x in (1, 2, 3):
        conv.append_action(tool="a", arguments={"x": x})
        conv.append_observation(tool="a", arguments={"x": x}, output={"v": x})
    sd = StuckDetector(window=3)
    # Different args ⇒ NOT stuck.
    assert sd.is_stuck(conv) is False
