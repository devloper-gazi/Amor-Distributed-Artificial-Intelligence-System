"""Cycle F Sprint 4 — tests for local_ai/skills/schema.py."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from local_ai.skills.schema import (
    SkillFrontmatter,
    estimate_frontmatter_tokens,
)


# ─── SkillFrontmatter validation ────────────────────────────────────


def test_minimal_valid_frontmatter():
    fm = SkillFrontmatter(
        name="todo_app",
        description="Build a todo list app",
        when_to_use=["User asks for a todo"],
    )
    assert fm.name == "todo_app"
    assert fm.description == "Build a todo list app"
    assert fm.when_to_use == ["User asks for a todo"]
    assert fm.languages == []
    assert fm.must_have_features == []


def test_name_rejects_uppercase():
    with pytest.raises(ValidationError):
        SkillFrontmatter(
            name="TodoApp",
            description="x",
            when_to_use=["x"],
        )


def test_name_rejects_too_short():
    with pytest.raises(ValidationError):
        SkillFrontmatter(name="ab", description="x", when_to_use=["x"])


def test_name_accepts_underscore_and_dash():
    SkillFrontmatter(
        name="my_skill_v2-beta",
        description="x",
        when_to_use=["x"],
    )


def test_description_rejects_empty():
    with pytest.raises(ValidationError):
        SkillFrontmatter(
            name="x_skill",
            description="",
            when_to_use=["x"],
        )


def test_description_rejects_newline():
    with pytest.raises(ValidationError):
        SkillFrontmatter(
            name="x_skill",
            description="line1\nline2",
            when_to_use=["x"],
        )


def test_description_rejects_too_long():
    with pytest.raises(ValidationError):
        SkillFrontmatter(
            name="x_skill",
            description="x" * 250,
            when_to_use=["x"],
        )


def test_when_to_use_rejects_empty_list():
    with pytest.raises(ValidationError):
        SkillFrontmatter(
            name="x_skill",
            description="x",
            when_to_use=[],
        )


def test_when_to_use_filters_empty_strings():
    fm = SkillFrontmatter(
        name="x_skill",
        description="x",
        when_to_use=["valid", "", "   ", "also valid"],
    )
    assert fm.when_to_use == ["valid", "also valid"]


def test_when_to_use_caps_at_10_entries():
    with pytest.raises(ValidationError):
        SkillFrontmatter(
            name="x_skill",
            description="x",
            when_to_use=[f"trigger {i}" for i in range(20)],
        )


def test_languages_list_normalized():
    fm = SkillFrontmatter(
        name="x_skill",
        description="x",
        when_to_use=["x"],
        languages=["python", "  ", " javascript "],
    )
    assert fm.languages == ["python", "javascript"]


# ─── estimate_frontmatter_tokens ────────────────────────────────────


def test_token_estimate_increases_with_content():
    short = SkillFrontmatter(
        name="x_skill",
        description="short",
        when_to_use=["a"],
    )
    long = SkillFrontmatter(
        name="x_skill",
        description="x" * 100,
        when_to_use=["trigger phrase " * 10],
        languages=["python", "html", "javascript", "go"],
        must_have_features=["feature " * 20] * 5,
    )
    assert estimate_frontmatter_tokens(long) > estimate_frontmatter_tokens(short)


def test_token_estimate_floor_is_one():
    """Even a vacuous-by-schema frontmatter shouldn't return 0."""
    fm = SkillFrontmatter(
        name="a_b", description="y", when_to_use=["z"],
    )
    assert estimate_frontmatter_tokens(fm) >= 1
