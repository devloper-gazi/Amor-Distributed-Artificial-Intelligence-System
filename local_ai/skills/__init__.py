"""
Cycle F Sprint 4 — Anthropic Agent Skills loader for AMOR.

Implements the agentskills.io progressive-disclosure pattern:

* Boot: discover all `./skills/<name>/SKILL.md` files, parse their
  frontmatter, validate via Pydantic, and surface the frontmatter
  (≤200 tokens per skill) to the planner system prompt as a
  `SKILLS AVAILABLE:` block.  Bodies stay on disk.
* Runtime: when the planner picks a skill, the `load_skill` MCP
  tool returns the full body (≤2000 tokens) and sets the
  `_ACTIVE_SKILL` ContextVar so the next coder/tester invocation
  reads "skill X is active" and injects the body into the system
  prompt.

Reference:
  * https://agentskills.io/ standard
  * Anthropic Claude Skills blog (Oct 2025)
  * Swirlai measurement: ~1700 tokens for Anthropic's stock
    17-skill library frontmatter total

Public surface:
  load_skills(root)            -> list[Skill]      # boot-time discovery
  Skill                                            # dataclass
  SkillFrontmatter             # Pydantic schema
  _ACTIVE_SKILL                # ContextVar[str | None]
  set_active_skill(name)       # mirrors set_active_role from Sprint 3
  active_skill_name()          -> str | None
  LoadSkillTool                # MCP tool subclass
  register_into(registry)      # one-call MCP integration
"""

from __future__ import annotations

from .activation import (
    _ACTIVE_SKILL,
    active_skill_name,
    set_active_skill,
)
from .loader import Skill, load_skills, render_skill_index
from .registry_integration import LoadSkillTool, register_into
from .schema import SkillFrontmatter

__all__ = [
    "_ACTIVE_SKILL",
    "active_skill_name",
    "load_skills",
    "render_skill_index",
    "register_into",
    "set_active_skill",
    "LoadSkillTool",
    "Skill",
    "SkillFrontmatter",
]
