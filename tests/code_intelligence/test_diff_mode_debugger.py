"""Tests for Phase 17 Commit T — diff-mode DebuggerAgent.

Search/replace block format chosen over unified diff because
small LLMs generate it more reliably (Aider, Cline, OpenHands
all use the same).  Falls back to whole-file rewrite when the
patch doesn't apply cleanly.
"""

from __future__ import annotations

import asyncio

import pytest

from document_processor.code_intelligence.diff_apply import (
    SearchReplaceBlock,
    apply_blocks,
    apply_search_replace_diff,
    extract_blocks,
)


def _run(coro):
    return asyncio.run(coro)


# ─── extract_blocks ───────────────────────────────────────────────


def test_extract_single_block_inside_diff_fence():
    raw = (
        "Here is the fix:\n\n"
        "```diff\n"
        "<<<<<<< SEARCH\n"
        "old line\n"
        "=======\n"
        "new line\n"
        ">>>>>>> REPLACE\n"
        "```\n"
    )
    blocks = extract_blocks(raw)
    assert len(blocks) == 1
    assert blocks[0].search == "old line"
    assert blocks[0].replace == "new line"


def test_extract_multiple_blocks_in_one_fence():
    raw = (
        "```diff\n"
        "<<<<<<< SEARCH\n"
        "alpha\n"
        "=======\n"
        "ALPHA\n"
        ">>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\n"
        "beta\n"
        "=======\n"
        "BETA\n"
        ">>>>>>> REPLACE\n"
        "```\n"
    )
    blocks = extract_blocks(raw)
    assert len(blocks) == 2
    assert [b.search for b in blocks] == ["alpha", "beta"]
    assert [b.replace for b in blocks] == ["ALPHA", "BETA"]


def test_extract_no_fence_falls_through_to_raw():
    """LLMs sometimes forget the fence wrapper; we still parse."""
    raw = (
        "<<<<<<< SEARCH\n"
        "old\n"
        "=======\n"
        "new\n"
        ">>>>>>> REPLACE\n"
    )
    assert len(extract_blocks(raw)) == 1


def test_extract_empty_input():
    assert extract_blocks("") == []
    assert extract_blocks(None) == []  # type: ignore


def test_extract_handles_multi_line_search_and_replace():
    raw = (
        "```diff\n"
        "<<<<<<< SEARCH\n"
        "def fib(n):\n"
        "    return n if n < 2 else fib(n-1)+fib(n-2)\n"
        "=======\n"
        "def fib(n: int) -> int:\n"
        "    if n < 2:\n"
        "        return n\n"
        "    a, b = 0, 1\n"
        "    for _ in range(n - 1):\n"
        "        a, b = b, a + b\n"
        "    return b\n"
        ">>>>>>> REPLACE\n"
        "```\n"
    )
    blocks = extract_blocks(raw)
    assert len(blocks) == 1
    assert "def fib(n):" in blocks[0].search
    assert "def fib(n: int) -> int:" in blocks[0].replace


# ─── apply_blocks ─────────────────────────────────────────────────


def test_apply_clean_single_block():
    code = "line one\nbroken line\nline three\n"
    blocks = [SearchReplaceBlock(search="broken line", replace="fixed line")]
    out = apply_blocks(code, blocks)
    assert out.ok is True
    assert out.patched == "line one\nfixed line\nline three\n"
    assert out.blocks_applied == 1


def test_apply_multi_hunk_in_order():
    code = "alpha\nbeta\ngamma\n"
    blocks = [
        SearchReplaceBlock(search="alpha", replace="ALPHA"),
        SearchReplaceBlock(search="gamma", replace="GAMMA"),
    ]
    out = apply_blocks(code, blocks)
    assert out.ok is True
    assert out.patched == "ALPHA\nbeta\nGAMMA\n"
    assert out.blocks_applied == 2


def test_apply_rejects_when_search_not_found():
    code = "line one\n"
    blocks = [SearchReplaceBlock(search="nope", replace="x")]
    out = apply_blocks(code, blocks)
    assert out.ok is False
    assert "not found" in out.error
    # Original code is preserved.
    assert out.patched == code


def test_apply_rejects_ambiguous_match():
    code = "x = 1\nx = 1\n"   # search appears twice → ambiguous
    blocks = [SearchReplaceBlock(search="x = 1", replace="x = 2")]
    out = apply_blocks(code, blocks)
    assert out.ok is False
    assert "ambiguous" in out.error or "appears 2 times" in out.error


def test_apply_preserves_untouched_lines():
    """The Plan agent flagged whole-file mode for "introducing
    regressions in untouched lines".  Diff mode must not touch
    anything outside SEARCH."""
    code = (
        "def util():\n"
        "    return 'unchanged'\n"
        "\n"
        "def buggy():\n"
        "    return wrong()\n"
        "\n"
        "def main():\n"
        "    return util()\n"
    )
    blocks = [SearchReplaceBlock(
        search="def buggy():\n    return wrong()",
        replace="def buggy():\n    return correct()",
    )]
    out = apply_blocks(code, blocks)
    assert out.ok is True
    assert "return 'unchanged'" in out.patched   # util() untouched
    assert "return util()" in out.patched         # main() untouched
    assert "return correct()" in out.patched      # buggy() fixed


