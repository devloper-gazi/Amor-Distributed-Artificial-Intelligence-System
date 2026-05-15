"""Cycle F Sprint 4 — tests for local_ai/skills/activation.py +
registry_integration.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_ai.skills.activation import (
    _ACTIVE_SKILL,
    active_skill_name,
    set_active_skill,
)
from local_ai.skills.loader import Skill, load_skills
from local_ai.skills.registry_integration import (
    LoadSkillTool,
    register_into,
)
from local_ai.skills.schema import SkillFrontmatter
from local_ai.tools.base import ToolError
from local_ai.tools.registry import ToolRegistry


# ─── ContextVar ─────────────────────────────────────────────────────


def test_default_active_skill_is_none():
    # In a fresh context, no skill is active.
    import contextvars
    ctx = contextvars.copy_context()
    assert ctx.run(active_skill_name) is None


def test_set_active_skill_stores_value():
    token = set_active_skill("foo_skill")
    try:
        assert active_skill_name() == "foo_skill"
    finally:
        _ACTIVE_SKILL.reset(token)


def test_set_active_skill_none_clears():
    token = set_active_skill("x")
    try:
        set_active_skill(None)
        assert active_skill_name() is None
    finally:
        _ACTIVE_SKILL.reset(token)


# ─── LoadSkillTool ──────────────────────────────────────────────────


def _make_skill(name: str = "alpha_skill", body: str = "body!") -> Skill:
    return Skill(
        frontmatter=SkillFrontmatter(
            name=name,
            description="desc",
            when_to_use=["trigger"],
            languages=["python"],
        ),
        body=body,
        path=Path(f"/fake/{name}/SKILL.md"),
    )


def test_load_skill_tool_returns_body():
    sk = _make_skill("alpha_skill", body="this is alpha's body content")
    tool = LoadSkillTool({"alpha_skill": sk})
    args = tool.InputModel(name="alpha_skill")
    result = tool.execute(args)
    assert result.ok is True
    assert result.output["name"] == "alpha_skill"
    assert "alpha's body" in result.output["body"]
    assert active_skill_name() == "alpha_skill"
    # Cleanup
    set_active_skill(None)


def test_load_skill_tool_unknown_raises():
    sk = _make_skill("alpha_skill")
    tool = LoadSkillTool({"alpha_skill": sk})
    args = tool.InputModel(name="does_not_exist")
    with pytest.raises(ToolError) as exc:
        tool.execute(args)
    assert "unknown" in str(exc.value).lower()


def test_load_skill_tool_case_normalized():
    sk = _make_skill("alpha_skill")
    tool = LoadSkillTool({"alpha_skill": sk})
    args = tool.InputModel(name="ALPHA_SKILL")
    result = tool.execute(args)
    assert result.ok is True
    set_active_skill(None)


def test_input_validation_rejects_empty_name():
    tool = LoadSkillTool({})
    with pytest.raises(Exception):  # pydantic ValidationError
        tool.validate({"name": ""})


# ─── register_into ──────────────────────────────────────────────────


def test_register_into_adds_tool_to_registry():
    reg = ToolRegistry()
    skills = [_make_skill("alpha_skill"), _make_skill("beta_skill")]
    register_into(reg, skills)
    assert "load_skill" in reg
    tool = reg.get("load_skill")
    assert isinstance(tool, LoadSkillTool)


def test_register_into_accepts_dict():
    reg = ToolRegistry()
    skills = {"alpha_skill": _make_skill("alpha_skill")}
    register_into(reg, skills)
    assert "load_skill" in reg


def test_register_into_idempotent_with_replace():
    reg = ToolRegistry()
    register_into(reg, [_make_skill("alpha_skill")])
    register_into(reg, [_make_skill("alpha_skill")], replace=True)
    assert len(reg) == 1
