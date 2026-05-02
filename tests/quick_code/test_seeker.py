"""
Unit tests for ``document_processor/quick_code/seeker.py``.

Tests use mocked LLM + sandbox.  We validate:

* the failure classifier returns the right slug for each common
  Python error,
* the scanner picks lines from the traceback,
* the predator parses LLM JSON output into candidate code,
* the ranker prefers candidates close to the original,
* the full ``refine`` loop calls Scanner → Predator → Handler the
  expected number of times and stops on success.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from document_processor.quick_code.contracts import SandboxResult, SandboxTier
from document_processor.quick_code.seeker import (
    PREDATOR_SYSTEM_PROMPT,
    SeekerDebugger,
    classify_failure,
    scan_failure,
)


def _run(coro):
    return asyncio.run(coro)


def _failure(stderr: str, *, exit_code: int = 1) -> SandboxResult:
    return SandboxResult(
        ok=False, stderr=stderr, exit_code=exit_code, tier=SandboxTier.QUICK
    )


# ─────────────────────────────────────────────────────────────────────
# Detector
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stderr, expected",
    [
        ("Traceback (most recent call last):\nValueError: bad input", "value_error"),
        ("ImportError: No module named foo", "import_error"),
        ("ModuleNotFoundError: No module named bar", "import_error"),
        ("AttributeError: 'X' object has no attribute 'y'", "attribute_error"),
        ("TypeError: unsupported operand", "type_error"),
        ("AssertionError", "assertion_error"),
        ("IndexError: list index out of range", "index_error"),
        ("ZeroDivisionError: division by zero", "zero_division"),
        ("RecursionError", "recursion_error"),
        ("SyntaxError: invalid syntax", "syntax_error"),
        ("Process timed out after 15s", "timeout"),
        ("MemoryError", "memory_error"),
        ("", "unknown"),
        ("clean output", "unknown"),
    ],
)
def test_classify_failure(stderr, expected):
    assert classify_failure(stderr) == expected


# ─────────────────────────────────────────────────────────────────────
# Scanner
# ─────────────────────────────────────────────────────────────────────


def test_scan_pulls_lines_from_traceback():
    code = "\n".join(
        [
            "def f(x):",
            "    return x + 1",
            "result = f(None)",
            "print(result)",
        ]
    )
    stderr = (
        'Traceback (most recent call last):\n'
        '  File "main.py", line 3, in <module>\n'
        '    result = f(None)\n'
        "TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'\n"
    )
    out = scan_failure(code, _failure(stderr), context_lines=1)
    suspect_lines = [ln for ln, _ in out["suspects"]]
    assert 3 in suspect_lines  # the line cited in the traceback


def test_scan_extracts_missing_names():
    out = scan_failure("x = 1", _failure("NameError: name 'undefined' is not defined"))
    assert "undefined" in out["missing_names"]


def test_scan_extracts_missing_attrs():
    out = scan_failure(
        "x = 1", _failure("AttributeError: 'X' object has no attribute 'y'")
    )
    assert "y" in out["missing_attrs"]


# ─────────────────────────────────────────────────────────────────────
# Predator parsing
# ─────────────────────────────────────────────────────────────────────


def test_parse_candidates_strips_fences():
    text = """```json
{"candidates":[{"label":"a","code":"def f():\\n    return 1","rationale":"fix"}]}
```"""
    out = SeekerDebugger._parse_candidates(text)
    assert out == ["def f():\n    return 1"]


def test_parse_candidates_returns_empty_on_garbage():
    assert SeekerDebugger._parse_candidates("nope") == []


def test_parse_candidates_caps_at_three():
    blob = json.dumps({
        "candidates": [
            {"label": f"l{i}", "code": f"def f{i}(): pass", "rationale": ""}
            for i in range(8)
        ]
    })
    assert len(SeekerDebugger._parse_candidates(blob)) == 3


# ─────────────────────────────────────────────────────────────────────
# Ranker heuristic
# ─────────────────────────────────────────────────────────────────────


def test_heuristic_prefers_close_candidate():
    original = "def f(x): return x + 1"
    near = "def f(x): return x + 2"
    far = "import os\nfor i in range(99): print(i)"
    assert SeekerDebugger._heuristic_score(original, near) > SeekerDebugger._heuristic_score(original, far)


def test_heuristic_penalises_size_blowup():
    original = "def f(x): return x + 1"
    blowup = original + "\n# pad" * 200
    near = "def f(x): return x + 2"
    assert SeekerDebugger._heuristic_score(original, near) > SeekerDebugger._heuristic_score(original, blowup)


# ─────────────────────────────────────────────────────────────────────
# Refine loop
# ─────────────────────────────────────────────────────────────────────


class _LLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self, prompt: str, system: str | None, max_tokens: int) -> str:
        self.calls += 1
        if not self.responses:
            return ""
        return self.responses.pop(0)


class _Sandbox:
    def __init__(self, results: list[bool]) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    async def execute(self, code: str, **kwargs):
        self.calls.append(code)
        ok = self.results.pop(0) if self.results else False
        return SandboxResult(
            ok=ok,
            stderr="" if ok else "Traceback...\nValueError: still broken",
            exit_code=0 if ok else 1,
            tier=SandboxTier.QUICK,
        )


def _candidate_response(code: str) -> str:
    return json.dumps({
        "candidates": [
            {"label": "a", "code": code, "rationale": "fix"},
        ]
    })


def test_refine_returns_passing_patch_on_first_iter():
    llm = _LLM([_candidate_response("def f(x): return x + 1")])
    sandbox = _Sandbox([True])
    seeker = SeekerDebugger(llm_call=llm, sandbox=sandbox, max_iters=2)
    code, tests, iters = _run(
        seeker.refine(
            code="def f(x): return x + None",
            tests=None,
            last_failure=_failure("TypeError: NoneType + int"),
        )
    )
    assert iters == 1
    assert "x + 1" in code
    assert tests is None


def test_refine_iterates_until_pass():
    llm = _LLM([
        _candidate_response("def f(x): return x + None  # still broken 1"),
        _candidate_response("def f(x): return x + 1  # fixed"),
    ])
    sandbox = _Sandbox([False, True])
    seeker = SeekerDebugger(llm_call=llm, sandbox=sandbox, max_iters=3)
    code, _tests, iters = _run(
        seeker.refine(
            code="def f(x): return x + None",
            tests=None,
            last_failure=_failure("TypeError"),
        )
    )
    assert iters == 2
    assert "x + 1" in code


def test_refine_caps_at_max_iters():
    # Always returns broken code; sandbox always fails.
    llm = _LLM([_candidate_response("def f(x): return None")] * 5)
    sandbox = _Sandbox([False] * 5)
    seeker = SeekerDebugger(llm_call=llm, sandbox=sandbox, max_iters=2)
    _, _, iters = _run(
        seeker.refine(
            code="def f(x): return x + None",
            tests=None,
            last_failure=_failure("TypeError"),
        )
    )
    assert iters == 2


def test_refine_max_iters_zero_returns_unchanged():
    seeker = SeekerDebugger(max_iters=0)
    code = "def f(): pass"
    out_code, _t, iters = _run(
        seeker.refine(code=code, tests=None, last_failure=_failure("Anything"))
    )
    assert iters == 0
    assert out_code == code


def test_refine_emits_phase_events():
    seen: list[tuple[str, dict[str, Any]]] = []

    async def cb(event: str, payload: dict[str, Any]) -> None:
        seen.append((event, payload))

    llm = _LLM([_candidate_response("def f(): return 1")])
    sandbox = _Sandbox([True])
    seeker = SeekerDebugger(
        llm_call=llm, sandbox=sandbox, max_iters=1, on_event=cb
    )
    _run(
        seeker.refine(
            code="def f(): return None",
            tests=None,
            last_failure=_failure("TypeError"),
        )
    )
    events = [name for name, _ in seen]
    assert "seeker_scan" in events
    assert "seeker_handler" in events


# ─────────────────────────────────────────────────────────────────────
# Prompt sanity
# ─────────────────────────────────────────────────────────────────────


def test_predator_system_prompt_no_filters():
    text = PREDATOR_SYSTEM_PROMPT.lower()
    for token in (
        "i cannot",
        "i won't",
        "as an ai",
        "consult a lawyer",
        "if illegal",
    ):
        assert token not in text
