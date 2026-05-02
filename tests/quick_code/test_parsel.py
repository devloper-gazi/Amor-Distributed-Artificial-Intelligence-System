"""
Unit tests for ``document_processor/quick_code/parsel.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from document_processor.quick_code.contracts import TaskIR
from document_processor.quick_code.parsel import (
    PARSEL_SYSTEM_PROMPT,
    ParselDecomposer,
)


def _run(coro):
    return asyncio.run(coro)


def _ir(prompt: str) -> TaskIR:
    return TaskIR(id="t1", prompt=prompt)


# ─────────────────────────────────────────────────────────────────────
# LLM stubs
# ─────────────────────────────────────────────────────────────────────


class _FixedLLM:
    def __init__(self, response: str, *, raises: Exception | None = None) -> None:
        self.response = response
        self.raises = raises
        self.calls: list[tuple[str, str | None, int]] = []

    async def __call__(self, prompt: str, system: str | None, max_tokens: int) -> str:
        self.calls.append((prompt, system, max_tokens))
        if self.raises is not None:
            raise self.raises
        return self.response


# ─────────────────────────────────────────────────────────────────────
# Short-circuit on tiny prompts
# ─────────────────────────────────────────────────────────────────────


def test_short_prompt_skips_llm():
    llm = _FixedLLM("ignored")
    pd = ParselDecomposer(llm_call=llm, short_circuit_chars=200)
    ir = _ir("foo")
    out = _run(pd.decompose(ir))
    assert out is ir
    assert ir.subtasks == []
    assert llm.calls == []


# ─────────────────────────────────────────────────────────────────────
# Successful decomposition
# ─────────────────────────────────────────────────────────────────────


def test_decompose_populates_subtasks():
    valid_response = json.dumps({
        "subtasks": [
            {
                "id": "parse_input",
                "title": "Parse the input string",
                "description": "Tokenise and validate",
                "contract_pre": [
                    {"kind": "pre", "expression": "len(input) > 0", "description": "non-empty"}
                ],
                "contract_post": [
                    {"kind": "post", "expression": "tokens == split(input)", "description": "tokens match"}
                ],
                "dependencies": [],
            },
            {
                "id": "compute_result",
                "title": "Compute the result",
                "description": "Run algorithm",
                "contract_pre": [],
                "contract_post": [
                    {"kind": "post", "expression": "result is not None"}
                ],
                "dependencies": ["parse_input"],
            },
        ]
    })
    llm = _FixedLLM(valid_response)
    pd = ParselDecomposer(llm_call=llm, short_circuit_chars=10)
    ir = _ir("Build a calculator that parses arithmetic expressions and evaluates them with operator precedence.")
    out = _run(pd.decompose(ir))
    assert len(out.subtasks) == 2
    assert out.subtasks[0].id == "parse_input"
    assert out.subtasks[1].dependencies == ["parse_input"]


def test_decompose_drops_unknown_dependencies():
    """The LLM may hallucinate a dep id that doesn't appear elsewhere
    in its response.  ParselDecomposer must scrub those before
    handing the result to TaskIR (which would reject the whole graph
    otherwise)."""
    response = json.dumps({
        "subtasks": [
            {
                "id": "a",
                "title": "Step A",
                "dependencies": ["nonexistent"],
            },
            {
                "id": "b",
                "title": "Step B",
                "dependencies": ["a", "another_ghost"],
            },
        ]
    })
    llm = _FixedLLM(response)
    pd = ParselDecomposer(llm_call=llm, short_circuit_chars=10)
    ir = _ir(
        "Build a service that does X. " * 30
    )  # long enough to bypass short-circuit
    out = _run(pd.decompose(ir))
    assert len(out.subtasks) == 2
    assert out.subtasks[0].dependencies == []
    assert out.subtasks[1].dependencies == ["a"]


def test_decompose_caps_at_max_subtasks():
    response = json.dumps({
        "subtasks": [
            {"id": f"s{i}", "title": f"Step {i}", "dependencies": []}
            for i in range(20)
        ]
    })
    llm = _FixedLLM(response)
    pd = ParselDecomposer(llm_call=llm, short_circuit_chars=10, max_subtasks=3)
    ir = _ir("Long prompt. " * 30)
    out = _run(pd.decompose(ir))
    assert len(out.subtasks) == 3


# ─────────────────────────────────────────────────────────────────────
# Robustness against malformed / failing LLM
# ─────────────────────────────────────────────────────────────────────


def test_unparseable_response_is_no_op():
    llm = _FixedLLM("not even close to JSON")
    pd = ParselDecomposer(llm_call=llm, short_circuit_chars=10)
    ir = _ir("Long prompt. " * 30)
    out = _run(pd.decompose(ir))
    assert out.subtasks == []


def test_llm_exception_is_no_op():
    llm = _FixedLLM("", raises=RuntimeError("ollama is down"))
    pd = ParselDecomposer(llm_call=llm, short_circuit_chars=10)
    ir = _ir("Long prompt. " * 30)
    out = _run(pd.decompose(ir))
    assert out.subtasks == []


def test_already_decomposed_ir_is_left_alone():
    # Construct an IR with subtasks pre-populated.
    ir = TaskIR(
        id="t1",
        prompt="anything",
        subtasks=[],
    )
    # Manually set a single subtask via direct dict mutation through
    # the validator path:
    from document_processor.quick_code.contracts import SubTask

    ir.subtasks = [SubTask(id="manual", title="hand-rolled")]
    llm = _FixedLLM("{}")
    pd = ParselDecomposer(llm_call=llm, short_circuit_chars=10)
    out = _run(pd.decompose(ir))
    assert len(out.subtasks) == 1
    assert out.subtasks[0].id == "manual"
    assert llm.calls == []


# ─────────────────────────────────────────────────────────────────────
# Event emission
# ─────────────────────────────────────────────────────────────────────


def test_emits_decomposed_event_with_ids():
    seen: list[tuple[str, dict[str, Any]]] = []

    async def cb(event: str, payload: dict[str, Any]) -> None:
        seen.append((event, payload))

    response = json.dumps({
        "subtasks": [
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B"},
        ]
    })
    llm = _FixedLLM(response)
    pd = ParselDecomposer(llm_call=llm, short_circuit_chars=10, on_event=cb)
    _run(pd.decompose(_ir("Long prompt. " * 30)))
    decomposed = [(e, p) for e, p in seen if e == "parsel_decomposed"]
    assert decomposed
    assert decomposed[0][1]["count"] == 2
    assert decomposed[0][1]["ids"] == ["a", "b"]


# ─────────────────────────────────────────────────────────────────────
# No-filter prompt sanity
# ─────────────────────────────────────────────────────────────────────


def test_system_prompt_has_no_refusal_language():
    text = PARSEL_SYSTEM_PROMPT.lower()
    for token in (
        "i cannot",
        "i won't",
        "i'm sorry",
        "as an ai",
        "if illegal",
        "consult a lawyer",
        "educational purposes only",
    ):
        assert token not in text, f"parsel prompt contains banned token: {token!r}"
