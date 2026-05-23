"""Cycle F Sprint 5 — `ToolRegistry.dispatch` x `ApprovalPolicy` gate.

Verifies the wrap-around added in `local_ai/tools/registry.py`:

  * Disabled policy → tool runs (Sprints 1-4 behaviour preserved).
  * Enabled + DENY → tool blocked with `approval_denied` metadata.
  * Enabled + PROMPT + no callback → fail closed (deny).
  * Enabled + PROMPT + callback returns True → tool runs.
  * Enabled + PROMPT + callback returns False → tool blocked.
  * Tool.category attribute respected.
"""

from __future__ import annotations

from typing import ClassVar, Optional, Type

import pytest
from pydantic import BaseModel

from local_ai.approval import (
    DEFAULT_POLICY,
    refresh_default_policy,
    settings_to_policy,
)
from local_ai.tools.base import MCPToolResult, Tool
from local_ai.tools.registry import ToolRegistry


class _PingInput(BaseModel):
    message: str = "ok"


class _PingTool(Tool):
    """Read-category tool that succeeds with a constant message."""

    name: ClassVar[str] = "ping"
    description: ClassVar[str] = "no-op ping"
    InputModel: ClassVar[Optional[Type[BaseModel]]] = _PingInput
    category: ClassVar[str] = "read"

    def execute(self, args):
        return MCPToolResult(name=self.name, ok=True, output="pong")


class _DeleteTool(Tool):
    """Delete-category tool — should be denied by default policy."""

    name: ClassVar[str] = "rm_rf"
    description: ClassVar[str] = "destructive delete"
    InputModel: ClassVar[Optional[Type[BaseModel]]] = _PingInput
    category: ClassVar[str] = "delete"

    def execute(self, args):
        return MCPToolResult(name=self.name, ok=True, output="deleted")


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.register(_PingTool())
    r.register(_DeleteTool())
    return r


# ─── Disabled policy: passthrough ───────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_policy_dispatch_runs_tool(registry, monkeypatch):
    # Force-disable by mutating DEFAULT_POLICY directly.
    DEFAULT_POLICY.enabled = False
    refresh_default_policy.__wrapped__ if hasattr(refresh_default_policy, "__wrapped__") else None
    # Override the lazy refresh inside dispatch — we don't want it
    # rebuilding from real settings mid-test.
    from local_ai.tools import registry as reg_mod
    monkeypatch.setattr(
        "local_ai.approval.refresh_default_policy",
        lambda *a, **kw: DEFAULT_POLICY,
    )
    result = await registry.dispatch("rm_rf", {"message": "hi"})
    assert result.ok is True
    assert result.output == "deleted"


# ─── DENY path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enabled_policy_deny_blocks_tool(registry, monkeypatch):
    fake_policy = settings_to_policy(
        enabled=True,
        deny_csv="rm_rf",
    )
    DEFAULT_POLICY.allow_silent_tools = fake_policy.allow_silent_tools
    DEFAULT_POLICY.deny_tools = fake_policy.deny_tools
    DEFAULT_POLICY.prompt_tools = fake_policy.prompt_tools
    DEFAULT_POLICY.category_actions = fake_policy.category_actions
    DEFAULT_POLICY.default_action = fake_policy.default_action
    DEFAULT_POLICY.enabled = True
    monkeypatch.setattr(
        "local_ai.approval.refresh_default_policy",
        lambda *a, **kw: DEFAULT_POLICY,
    )

    result = await registry.dispatch("rm_rf", {"message": "x"})
    assert result.ok is False
    assert "denied" in result.error.lower()
    assert result.metadata.get("code") == "approval_denied"

    # Cleanup — restore disabled state.
    DEFAULT_POLICY.enabled = False


# ─── PROMPT path — no callback → fail closed ────────────────────────


