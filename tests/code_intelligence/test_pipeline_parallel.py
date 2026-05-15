"""Cycle F Sprint 6 — tests for the async pipeline parallelism.

Coverage:
  * `code_pipeline_parallel` setting toggle path
  * Test phase added to the gather group when parallel mode is on
  * `_warmup_critic_prefix` is a no-op without code
  * `_warmup_critic_prefix` calls the LLM with critic role
  * Warmup task cancelled if not done after review

We don't spin up the full engine — instead we exercise the helpers
in isolation and mock the LLM call.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


# ─── _warmup_critic_prefix ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_warmup_no_op_without_code():
    """If self.code is None, warmup is a no-op (returns immediately)."""

    from document_processor.code_intelligence.engine import CodeIntelligenceEngine

    eng = CodeIntelligenceEngine.__new__(CodeIntelligenceEngine)
    eng.code = None
    eng._role_setter = lambda role: None
    eng.llm_call = MagicMock()
    eng.prompt = ""
    eng.plan = {}

    await eng._warmup_critic_prefix()

    # Should not have called the LLM since code wasn't ready.
    eng.llm_call.assert_not_called()


@pytest.mark.asyncio
async def test_warmup_calls_llm_with_critic_role():
    """When code is ready, warmup sets the critic role + fires a
    short LLM call."""

    from document_processor.code_intelligence.engine import CodeIntelligenceEngine

    role_set: list[str] = []
    llm_calls: list[tuple] = []

    async def fake_llm(prompt, system, max_tokens):
        llm_calls.append((prompt, system, max_tokens))
        return ""

    eng = CodeIntelligenceEngine.__new__(CodeIntelligenceEngine)
    eng.code = "def f(): return 1"
    eng._role_setter = lambda role: role_set.append(role)
    eng.llm_call = fake_llm
    eng.prompt = "build a thing"
    eng.plan = {"language": "python"}

    await eng._warmup_critic_prefix()

    assert "critic" in role_set
    assert len(llm_calls) == 1
    # max_tokens should be the small warmup value.
    assert llm_calls[0][2] == 2


@pytest.mark.asyncio
async def test_warmup_swallows_llm_exception():
    """Best-effort: a failing LLM call must not raise to the caller."""

    from document_processor.code_intelligence.engine import CodeIntelligenceEngine

    async def crashing_llm(prompt, system, max_tokens):
        raise ConnectionError("simulated llama-swap unreachable")

    eng = CodeIntelligenceEngine.__new__(CodeIntelligenceEngine)
    eng.code = "def f(): return 1"
    eng._role_setter = lambda role: None
    eng.llm_call = crashing_llm
    eng.prompt = "x"
    eng.plan = {}

    # Should NOT raise.
    await eng._warmup_critic_prefix()


# ─── Settings flags ─────────────────────────────────────────────────


def test_pipeline_parallel_default_is_true():
    from document_processor.config.settings import settings
    assert settings.code_pipeline_parallel is True


def test_critic_warmup_default_is_true():
    from document_processor.config.settings import settings
    assert settings.code_critic_prefix_warmup is True


# ─── Integration shape — gather with test in parallel ───────────────


@pytest.mark.asyncio
async def test_parallel_branch_runs_three_phases_concurrently():
    """Synthetic stress: three async tasks complete near-simultaneously
    when gathered together vs sequentially.  Validates the gather
    pattern, not the engine specifically."""

    import time

    async def fake_phase(name, duration_s):
        await asyncio.sleep(duration_s)
        return name

    # Sequential baseline: 0.05 + 0.05 + 0.05 = ~0.15s
    start = time.monotonic()
    await fake_phase("execute", 0.05)
    await fake_phase("analyze", 0.05)
    await fake_phase("test", 0.05)
    sequential = time.monotonic() - start

    # Parallel: max(0.05, 0.05, 0.05) ≈ 0.05s
    start = time.monotonic()
    await asyncio.gather(
        fake_phase("execute", 0.05),
        fake_phase("analyze", 0.05),
        fake_phase("test", 0.05),
        return_exceptions=True,
    )
    parallel = time.monotonic() - start

    # Parallel should be at least 2× faster than sequential.
    assert parallel < sequential / 2.0


@pytest.mark.asyncio
async def test_gather_return_exceptions_isolates_failures():
    """One phase raising shouldn't kill the others — matches the
    `return_exceptions=True` semantics the engine relies on."""

    async def good():
        return "ok"

    async def bad():
        raise RuntimeError("simulated phase failure")

    results = await asyncio.gather(good(), bad(), return_exceptions=True)
    assert results[0] == "ok"
    assert isinstance(results[1], RuntimeError)


# ─── Warmup task lifecycle ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_warmup_task_cancellable_when_still_running():
    """The engine cancels the warmup task if review finishes
    before it does.  Verifies the asyncio.Task cancellation
    semantics the engine relies on."""

    started = asyncio.Event()
    completed = asyncio.Event()

    async def long_warmup():
        started.set()
        try:
            await asyncio.sleep(10)
            completed.set()
        except asyncio.CancelledError:
            raise

    task = asyncio.create_task(long_warmup())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not completed.is_set()
