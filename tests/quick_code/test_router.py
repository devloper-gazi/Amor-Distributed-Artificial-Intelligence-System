"""
Unit tests for ``document_processor/quick_code/router.py``.

The router is a hot path; we keep these tests deterministic with a
mock LLM call so the behaviour is independent of Ollama.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from document_processor.quick_code.contracts import TaskComplexity, TaskIR
from document_processor.quick_code.router import TaskClassifier


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


class _RecordingLLM:
    """Async callable that records calls and returns canned text."""

    def __init__(self, response: str = "simple", raises: Exception | None = None) -> None:
        self.response = response
        self.raises = raises
        self.calls: list[tuple[str, str | None, int]] = []

    async def __call__(
        self,
        prompt: str,
        system: str | None,
        max_tokens: int,
    ) -> str:
        self.calls.append((prompt, system, max_tokens))
        if self.raises is not None:
            raise self.raises
        return self.response


def _run(coro):  # tiny convenience for sync tests
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────
# Heuristic short-circuits (no LLM call expected)
# ─────────────────────────────────────────────────────────────────────


def test_classify_trivial_via_heuristic_no_llm_call():
    llm = _RecordingLLM("never-called")
    cls = TaskClassifier(llm_call=llm)
    out = _run(cls.classify("reverse a string in python"))
    assert out is TaskComplexity.TRIVIAL
    assert llm.calls == [], "heuristic should short-circuit, no LLM call"


def test_classify_math_via_heuristic_no_llm_call():
    llm = _RecordingLLM("never-called")
    cls = TaskClassifier(llm_call=llm)
    out = _run(cls.classify("compute the derivative of sin(x)"))
    assert out is TaskComplexity.MATH
    assert llm.calls == []


def test_classify_complex_via_long_prompt_no_llm_call():
    llm = _RecordingLLM("never-called")
    cls = TaskClassifier(llm_call=llm)
    long_prompt = "Build me a service. " * 100  # ~2000 chars
    out = _run(cls.classify(long_prompt))
    assert out is TaskComplexity.COMPLEX
    assert llm.calls == []


def test_classify_complex_via_keyword():
    llm = _RecordingLLM("never-called")
    cls = TaskClassifier(llm_call=llm)
    out = _run(cls.classify("design a distributed OAuth2 service"))
    assert out is TaskComplexity.COMPLEX
    assert llm.calls == []


def test_classify_simple_short_no_keyword():
    llm = _RecordingLLM("never-called")
    cls = TaskClassifier(llm_call=llm)
    out = _run(cls.classify("write a function that merges two dicts"))
    assert out is TaskComplexity.SIMPLE
    assert llm.calls == []


# ─────────────────────────────────────────────────────────────────────
# LLM fallback path
# ─────────────────────────────────────────────────────────────────────


def test_classify_ambiguous_falls_through_to_llm():
    # Long-ish but no keyword — needs an LLM verdict.
    prompt = (
        "Write something that takes input data and processes it through "
        "several steps, returning a useful output."
    ) * 4
    llm = _RecordingLLM("complex")
    cls = TaskClassifier(llm_call=llm)
    out = _run(cls.classify(prompt))
    assert out is TaskComplexity.COMPLEX
    assert len(llm.calls) == 1


def test_llm_failure_falls_back_to_simple():
    prompt = (
        "Write something that takes input data and processes it through "
        "several steps, returning a useful output."
    ) * 4
    llm = _RecordingLLM(raises=RuntimeError("ollama is down"))
    cls = TaskClassifier(llm_call=llm)
    out = _run(cls.classify(prompt))
    assert out is TaskComplexity.SIMPLE


def test_llm_unparseable_response_falls_back_to_simple():
    prompt = (
        "Write something that takes input data and processes it through "
        "several steps, returning a useful output."
    ) * 4
    llm = _RecordingLLM("I think this needs more context, possibly")
    cls = TaskClassifier(llm_call=llm)
    out = _run(cls.classify(prompt))
    assert out is TaskComplexity.SIMPLE


# ─────────────────────────────────────────────────────────────────────
# Redirect logic
# ─────────────────────────────────────────────────────────────────────


def test_redirect_complex_quick_mode():
    cls = TaskClassifier()
    assert _run(cls.should_redirect_to_pro(TaskComplexity.COMPLEX, "quick")) is True


def test_no_redirect_when_already_pro():
    cls = TaskClassifier()
    assert _run(cls.should_redirect_to_pro(TaskComplexity.COMPLEX, "pro")) is False


def test_no_redirect_for_non_complex():
    cls = TaskClassifier()
    for c in (TaskComplexity.TRIVIAL, TaskComplexity.SIMPLE, TaskComplexity.MATH):
        assert _run(cls.should_redirect_to_pro(c, "quick")) is False


def test_redirect_disabled_globally():
    cls = TaskClassifier(redirect_to_pro=False)
    assert _run(cls.should_redirect_to_pro(TaskComplexity.COMPLEX, "quick")) is False


# ─────────────────────────────────────────────────────────────────────
# IR mutation + event emission
# ─────────────────────────────────────────────────────────────────────


def test_classify_ir_sets_complexity_in_place():
    ir = TaskIR(id="x", prompt="reverse a string in python")
    cls = TaskClassifier()
    out = _run(cls.classify_ir(ir))
    assert out is TaskComplexity.TRIVIAL
    assert ir.complexity is TaskComplexity.TRIVIAL


def test_classify_emits_event_with_source_field():
    seen: list[tuple[str, dict[str, Any]]] = []

    async def cb(event: str, payload: dict[str, Any]) -> None:
        seen.append((event, payload))

    cls = TaskClassifier(on_event=cb)
    _run(cls.classify("reverse a string"))
    assert len(seen) == 1
    event, payload = seen[0]
    assert event == "router_classified"
    assert payload["complexity"] == "trivial"
    assert payload["source"] == "heuristic"


def test_redirect_emits_event_with_target():
    seen: list[tuple[str, dict[str, Any]]] = []

    async def cb(event: str, payload: dict[str, Any]) -> None:
        seen.append((event, payload))

    cls = TaskClassifier(on_event=cb)
    _run(cls.should_redirect_to_pro(TaskComplexity.COMPLEX, "quick"))
    assert any(name == "router_redirect_pro" for name, _ in seen)


def test_no_filter_language_in_system_prompt():
    """User explicitly forbade refusal/filter language in any new
    prompt template.  This test pins that contract."""
    from document_processor.quick_code.router import _LLM_SYSTEM_PROMPT

    banned = (
        "i cannot",
        "i won't",
        "i'm sorry",
        "as an ai",
        "it is not appropriate",
        "if illegal",
        "consult a lawyer",
    )
    lower = _LLM_SYSTEM_PROMPT.lower()
    for token in banned:
        assert token not in lower, f"router prompt contains banned token: {token!r}"
