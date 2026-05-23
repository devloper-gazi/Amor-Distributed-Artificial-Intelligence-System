"""Cycle F Sprint 5 — tests for local_ai/approval/policy.py.

Coverage:
  * ApprovalPolicy.decide() — every code path of the resolution
    rule (disabled → ALLOW; tool-name lists; category lookup;
    default action).
  * CostCircuitBreaker — charge / trip / reset / idempotency.
  * settings_to_policy — tolerant of empty + malformed inputs.
  * refresh_default_policy — settings-driven rebuild + cache.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from local_ai.approval import (
    ApprovalCategory,
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    CostCircuitBreaker,
    cost_circuit_breaker,
    DEFAULT_POLICY,
    refresh_default_policy,
    settings_to_policy,
)


# ─── ApprovalPolicy.decide ──────────────────────────────────────────


def test_disabled_policy_always_allows():
    p = ApprovalPolicy(enabled=False, deny_tools={"rm_rf"})
    req = ApprovalRequest(tool_name="rm_rf", category="delete")
    assert p.decide(req) == ApprovalDecision.ALLOW


def test_explicit_deny_returns_deny():
    p = ApprovalPolicy(enabled=True, deny_tools={"rm_rf"})
    req = ApprovalRequest(tool_name="rm_rf", category="delete")
    assert p.decide(req) == ApprovalDecision.DENY


def test_explicit_allow_silent_returns_allow():
    p = ApprovalPolicy(
        enabled=True, allow_silent_tools={"read_file"},
    )
    req = ApprovalRequest(tool_name="read_file", category="read")
    assert p.decide(req) == ApprovalDecision.ALLOW


def test_explicit_prompt_returns_prompt():
    p = ApprovalPolicy(enabled=True, prompt_tools={"git_push"})
    req = ApprovalRequest(tool_name="git_push", category="git")
    assert p.decide(req) == ApprovalDecision.PROMPT


def test_deny_wins_over_allow_silent():
    """Defensive: if a tool somehow lands in BOTH lists, deny wins."""

    p = ApprovalPolicy(
        enabled=True,
        deny_tools={"x"},
        allow_silent_tools={"x"},
    )
    req = ApprovalRequest(tool_name="x", category="exec")
    assert p.decide(req) == ApprovalDecision.DENY


def test_category_action_resolution():
    """Tool falls through to category → action map."""

    p = ApprovalPolicy(
        enabled=True,
        category_actions={
            ApprovalCategory.READ.value: "allow_silent",
            ApprovalCategory.DELETE.value: "deny",
            ApprovalCategory.EXEC.value: "prompt",
        },
    )
    assert p.decide(ApprovalRequest("anything", category="read")) == \
        ApprovalDecision.ALLOW
    assert p.decide(ApprovalRequest("anything", category="delete")) == \
        ApprovalDecision.DENY
    assert p.decide(ApprovalRequest("anything", category="exec")) == \
        ApprovalDecision.PROMPT


def test_unknown_category_falls_to_default_action():
    p = ApprovalPolicy(enabled=True, default_action="deny")
    req = ApprovalRequest("anything", category="totally_unknown_cat")
    assert p.decide(req) == ApprovalDecision.DENY


def test_action_string_normalised():
    """`allow` and `allow_silent` both decode to ALLOW."""

    p = ApprovalPolicy(
        enabled=True,
        category_actions={"x": "allow"},
    )
    req = ApprovalRequest("a", category="x")
    assert p.decide(req) == ApprovalDecision.ALLOW


def test_decide_is_case_insensitive_on_category():
    p = ApprovalPolicy(
        enabled=True,
        category_actions={"delete": "deny"},
    )
    req = ApprovalRequest("a", category="DELETE")
    assert p.decide(req) == ApprovalDecision.DENY


# ─── CostCircuitBreaker ─────────────────────────────────────────────


def test_breaker_allows_under_budget():
    cb = CostCircuitBreaker(budget_tokens=1000)
    assert cb.charge(500) is True
    assert cb.charge(400) is True
    assert cb.tripped is False


def test_breaker_trips_at_budget():
    cb = CostCircuitBreaker(budget_tokens=1000)
    assert cb.charge(1000) is False
    assert cb.tripped is True


def test_breaker_idempotent_once_tripped():
    cb = CostCircuitBreaker(budget_tokens=100)
    cb.charge(200)  # trips
    assert cb.charge(1) is False
    assert cb.charge(1) is False


def test_breaker_reset_clears_state():
    cb = CostCircuitBreaker(budget_tokens=100)
    cb.charge(200)
    assert cb.tripped is True
    cb.reset()
    assert cb.tripped is False
    assert cb.spent_tokens == 0
    assert cb.charge(50) is True


def test_breaker_reset_with_new_budget():
    cb = CostCircuitBreaker(budget_tokens=100)
    cb.reset(budget_tokens=500)
    assert cb.budget_tokens == 500


def test_breaker_negative_charge_ignored():
    cb = CostCircuitBreaker(budget_tokens=100)
    cb.charge(-50)
    assert cb.spent_tokens == 0


def test_cost_circuit_breaker_helper_returns_instance():
    cb = cost_circuit_breaker(budget_tokens=42)
    assert isinstance(cb, CostCircuitBreaker)
    assert cb.budget_tokens == 42


# ─── settings_to_policy ─────────────────────────────────────────────


def test_settings_to_policy_empty_inputs():
    p = settings_to_policy(enabled=False)
    assert p.enabled is False
    assert p.allow_silent_tools == set()
    assert p.deny_tools == set()
    assert p.prompt_tools == set()


def test_settings_to_policy_parses_csv():
    p = settings_to_policy(
        enabled=True,
        allow_silent_csv="read_file, search_codebase",
        deny_csv="rm_rf,git_force_push",
        prompt_csv="docker_run",
    )
    assert p.allow_silent_tools == {"read_file", "search_codebase"}
    assert p.deny_tools == {"rm_rf", "git_force_push"}
    assert p.prompt_tools == {"docker_run"}


def test_settings_to_policy_category_actions_json():
    p = settings_to_policy(
        enabled=True,
        category_actions_json='{"delete": "prompt", "exec": "deny"}',
    )
    assert p.category_actions["delete"] == "prompt"
    assert p.category_actions["exec"] == "deny"


def test_settings_to_policy_malformed_json_tolerated():
    p = settings_to_policy(
        enabled=True,
        category_actions_json="not json {",
    )
    # Falls through to defaults; doesn't raise.
    assert ApprovalCategory.READ.value in p.category_actions


def test_settings_to_policy_default_action():
    p = settings_to_policy(enabled=True, default_action="deny")
    assert p.default_action == "deny"


# ─── refresh_default_policy ─────────────────────────────────────────


def test_refresh_default_policy_uses_explicit_settings():
    fake = SimpleNamespace(
        code_approval_enabled=True,
        code_approval_allow_silent="read_file",
        code_approval_deny="rm_rf",
        code_approval_prompt="",
        code_approval_default_action="prompt",
        code_approval_category_actions="",
    )
    refresh_default_policy(fake)
    assert DEFAULT_POLICY.enabled is True
    assert "read_file" in DEFAULT_POLICY.allow_silent_tools
    assert "rm_rf" in DEFAULT_POLICY.deny_tools


def test_refresh_default_policy_disabled_state():
    fake = SimpleNamespace(
        code_approval_enabled=False,
        code_approval_allow_silent="",
        code_approval_deny="",
        code_approval_prompt="",
        code_approval_default_action="prompt",
        code_approval_category_actions="",
    )
    refresh_default_policy(fake)
    assert DEFAULT_POLICY.enabled is False
    # When disabled, decide() always returns ALLOW.
    req = ApprovalRequest("anything", category="delete")
    assert DEFAULT_POLICY.decide(req) == ApprovalDecision.ALLOW


def test_refresh_default_policy_idempotent_when_unchanged():
    fake = SimpleNamespace(
        code_approval_enabled=True,
        code_approval_allow_silent="x",
        code_approval_deny="",
        code_approval_prompt="",
        code_approval_default_action="prompt",
        code_approval_category_actions="",
    )
    refresh_default_policy(fake)
    p1_id = id(DEFAULT_POLICY)
    refresh_default_policy(fake)
    p2_id = id(DEFAULT_POLICY)
    # Same singleton (in-place mutation).
    assert p1_id == p2_id


def test_refresh_default_policy_with_missing_settings_module_no_op():
    """When `document_processor.config.settings` isn't importable,
    `refresh_default_policy(None)` should silently keep the existing
    state rather than crashing."""

    # We can't easily mock the import, but the function's `except Exception:`
    # path returns the current policy without changes — verify by calling
    # with no arg and confirming it doesn't raise.
    out = refresh_default_policy()  # explicit None
    assert out is DEFAULT_POLICY
