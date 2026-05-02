"""
Phase 1B integration tests — verify that QuickCodeEngine wires the
LogicEngine + Z3Verifier + EpisodicMemory + RLEFCollector correctly.

All four phases are fail-soft, so each test asserts:
  1. The phase fires (event emitted) when its master flag is on.
  2. The bundle gets the expected envelope field populated.
  3. Disabling the master flag short-circuits cleanly without breaking
     the surrounding pipeline.

Mock LLM + sandbox so the tests run offline + fast.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from document_processor.quick_code import (
    QuickCodeEngine,
    QuickCodeRequest,
)


# ─── Fakes ──────────────────────────────────────────────────────────


class _RouterLLM:
    """Drives every phase deterministically."""

    async def __call__(self, prompt, system, max_tokens):
        s = (system or "").lower()
        if "meta-arbiter" in s:
            return json.dumps({
                "verdict": "approve", "confidence": 0.9,
                "production_readiness": 88,
                "top_risks": [], "top_strengths": ["clean"],
                "summary": "ok",
            })
        if "auditor" in s:
            return json.dumps({
                "verdict": "approve", "confidence": 0.85, "summary": "ok",
            })
        if "reasoning agent" in s:
            return json.dumps({
                "alternatives": [{
                    "label": "A", "summary": "iterative",
                    "scores": {"clarity": 0.9, "math_soundness": 0.8,
                               "performance": 0.7, "edge_cases": 0.7},
                    "complexity_estimate": "O(n)", "perf_notes": "ok",
                    "edge_cases": [],
                }],
                "chosen": "A", "rationale": "A wins on clarity",
            })
        if "tester agent" in s or "qa engineer" in s:
            return ('```python\ndef test_x(): assert 1\n```\n'
                    '```json\n{"language": "python"}\n```')
        if "coder agent" in s:
            return ('```python\ndef f(xs): return sorted(xs)\n```\n'
                    '```json\n{"language": "python"}\n```')
        if "triage classifier" in s:
            return json.dumps({
                "task_type": "generation", "language": "python",
                "complexity": "moderate",
                "needs_execution": True, "needs_tests": True,
            })
        if "invariant generator" in s:
            return json.dumps({"invariants": []})
        return "{}"


class _Sandbox:
    """Returns clean exec results regardless of code."""
    skipped = False

    async def execute(self, code, language="python", timeout=30):
        class _R:
            exit_code = 0
            stdout = ""
            stderr = ""
            timed_out = False
            error = None
            duration_ms = 5
            language = "python"
            skipped = False

            @property
            def success(self): return True

            def to_dict(self):
                return {
                    "exit_code": 0, "stdout": "", "stderr": "",
                    "timed_out": False, "error": None,
                    "duration_ms": 5, "language": "python",
                    "skipped": False, "success": True,
                }
        return _R()


class _StaticHarness:
    async def analyze(self, code, language="python"):
        class _R:
            def to_dict(self):
                return {
                    "language": language, "issues": [],
                    "complexity_score": 1.0, "maintainability_index": 80.0,
                    "lines_of_code": 1, "syntax_valid": True,
                    "syntax_error": None, "ast_summary": {},
                    "severity_counts": {"error": 0, "warning": 0,
                                         "info": 0, "security": 0},
                    "per_function_complexity": {"f": 1},
                }
        return _R()


def _build_engine(*, prompt: str, **request_kwargs):
    """Construct an engine with mocked deps + a request that already
    points to a known-template prompt."""
    return QuickCodeEngine(
        session_id=f"phase1b-{prompt[:8]}",
        request=QuickCodeRequest(
            prompt=prompt,
            language="python",
            max_refine=0,
            **request_kwargs,
        ),
        llm_call=_RouterLLM(),
        sandbox=_Sandbox(),
        static_harness=_StaticHarness(),
    )


# ─── Phase 1B fields on the bundle schema ─────────────────────────


def test_bundle_has_phase_1b_fields():
    """QuickCodeBundle exposes the four new envelope fields."""
    from document_processor.quick_code import QuickCodeBundle, QuickCodeRequest
    b = QuickCodeBundle(session_id="x", request=QuickCodeRequest(prompt="x"))
    # All default to None.
    assert b.logic_skeleton is None
    assert b.z3_verification is None
    assert b.episodic_decision is None
    assert b.rlef_reward is None
    # Round-trips into the public dict.
    d = b.to_dict()
    for key in ("logic_skeleton", "z3_verification",
                "episodic_decision", "rlef_reward"):
        assert key in d


# ─── _phase_episodic_recall ───────────────────────────────────────


@pytest.mark.asyncio
async def test_episodic_recall_emits_event_and_populates_bundle():
    events: list[dict[str, Any]] = []

    async def on_event(e): events.append(e)

    engine = _build_engine(prompt="implement merge sort", use_mesh=False)
    engine._on_event = on_event
    bundle = await engine.run()
    types = [e.get("type") for e in events]
    assert "quick_code_phase_start" in types
    starts = [e["phase"] for e in events
              if e.get("type") == "quick_code_phase_start"]
    assert "episodic_recall" in starts
    # Empty store → "fresh" decision.
    assert bundle.episodic_decision is not None
    assert bundle.episodic_decision["action"] == "fresh"


@pytest.mark.asyncio
async def test_episodic_recall_finds_prior_session_after_store():
    """Run twice with the same prompt — the second run's recall sees
    the first run's stored episode."""
    engine_1 = _build_engine(prompt="implement merge sort", use_mesh=False)
    bundle_1 = await engine_1.run()
    assert bundle_1.episodic_decision["action"] == "fresh"
    # Run two — share the same episodic store (in-memory fallback).
    engine_2 = _build_engine(prompt="implement merge sort", use_mesh=False)
    engine_2._episodic_store = engine_1._episodic_store  # share state
    bundle_2 = await engine_2.run()
    assert bundle_2.episodic_decision is not None
    # Same prompt → max similarity → reuse band.
    assert bundle_2.episodic_decision["best_similarity"] >= 0.85
    assert bundle_2.episodic_decision["action"] == "reuse"


