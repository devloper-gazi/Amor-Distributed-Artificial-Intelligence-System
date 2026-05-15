"""Cycle F Sprint 4 — tests for local_ai/skills/loader.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_ai.skills.loader import (
    Skill,
    SkillLoadError,
    _parse_frontmatter_block,
    load_skills,
    render_skill_index,
)


# ─── _parse_frontmatter_block (mini-YAML) ───────────────────────────


def test_parse_flat_keys():
    out = _parse_frontmatter_block(
        "name: foo_skill\n"
        "description: a one-line description\n"
    )
    assert out == {
        "name": "foo_skill",
        "description": "a one-line description",
    }


def test_parse_list_values():
    out = _parse_frontmatter_block(
        "when_to_use:\n"
        "  - item one\n"
        "  - item two\n"
        "  - item three\n"
    )
    assert out["when_to_use"] == ["item one", "item two", "item three"]


def test_parse_mixed_flat_and_list():
    out = _parse_frontmatter_block(
        "name: x_skill\n"
        "description: desc\n"
        "when_to_use:\n"
        "  - first\n"
        "  - second\n"
        "languages:\n"
        "  - python\n"
    )
    assert out == {
        "name": "x_skill",
        "description": "desc",
        "when_to_use": ["first", "second"],
        "languages": ["python"],
    }


def test_parse_strips_quotes():
    out = _parse_frontmatter_block(
        'name: "x_skill"\n'
        "description: 'with apostrophe'\n"
    )
    assert out["name"] == "x_skill"
    assert out["description"] == "with apostrophe"


def test_parse_skips_blank_and_comment_lines():
    out = _parse_frontmatter_block(
        "# top comment\n"
        "\n"
        "name: x_skill\n"
        "# mid-block comment\n"
        "description: desc\n"
    )
    assert out == {"name": "x_skill", "description": "desc"}


def test_parse_raises_on_malformed_line():
    with pytest.raises(ValueError):
        _parse_frontmatter_block("this is not key value\n")


# ─── load_skills end-to-end ─────────────────────────────────────────


def _write_skill(root: Path, name: str, body: str = "body content") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(
        "---\n"
        f"name: {name}\n"
        "description: test skill description\n"
        "when_to_use:\n"
        "  - user asks for x\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return f


def test_load_skills_empty_root_returns_no_skills(tmp_path: Path):
    result = load_skills(tmp_path)
    assert result.skills == []
    assert result.errors == []


def test_load_skills_missing_root_does_not_raise(tmp_path: Path):
    result = load_skills(tmp_path / "does_not_exist")
    assert result.skills == []
    assert result.errors == []


def test_load_skills_finds_valid_skill(tmp_path: Path):
    _write_skill(tmp_path, "foo_skill", body="this is foo's body")
    result = load_skills(tmp_path)
    assert len(result.skills) == 1
    sk = result.skills[0]
    assert isinstance(sk, Skill)
    assert sk.name == "foo_skill"
    assert "foo's body" in sk.body
    assert result.errors == []


def test_load_skills_skips_files_outside_skill_dirs(tmp_path: Path):
    (tmp_path / "README.md").write_text("not a skill", encoding="utf-8")
    _write_skill(tmp_path, "valid_skill")
    result = load_skills(tmp_path)
    assert {s.name for s in result.skills} == {"valid_skill"}


def test_load_skills_collects_errors_without_aborting(tmp_path: Path):
    _write_skill(tmp_path, "valid_skill")
    bad_dir = tmp_path / "bad_skill"
    bad_dir.mkdir()
    (bad_dir / "SKILL.md").write_text(
        "no frontmatter\n", encoding="utf-8",
    )
    result = load_skills(tmp_path)
    assert len(result.skills) == 1
    assert len(result.errors) == 1
    assert isinstance(result.errors[0], SkillLoadError)


def test_load_skills_name_mismatch_caught(tmp_path: Path):
    d = tmp_path / "actual_name"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\n"
        "name: different_name\n"
        "description: x\n"
        "when_to_use:\n"
        "  - x\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    result = load_skills(tmp_path)
    assert result.skills == []
    assert len(result.errors) == 1
    assert "doesn't match" in result.errors[0].error


# ─── render_skill_index ─────────────────────────────────────────────


def test_render_empty_returns_empty(tmp_path: Path):
    assert render_skill_index([]) == ""


def test_render_includes_all_skills_under_budget(tmp_path: Path):
    _write_skill(tmp_path, "skill_a")
    _write_skill(tmp_path, "skill_b")
    _write_skill(tmp_path, "skill_c")
    result = load_skills(tmp_path)
    out = render_skill_index(result.skills, token_budget=10000)
    assert "skill_a" in out
    assert "skill_b" in out
    assert "skill_c" in out
    assert "SKILLS AVAILABLE" in out


def test_render_truncates_when_over_budget(tmp_path: Path):
    # Generate 20 skills; render with a tiny budget.
    for i in range(20):
        _write_skill(tmp_path, f"skill_{i:02d}")
    result = load_skills(tmp_path)
    out = render_skill_index(result.skills, token_budget=200)
    # Truncation: not all 20 names should appear.
    names_present = sum(1 for i in range(20) if f"skill_{i:02d}" in out)
    assert names_present < 20
    # Empty-string return is acceptable when even one skill won't fit.
    if out:
        assert "SKILLS AVAILABLE" in out
