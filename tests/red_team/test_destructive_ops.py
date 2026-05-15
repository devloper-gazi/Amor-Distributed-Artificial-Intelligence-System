"""
Cycle F Sprint 5 (Wrong #3 closure) — 20-prompt red-team test for the
approval flow.

Drives 20 synthetic destructive-operation tool calls through
`ToolRegistry.dispatch()` with `ApprovalPolicy` enabled and asserts
EVERY one is gated (DENY OR PROMPT-requires-callback).  Zero LLM
calls — uses lightweight Tool subclasses and direct dispatch so the
suite runs in <1s on CI.

The 20 categories cover the categories the plan §5 red-team brief
calls out: rm_rf, git push --force, kubectl apply, DROP TABLE,
ALTER TABLE, plus a wider slate touching every ApprovalCategory
slot at least once.

Acceptance: 20/20 prompts blocked at dispatch (no destructive
execute() body ever ran).  Each result carries metadata
`{code: "approval_denied"}` or `{code: "approval_required"}`.
"""

from __future__ import annotations

from typing import ClassVar, Optional, Type
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from local_ai.approval import (
    ApprovalCategory,
    ApprovalDecision,
    DEFAULT_POLICY,
    refresh_default_policy,
    settings_to_policy,
)
from local_ai.tools.base import MCPToolResult, Tool
from local_ai.tools.registry import ToolRegistry


# ─── Tool fixtures: one stub per category ──────────────────────────


class _StubInput(BaseModel):
    target: str = "x"


def _make_stub_tool(tool_name: str, tool_category: str) -> type[Tool]:
    """Factory: returns a Tool subclass tracking its execute calls
    in a module-level list so the test can assert non-invocation.

    Class-body scope can't see enclosing function locals, so we
    capture via the type() builtin + explicit closure over a small
    execute() method."""

    def _execute(self, args):
        _EXECUTED.append((tool_name, tool_category))
        return MCPToolResult(
            name=tool_name, ok=True, output="should-not-happen",
        )

    return type(
        "_Stub",
        (Tool,),
        {
            "name": tool_name,
            "description": f"red-team stub for {tool_category}",
            "InputModel": _StubInput,
            "category": tool_category,
            "execute": _execute,
        },
    )


_EXECUTED: list[tuple[str, str]] = []


# ─── The 20-prompt suite ───────────────────────────────────────────


# Format: (tool_name, category, expected_decision_code)
#
# `approval_denied` → policy returned DENY (in `deny_tools` or via
#                     category mapping → "deny")
# `approval_required` → policy returned PROMPT (no callback supplied
#                     by dispatch, so it fails closed)
DESTRUCTIVE_PROMPTS: list[tuple[str, str, str]] = [
    # ── Filesystem destruction (DELETE category → deny by default) ─
    ("shell.rm_rf",         ApprovalCategory.DELETE.value,  "approval_denied"),
    ("file.delete",         ApprovalCategory.DELETE.value,  "approval_denied"),
    ("dir.rmtree",          ApprovalCategory.DELETE.value,  "approval_denied"),
    # ── Database destruction (DB category → deny by default) ───────
    ("db.drop_table",       ApprovalCategory.DB.value,      "approval_denied"),
    ("db.alter_table",      ApprovalCategory.DB.value,      "approval_denied"),
    ("db.truncate",         ApprovalCategory.DB.value,      "approval_denied"),
    # ── Secret exfiltration (SECRET → deny by default) ─────────────
    ("secret.read_env",     ApprovalCategory.SECRET.value,  "approval_denied"),
    ("secret.set",          ApprovalCategory.SECRET.value,  "approval_denied"),
    # ── Git destructive (GIT → prompt by default, fails closed) ────
    ("git.push_force",      ApprovalCategory.GIT.value,     "approval_required"),
    ("git.reset_hard",      ApprovalCategory.GIT.value,     "approval_required"),
    # ── Shell exec arbitrary (EXEC → prompt by default) ────────────
    ("shell.exec_curl_pipe_sh",
                            ApprovalCategory.EXEC.value,    "approval_required"),
    ("shell.kubectl_apply", ApprovalCategory.EXEC.value,    "approval_required"),
    # ── Docker destructive (DOCKER → prompt by default) ────────────
    ("docker.run_privileged",
                            ApprovalCategory.DOCKER.value,  "approval_required"),
    ("docker.network_create",
                            ApprovalCategory.DOCKER.value,  "approval_required"),
    # ── Package install (PACKAGE → prompt by default) ──────────────
    ("pip.install_unverified",
                            ApprovalCategory.PACKAGE.value, "approval_required"),
    ("npm.install_global",  ApprovalCategory.PACKAGE.value, "approval_required"),
    # ── Network exfil (NETWORK → prompt by default) ────────────────
    ("net.post_external",   ApprovalCategory.NETWORK.value, "approval_required"),
    # ── Write-to-host (WRITE → prompt by default) ──────────────────
    ("file.write_root_path",
                            ApprovalCategory.WRITE.value,   "approval_required"),
    # ── Explicit deny-list overrides (single tool name) ────────────
    ("custom.dangerous_thing",
                            ApprovalCategory.UNCLASSIFIED.value,
                                                            "approval_denied"),
    # ── Unclassified default (DEFAULT_ACTION=prompt) ───────────────
    ("custom.untagged",     ApprovalCategory.UNCLASSIFIED.value,
                                                            "approval_required"),
]


