"""Tests for thinking/engine._extract_json robustness.

v2.9.1 — reasoning models (qwen3:8b, deepseek-r1) wrap their reply in a
<think>…</think> chain-of-thought block whose prose contains stray { }
braces.  That corrupted the "widest balanced braces" fallback and caused
``thinking.phase_failed phase=decompose`` (a JSON parse error), which in
turn left the synthesize phase with nothing → the user saw an empty
"(done)" instead of an answer.  These pin the <think>-stripping fix.
"""

import pytest

from document_processor.thinking.engine import _extract_json


def test_direct_json():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_fenced_json():
    raw = "Here you go:\n```json\n{\"a\": 2}\n```\n"
    assert _extract_json(raw) == {"a": 2}


def test_think_block_then_json():
    # The exact qwen3:8b shape that used to fail.
    raw = "<think>\nLet me reason about this carefully.\n</think>\n{\"plan\": \"x\"}"
    assert _extract_json(raw) == {"plan": "x"}


def test_think_block_containing_braces():
    # Stray braces inside the think block must NOT corrupt extraction.
    raw = (
        "<think>I should emit JSON like { foo: bar } maybe, or {nested}.\n"
        "Decision: keep it flat.</think>\n"
        '{"sub_questions": ["q1", "q2"]}'
    )
    assert _extract_json(raw) == {"sub_questions": ["q1", "q2"]}


def test_think_block_with_fenced_json_after():
    raw = "<think>hmm { } </think>\n```json\n{\"ok\": true}\n```"
    assert _extract_json(raw) == {"ok": True}


def test_empty_raises():
    with pytest.raises(ValueError):
        _extract_json("")


def test_no_json_raises():
    with pytest.raises(ValueError):
        _extract_json("<think>just thinking, no json</think> plain prose")