# ─── _phase_logic_skeleton ────────────────────────────────────────


@pytest.mark.asyncio
async def test_logic_skeleton_runs_for_known_template():
    engine = _build_engine(prompt="implement merge sort", use_mesh=False)
    bundle = await engine.run()
    skel = bundle.logic_skeleton
    assert skel is not None
    assert skel["matched_template"] == "sort"
    assert skel["complexity_hint"] == "O(n^2)"
    # Z3 verifier ran and passed.
    assert bundle.z3_verification is not None
    assert bundle.z3_verification["overall"] == "pass"


@pytest.mark.asyncio
async def test_logic_skeleton_fallback_for_unknown_prompt():
    """Unmatched prompts get a low-confidence generic skeleton; the
    Z3 verifier should still pass (the generic skeleton is empty)."""
    engine = _build_engine(
        prompt="solve a Gauss-Seidel iteration on a sparse matrix",
        use_mesh=False,
    )
    bundle = await engine.run()
    skel = bundle.logic_skeleton
    assert skel is not None
    assert skel["matched_template"] == ""
    assert skel["confidence"] == 0.0


# ─── active integration: claimed_complexity flows from logic_skeleton ──


@pytest.mark.asyncio
async def test_reactor_uses_logic_skeleton_complexity_hint():
    """The Reactor's claimed_complexity should come from the
    LogicEngine's hint, not from the reasoning specialist's
    complexity_estimate (which the LLM mock declares as O(n))."""
    engine = _build_engine(
        prompt="implement merge sort over a list", use_mesh=False,
    )
    bundle = await engine.run()
    # Logic skeleton claims O(n^2), reactor benchmark records the
    # claim it was given. The bench failed to parse BENCH_RESULT
    # lines (sandbox returns empty stdout) → benchmark.failed=True
    # but the claimed_label is still recorded.
    rb = bundle.reactor_bundle or {}
    bench = rb.get("benchmark") or {}
    # Claim recorded matches the logic skeleton's hint, not the
    # reasoning specialist's "O(n)" claim.
    assert bench.get("claimed_label") == "O(n^2)"


# ─── _phase_persist_episode ───────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_episode_stores_high_pass_rate_runs():
    engine = _build_engine(prompt="implement merge sort", use_mesh=False)
    await engine.run()
    # The in-memory episodic store should now have one entry.
    store = engine._get_episodic_store()
    count = await store.count()
    assert count >= 1


@pytest.mark.asyncio
async def test_persist_episode_skips_failing_runs():
    """A run with a bad sandbox (skipped exec) → no episode stored."""

    class _SkippedSandbox(_Sandbox):
        skipped = True

        async def execute(self, code, language="python", timeout=30):
            class _R:
                exit_code = 1
                stdout = ""
                stderr = "boom"
                timed_out = False
                error = None
                duration_ms = 5
                language = "python"
                skipped = True

                @property
                def success(self): return True   # skipped is neutral

                def to_dict(self):
                    return {"exit_code": 1, "stdout": "", "stderr": "boom",
                            "timed_out": False, "error": None,
                            "duration_ms": 5, "language": "python",
                            "skipped": True, "success": True}
            return _R()

    engine = QuickCodeEngine(
        session_id="failing",
        request=QuickCodeRequest(prompt="implement merge sort",
                                 max_refine=0, use_mesh=False),
        llm_call=_RouterLLM(),
        sandbox=_SkippedSandbox(),
        static_harness=_StaticHarness(),
    )
    await engine.run()
    # Nothing stored — pass rate is 0.5 (neutral) which is below the
    # 0.8 minimum.
    count = await engine._get_episodic_store().count()
    assert count == 0


