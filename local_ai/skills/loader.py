"""
Cycle F Sprint 4 — SKILL.md discoverer + frontmatter parser.

Stdlib-only parser.  AMOR's runtime container doesn't ship PyYAML by
default and the frontmatter we accept is restricted to a strict
subset (flat keys + lists-of-strings), so a manual parser is both
smaller and safer than dragging in PyYAML.

Public surface:

  load_skills(root)            -> list[Skill]
  render_skill_index(skills, budget) -> str
  Skill                        # dataclass with frontmatter + body
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from .schema import SkillFrontmatter, estimate_frontmatter_tokens


logger = logging.getLogger(__name__)


# ─── Skill dataclass ────────────────────────────────────────────────


@dataclass
class Skill:
    """One loaded skill — frontmatter + body + filesystem location."""

    frontmatter: SkillFrontmatter
    body: str
    path: Path
    scripts_dir: Path | None = None  # optional executable-templates root

    @property
    def name(self) -> str:
        return self.frontmatter.name


@dataclass
class SkillLoadError:
    """Captures one SKILL.md that failed to parse / validate."""

    path: Path
    error: str


@dataclass
class SkillLoadResult:
    skills: list[Skill] = field(default_factory=list)
    errors: list[SkillLoadError] = field(default_factory=list)


# ─── Frontmatter parser (manual mini-YAML) ──────────────────────────


_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<fm>.*?)\n---\s*\n(?P<body>.*)$",
    re.DOTALL,
)

_KEY_RE = re.compile(r"^(?P<key>[a-z_][a-z0-9_]*)\s*:\s*(?P<rest>.*)$")
_LIST_ITEM_RE = re.compile(r"^\s+-\s+(?P<value>.+)$")


def _parse_frontmatter_block(text: str) -> dict:
    """Parse a YAML-like frontmatter block.

    Supports:
      key: value
      key: |
        multi-line body  (not actually used by SkillFrontmatter, but
                          tolerated for forward compat)
      key:
        - item
        - item

    Raises ValueError on malformed input.  Doesn't try to be PyYAML;
    the schema is intentionally minimal.
    """

    out: dict = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        # Comment lines (full-line only — we don't strip trailing # comments)
        if line.lstrip().startswith("#"):
            continue
        # List continuation
        m_item = _LIST_ITEM_RE.match(line)
        if m_item and current_list is not None:
            value = m_item.group("value").strip()
            # Strip surrounding quotes if present.
            value = _strip_quotes(value)
            current_list.append(value)
            continue
        # New key
        m_key = _KEY_RE.match(line)
        if not m_key:
            raise ValueError(f"frontmatter parse error at line: {line!r}")
        key = m_key.group("key")
        rest = m_key.group("rest").strip()
        if not rest:
            # Open a list block.
            current_list = []
            out[key] = current_list
            current_key = key
        else:
            current_list = None
            current_key = key
            out[key] = _strip_quotes(rest)
    return out


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


# ─── Discovery ─────────────────────────────────────────────────────


def _load_one(skill_dir: Path) -> Skill | SkillLoadError:
    """Load + validate one SKILL.md, returning either a Skill or an
    error record so the boot loop can collect partial failures."""

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return SkillLoadError(path=skill_md, error="SKILL.md missing")

    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return SkillLoadError(path=skill_md, error=f"read failed: {exc}")

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return SkillLoadError(
            path=skill_md,
            error="no `---` frontmatter delimiter pair at file head",
        )

    try:
        fm_dict = _parse_frontmatter_block(match.group("fm"))
    except ValueError as exc:
        return SkillLoadError(path=skill_md, error=f"frontmatter: {exc}")

    try:
        fm = SkillFrontmatter(**fm_dict)
    except ValidationError as exc:
        return SkillLoadError(
            path=skill_md,
            error=f"schema validation: {exc.errors()[:3]}",
        )

    # name MUST match the directory name — keeps the planner's name
    # references unambiguous.
    if fm.name != skill_dir.name:
        return SkillLoadError(
            path=skill_md,
            error=(
                f"frontmatter name {fm.name!r} doesn't match directory "
                f"{skill_dir.name!r}"
            ),
        )

    body = match.group("body").strip()
    scripts = skill_dir / "scripts"
    return Skill(
        frontmatter=fm,
        body=body,
        path=skill_md,
        scripts_dir=scripts if scripts.is_dir() else None,
    )


def load_skills(root: Path | str) -> SkillLoadResult:
    """Walk `root/*/SKILL.md`, return all parsed skills + per-file errors."""

    root = Path(root)
    result = SkillLoadResult()
    if not root.is_dir():
        # Empty root is fine; deployments without skills behave like
        # Sprint 3 (no skill index injection).
        return result

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        outcome = _load_one(entry)
        if isinstance(outcome, Skill):
            result.skills.append(outcome)
        else:
            logger.warning(
                "skill_load_failed path=%s err=%s", outcome.path, outcome.error,
            )
            result.errors.append(outcome)
    return result


# ─── Render skill index for planner prompt ──────────────────────────


def render_skill_index(
    skills: Iterable[Skill],
    *,
    token_budget: int = 2000,
) -> str:
    """Produce the `SKILLS AVAILABLE:` block for the planner system
    prompt.  Truncates by ascending estimated token cost when the
    total exceeds `token_budget` — most-expensive skills drop first
    (rare; defensive for >50-skill libraries).

    Returns "" when no skills loaded (planner reverts to Sprint 3
    behaviour with no skill awareness).
    """

    skills = list(skills)
    if not skills:
        return ""

    # Pre-compute token estimates.
    weighted = [
        (estimate_frontmatter_tokens(s.frontmatter), s) for s in skills
    ]
    # Sort by ascending cost — we keep cheap-and-many over costly-and-few.
    weighted.sort(key=lambda t: t[0])

    chosen: list[Skill] = []
    total = 0
    # Reserve ~100 tokens for the header + footer.
    overhead = 100
    for cost, sk in weighted:
        if total + cost + overhead > token_budget:
            break
        chosen.append(sk)
        total += cost

    if not chosen:
        return ""

    lines: list[str] = [
        "SKILLS AVAILABLE: when the user's request maps to one of",
        "these skills, call `load_skill(name)` and follow the loaded",
        "body's guidance.  Activation is OPT-IN; if none fit, fall",
        "through to the regular plan.",
        "",
    ]
    # Stable order in the rendered block: alphabetical by name.
    for sk in sorted(chosen, key=lambda s: s.name):
        fm = sk.frontmatter
        lines.append(f"- {fm.name}: {fm.description}")
        if fm.when_to_use:
            triggers = "; ".join(fm.when_to_use[:3])
            lines.append(f"    triggers: {triggers}")
        if fm.languages:
            lines.append(f"    languages: {', '.join(fm.languages)}")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "Skill",
    "SkillLoadError",
    "SkillLoadResult",
    "load_skills",
    "render_skill_index",
]
