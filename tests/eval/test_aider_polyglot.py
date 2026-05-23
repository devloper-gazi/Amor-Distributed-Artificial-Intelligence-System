"""Cycle G G1 — coverage for tools/eval/aider_polyglot.py.

What's being tested
-------------------
* Manifest registration with live runner
* Metadata loader (real fixture + missing-fixture stub fallback)
* Per-language harness wrapping (Python, JS, TS, Go, Rust, C++)
* Completion extraction from fenced + raw responses
* Output checker matches CASE_<i>:<value> lines
* Aggregation surfaces per_language pass-rate breakdown
* End-to-end with mocked LLM + mocked sandbox
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def isolated_eval_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AMOR_EVAL_OUT_ROOT", str(tmp_path))
    from tools.eval import aider_polyglot
    monkeypatch.setattr(aider_polyglot, "DATA_OUT_ROOT", tmp_path)
    return tmp_path


# ─── Manifest ──────────────────────────────────────────────────────


def test_aider_polyglot_registered_with_live_runner():
    """The manifest must ship with `runner=run_aider_polyglot`, not None."""
    from tools.eval import aider_polyglot
    from document_processor.api.admin_evals_routes import _EVAL_MANIFEST

    descriptor = _EVAL_MANIFEST.get("aider_polyglot_50")
    assert descriptor is not None
    assert descriptor.runner is aider_polyglot.run_aider_polyglot


def test_aider_polyglot_summary_keys_match_admin_ui_contract():
    from document_processor.api.admin_evals_routes import _EVAL_MANIFEST
    descriptor = _EVAL_MANIFEST.get("aider_polyglot_50")
    assert descriptor is not None
    # The keys the v19 launch gate + /admin/evals UI consume.
    for key in ("passed", "total", "pass_rate", "pass_rate_percent",
                "per_language"):
        assert key in descriptor.summary_keys


# ─── Metadata loader ───────────────────────────────────────────────


def test_metadata_loader_reads_real_fixture():
    from tools.eval.aider_polyglot import _load_task_metadata
    tasks = _load_task_metadata()
    # The committed fixture has 7 hand-curated tasks across 6 langs.
    assert len(tasks) >= 6
    languages = {t["language"] for t in tasks}
    assert "python" in languages
    assert "cpp" in languages
    # No task should be marked as stub when reading the real fixture.
    assert not any(t.get("stub") for t in tasks)


def test_metadata_loader_falls_back_to_stub_when_missing(
    monkeypatch, tmp_path,
):
    from tools.eval import aider_polyglot
    monkeypatch.setattr(
        aider_polyglot, "METADATA_PATH", tmp_path / "missing.json",
    )
    tasks = aider_polyglot._load_task_metadata()
    assert len(tasks) == len(aider_polyglot.LANGUAGES)
    assert all(t.get("stub") for t in tasks)


# ─── Code extraction ───────────────────────────────────────────────


def test_extract_code_handles_python_fence():
    from tools.eval.aider_polyglot import _extract_code
    raw = "Sure:\n```python\ndef solve(a, b):\n    return a + b\n```\nDone."
    out = _extract_code(raw, "python")
    assert "def solve" in out
    assert "return a + b" in out


def test_extract_code_handles_generic_fence():
    from tools.eval.aider_polyglot import _extract_code
    raw = "```\nfn solve() {}\n```"
    out = _extract_code(raw, "rust")
    assert "fn solve" in out


def test_extract_code_handles_unfenced():
    from tools.eval.aider_polyglot import _extract_code
    raw = "def solve(): return 42"
    out = _extract_code(raw, "python")
    assert out == "def solve(): return 42"


# ─── Per-language harness wrapping ─────────────────────────────────


def test_wrap_python_harness_invokes_function_per_case():
    from tools.eval.aider_polyglot import _wrap_for_execution
    task = {
        "language": "python",
        "function_name": "solve",
        "test_cases": [
            {"args": [1, 2], "expected": "3"},
            {"args": [0, 0], "expected": "0"},
        ],
    }
    out = _wrap_for_execution("def solve(a, b): return a + b", task)
    assert "def solve" in out
    assert "_CASES" in out
    assert "CASE_" in out


def test_wrap_javascript_harness():
    from tools.eval.aider_polyglot import _wrap_for_execution
    task = {
        "language": "javascript",
        "function_name": "solve",
        "test_cases": [{"args": [1, 2], "expected": "3"}],
    }
    out = _wrap_for_execution("function solve(a,b){return a+b;}", task)
    assert "function solve" in out
    assert "console.log" in out
    # CASE_ prefix is concatenated at runtime via `'CASE_' + i`
    assert "CASE_" in out
    # The completion's function is called with the test case's args.
    assert "solve(..._CASES[i]" in out


def test_wrap_go_harness_emits_main():
    from tools.eval.aider_polyglot import _wrap_for_execution
    task = {
        "language": "go",
        "function_name": "solve",
        "test_cases": [{"args": [1, 2], "expected": "3"}],
    }
    out = _wrap_for_execution(
        "package main\nimport \"fmt\"\nfunc solve(a, b int) int { return a + b }",
        task,
    )
    assert "func main()" in out
    assert "solve(1, 2)" in out


def test_wrap_rust_harness_emits_main():
    from tools.eval.aider_polyglot import _wrap_for_execution
    task = {
        "language": "rust",
        "function_name": "solve",
        "test_cases": [{"args": [1, 2], "expected": "3"}],
    }
    out = _wrap_for_execution(
        "fn solve(a: i32, b: i32) -> i32 { a + b }",
        task,
    )
    assert "fn main()" in out
    assert "println!" in out
    assert "solve(1, 2)" in out


def test_wrap_cpp_harness_includes_iostream():
    from tools.eval.aider_polyglot import _wrap_for_execution
    task = {
        "language": "cpp",
        "function_name": "solve",
        "test_cases": [{"args": [1, 2], "expected": "3"}],
    }
    out = _wrap_for_execution(
        "int solve(int a, int b) { return a + b; }",
        task,
    )
    assert "#include <iostream>" in out
    assert "int main()" in out
    assert "solve(1, 2)" in out


# ─── Output checker ────────────────────────────────────────────────


def test_check_output_matches_all_cases():
    from tools.eval.aider_polyglot import _check_output
    task = {
        "test_cases": [
            {"args": [1, 2], "expected": "3"},
            {"args": [5, 5], "expected": "10"},
        ],
    }
    stdout = "CASE_0:3\nCASE_1:10\n"
    assert _check_output(stdout, task) is True


def test_check_output_fails_on_partial_match():
    from tools.eval.aider_polyglot import _check_output
    task = {
        "test_cases": [
            {"args": [1, 2], "expected": "3"},
            {"args": [5, 5], "expected": "10"},
        ],
    }
    stdout = "CASE_0:3\nCASE_1:42\n"  # second case wrong
    assert _check_output(stdout, task) is False


def test_check_output_fails_on_empty_stdout():
    from tools.eval.aider_polyglot import _check_output
    task = {"test_cases": [{"args": [], "expected": "x"}]}
    assert _check_output("", task) is False


def test_check_output_fails_when_no_test_cases():
    from tools.eval.aider_polyglot import _check_output
    assert _check_output("CASE_0:42", {"test_cases": []}) is False


# ─── Aggregation ───────────────────────────────────────────────────


def test_aggregate_summary_groups_by_language():
    from tools.eval.aider_polyglot import _aggregate_summary
    cases = [
        {"task_id": "a", "language": "python", "passed": True, "wall_ms": 100},
        {"task_id": "b", "language": "python", "passed": False, "wall_ms": 200},
        {"task_id": "c", "language": "rust", "passed": True, "wall_ms": 300},
    ]
    summary = _aggregate_summary(
        cases,
        model="amor-editor",
        base_url="http://x",
        predictions_path=Path("/tmp/p.jsonl"),
    )
    assert summary["passed"] == 2
    assert summary["total"] == 3
    assert abs(summary["pass_rate"] - 2/3) < 0.001
    assert summary["per_language"]["python"]["passed"] == 1
    assert summary["per_language"]["python"]["total"] == 2
    assert summary["per_language"]["rust"]["passed"] == 1


def test_aggregate_summary_handles_empty():
    from tools.eval.aider_polyglot import _aggregate_summary
    summary = _aggregate_summary(
        [],
        model="amor-editor",
        base_url="http://x",
        predictions_path=Path("/tmp/p.jsonl"),
    )
    assert summary["passed"] == 0
    assert summary["total"] == 0
    assert summary["pass_rate"] == 0.0


# ─── End-to-end (mocked LLM + mocked sandbox) ──────────────────────


def test_run_aider_polyglot_e2e_smoke(monkeypatch, tmp_path):
    """Run the full runner with limit=2 against an LLM stub that
    returns a working Python function + a sandbox stub that returns
    matching output."""
    from tools.eval import aider_polyglot

    # Limit to 1 so the python sum_pair fixture matches the stub LLM
    # output (def solve(a, b): return a+b) for all 3 test cases.
    monkeypatch.setenv("AMOR_EVAL_LIMIT", "1")
    monkeypatch.setattr(aider_polyglot, "DATA_OUT_ROOT", tmp_path)

    # LLM stub
    class StubResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "```python\ndef solve(a, b):\n    return a + b\n```"}}]}
    class StubClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **k): return StubResponse()
    monkeypatch.setattr(aider_polyglot.httpx, "AsyncClient", StubClient)

    # Sandbox stub — return output matching the sum_pair fixture's
    # 3 cases (1+2=3, 10+(-3)=7, 0+0=0).
    class StubResult:
        exit_code = 0
        stdout = "CASE_0:3\nCASE_1:7\nCASE_2:0\n"
        stderr = ""
        skipped = False
    class StubSandbox:
        async def execute(self, code, language, timeout=None, **k):
            return StubResult()
    monkeypatch.setattr(aider_polyglot, "ExecutionSandbox", lambda: StubSandbox())

    async def driver():
        progress_log: list[str] = []
        async def progress(msg): progress_log.append(msg)
        return await aider_polyglot.run_aider_polyglot("test_e2e", progress)

    summary = asyncio.run(driver())
    assert summary["total"] == 1
    assert summary["passed"] == 1
    assert summary["pass_rate"] == 1.0

    # Predictions JSONL exists.
    pred_path = tmp_path / "aider_polyglot" / "predictions_test_e2e.jsonl"
    assert pred_path.is_file()
    rows = [json.loads(l) for l in pred_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert all(r["passed"] for r in rows)


def test_run_aider_polyglot_handles_sandbox_skipped(monkeypatch, tmp_path):
    """When sandbox returns skipped=True (e.g., no Docker access),
    cases mark `skipped=True` and `passed=False` but don't crash."""
    from tools.eval import aider_polyglot

    monkeypatch.setenv("AMOR_EVAL_LIMIT", "1")
    monkeypatch.setattr(aider_polyglot, "DATA_OUT_ROOT", tmp_path)

    class StubResponse:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "def solve(): pass"}}]}
    class StubClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **k): return StubResponse()
    monkeypatch.setattr(aider_polyglot.httpx, "AsyncClient", StubClient)

    class SkippedResult:
        exit_code = -1
        stdout = ""
        stderr = ""
        skipped = True
    class StubSandbox:
        async def execute(self, *a, **k): return SkippedResult()
    monkeypatch.setattr(aider_polyglot, "ExecutionSandbox", lambda: StubSandbox())

    async def driver():
        return await aider_polyglot.run_aider_polyglot(
            "test_skip",
            AsyncMock(),
        )

    summary = asyncio.run(driver())
    assert summary["total"] == 1
    assert summary["passed"] == 0
