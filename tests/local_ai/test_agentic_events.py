"""
Cycle C Sprint 8 Day 1 — event taxonomy + conversation tests.

Pins three contracts the rest of the loop depends on:

* Events are immutable, JSON-serialisable, with stable ``kind`` + ULID.
* ``Conversation`` is append-only with iteration tracking.
* ``Event.to_tool_stream`` projects into the Sprint 4 SSE envelope so
  ``ToolCallCard`` already understands the shape.
"""

from __future__ import annotations

import pytest

from local_ai.agentic.events import (
    ActionEvent,
    Event,
    MessageEvent,
    ObservationEvent,
    ThoughtEvent,
    _ulid,
)
from local_ai.agentic.conversation import Conversation


# ─── id / serialisation ─────────────────────────────────────────


def test_ulid_is_26_chars_uppercase():
    u = _ulid()
    assert len(u) == 26
    assert u == u.upper()


def test_ulid_is_unique():
    seen = {_ulid() for _ in range(2_000)}
    assert len(seen) == 2_000


def test_event_is_frozen():
    ev = MessageEvent(role="user", text="hi")
    with pytest.raises(Exception):  # ValidationError or AttributeError
        ev.text = "mutated"  # type: ignore[misc]


def test_event_extras_forbidden():
    """A typo in a producer must surface at validation time, not
    silently land on the wire."""
    with pytest.raises(Exception):
        MessageEvent(role="user", text="hi", typo="x")  # type: ignore[call-arg]


def test_event_round_trips_json():
    ev = ActionEvent(tool="echo", arguments={"k": 1}, iteration=2)
    blob = ev.model_dump_json()
    again = ActionEvent.model_validate_json(blob)
    assert again == ev
    assert again.id == ev.id


# ─── kind discriminators ────────────────────────────────────────


def test_kinds_are_pinned():
    assert MessageEvent(role="user", text="x").kind == "message"
    assert ThoughtEvent(text="x").kind == "thought"
    assert ActionEvent(tool="t").kind == "action"
    assert ObservationEvent(tool="t", output=None).kind == "observation"


# ─── tool-stream projection ─────────────────────────────────────


def test_action_projects_to_input_pair():
    ev = ActionEvent(tool="sandbox-execute", arguments={"language": "python"}, iteration=2)
    out = ev.to_tool_stream()
    assert len(out) == 2
    assert out[0]["type"] == "tool-input-start"
    assert out[0]["tool"] == "sandbox-execute"
    assert out[0]["toolCallId"] == "sandbox-execute-2"
    assert out[1]["type"] == "tool-input-available"
    assert out[1]["input"] == {"language": "python"}


def test_observation_success_projects_single_output():
    ev = ObservationEvent(
        tool="sandbox-execute",
        iteration=2,
        output={"exit_code": 0, "stdout": "ok"},
    )
    out = ev.to_tool_stream()
    assert len(out) == 1
    assert out[0]["type"] == "tool-output-available"
    assert out[0]["isError"] is False


def test_observation_error_projects_pair():
    ev = ObservationEvent(
        tool="sandbox-execute",
        iteration=2,
        output=None,
        is_error=True,
        error_message="boom",
    )
    out = ev.to_tool_stream()
    assert len(out) == 2
    assert out[0]["type"] == "tool-output-available"
    assert out[0]["isError"] is True
    assert out[1]["type"] == "tool-error"
    assert "boom" in out[1]["message"]


def test_message_and_thought_project_empty():
    assert MessageEvent(role="user", text="hi").to_tool_stream() == []
    assert ThoughtEvent(text="hmm").to_tool_stream() == []


# ─── conversation: append-only ──────────────────────────────────


def test_conversation_append_grows_log():
    conv = Conversation(session_id="s1")
    assert len(conv) == 0
    conv.append_message("user", "do it")
    conv.append_thought("plan: …")
    conv.append_action(tool="echo", arguments={"x": 1})
    conv.append_observation(tool="echo", arguments={"x": 1}, output=1)
    assert len(conv) == 4


def test_conversation_iter_kind_filters():
    conv = Conversation(session_id="s2")
    conv.append_message("user", "go")
    conv.append_action(tool="echo", arguments={})
    conv.append_observation(tool="echo", arguments={}, output=None)
    conv.append_action(tool="echo2", arguments={})
    actions = list(conv.iter_kind("action"))
    obs = list(conv.iter_kind("observation"))
    assert len(actions) == 2
    assert len(obs) == 1


def test_conversation_iteration_advances():
    conv = Conversation(session_id="s3")
    assert conv.iteration == 0
    assert conv.start_iteration() == 1
    assert conv.start_iteration() == 2
    conv.append_action(tool="echo", arguments={})
    last = conv.last("action")
    assert last is not None
    assert getattr(last, "iteration", -1) == 2


def test_conversation_finish_blocks_further_appends():
    conv = Conversation(session_id="s4")
    conv.append_message("user", "go")
    conv.finish("done")
    assert conv.finished is True
    assert conv.finish_reason == "done"
    with pytest.raises(RuntimeError):
        conv.append_message("user", "again")


def test_conversation_events_returned_as_defensive_copy():
    conv = Conversation(session_id="s5")
    conv.append_message("user", "go")
    snap1 = conv.events
    snap1.append("garbage")  # type: ignore[arg-type]
    assert len(conv) == 1  # internal log was NOT mutated


def test_conversation_action_observation_pairs():
    conv = Conversation(session_id="s6")
    conv.append_action(tool="a", arguments={})
    conv.append_observation(tool="a", arguments={}, output=1)
    conv.append_action(tool="b", arguments={})
    # b has no observation yet — must be excluded.
    pairs = conv.action_observation_pairs()
    assert len(pairs) == 1
    assert pairs[0][0].tool == "a"
    assert pairs[0][1].output == 1
    conv.append_observation(tool="b", arguments={}, output=2)
    pairs = conv.action_observation_pairs()
    assert len(pairs) == 2


def test_conversation_snapshot_carries_state():
    conv = Conversation(session_id="s7")
    conv.start_iteration()
    conv.append_message("user", "go")
    conv.finish("max-iter")
    snap = conv.snapshot()
    assert snap.session_id == "s7"
    assert len(snap.events) == 1
    assert snap.iteration == 1
    assert snap.finished is True
    assert snap.finish_reason == "max-iter"
