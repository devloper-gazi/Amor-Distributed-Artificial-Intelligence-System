"""Tests for v17 PR #1 — architect + editor roles routing.

The user's acceptance criteria (paraphrased from Turkish):
    (a) ``architect`` role routes to ``deepseek-r1:7b`` when that
        tag is installed.
    (b) ``editor`` role routes to ``qwen2.5-coder:7b`` (or 14b when
        installed).
    (c) Two distinct models stay loaded across one Code Intelligence
        session (architect + editor → different tags).
    (d) Routing tests cover the new roles end-to-end.
    (e) Existing planner/coder callers still work (back-compat).
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence.model_registry import (
    CODE_MODEL_CATALOGUE,
    ROLE_STRENGTH_MAP,
    CodeModelRegistry,
)


# ─── ROLE_STRENGTH_MAP shape ──────────────────────────────────────


def test_architect_and_editor_keys_present():
    """The two new roles must exist alongside the legacy ones."""
    for role in ("architect", "editor"):
        assert role in ROLE_STRENGTH_MAP, f"missing role: {role!r}"
    # Back-compat: planner/coder must still be present so older
    # callers (quick_code, sentinel) and existing tests don't break.
    for legacy in ("planner", "coder", "tester", "debugger", "critic", "triage"):
        assert legacy in ROLE_STRENGTH_MAP, f"legacy role removed: {legacy!r}"


def test_architect_strengths_lean_reasoning_plus_code():
    arch = set(ROLE_STRENGTH_MAP["architect"])
    # Reasoning-heavy core (DeepSeek-R1's strengths).
    assert "reasoning" in arch
    assert "step-by-step" in arch
    # Should still touch code / review so DeepSeek-R1's code-gen
    # strength contributes to the score.
    assert "code generation" in arch
    assert "review" in arch


def test_editor_strengths_lean_code_specialist():
    ed = set(ROLE_STRENGTH_MAP["editor"])
    assert "code generation" in ed
    assert "python" in ed
    assert "multi-file editing" in ed
    # Editor is code-only — no "reasoning" or "planning" so the
    # qwen2.5-coder family wins outright over qwen2.5:7b.
    assert "reasoning" not in ed
    assert "planning" not in ed


# ─── select_model — installed-pool routing ────────────────────────


@pytest.fixture
def two_model_registry() -> CodeModelRegistry:
    """qwen2.5:7b + qwen2.5-coder:7b — what the user's box runs
    today before deepseek-r1 finishes pulling."""
    reg = CodeModelRegistry("http://localhost:11434")
    reg._available = ["qwen2.5:7b", "qwen2.5-coder:7b"]
    reg._probed = True
    return reg


@pytest.fixture
def three_model_registry() -> CodeModelRegistry:
    """qwen2.5:7b + qwen2.5-coder:7b + deepseek-r1:7b — once the
    user's pull finishes, this is the steady-state fleet."""
    reg = CodeModelRegistry("http://localhost:11434")
    reg._available = [
        "qwen2.5:7b", "qwen2.5-coder:7b", "deepseek-r1:7b",
    ]
    reg._probed = True
    return reg


def test_architect_picks_deepseek_r1_when_installed(
    three_model_registry: CodeModelRegistry,
):
    """(Acceptance a) — architect → deepseek-r1:7b when r1 is
    installed.  The +50 already-installed bonus + DeepSeek's
    swebench=49.2 push it above qwen2.5:7b's higher strength
    match-count."""
    spec, installed = three_model_registry.select_model(
        "architect", effort="medium",
    )
    assert installed is True
    assert spec.ollama_tag == "deepseek-r1:7b"


def test_architect_falls_back_to_qwen25_when_r1_missing(
    two_model_registry: CodeModelRegistry,
):
    """When deepseek-r1 isn't pulled yet, architect must still
    return SOME installed candidate (the planner/reasoning fallback,
    qwen2.5:7b) rather than recommend a download."""
    spec, installed = two_model_registry.select_model(
        "architect", effort="medium", installed_only=True,
    )
    assert installed is True
    # qwen2.5:7b is the closest installed match for reasoning.
    assert spec.ollama_tag == "qwen2.5:7b"