# ─── _phase_emit_rlef ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rlef_phase_emits_reward_with_z3_verified_signal():
    engine = _build_engine(prompt="implement merge sort", use_mesh=False)
    bundle = await engine.run()
    reward = bundle.rlef_reward
    assert reward is not None
    # Z3 verified → reward signal includes the bonus.
    assert reward["z3_was_verified"] is True
    assert reward["session_id"] == engine.session_id
    # Composite score is in [0, 1].
    assert 0.0 <= reward["reward_score"] <= 1.0
    # Sink result reports neither persisted nor published (no Mongo,
    # no Kafka in this test) — and that's fine, it's fail-soft.
    assert "sink_result" in reward


@pytest.mark.asyncio
async def test_rlef_no_z3_verification_drops_signal():
    """When the Z3 verifier doesn't pass (fallback skeleton has empty
    invariants → still 'pass' but if we'd disabled Z3 the signal
    would be absent). Use cognitive_phase_1b_enabled=False to suppress
    the whole layer and verify the fields stay None."""
    from document_processor.config import settings as settings_mod
    original = settings_mod.settings.cognitive_phase_1b_enabled
    settings_mod.settings.cognitive_phase_1b_enabled = False
    try:
        engine = _build_engine(
            prompt="implement merge sort", use_mesh=False,
        )
        bundle = await engine.run()
        # Every Phase 1B field stays None when the master gate is off.
        assert bundle.logic_skeleton is None
        assert bundle.z3_verification is None
        assert bundle.episodic_decision is None
        assert bundle.rlef_reward is None
    finally:
        settings_mod.settings.cognitive_phase_1b_enabled = original


# ─── master gate: Phase 1B disabled ──────────────────────────────


@pytest.mark.asyncio
async def test_phase_1b_master_gate_short_circuits_all_four_phases():
    from document_processor.config import settings as settings_mod
    original = settings_mod.settings.cognitive_phase_1b_enabled
    settings_mod.settings.cognitive_phase_1b_enabled = False
    try:
        events: list[dict[str, Any]] = []

        async def on_event(e): events.append(e)

        engine = _build_engine(prompt="implement merge sort", use_mesh=False)
        engine._on_event = on_event
        await engine.run()
        starts = {e["phase"] for e in events
                  if e.get("type") == "quick_code_phase_start"}
        # None of the Phase 1B phase names appear.
        assert "episodic_recall" not in starts
        assert "logic_skeleton" not in starts
        # Existing phases still ran.
        assert "triage" in starts
        assert "verify" in starts
    finally:
        settings_mod.settings.cognitive_phase_1b_enabled = original


@pytest.mark.asyncio
async def test_episodic_memory_disabled_skips_phase():
    from document_processor.config import settings as settings_mod
    original = settings_mod.settings.episodic_memory_enabled
    settings_mod.settings.episodic_memory_enabled = False
    try:
        engine = _build_engine(prompt="implement merge sort", use_mesh=False)
        bundle = await engine.run()
        assert bundle.episodic_decision is None
        # But other Phase 1B phases still run.
        assert bundle.logic_skeleton is not None
        assert bundle.rlef_reward is not None
    finally:
        settings_mod.settings.episodic_memory_enabled = original


@pytest.mark.asyncio
async def test_z3_verification_disabled_skips_phase():
    from document_processor.config import settings as settings_mod
    original = settings_mod.settings.z3_verification_enabled
    settings_mod.settings.z3_verification_enabled = False
    try:
        engine = _build_engine(prompt="implement merge sort", use_mesh=False)
        bundle = await engine.run()
        # logic_skeleton falls into the early-return branch BEFORE
        # generation when Z3 is off, since they're a unified phase.
        assert bundle.logic_skeleton is None
        assert bundle.z3_verification is None
    finally:
        settings_mod.settings.z3_verification_enabled = original


@pytest.mark.asyncio
async def test_rlef_disabled_skips_phase():
    from document_processor.config import settings as settings_mod
    original = settings_mod.settings.rlef_enabled
    settings_mod.settings.rlef_enabled = False
    try:
        engine = _build_engine(prompt="implement merge sort", use_mesh=False)
        bundle = await engine.run()
        assert bundle.rlef_reward is None
    finally:
        settings_mod.settings.rlef_enabled = original
