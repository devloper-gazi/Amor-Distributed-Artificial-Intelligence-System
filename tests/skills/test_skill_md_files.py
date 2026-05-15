"""Cycle F Sprint 4 — invariants on the 8 shipped SKILL.md files.

If the user adds a new skill or edits an existing one and breaks
the schema, these tests catch it before deploy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_ai.skills.loader import load_skills, render_skill_index
from local_ai.skills.schema import estimate_frontmatter_tokens


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_ROOT = REPO_ROOT / "skills"


EXPECTED_SKILLS = {
    "snake_game_builder",
    "todo_app",
    "landing_page",
    "dashboard",
    "rest_api_service",
    "cli_tool",
    "data_viz",
    "blog_post",
}


@pytest.fixture(scope="module")
def loaded():
    return load_skills(SKILLS_ROOT)


def test_skills_root_exists():
    assert SKILLS_ROOT.is_dir(), f"skills root missing: {SKILLS_ROOT}"


def test_all_eight_skills_load(loaded):
    names = {s.name for s in loaded.skills}
    missing = EXPECTED_SKILLS - names
    assert not missing, f"missing skills: {missing}"


def test_zero_load_errors(loaded):
    assert loaded.errors == [], (
        "skill load errors: " + ", ".join(
            f"{e.path}: {e.error}" for e in loaded.errors
        )
    )


def test_every_skill_has_at_least_one_when_to_use(loaded):
    for sk in loaded.skills:
        assert sk.frontmatter.when_to_use, (
            f"{sk.name} has empty when_to_use"
        )


def test_every_skill_has_languages(loaded):
    # While `languages` defaults to empty in the schema, every
    # shipped skill should declare its target languages.
    for sk in loaded.skills:
        assert sk.frontmatter.languages, (
            f"{sk.name} should declare at least one target language"
        )


def test_every_skill_has_a_body(loaded):
    for sk in loaded.skills:
        assert sk.body.strip(), f"{sk.name} has empty body"


def test_skill_index_under_token_budget(loaded):
    """The combined 8-skill index must fit comfortably in the 2K
    planner-prompt token budget."""

    out = render_skill_index(loaded.skills, token_budget=2000)
    estimated_tokens = len(out) // 4
    assert estimated_tokens < 2000, (
        f"index estimated at {estimated_tokens} tokens; over budget"
    )


def test_each_skill_frontmatter_under_300_tokens(loaded):
    """Per-skill frontmatter should stay slim so the budget scales
    with skill-count growth (≤300 leaves room for ~6 skills at the
    2K budget; the truncation rule kicks in past that)."""

    for sk in loaded.skills:
        cost = estimate_frontmatter_tokens(sk.frontmatter)
        assert cost < 300, (
            f"{sk.name} frontmatter ~{cost} tokens; trim it down"
        )


def test_skill_directory_matches_frontmatter_name(loaded):
    for sk in loaded.skills:
        assert sk.path.parent.name == sk.name, (
            f"directory {sk.path.parent.name} != name {sk.name}"
        )


def test_skill_names_are_snake_case(loaded):
    import re
    pattern = re.compile(r"^[a-z][a-z0-9_]+$")
    for sk in loaded.skills:
        assert pattern.match(sk.name), (
            f"{sk.name} not snake_case"
        )
