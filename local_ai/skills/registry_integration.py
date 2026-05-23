"""
Cycle F Sprint 4 — `load_skill` MCP tool integration.

When the planner identifies a matching skill from the frontmatter
index (see `loader.render_skill_index`), it issues a tool call:

    load_skill(name="snake_game_builder")

The dispatch:
  1. Validates the name against the loaded skills set.
  2. Sets `_ACTIVE_SKILL` ContextVar so the next coder / tester
     invocation reads "skill is active".
  3. Returns the full skill body (≤2000 tokens) as the MCP result.

The body itself is then re-injected into the agent's next system
prompt by `prompts.py` (Sprint 4 wiring on engine side).
"""

from __future__ import annotations

import logging
from typing import ClassVar, Dict, Optional, Type

from pydantic import BaseModel, Field

from local_ai.tools.base import MCPToolResult, Tool, ToolError
from local_ai.tools.registry import ToolRegistry

from .activation import set_active_skill
from .loader import Skill


logger = logging.getLogger(__name__)


class _LoadSkillInput(BaseModel):
    """Argument schema for the `load_skill` MCP tool."""

    name: str = Field(
        ...,
        description="Skill identifier from the frontmatter index.",
        min_length=1,
        max_length=64,
    )


class LoadSkillTool(Tool):
    """MCP tool: activate a skill and return its full body."""

    name: ClassVar[str] = "load_skill"
    description: ClassVar[str] = (
        "Activate a skill and return its full body.  Sets the "
        "_ACTIVE_SKILL ContextVar so subsequent coder/tester calls "
        "follow the skill's guidance."
    )
    InputModel: ClassVar[Optional[Type[BaseModel]]] = _LoadSkillInput
    is_async: ClassVar[bool] = False

    def __init__(self, skills_by_name: Dict[str, Skill]) -> None:
        """Bind to a snapshot of the loaded skills.  Reloads
        require a fresh ``LoadSkillTool`` instance (caller's
        responsibility — keeps the dispatch hot path free of
        filesystem checks)."""

        self._skills = dict(skills_by_name)

    def execute(self, args: BaseModel) -> MCPToolResult:
        assert isinstance(args, _LoadSkillInput)
        name = args.name.strip().lower()
        skill = self._skills.get(name)
        if skill is None:
            known = sorted(self._skills.keys())
            raise ToolError(
                f"unknown skill {name!r}.  Known: {known}",
                code="unknown_skill",
            )
        set_active_skill(name)
        logger.info("load_skill activated name=%s", name)
        return MCPToolResult(
            name=self.name,
            ok=True,
            output={
                "name": skill.name,
                "description": skill.frontmatter.description,
                "languages": list(skill.frontmatter.languages),
                "must_have_features": list(skill.frontmatter.must_have_features),
                "body": skill.body,
            },
            mime_type="application/json",
            metadata={"skill_name": skill.name},
        )


def register_into(
    registry: ToolRegistry,
    skills: list[Skill] | dict[str, Skill],
    *,
    replace: bool = False,
) -> None:
    """Register the `load_skill` tool against the given registry.

    Idempotent when ``replace=True`` so a hot-reload doesn't need
    to clear the registry first.
    """

    if isinstance(skills, list):
        skills_by_name = {s.name: s for s in skills}
    else:
        skills_by_name = dict(skills)
    tool = LoadSkillTool(skills_by_name)
    registry.register(tool, replace=replace)


__all__ = ["LoadSkillTool", "register_into"]
