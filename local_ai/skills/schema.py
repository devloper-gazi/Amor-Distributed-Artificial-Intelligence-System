"""
Cycle F Sprint 4 — Pydantic schema for SKILL.md frontmatter.

Anthropic Agent Skills (agentskills.io) frontmatter shape:

    ---
    name: snake_game_builder
    description: Build a snake game with arrow-key controls...
    when_to_use:
      - User asks for "snake game"
      - User wants a browser-playable arcade game
    languages:
      - html
      - javascript
    must_have_features:
      - HTML5 canvas
      - Arrow-key controls
      - Score + game-over screen
    ---

Validation rules (deliberately conservative to keep boot fast):

* `name` is the directory name (must match the parent dir on disk).
  Snake-case, 3-64 chars, [a-z0-9_-].
* `description` is a single line, ≤200 chars.
* `when_to_use` is a list of trigger phrases (≥1, ≤10 entries).
* `languages` defaults to empty (matches any).
* `must_have_features` defaults to empty.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


class SkillFrontmatter(BaseModel):
    """Strict schema for the YAML frontmatter at the top of SKILL.md."""

    name: str = Field(..., description="Snake-case skill identifier.")
    description: str = Field(..., description="One-line summary.")
    when_to_use: list[str] = Field(
        default_factory=list,
        description="Trigger phrases for the planner.",
    )
    languages: list[str] = Field(
        default_factory=list,
        description="Target language(s) when the skill activates.",
    )
    must_have_features: list[str] = Field(
        default_factory=list,
        description=("Features the coder MUST include when this "
                     "skill is active.  Mirrors domain_templates.py."),
    )

    @field_validator("name")
    @classmethod
    def _name_format(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"name {v!r} must match {_NAME_RE.pattern} (snake-case, "
                "3-64 chars, [a-z0-9_-])"
            )
        return v

    @field_validator("description")
    @classmethod
    def _description_one_line(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("description must be non-empty")
        if "\n" in v:
            raise ValueError("description must be a single line")
        if len(v) > 200:
            raise ValueError(
                f"description too long ({len(v)} chars; max 200)"
            )
        return v

    @field_validator("when_to_use")
    @classmethod
    def _when_to_use_bounded(cls, v: list[str]) -> list[str]:
        if not isinstance(v, list):
            raise ValueError("when_to_use must be a list")
        cleaned = [str(item).strip() for item in v if str(item).strip()]
        if not cleaned:
            raise ValueError(
                "when_to_use must contain at least one trigger phrase"
            )
        if len(cleaned) > 10:
            raise ValueError(
                f"when_to_use has {len(cleaned)} entries; max 10"
            )
        return cleaned

    @field_validator("languages", "must_have_features")
    @classmethod
    def _string_list(cls, v: list[str]) -> list[str]:
        if not isinstance(v, list):
            raise ValueError("expected a list")
        return [str(item).strip() for item in v if str(item).strip()]


def estimate_frontmatter_tokens(fm: SkillFrontmatter) -> int:
    """Quick token estimate (~4 chars / token).  Used by the planner-
    prompt budget check; not exact, but reliable as a relative
    ordering for the truncation rule in render_skill_index()."""

    blob = (
        f"{fm.name} {fm.description} "
        + " ".join(fm.when_to_use)
        + " " + " ".join(fm.languages)
        + " " + " ".join(fm.must_have_features)
    )
    return max(1, len(blob) // 4)


__all__ = ["SkillFrontmatter", "estimate_frontmatter_tokens"]