def test_apply_empty_search_prepends():
    code = "existing\n"
    blocks = [SearchReplaceBlock(search="", replace="brand new\n")]
    out = apply_blocks(code, blocks)
    assert out.ok is True
    assert out.patched.startswith("brand new")
    assert "existing" in out.patched


def test_apply_empty_block_list_rejects():
    out = apply_blocks("anything", [])
    assert out.ok is False


# ─── apply_search_replace_diff (extract + apply) ──────────────────


def test_apply_search_replace_diff_end_to_end():
    code = "alpha\nbeta\n"
    raw = (
        "```diff\n"
        "<<<<<<< SEARCH\n"
        "beta\n"
        "=======\n"
        "BETA\n"
        ">>>>>>> REPLACE\n"
        "```\n"
    )
    out = apply_search_replace_diff(code, raw)
    assert out.ok is True
    assert out.patched == "alpha\nBETA\n"


def test_apply_search_replace_diff_rejects_no_blocks():
    out = apply_search_replace_diff("x", "no blocks here")
    assert out.ok is False
    assert "no SEARCH/REPLACE blocks" in out.error


# ─── DebuggerAgent integration (mocked LLM) ───────────────────────


def _make_debugger_with_diff_response(diff_text: str):
    """Build a DebuggerAgent whose LLM returns a fixed diff +
    metadata.  The first call returns the diff; subsequent calls
    (the whole-file fallback) return a marker so we can assert
    the fallback was reached."""
    from document_processor.code_intelligence.agents import DebuggerAgent

    calls: list[tuple[str, str]] = []

    async def _stub(prompt, system, max_tokens):
        calls.append((system or "", prompt or ""))
        if "DIFF MODE" in (system or "") or "search[-_]?replace" in (system or "").lower():
            return diff_text
        # Whole-file fallback.
        return (
            "```python\n"
            "fixed_whole_file = True\n"
            "```\n"
            '```json\n{"root_cause":"x","fix_description":"x",'
            '"lines_changed":1,"confidence":"low"}\n```'
        )

    agent = DebuggerAgent(llm_call=_stub, max_tokens=500)
    return agent, calls


def test_debugger_diff_mode_clean_apply():
    from document_processor.code_intelligence.agents import AgentContext

    diff = (
        "```diff\n"
        "<<<<<<< SEARCH\n"
        "x = 1\n"
        "=======\n"
        "x = 2\n"
        ">>>>>>> REPLACE\n"
        "```\n"
        '```json\n{"root_cause":"off-by-one","fix_description":"bumped",'
        '"lines_changed":1,"confidence":"high"}\n```'
    )
    agent, calls = _make_debugger_with_diff_response(diff)
    ctx = AgentContext(
        user_prompt="fix it",
        code="x = 1\n",
        execution_feedback="failed",
        language="python",
    )
    out = _run(agent.run(ctx))
    assert out.error is None
    assert out.code == "x = 2\n"
    assert out.data.get("diff_mode") is True
    assert out.data.get("diff_blocks_applied") == 1


def test_debugger_diff_mode_falls_back_when_search_drifts():
    from document_processor.code_intelligence.agents import AgentContext

    # SEARCH text doesn't appear in the actual code → diff fails →
    # fallback to whole-file mode triggers.
    diff = (
        "```diff\n"
        "<<<<<<< SEARCH\n"
        "this text isn't in the file\n"
        "=======\n"
        "irrelevant\n"
        ">>>>>>> REPLACE\n"
        "```"
    )
    agent, calls = _make_debugger_with_diff_response(diff)
    ctx = AgentContext(
        user_prompt="fix it",
        code="actual code\n",
        execution_feedback="failed",
        language="python",
    )
    out = _run(agent.run(ctx))
    # Whole-file fallback fired.
    assert out.error is None
    assert "fixed_whole_file" in (out.code or "")
    assert out.data.get("diff_mode") is False  # whole-file path


def test_debugger_settings_flag_disables_diff_mode(monkeypatch):
    from document_processor.code_intelligence.agents import (
        AgentContext, DebuggerAgent,
    )
    from document_processor.config.settings import settings

    monkeypatch.setattr(settings, "code_debug_diff_mode_enabled", False)
    calls: list[tuple[str, str]] = []

    async def _stub(prompt, system, max_tokens):
        calls.append((system or "", prompt or ""))
        return (
            "```python\nx = 2\n```\n"
            '```json\n{"root_cause":"x","fix_description":"x",'
            '"lines_changed":1,"confidence":"high"}\n```'
        )

    agent = DebuggerAgent(llm_call=_stub, max_tokens=500)
    ctx = AgentContext(
        user_prompt="fix it", code="x = 1\n",
        execution_feedback="failed", language="python",
    )
    out = _run(agent.run(ctx))
    # Whole-file mode: parser strips trailing newline, accepts both.
    assert (out.code or "").strip() == "x = 2"
    assert out.data.get("diff_mode") is False
    # Only the whole-file system prompt was sent.
    for system, _ in calls:
        assert "DIFF MODE" not in system
