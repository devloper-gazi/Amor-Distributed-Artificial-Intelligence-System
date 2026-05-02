"""Tests for the Phase 16.5 role-diversity upgrade to
``CodeModelRegistry``.

Covers
* The catalogue gained the brief-recommended models
  (deepseek-r1:7b, qwen3:8b, qwen3:4b, qwen2.5-coder:14b,
  josiefied-qwen3:8b).
* ``ROLE_STRENGTH_MAP`` is tuned so a 2-model rig (qwen2.5:7b +
  qwen2.5-coder:7b) splits planner/debugger/critic onto qwen2.5:7b
  and coder/tester onto qwen2.5-coder:7b — *real* role diversity.
* ``select_model`` honours ``installed_only`` and ``exclude_tags``.
* ``select_models_for_session`` spreads roles across multiple
  installed models when possible without forcing a degraded pick.
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence.model_registry import (
    CODE_MODEL_CATALOGUE,
    ROLE_STRENGTH_MAP,
    CodeModelRegistry,
)


# ─── Catalogue contents ────────────────────────────────────────────


def test_catalogue_includes_phase_16_models():
    tags = {spec.ollama_tag for spec in CODE_MODEL_CATALOGUE}
    expected = {
        "deepseek-r1:7b",
        "qwen3:8b",
        "qwen3:4b",
        "qwen2.5-coder:14b",
        "josiefied-qwen3:8b",
    }
    missing = expected - tags
    assert not missing, f"missing brief-recommended models: {missing}"


def test_qwen25_general_strengths_include_reasoning():
    spec = next(
        s for s in CODE_MODEL_CATALOGUE if s.ollama_tag == "qwen2.5:7b"
    )
    for needed in ("reasoning", "debugging", "review", "planning"):
        assert needed in spec.strengths, f"missing strength: {needed!r}"


# ─── Role strength tuning ──────────────────────────────────────────


def test_role_strength_map_covers_all_roles():
    expected = {"planner", "coder", "tester", "debugger", "critic", "triage"}
    assert expected.issubset(ROLE_STRENGTH_MAP.keys())


def test_role_strengths_distinct_for_planner_vs_coder():
    """The whole point of Phase 16.5 — planner and coder pick
    GENUINELY different strength sets so a 2-model rig assigns
    different models."""
    pl = set(ROLE_STRENGTH_MAP["planner"])
    co = set(ROLE_STRENGTH_MAP["coder"])
    # Planner is reasoning-heavy.
    assert "reasoning" in pl
    assert "planning" in pl
    # Coder is code-gen-heavy.
    assert "code generation" in co
    assert "python" in co
    # The two role profiles must overlap on no more than two
    # strengths (small overlap is OK — both want "multi-file
    # editing" — but the bulk should be disjoint).
    assert len(pl & co) <= 2


# ─── select_model with a 2-model installed pool ────────────────────


@pytest.fixture
def two_model_registry() -> CodeModelRegistry:
    """Registry that pretends qwen2.5:7b + qwen2.5-coder:7b are
    installed (matching what amor-ollama actually has today)."""
    reg = CodeModelRegistry("http://localhost:11434")
    reg._available = ["qwen2.5:7b", "qwen2.5-coder:7b"]
    reg._probed = True
    return reg


def test_select_model_planner_picks_qwen25_general(
    two_model_registry: CodeModelRegistry,
):
    spec, installed = two_model_registry.select_model(
        "planner", effort="medium",
    )
    assert installed is True
    assert spec.ollama_tag == "qwen2.5:7b"


def test_select_model_coder_picks_qwen25_coder(
    two_model_registry: CodeModelRegistry,
):
    spec, installed = two_model_registry.select_model(
        "coder", effort="medium",
    )
    assert installed is True
    assert spec.ollama_tag == "qwen2.5-coder:7b"


def test_select_model_debugger_picks_qwen25_general(
    two_model_registry: CodeModelRegistry,
):
    """The user's complaint: 'debugger dahi aynı LLM olursa nasıl
    bir verim alabiliriz ki?' — verifies debugger now picks the
    general model (different from the coder pick) on a 2-model rig."""
    spec, installed = two_model_registry.select_model(
        "debugger", effort="medium",
    )
    assert installed is True
    # Debugger's reasoning-heavy strengths should land on qwen2.5:7b,
    # NOT on qwen2.5-coder:7b.
    assert spec.ollama_tag == "qwen2.5:7b"


def test_select_model_critic_picks_qwen25_general(
    two_model_registry: CodeModelRegistry,
):
    spec, installed = two_model_registry.select_model(
        "critic", effort="medium",
    )
    assert installed is True
    assert spec.ollama_tag == "qwen2.5:7b"


def test_select_model_tester_picks_qwen25_coder(
    two_model_registry: CodeModelRegistry,
):
    spec, installed = two_model_registry.select_model(
        "tester", effort="medium",
    )
    assert installed is True
    assert spec.ollama_tag == "qwen2.5-coder:7b"


# ─── installed_only + exclude_tags ────────────────────────────────


def test_select_model_installed_only_skips_uninstalled_flagship(
    two_model_registry: CodeModelRegistry,
):
    spec, installed = two_model_registry.select_model(
        "coder", effort="ultra", installed_only=True,
    )
    # Even at "ultra" tier the installed-only flag prevents the
    # 32B flagship pick.
    assert installed is True
    assert spec.ollama_tag in {"qwen2.5:7b", "qwen2.5-coder:7b"}


def test_select_model_exclude_tags_redirects_choice(
    two_model_registry: CodeModelRegistry,
):
    spec, _ = two_model_registry.select_model(
        "coder", effort="medium",
        exclude_tags=("qwen2.5-coder:7b",),
    )
    assert spec.ollama_tag != "qwen2.5-coder:7b"


def test_select_model_falls_back_when_nothing_installed():
    """Empty registry → returns best uninstalled candidate so the
    pull pipeline knows what to fetch."""
    reg = CodeModelRegistry("http://localhost:11434")
    reg._available = []
    reg._probed = True
    spec, installed = reg.select_model("coder", effort="medium")
    assert installed is False
    assert spec.ollama_tag  # something was chosen


# ─── select_models_for_session — the real diversity smoke test ────


def test_session_selector_two_model_split(
    two_model_registry: CodeModelRegistry,
):
    """The headline test: with planner / coder / tester / debugger /
    critic and only two installed models, the selector should
    distribute roles across BOTH installed models — not all five
    on the same one."""
    chosen = two_model_registry.select_models_for_session(
        ["planner", "coder", "tester", "debugger", "critic"],
        effort="medium",
    )
    tags = {role: spec.ollama_tag for role, spec in chosen.items()}
    # Two distinct installed tags must appear.
    assert len(set(tags.values())) == 2
    # Coder + tester should land on the coder model.
    assert tags["coder"] == "qwen2.5-coder:7b"
    assert tags["tester"] == "qwen2.5-coder:7b"
    # Planner / debugger / critic should land on the reasoning model.
    assert tags["planner"] == "qwen2.5:7b"
    assert tags["debugger"] == "qwen2.5:7b"
    assert tags["critic"] == "qwen2.5:7b"


def test_session_selector_single_model_no_op():
    """When only one model is installed, every role gets that
    model — no forced degradation."""
    reg = CodeModelRegistry("http://localhost:11434")
    reg._available = ["qwen2.5-coder:7b"]
    reg._probed = True
    chosen = reg.select_models_for_session(
        ["planner", "coder", "tester", "debugger", "critic"],
        effort="medium",
    )
    tags = {spec.ollama_tag for spec in chosen.values()}
    assert tags == {"qwen2.5-coder:7b"}


def test_session_selector_three_model_richer_split():
    """When deepseek-r1:7b is also installed, debugger should
    benefit from the dedicated reasoning model."""
    reg = CodeModelRegistry("http://localhost:11434")
    reg._available = [
        "qwen2.5:7b", "qwen2.5-coder:7b", "deepseek-r1:7b",
    ]
    reg._probed = True
    chosen = reg.select_models_for_session(
        ["planner", "coder", "tester", "debugger", "critic"],
        effort="medium",
    )
    tags = {role: spec.ollama_tag for role, spec in chosen.items()}
    # 3 distinct tags should appear across the 5 roles.
    assert len(set(tags.values())) == 3
    # Coder picks the code-specific model.
    assert tags["coder"] == "qwen2.5-coder:7b"
    # Debugger or tester or critic should pick deepseek-r1 (reasoning).
    assert "deepseek-r1:7b" in set(tags.values())


def test_session_selector_spread_can_be_disabled(
    two_model_registry: CodeModelRegistry,
):
    """``spread=False`` reverts to the per-role best, even if
    multiple roles end up on the same model."""
    chosen = two_model_registry.select_models_for_session(
        ["coder", "tester"], effort="medium", spread=False,
    )
    # With spread off, both pick the same coder-strong model.
    assert chosen["coder"].ollama_tag == "qwen2.5-coder:7b"
    assert chosen["tester"].ollama_tag == "qwen2.5-coder:7b"