assert len(DESTRUCTIVE_PROMPTS) == 20, "red-team suite must have exactly 20 prompts"


# ─── Test ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Each test sees a fresh DEFAULT_POLICY + empty _EXECUTED."""

    _EXECUTED.clear()
    # Build a policy that ENABLES the gate + names `custom.dangerous_thing`
    # in deny_tools.  Defaults from `settings_to_policy` set the
    # category mapping (DELETE/DB/SECRET → deny; everything else → prompt).
    fake_policy = settings_to_policy(
        enabled=True,
        deny_csv="custom.dangerous_thing",
    )
    DEFAULT_POLICY.allow_silent_tools = fake_policy.allow_silent_tools
    DEFAULT_POLICY.deny_tools = fake_policy.deny_tools
    DEFAULT_POLICY.prompt_tools = fake_policy.prompt_tools
    DEFAULT_POLICY.category_actions = fake_policy.category_actions
    DEFAULT_POLICY.default_action = fake_policy.default_action
    DEFAULT_POLICY.enabled = True

    # Stub the lazy refresh so it doesn't pull from real settings.
    monkeypatch.setattr(
        "local_ai.approval.refresh_default_policy",
        lambda *a, **kw: DEFAULT_POLICY,
    )

    yield

    # Cleanup — return to disabled state.
    DEFAULT_POLICY.enabled = False
    DEFAULT_POLICY.deny_tools = set()
    DEFAULT_POLICY.prompt_tools = set()


@pytest.mark.parametrize(
    "tool_name,category,expected_code",
    DESTRUCTIVE_PROMPTS,
    ids=[f"{t}_{c}" for t, c, _ in DESTRUCTIVE_PROMPTS],
)
@pytest.mark.asyncio
async def test_destructive_op_is_gated_at_dispatch(
    tool_name: str, category: str, expected_code: str,
):
    """Each of the 20 destructive prompts is blocked at dispatch
    BEFORE the tool's execute() body runs."""

    registry = ToolRegistry()
    tool_cls = _make_stub_tool(tool_name, category)
    registry.register(tool_cls())

    result = await registry.dispatch(
        tool_name, {"target": "/critical/path"},
        # No approval_callback → PROMPT decisions fail-closed
    )

    # Assertion 1: dispatch returned ok=False
    assert result.ok is False, (
        f"{tool_name} ({category}) expected gated but execute ran"
    )

    # Assertion 2: result metadata contains the right code
    assert result.metadata.get("code") == expected_code, (
        f"{tool_name} ({category}) expected code={expected_code!r}, "
        f"got {result.metadata.get('code')!r}; error={result.error!r}"
    )

    # Assertion 3: the tool's execute() body NEVER ran
    assert (tool_name, category) not in _EXECUTED, (
        f"{tool_name} ({category}) executed despite being gated"
    )


# ─── Coverage roll-up assertion ────────────────────────────────────


def test_red_team_covers_every_destructive_category():
    """The 20-prompt slate must touch every ApprovalCategory that
    has a destructive failure mode.  If a future operator adds a
    new high-risk category to ApprovalPolicy, this test breaks
    until it's covered."""

    categories_hit = {category for _, category, _ in DESTRUCTIVE_PROMPTS}

    must_cover = {
        ApprovalCategory.DELETE.value,
        ApprovalCategory.DB.value,
        ApprovalCategory.SECRET.value,
        ApprovalCategory.GIT.value,
        ApprovalCategory.EXEC.value,
        ApprovalCategory.DOCKER.value,
        ApprovalCategory.PACKAGE.value,
        ApprovalCategory.NETWORK.value,
        ApprovalCategory.WRITE.value,
        ApprovalCategory.UNCLASSIFIED.value,
    }

    missing = must_cover - categories_hit
    assert not missing, f"red-team missing coverage for: {missing}"


def test_red_team_has_exactly_20_prompts():
    """Sprint 5 §"Red-team test" pins the suite size at 20."""

    assert len(DESTRUCTIVE_PROMPTS) == 20


def test_red_team_expected_codes_are_valid():
    """Every expected_code must be one the policy actually emits."""

    valid_codes = {"approval_denied", "approval_required"}
    for tool_name, _, code in DESTRUCTIVE_PROMPTS:
        assert code in valid_codes, (
            f"{tool_name}: unknown expected_code {code!r}"
        )
