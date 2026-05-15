"""v18.1 Step 4 (Cycle G) — coverage for the fully-async critic
decouple in `CodeIntelligenceEngine`.

What's being tested
-------------------
The engine no longer blocks on the critic LLM call inside
`_phase_review`.  Instead:

  1. After the parallel block (execute / analyze / test) completes,
     the orchestrator calls `_kickoff_critic_task()` which fires
     `CriticAgent.run()` as `asyncio.create_task`.
  2. `_phase_debug` runs concurrently with the in-flight critic
     call.  If debug mutates `self.code`, the staleness check in
     `_phase_review` re-launches the critic on the post-debug code.
  3. `_phase_review` enters and calls `_resolve_critic_task()` with
     a verdict-freshness timeout (default 8s).  On timeout, the
     review falls back to `approved_with_minor` score=70 — matching
     the existing critic-unavailable error path so `_score_candidate`
     stays well-defined.

Tested separately from the larger engine sweep because the async
plumbing is the kind of code that breaks invisibly on refactors.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from document_processor.code_intelligence.engine import (
    CodeIntelligenceEngine,
)


# ─── helpers ──────────────────────────────────────────────────────


async def _fake_llm_call(prompt, system, max_tokens):
    """Default LLM stub — never called in the patched-CriticAgent
    tests, but the engine constructs `CriticAgent(self.llm_call, ...)`
    so the attribute must exist on the bare engine."""
    return "fake-llm-response"


def _minimal_engine(monkeypatch=None) -> CodeIntelligenceEngine:
    """Construct an engine with no LLM call wired — we monkeypatch
    the critic agent in each test so no real LLM traffic happens."""
    eng = CodeIntelligenceEngine.__new__(CodeIntelligenceEngine)
    # Hand-populate just the attributes the methods under test touch.
    eng.prompt = "build a snake game"
    eng.plan = {"task_type": "generation"}
    eng.code = "print('hello world')\n"
    eng.detected_language = "python"
    eng.execution_results = []
    eng.test_execution_result = None
    eng.static_analysis = None
    eng.review = {}
    eng._budgets = {"review": 1024}
    eng.llm_call = _fake_llm_call            # CriticAgent ctor needs this
    eng._role_setter = lambda role: None     # no-op
    eng._on_event = AsyncMock()
    # v18.1 Step 4 new attributes
    eng._critic_task = None
    eng._critic_code_hash = None
    return eng


def _verdict(score: int = 80) -> dict:
    """Shape of a successful CriticAgent.run().data payload."""
    return {
        "verdict": "approved",
        "score": score,
        "strengths": ["clear logic"],
        "issues": [],
        "security_concerns": [],
        "performance_concerns": [],
        "final_comment": "looks good",
    }


# ─── fallback verdict shape ────────────────────────────────────────


def test_critic_fallback_verdict_has_expected_shape():
    eng = _minimal_engine()
    fb = eng._critic_fallback_verdict("test_reason")
    assert fb["verdict"] == "approved_with_minor"
    assert fb["score"] == 70
    assert "test_reason" in fb["final_comment"]
    # All score-related fields the score function reads must be present.
    for key in ("strengths", "issues", "security_concerns",
                "performance_concerns", "final_comment"):
        assert key in fb


def test_critic_fallback_verdict_truncates_long_reason():
    eng = _minimal_engine()
    fb = eng._critic_fallback_verdict("x" * 1000)
    assert len(fb["final_comment"]) <= 400


# ─── code hash ─────────────────────────────────────────────────────


def test_code_hash_stable_for_same_text():
    eng = _minimal_engine()
    eng.code = "print('a')"
    h1 = eng._code_hash()
    h2 = eng._code_hash()
    assert h1 == h2
    assert h1 is not None


def test_code_hash_changes_when_code_changes():
    eng = _minimal_engine()
    eng.code = "print('a')"
    h1 = eng._code_hash()
    eng.code = "print('b')"
    h2 = eng._code_hash()
    assert h1 != h2


def test_code_hash_returns_none_when_no_code():
    eng = _minimal_engine()
    eng.code = None
    assert eng._code_hash() is None


# ─── critic context builder ────────────────────────────────────────


def test_build_critic_context_includes_execution_feedback():
    eng = _minimal_engine()
    eng.execution_results = [{
        "exit_code": 0, "timed_out": False, "stdout": "ok",
        "stderr": "",
    }]
    ctx = eng._build_critic_context()
    assert ctx.execution_feedback is not None
    assert "exit=0" in ctx.execution_feedback


def test_build_critic_context_handles_missing_feedback_gracefully():
    """When execute/analyze/test all skipped, critic context still
    builds (feedback fields just None) — the critic agent itself
    handles None feedback."""
    eng = _minimal_engine()
    eng.execution_results = []
    eng.test_execution_result = None
    eng.static_analysis = None
    ctx = eng._build_critic_context()
    assert ctx.execution_feedback is None
    assert ctx.test_execution_feedback is None
    assert ctx.static_feedback is None
    assert ctx.code == eng.code


# ─── kickoff + resolve happy path ──────────────────────────────────


def test_kickoff_creates_task_and_records_code_hash():
    """`_kickoff_critic_task()` sets both `_critic_task` and
    `_critic_code_hash`; the hash matches the current code."""
    eng = _minimal_engine()
    expected_hash = eng._code_hash()

    # Patch CriticAgent so .run() returns a coroutine yielding a verdict.
    class FakeAgent:
        def __init__(self, *a, **k):
            pass
        async def run(self, ctx):
            return SimpleNamespace(data=_verdict(80), error=None)

    import document_processor.code_intelligence.engine as engmod
    saved = engmod.CriticAgent
    engmod.CriticAgent = FakeAgent
    try:
        asyncio.run(eng._kickoff_critic_task())
    finally:
        engmod.CriticAgent = saved

    assert eng._critic_task is not None
    assert eng._critic_code_hash == expected_hash


def test_kickoff_with_no_code_is_noop():
    eng = _minimal_engine()
    eng.code = None
    asyncio.run(eng._kickoff_critic_task())
    assert eng._critic_task is None
    assert eng._critic_code_hash is None


def test_resolve_returns_verdict_when_task_completes_in_time():
    eng = _minimal_engine()

    async def fast_critic():
        return SimpleNamespace(data=_verdict(85), error=None)

    eng._critic_task = asyncio.get_event_loop().create_task(
        fast_critic(),
    ) if False else None

    # Build the task inside the test loop.
    async def driver():
        eng._critic_task = asyncio.create_task(fast_critic())
        out = await eng._resolve_critic_task(timeout_s=2.0)
        return out

    out = asyncio.run(driver())
    assert out is not None
    assert out.data["score"] == 85


def test_resolve_returns_none_when_no_task():
    eng = _minimal_engine()
    out = asyncio.run(eng._resolve_critic_task(timeout_s=1.0))
    assert out is None


def test_resolve_returns_none_on_timeout():
    """The freshness fallback: when the critic hasn't finished
    within `timeout_s`, _resolve returns None and caller falls back
    to the approved_with_minor default."""
    eng = _minimal_engine()

    async def slow_critic():
        await asyncio.sleep(10)
        return SimpleNamespace(data=_verdict(99), error=None)

    async def driver():
        eng._critic_task = asyncio.create_task(slow_critic())
        out = await eng._resolve_critic_task(timeout_s=0.1)
        # Cancel to avoid leaking the task into the next test.
        eng._critic_task.cancel()
        try:
            await eng._critic_task
        except (asyncio.CancelledError, Exception):
            pass
        return out

    out = asyncio.run(driver())
    assert out is None


def test_resolve_handles_critic_exception():
    eng = _minimal_engine()

    async def crashing_critic():
        raise RuntimeError("network down")

    async def driver():
        eng._critic_task = asyncio.create_task(crashing_critic())
        return await eng._resolve_critic_task(timeout_s=1.0)

    out = asyncio.run(driver())
    assert out is None


# ─── staleness re-launch ───────────────────────────────────────────


def test_kickoff_cancels_previous_task_before_relaunching():
    eng = _minimal_engine()

    class FakeAgent:
        def __init__(self, *a, **k):
            pass
        async def run(self, ctx):
            await asyncio.sleep(1.0)   # never completes in the test window
            return SimpleNamespace(data=_verdict(50), error=None)

    import document_processor.code_intelligence.engine as engmod
    saved = engmod.CriticAgent
    engmod.CriticAgent = FakeAgent

    async def driver():
        await eng._kickoff_critic_task()
        first_task = eng._critic_task
        # Simulate code change between kickoffs.
        eng.code = "print('debug fixed me')\n"
        await eng._kickoff_critic_task()
        second_task = eng._critic_task
        return first_task, second_task

    try:
        t1, t2 = asyncio.run(driver())
    finally:
        engmod.CriticAgent = saved

    assert t1 is not t2
    assert t1.cancelled() or t1.done()


# ─── _phase_review integration with async-task path ────────────────


def test_phase_review_consumes_kicked_off_task():
    """When `_critic_task` was already kicked off (the v18.1 happy
    path), `_phase_review` awaits it via `_resolve_critic_task` and
    populates `self.review` without launching a new agent call."""
    eng = _minimal_engine()
    inline_calls: list[int] = []

    class FakeAgent:
        def __init__(self, *a, **k):
            pass
        async def run(self, ctx):
            # Called only when kickoff fires; NOT a second time from
            # _phase_review's inline fallback.
            inline_calls.append(1)
            return SimpleNamespace(data=_verdict(82), error=None)

    import document_processor.code_intelligence.engine as engmod
    saved = engmod.CriticAgent
    engmod.CriticAgent = FakeAgent

    async def driver():
        await eng._kickoff_critic_task()
        result = await eng._phase_review()
        return result

    try:
        out = asyncio.run(driver())
    finally:
        engmod.CriticAgent = saved

    assert out["score"] == 82
    assert eng.review["verdict"] == "approved"
    # Only the kickoff call hit the agent — no inline fallback.
    assert len(inline_calls) == 1


def test_phase_review_uses_fallback_on_timeout():
    """When the critic task misses the freshness timeout,
    `_phase_review` populates `self.review` with the
    `approved_with_minor` default."""
    eng = _minimal_engine()

    # Make the timeout extremely tight via monkeypatched settings.
    import document_processor.config.settings as settings_module
    monkeypatched_timeout = 0.05  # 50ms

    class FakeAgent:
        def __init__(self, *a, **k):
            pass
        async def run(self, ctx):
            await asyncio.sleep(5.0)
            return SimpleNamespace(data=_verdict(99), error=None)

    import document_processor.code_intelligence.engine as engmod
    saved_agent = engmod.CriticAgent
    saved_timeout = getattr(
        settings_module.settings, "code_critic_async_timeout_s", 8.0,
    )
    engmod.CriticAgent = FakeAgent
    settings_module.settings.code_critic_async_timeout_s = monkeypatched_timeout

    async def driver():
        await eng._kickoff_critic_task()
        result = await eng._phase_review()
        # Clean up the slow task so it doesn't pollute pytest's loop.
        if eng._critic_task and not eng._critic_task.done():
            eng._critic_task.cancel()
            try:
                await eng._critic_task
            except (asyncio.CancelledError, Exception):
                pass
        return result

    try:
        out = asyncio.run(driver())
    finally:
        engmod.CriticAgent = saved_agent
        settings_module.settings.code_critic_async_timeout_s = saved_timeout

    assert out["verdict"] == "approved_with_minor"
    assert out["score"] == 70
    assert "no_result" in out["final_comment"] or "stalled" in out["final_comment"].lower()


def test_phase_review_skips_when_no_code():
    """No code → review is skipped without invoking any critic."""
    eng = _minimal_engine()
    eng.code = None
    # Patch the phase recorder so _skip doesn't error.
    eng._skip = lambda phase, reason: None
    out = asyncio.run(eng._phase_review())
    assert out == {"skipped": True}


def test_phase_review_falls_back_to_inline_when_no_task_kicked_off():
    """Legacy path (no kickoff): `_phase_review` runs the critic
    agent inline.  Important for unit tests / CLI single-mode runs
    that bypass the orchestrator's kickoff step."""
    eng = _minimal_engine()
    eng._critic_task = None
    eng._critic_code_hash = None
    inline_calls: list[int] = []

    class FakeAgent:
        def __init__(self, *a, **k):
            pass
        async def run(self, ctx):
            inline_calls.append(1)
            return SimpleNamespace(data=_verdict(77), error=None)

    import document_processor.code_intelligence.engine as engmod
    saved = engmod.CriticAgent
    engmod.CriticAgent = FakeAgent
    try:
        out = asyncio.run(eng._phase_review())
    finally:
        engmod.CriticAgent = saved

    assert out["score"] == 77
    assert len(inline_calls) == 1


def test_phase_review_relaunches_critic_when_code_changed_during_debug():
    """The staleness check: if `self.code` differs from the hash
    recorded at kickoff, `_phase_review` cancels the old task +
    re-launches on the post-debug code, then awaits the new task."""
    eng = _minimal_engine()
    eng.code = "print('original')\n"
    call_log: list[str] = []

    class FakeAgent:
        def __init__(self, *a, **k):
            pass
        async def run(self, ctx):
            # Record which code version the critic actually reviewed.
            call_log.append(ctx.code)
            return SimpleNamespace(
                data=_verdict(85 if "fixed" in (ctx.code or "") else 60),
                error=None,
            )

    import document_processor.code_intelligence.engine as engmod
    saved = engmod.CriticAgent
    engmod.CriticAgent = FakeAgent

    async def driver():
        await eng._kickoff_critic_task()      # critic 1 on "original"
        eng.code = "print('debug fixed')\n"   # simulate debug mutation
        return await eng._phase_review()

    try:
        out = asyncio.run(driver())
    finally:
        engmod.CriticAgent = saved

    # The second call (on fixed code) should win.
    assert any("fixed" in c for c in call_log if c)
    assert out["score"] == 85
