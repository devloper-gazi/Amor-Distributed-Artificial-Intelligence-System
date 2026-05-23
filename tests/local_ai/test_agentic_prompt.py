"""
Cycle C Sprint 8 Day 2 — ReAct prompt + parser tests.

The agent's reliability depends on the parser being permissive but
deterministic.  These tests pin both directions:

* ``render_prompt`` produces a string that includes the user's task,
  the catalogue, and the format rules.
* ``parse_react`` extracts the first thought+action pair from a wide
  range of model outputs (clean, prose-padded, trailing-comma JSON,
  malformed) without crashing.
"""

from __future__ import annotations

import pytest

from local_ai.agentic.prompt import (
    parse_react,
    render_history,
    render_prompt,
    render_tool_catalogue,
)


# ─── render ─────────────────────────────────────────────────────


def _tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "sandbox-execute",
                "description": "Run code in the sandbox.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "language": {"type": "string"},
                        "code": {"type": "string"},
                        "timeout": {"type": "integer"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "repo-symbol-search",
                "description": "BM25 over indexed symbols.",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}},
                },
            },
        },
    ]


def test_render_tool_catalogue_emits_signature():
    out = render_tool_catalogue(_tools())
    assert "sandbox-execute(language, code, timeout)" in out
    assert "repo-symbol-search(q, limit)" in out
    assert "finish(answer)" in out  # synthetic tool always available


def test_render_prompt_includes_task_and_rules():
    p = render_prompt(user_task="solve fizzbuzz", tools=_tools(), max_iterations=5)
    assert "solve fizzbuzz" in p
    assert "<thought>" in p
    assert "<action>" in p
    assert "max 5 iterations" in p


def test_render_prompt_includes_history_when_provided():
    p = render_prompt(
        user_task="x",
        tools=_tools(),
        history="<thought>thinking...</thought>",
        max_iterations=3,
    )
    assert "Conversation so far" in p
    assert "thinking..." in p


# ─── parse: happy path ─────────────────────────────────────────


def test_parse_clean_react_block():
    text = (
        "<thought>Need to inspect the repo first.</thought>\n"
        '<action>{"tool": "repo-symbol-search", "arguments": {"q": "Engine", "limit": 4}}</action>\n'
    )
    p = parse_react(text)
    assert p.thought == "Need to inspect the repo first."
    assert p.action_tool == "repo-symbol-search"
    assert p.action_arguments == {"q": "Engine", "limit": 4}
    assert p.parse_error is None


def test_parse_strips_prose_around_blocks():
    text = (
        "Sure, here's what I'll do:\n"
        "<thought>Run python.</thought>\n"
        "Then I'll check the output:\n"
        '<action>{"tool":"sandbox-execute","arguments":{"language":"python","code":"print(2+2)"}}</action>\n'
        "Hope this works!"
    )
    p = parse_react(text)
    assert p.thought == "Run python."
    assert p.action_tool == "sandbox-execute"
    assert p.action_arguments["language"] == "python"


def test_parse_takes_first_action_only():
    text = (
        "<thought>A.</thought>"
        '<action>{"tool":"a","arguments":{}}</action>'
        '<action>{"tool":"b","arguments":{}}</action>'
    )
    p = parse_react(text)
    assert p.action_tool == "a"


def test_parse_handles_trailing_comma_in_args():
    text = (
        "<thought>retry</thought>"
        '<action>{"tool":"echo","arguments":{"x":1,}}</action>'
    )
    p = parse_react(text)
    assert p.parse_error is None
    assert p.action_tool == "echo"
    assert p.action_arguments == {"x": 1}


def test_parse_finish_action():
    text = (
        "<thought>Done.</thought>"
        '<action>{"tool":"finish","arguments":{"answer":"42"}}</action>'
    )
    p = parse_react(text)
    assert p.action_tool == "finish"
    assert p.action_arguments == {"answer": "42"}


# ─── parse: error paths ────────────────────────────────────────


def test_parse_empty_text():
    p = parse_react("")
    assert p.parse_error == "empty response"
    assert p.action_tool is None


def test_parse_missing_action_block():
    p = parse_react("<thought>thinking…</thought>\n(no action!)")
    assert p.thought == "thinking…"
    assert p.action_tool is None
    assert p.parse_error == "no <action> block found"


def test_parse_invalid_json_action():
    text = "<thought>x</thought><action>not json at all</action>"
    p = parse_react(text)
    assert p.parse_error is not None
    assert "JSON" in p.parse_error or "json" in p.parse_error
    assert p.action_tool is None


def test_parse_missing_tool_field():
    text = "<thought>x</thought><action>{\"arguments\": {}}</action>"
    p = parse_react(text)
    assert p.parse_error is not None
    assert "tool" in p.parse_error
    assert p.action_tool is None


def test_parse_arguments_default_to_empty_dict_when_missing():
    text = "<thought>x</thought><action>{\"tool\":\"echo\"}</action>"
    p = parse_react(text)
    assert p.action_tool == "echo"
    assert p.action_arguments == {}


def test_parse_non_dict_arguments_coerced_to_empty():
    """If the model emits ``arguments: "string"`` we don't crash —
    we drop the bad value and treat the call as no-arg."""
    text = "<thought>x</thought><action>{\"tool\":\"echo\",\"arguments\":\"oops\"}</action>"
    p = parse_react(text)
    assert p.action_tool == "echo"
    assert p.action_arguments == {}


# ─── history rendering (re-prompt) ────────────────────────────


def test_render_history_round_trips_basic_events():
    from local_ai.agentic.events import (
        ActionEvent,
        MessageEvent,
        ObservationEvent,
        ThoughtEvent,
    )
    history = [
        MessageEvent(role="user", text="solve fizzbuzz"),
        ThoughtEvent(text="I'll write Python"),
        ActionEvent(tool="sandbox-execute", arguments={"language": "python"}, iteration=1),
        ObservationEvent(tool="sandbox-execute", arguments={}, output={"exit_code": 0}, iteration=1),
    ]
    out = render_history(history)
    assert "<message role='user'>solve fizzbuzz</message>" in out
    assert "<thought>I'll write Python</thought>" in out
    assert "sandbox-execute" in out
    assert "exit_code" in out