@pytest.mark.asyncio
async def test_enabled_policy_prompt_no_callback_fails_closed(
    registry, monkeypatch,
):
    DEFAULT_POLICY.allow_silent_tools = set()
    DEFAULT_POLICY.deny_tools = set()
    DEFAULT_POLICY.prompt_tools = {"rm_rf"}
    DEFAULT_POLICY.enabled = True
    monkeypatch.setattr(
        "local_ai.approval.refresh_default_policy",
        lambda *a, **kw: DEFAULT_POLICY,
    )

    result = await registry.dispatch("rm_rf", {"message": "x"})
    assert result.ok is False
    assert result.metadata.get("code") == "approval_required"

    DEFAULT_POLICY.enabled = False
    DEFAULT_POLICY.prompt_tools = set()


# ─── PROMPT path — callback approves ────────────────────────────────


@pytest.mark.asyncio
async def test_enabled_policy_prompt_callback_approves(
    registry, monkeypatch,
):
    DEFAULT_POLICY.allow_silent_tools = set()
    DEFAULT_POLICY.deny_tools = set()
    DEFAULT_POLICY.prompt_tools = {"rm_rf"}
    DEFAULT_POLICY.enabled = True
    monkeypatch.setattr(
        "local_ai.approval.refresh_default_policy",
        lambda *a, **kw: DEFAULT_POLICY,
    )

    async def callback(req):
        assert req.tool_name == "rm_rf"
        return True

    result = await registry.dispatch(
        "rm_rf", {"message": "x"},
        approval_callback=callback,
    )
    assert result.ok is True
    assert result.output == "deleted"

    DEFAULT_POLICY.enabled = False
    DEFAULT_POLICY.prompt_tools = set()


# ─── PROMPT path — callback rejects ─────────────────────────────────


@pytest.mark.asyncio
async def test_enabled_policy_prompt_callback_rejects(
    registry, monkeypatch,
):
    DEFAULT_POLICY.allow_silent_tools = set()
    DEFAULT_POLICY.deny_tools = set()
    DEFAULT_POLICY.prompt_tools = {"rm_rf"}
    DEFAULT_POLICY.enabled = True
    monkeypatch.setattr(
        "local_ai.approval.refresh_default_policy",
        lambda *a, **kw: DEFAULT_POLICY,
    )

    async def callback(req):
        return False

    result = await registry.dispatch(
        "rm_rf", {"message": "x"},
        approval_callback=callback,
    )
    assert result.ok is False
    assert result.metadata.get("code") == "approval_rejected"

    DEFAULT_POLICY.enabled = False
    DEFAULT_POLICY.prompt_tools = set()


# ─── ALLOW path: read tool always runs ──────────────────────────────


@pytest.mark.asyncio
async def test_read_tool_allowed_silently_under_default_category_actions(
    registry, monkeypatch,
):
    """`read` category is `allow_silent` in the default mapping."""

    DEFAULT_POLICY.allow_silent_tools = set()
    DEFAULT_POLICY.deny_tools = set()
    DEFAULT_POLICY.prompt_tools = set()
    DEFAULT_POLICY.enabled = True
    monkeypatch.setattr(
        "local_ai.approval.refresh_default_policy",
        lambda *a, **kw: DEFAULT_POLICY,
    )

    result = await registry.dispatch("ping", {"message": "x"})
    assert result.ok is True
    assert result.output == "pong"

    DEFAULT_POLICY.enabled = False


# ─── Tool.category attribute defaults ───────────────────────────────


def test_tool_base_category_default():
    """A Tool subclass that doesn't override `category` falls through
    to the policy's default_action via the `unclassified` lookup."""

    assert _PingTool.category == "read"
    assert _DeleteTool.category == "delete"

    class _NoCat(Tool):
        name = "no_cat"
        description = "no category declared"
        InputModel = _PingInput

        def execute(self, args):
            return MCPToolResult(name=self.name, ok=True)

    assert _NoCat.category == "unclassified"
