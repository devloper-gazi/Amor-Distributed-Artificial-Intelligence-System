"""Cycle I.1 — LFM2 cortex role + ModelSpec routing coverage.

The new `cortex` role in ROLE_STRENGTH_MAP and the LFM2-2.6B
ModelSpec entry must compose so that:
  * select_models_for_session(roles=["cortex"], ...) prefers the
    LFM2 entry over Qwen-Coder when LFM2 is installed
  * absence of LFM2 falls back gracefully (no exception)
  * the existing planner / coder / critic routes aren't perturbed
"""

from __future__ import annotations

import pytest


# ─── ModelSpec entry ────────────────────────────────────────────────


def test_lfm2_modelspec_in_catalogue():
    """The catalogue must include the LFM2 cortex entry so the
    auto-router can pick it up."""
    from document_processor.code_intelligence.model_registry import CODE_MODEL_CATALOGUE
    lfm2 = next((m for m in CODE_MODEL_CATALOGUE if "lfm2" in m.ollama_tag.lower()), None)
    assert lfm2 is not None, "LFM2 entry missing from CODE_MODEL_CATALOGUE"
    assert lfm2.params_b == 2.6
    assert lfm2.vram_gb <= 2          # CPU/light-GPU friendly
    assert lfm2.tier == "lightweight"
    # Strengths must include the keys the `cortex` role weights.
    assert "long context" in lfm2.strengths
    assert "associative" in lfm2.strengths
    assert "fast prefill" in lfm2.strengths


# ─── cortex role wiring ─────────────────────────────────────────────


def test_cortex_role_in_strength_map():
    """The ROLE_STRENGTH_MAP must declare a `cortex` role so
    select_models_for_session can score it."""
    from document_processor.code_intelligence.model_registry import ROLE_STRENGTH_MAP
    assert "cortex" in ROLE_STRENGTH_MAP
    assert "long context" in ROLE_STRENGTH_MAP["cortex"]
    assert "associative" in ROLE_STRENGTH_MAP["cortex"]


def test_bitnet_shadow_planner_role_in_strength_map():
    """Cycle H.1 — `bitnet_shadow_planner` role exists for the
    auto-router to surface BitNet without colliding with the regular
    planner."""
    from document_processor.code_intelligence.model_registry import ROLE_STRENGTH_MAP
    assert "bitnet_shadow_planner" in ROLE_STRENGTH_MAP
    assert "fast inference" in ROLE_STRENGTH_MAP["bitnet_shadow_planner"]


# ─── Settings flag ──────────────────────────────────────────────────


def test_lfm2_cortex_default_off():
    """Plan-agent locked: 2-week SWE-bench-Lite shadow before
    promote.  Default is OFF so flipping the bit can't accidentally
    activate without a real gate measurement."""
    from document_processor.config.settings import settings
    assert settings.code_lfm2_cortex_enabled is False
    assert settings.code_cortex_threshold_tokens == 16384


# ─── Pre-existing roles untouched ───────────────────────────────────


def test_existing_planner_role_unchanged():
    """Cycle I.1 must not perturb the existing roles' strength lists
    — Plan-agent risk #cn (subtle role-weight drift breaks Sprint-0
    median latency)."""
    from document_processor.code_intelligence.model_registry import ROLE_STRENGTH_MAP
    planner = ROLE_STRENGTH_MAP["planner"]
    # The locked planner strengths from earlier sprints.
    for s in ["planning", "reasoning", "step-by-step"]:
        assert s in planner, f"missing planner strength: {s}"


def test_existing_coder_role_unchanged():
    from document_processor.code_intelligence.model_registry import ROLE_STRENGTH_MAP
    coder = ROLE_STRENGTH_MAP["coder"]
    for s in ["code generation", "python", "fast inference"]:
        assert s in coder, f"missing coder strength: {s}"


# ─── Engine-level cortex routing ────────────────────────────────────


import asyncio
from typing import Optional


