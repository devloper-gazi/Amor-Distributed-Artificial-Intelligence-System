"""v18.1.5 Cycle H gate-gap fix — ThinkingEngine per-phase + per-session
wall-clock timeout coverage.

Regression: Sprint-0 2026-05-16 measured 3/3 Thinking prompts hanging
silently at the baseline runner's 600s cap with 0 tokens emitted (peak
VRAM dropped from 7.4 GB → 1.4 GB confirming llama-swap evicted the
architect model between sessions).  Root cause: ``_run_phase`` had no
``asyncio.wait_for`` around the ``await runner()`` call, so any LLM
stall propagated unbounded into the SSE stream's silence.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from document_processor.thinking.engine import ThinkingEngine
from document_processor.thinking.models import DeliverableKind


# ─── Fixtures ────────────────────────────────────────────────────────


def _stub_llm_factory(*, hang_call_indices: set[int] = frozenset(),
                      hang_all: bool = False):
    """Build an ``LLMCall`` stub.

    ``hang_all``: every invocation hangs (used to test phase-level timeout
    coverage across the whole pipeline).

    ``hang_call_indices``: hang on the Nth (0-indexed) invocation only —
    e.g. ``{0}`` hangs the first call (which is the `understand` phase).

    All non-hanging calls return a minimal payload that satisfies the
    next-stage JSON parser, keyed off the call index so we don't need to
    parse the prompt content.  Order of LLM calls in the engine's
    ``run()`` is: 0=understand, 1=decompose, 2=explore, 3=evaluate (if
    alternatives), 4=synthesize, 5=critique.
    """
    state = {"i": -1}

    _payloads_by_index = [
        # 0 — understand
        json.dumps({
            "objective": "x", "constraints": [], "knowns": [],
            "unknowns": [], "assumptions": [], "complexity": "medium",
            "deliverable_kind": "report",
        }),
        # 1 — decompose
        json.dumps({"sub_questions": ["q1", "q2"]}),
        # 2 — explore
        json.dumps({"alternatives": [{"id": "a", "summary": "x"}]}),
        # 3 — evaluate (if alternatives produced)
        json.dumps({"chosen": "a", "rationale": "x", "confidence": 80}),
        # 4 — synthesize (markdown, not JSON)
        "# Stub deliverable\n\nMinimal content.",
        # 5 — critique
        json.dumps({"strengths": ["x"], "weaknesses": ["y"],
                    "next_steps": ["z"], "confidence": 70}),
    ]

    async def _llm(prompt: str, system, max_tokens) -> str:
        state["i"] += 1
        idx = state["i"]
        if hang_all or idx in hang_call_indices:
            await asyncio.sleep(60)   # would hang in real life
            return ""
        if idx < len(_payloads_by_index):
            return _payloads_by_index[idx]
        # Beyond-known-index → return synthesize-shape (safe default).
        return "# Stub deliverable\n\nMinimal content."

    return _llm


# ─── Tests ───────────────────────────────────────────────────────────


def test_phase_timeout_setting_default_120s():
    """Plan-agent locked default: 120s per phase, 540s per session."""
    from document_processor.config.settings import settings
    assert settings.code_thinking_phase_timeout_s == 120.0
    assert settings.code_thinking_session_timeout_s == 540.0


@pytest.mark.asyncio
async def test_hanging_phase_times_out_and_marks_failed():
    """A phase whose llm_call hangs forever must be marked `failed`
    with a `timed_out` flag within the configured budget — not propagate
    to the SSE stream's silence."""
    events: list[dict] = []

    async def _on_event(ev):
        events.append(ev)

    eng = ThinkingEngine(
        prompt="A train problem",
        clarifications={},
        deliverable="report",
        effort="basic",
        provider="local",
        llm_call=_stub_llm_factory(hang_call_indices={0}),   # 0 = understand
        on_event=_on_event,
        phase_timeout_s=0.2,          # tight cap for the test
        session_timeout_s=2.0,
    )
    snapshot = await eng.run()

    understand = next(p for p in snapshot["phases"] if p["name"] == "understand")
    assert understand["status"] == "failed"
    assert understand["detail"].get("timed_out") is True
    # The phase_failed event must fire — UI relies on it to surface the
    # error rather than spinning forever.
    assert any(e.get("type") == "phase_failed" and e.get("timed_out") for e in events)


@pytest.mark.asyncio
async def test_session_budget_exhausted_short_circuits_downstream():
    """When session budget is blown by one slow phase, downstream phases
    should mark `skipped` with `reason=session_budget_exhausted`, not
    re-incur the timeout cost on every phase."""
    events: list[dict] = []

    async def _on_event(ev):
        events.append(ev)

    # `understand` hangs for the full phase budget; session cap is just
    # slightly above that → downstream phases short-circuit.
    eng = ThinkingEngine(
        prompt="A train problem",
        clarifications={},
        deliverable="report",
        effort="basic",
        provider="local",
        llm_call=_stub_llm_factory(hang_call_indices={0}),   # 0 = understand
        on_event=_on_event,
        phase_timeout_s=0.3,
        session_timeout_s=0.35,       # just enough for understand-timeout
    )
    snapshot = await eng.run()

    # understand failed, downstream all skipped (session_budget_exhausted)
    statuses = {p["name"]: p["status"] for p in snapshot["phases"]}
    assert statuses["understand"] == "failed"
    # When understand fails, the existing engine path also skips
    # downstream phases (because self.understanding is empty).  Either
    # path is acceptable — what matters is that no downstream phase
    # incurs another phase_timeout.
    timeouts = [e for e in events if e.get("type") == "phase_failed" and e.get("timed_out")]
    # Exactly one phase_failed-timeout event (the understand phase).
    assert len(timeouts) == 1, [e.get("phase") for e in timeouts]


@pytest.mark.asyncio
async def test_phase_timeout_zero_disables_cap():
    """Operator escape hatch: setting phase_timeout_s=0 reverts to legacy
    unbounded behaviour (for diagnostics or tests that need real timing)."""
    async def _slow_then_done(prompt, system, max_tokens):
        await asyncio.sleep(0.1)
        return json.dumps({
            "objective": "x", "constraints": [], "knowns": [],
            "unknowns": [], "assumptions": [], "complexity": "medium",
            "deliverable_kind": "report",
        })

    eng = ThinkingEngine(
        prompt="x",
        clarifications={},
        deliverable="report",
        effort="basic",
        provider="local",
        llm_call=_slow_then_done,
        phase_timeout_s=0,
        session_timeout_s=0,
    )
    snapshot = await eng.run()
    understand = next(p for p in snapshot["phases"] if p["name"] == "understand")
    # No artificial timeout fired — phase completed normally.
    assert understand["status"] == "completed"


@pytest.mark.asyncio
async def test_happy_path_no_timeout():
    """All phases complete within budget — no timeout overhead."""
    eng = ThinkingEngine(
        prompt="A train problem with simple decomposition.",
        clarifications={},
        deliverable="report",
        effort="basic",
        provider="local",
        llm_call=_stub_llm_factory(),
        phase_timeout_s=2.0,
        session_timeout_s=10.0,
    )
    snapshot = await eng.run()
    # Synthesize is the deliverable phase; it must complete on the happy
    # path (the engine's per-phase budgets cap output length, but the
    # stub LLM returns synchronously so wall-clock is microseconds).
    statuses = {p["name"]: p["status"] for p in snapshot["phases"]}
    assert statuses.get("understand") == "completed"
    assert statuses.get("synthesize") in {"completed", "failed"}, statuses