def test_editor_picks_qwen_coder_when_installed(
    two_model_registry: CodeModelRegistry,
):
    """(Acceptance b) — editor → qwen2.5-coder:7b on a 2-model rig."""
    spec, installed = two_model_registry.select_model(
        "editor", effort="medium",
    )
    assert installed is True
    assert spec.ollama_tag == "qwen2.5-coder:7b"


def test_editor_prefers_qwen_coder_14b_when_installed():
    """When the 14B coder variant is also pulled, editor should
    upgrade — its multi-file editing strength wins over the 7B."""
    reg = CodeModelRegistry("http://localhost:11434")
    reg._available = [
        "qwen2.5:7b", "qwen2.5-coder:7b", "qwen2.5-coder:14b",
    ]
    reg._probed = True
    spec, installed = reg.select_model("editor", effort="medium")
    assert installed is True
    assert spec.ollama_tag == "qwen2.5-coder:14b"


def test_editor_does_not_pick_general_model(
    two_model_registry: CodeModelRegistry,
):
    """The whole point of ``editor`` — the code-specialist tag must
    win, never the general-purpose qwen2.5:7b."""
    spec, _ = two_model_registry.select_model(
        "editor", effort="medium",
    )
    assert spec.ollama_tag != "qwen2.5:7b"


# ─── select_models_for_session — the live-stack diversity case ────


def test_session_architect_and_editor_pick_distinct_models_3_pool(
    three_model_registry: CodeModelRegistry,
):
    """(Acceptance c) — when both r1 and qwen2.5-coder are
    installed, a session firing both architect + editor lands on
    TWO distinct tags so ``ollama ps`` shows both loaded."""
    chosen = three_model_registry.select_models_for_session(
        ["architect", "editor", "tester", "debugger", "critic"],
        effort="medium",
    )
    arch_tag = chosen["architect"].ollama_tag
    ed_tag = chosen["editor"].ollama_tag
    assert arch_tag == "deepseek-r1:7b"
    assert ed_tag == "qwen2.5-coder:7b"
    assert arch_tag != ed_tag


def test_session_architect_and_editor_split_2_pool(
    two_model_registry: CodeModelRegistry,
):
    """Even without r1 installed, architect + editor on a 2-model
    rig still split: architect on qwen2.5:7b (reasoning), editor
    on qwen2.5-coder:7b (code)."""
    chosen = two_model_registry.select_models_for_session(
        ["architect", "editor", "tester", "debugger", "critic"],
        effort="medium",
    )
    assert chosen["architect"].ollama_tag == "qwen2.5:7b"
    assert chosen["editor"].ollama_tag == "qwen2.5-coder:7b"
    # Two distinct tags across the 5 roles.
    distinct = {spec.ollama_tag for spec in chosen.values()}
    assert len(distinct) == 2


def test_session_priority_orders_architect_before_planner():
    """``select_models_for_session`` must process architect BEFORE
    planner so a session that includes both (test edge case) gives
    architect first dibs on the strongest reasoning model."""
    reg = CodeModelRegistry("http://localhost:11434")
    reg._available = [
        "qwen2.5:7b", "qwen2.5-coder:7b", "deepseek-r1:7b",
    ]
    reg._probed = True
    chosen = reg.select_models_for_session(
        # Both new + legacy reasoning roles in the same session.
        ["architect", "planner", "editor", "coder"],
        effort="medium",
    )
    # Architect grabs r1 first; planner gets the next-best
    # reasoning fallback (qwen2.5:7b) when DEGRADATION_CAP allows.
    assert chosen["architect"].ollama_tag == "deepseek-r1:7b"
    assert chosen["editor"].ollama_tag == "qwen2.5-coder:7b"


# ─── back-compat — legacy planner/coder paths ────────────────────


def test_legacy_planner_role_still_routes(
    two_model_registry: CodeModelRegistry,
):
    """quick_code, sentinel and existing tests still call the
    registry with ``planner`` — that path must keep working."""
    spec, installed = two_model_registry.select_model(
        "planner", effort="medium",
    )
    assert installed is True
    assert spec.ollama_tag == "qwen2.5:7b"


def test_legacy_coder_role_still_routes(
    two_model_registry: CodeModelRegistry,
):
    """Same for ``coder`` — back-compat aliases of editor."""
    spec, installed = two_model_registry.select_model(
        "coder", effort="medium",
    )
    assert installed is True
    assert spec.ollama_tag == "qwen2.5-coder:7b"