def _build_engine(prompt: str, code_context: Optional[str] = None):
    """Construct a minimal engine instance to exercise _phase_plan
    routing logic.  Stub the planner LLM so we don't hit Ollama."""
    from document_processor.code_intelligence.engine import (
        CodeIntelligenceEngine,
    )

    async def _stub_llm(prompt, system, max_tokens):
        # Plan-shaped JSON the planner agent will normalize.
        return (
            '{"language":"python","title":"x",'
            '"summary":"x","steps":["a","b"],"spec":{"dependencies":[]}}'
        )

    role_box: dict = {}

    def _role_setter(role: str):
        role_box["role"] = role

    eng = CodeIntelligenceEngine(
        prompt=prompt,
        code_context=code_context,
        language="python",
        effort="medium",
        provider="local",
        llm_call=_stub_llm,
        sandbox=None,
        static_harness=None,
        enable_execution=False,
        enable_static_analysis=False,
        enable_testing=False,
        max_debug_iterations=0,
    )
    eng._role_setter = _role_setter
    eng.triage = {"domain": None}
    return eng, role_box


def test_cortex_routing_off_by_default_chooses_architect():
    """Plan-agent locked: cortex must be opt-in (default OFF)."""
    from document_processor.config.settings import settings
    settings.code_lfm2_cortex_enabled = False        # explicit
    eng, box = _build_engine(
        prompt="short prompt",
        code_context="def foo(): pass",
    )
    asyncio.run(eng._phase_plan())
    assert box["role"] == "architect"


def test_cortex_routing_engages_when_threshold_exceeded(monkeypatch):
    """With cortex enabled + input > threshold, the engine routes to
    'cortex' role (LFM2 via ROLE_STRENGTH_MAP)."""
    from document_processor.config.settings import settings
    monkeypatch.setattr(settings, "code_lfm2_cortex_enabled", True)
    monkeypatch.setattr(settings, "code_cortex_threshold_tokens", 100)

    # ~600 chars → ~150 tokens (above the 100 threshold).
    long_prompt = "Summarize this section.\n" + ("a" * 600)
    eng, box = _build_engine(prompt=long_prompt)
    asyncio.run(eng._phase_plan())
    assert box["role"] == "cortex"


def test_cortex_routing_stays_architect_below_threshold(monkeypatch):
    """Below threshold even with cortex enabled → architect path."""
    from document_processor.config.settings import settings
    monkeypatch.setattr(settings, "code_lfm2_cortex_enabled", True)
    monkeypatch.setattr(settings, "code_cortex_threshold_tokens", 1_000_000)

    eng, box = _build_engine(prompt="short")
    asyncio.run(eng._phase_plan())
    assert box["role"] == "architect"


def test_cortex_routing_failsafe_when_settings_explode(monkeypatch):
    """If the settings lookup raises, the engine MUST fall back to
    architect — Plan-agent locked: shadow features never fail closed."""
    from document_processor.config import settings as _settings_mod

    class _Boomy:
        """Raises only on the cortex flag access — leaves other
        attribute lookups (Python protocol) untouched."""
        def __getattr__(self, name):
            if name in {"code_lfm2_cortex_enabled", "code_cortex_threshold_tokens"}:
                raise RuntimeError("simulated settings explosion")
            return None

    monkeypatch.setattr(_settings_mod, "settings", _Boomy())
    eng, box = _build_engine(prompt="short")
    asyncio.run(eng._phase_plan())
    assert box["role"] == "architect"


def test_cortex_routing_emits_event_when_engaged(monkeypatch):
    """The cortex_routing_engaged event must surface to the SSE
    stream so the UI can show a routing banner."""
    from document_processor.config.settings import settings
    monkeypatch.setattr(settings, "code_lfm2_cortex_enabled", True)
    monkeypatch.setattr(settings, "code_cortex_threshold_tokens", 50)

    events: list = []

    async def _on_event(ev):
        events.append(ev)

    long_prompt = "x" * 1000
    eng, _box = _build_engine(prompt=long_prompt)
    eng._on_event = _on_event
    asyncio.run(eng._phase_plan())

    cortex_events = [e for e in events if e.get("type") == "cortex_routing_engaged"]
    assert len(cortex_events) == 1
    assert cortex_events[0]["threshold_tokens"] == 50
    assert cortex_events[0]["estimated_input_tokens"] > 50
