# Sprint 4 v18 — Anthropic Agent Skills loader runbook

> Cycle F Sprint 4 — `./skills/` directory with agentskills.io-format
> SKILL.md files.  Frontmatter (≤200 tokens per skill) lives in the
> planner system prompt; bodies (≤2000 tokens) load via the
> `load_skill(name)` MCP tool on activation.  OFF by default.

## What landed (this sprint)

| Artifact | Path | Purpose |
|---|---|---|
| Settings | `config/settings.py` | `code_skills_enabled=False`, `code_skills_root="skills"`, `code_skills_token_budget=2000` |
| Schema | `local_ai/skills/schema.py` | Pydantic `SkillFrontmatter` + `estimate_frontmatter_tokens` |
| Loader | `local_ai/skills/loader.py` | Stdlib mini-YAML frontmatter parser, walks `skills/*/SKILL.md`, returns `SkillLoadResult` (skills + per-file errors), renders `SKILLS AVAILABLE:` index for planner prompt |
| Activation | `local_ai/skills/activation.py` | `_ACTIVE_SKILL` ContextVar mirroring Sprint 3 `_ACTIVE_ROLE` |
| MCP integration | `local_ai/skills/registry_integration.py` | `LoadSkillTool` (Pydantic-validated `name: str` input) + `register_into(registry, skills)` helper |
| Planner injection | `code_intelligence/prompts.py:planner_prompt` | When `code_skills_enabled=True`, appends `render_skill_index(loaded.skills, token_budget=…)` between domain block + JSON schema |
| Skills shipped (8) | `skills/{snake_game_builder,todo_app,landing_page,dashboard,rest_api_service,cli_tool,data_viz,blog_post}/SKILL.md` | Production-quality specs covering the most common Sprint-0-corpus deliverables |
| Tests | `tests/skills/test_schema.py` (11), `test_loader.py` (16), `test_activation.py` (10), `test_skill_md_files.py` (10) | **47 new tests, all green** |

## Architectural decisions

* **Stdlib-only frontmatter parser.**  AMOR doesn't ship PyYAML.  A
  ~50-line mini-YAML parser handles the flat-key + lists-of-strings
  subset.  Test coverage on the parser is heavy (`test_loader.py
  ::test_parse_*`).
* **Pydantic is the schema.**  No external `jsonschema` lib —
  `SkillFrontmatter` validates name (snake_case 3-64), description
  (one line, ≤200 chars), `when_to_use` (1-10 entries, empty
  strings filtered), `languages` + `must_have_features`
  (normalised).
* **Per-skill error isolation.**  One malformed SKILL.md surfaces as
  a `SkillLoadError` in `result.errors`; the rest still load.
  Boot is best-effort, never aborts.
* **Forward-compat budget enforcement.**  `render_skill_index`
  sorts skills by ascending estimated token cost and packs until
  the budget is hit.  Today: 8 skills total ≈638 tokens — well
  under the 2K budget.  Tomorrow at 50+ skills the truncation rule
  kicks in.
* **Skill directory name MUST match frontmatter `name`.**  Caught
  at load time; prevents drift between
  `settings.code_skills_role_adapters` (Sprint 3 style) and the
  on-disk layout.
* **Body injection deferred to engine wire-up.**  Sprint 4 lands
  the loader + MCP tool + planner index.  Plugging the body into
  the coder system prompt (after `load_skill` activates) is a
  one-line addition in `engine._phase_implement` once the planner
  actually picks a skill from the index — gated by Sprint 5
  approval flow before any auto-activation.

## How to turn it on

```bash
# Set env vars in .env or compose:
echo 'AMOR_CODE_SKILLS_ENABLED=true' >> .env
docker compose restart app

# Verify boot picks up all 8 skills:
python -c "
from local_ai.skills import load_skills
r = load_skills('skills')
print(f'skills={len(r.skills)} errors={len(r.errors)}')
for s in r.skills: print(' ', s.name)
"
# Expected: skills=8 errors=0

# Verify the planner prompt includes the index:
python -c "
from document_processor.code_intelligence.prompts import planner_prompt
from document_processor.config.settings import settings
settings.code_skills_enabled = True
out = planner_prompt('build a snake game', triage={})
print('SKILLS AVAILABLE:' in out)
"
# Expected: True
```

## Adding a new skill

1. Create `skills/<new_name>/SKILL.md`.  Frontmatter MUST include
   `name`, `description`, `when_to_use`.  See existing skills for
   the format.
2. Restart `amor-app` (the loader scans `skills_root` at planner-
   call time, not boot — restart is for safety, not strictly
   required).
3. The new skill auto-appears in the index on the next planner
   call.
4. `pytest tests/skills/test_skill_md_files.py -v` catches any
   schema violations.

## Sprint 4 exit criteria

| # | criterion | status |
|---|---|---|
| 1 | Skill activation precision ≥90% on Sprint-0 "vague prompt" subset | needs live Sprint-0 run with `code_skills_enabled=True` |
| 2 | Deliverable rubric scores improve by ≥2 categories per matched skill | needs the same live run |
| 3 | Total skills + domain templates ≤2000 token budget | **8 skills ≈638 tokens ✓** |
| 4 | CI test sweep delta: +32 tests | **landed +47 tests** |
| 5 | Rollback verified: `code_skills_enabled=false` reverts to Sprint 3 | **verified — planner prompt unchanged when gate off** |

Exit #1 / #2 land at the next live Sprint-0 run with skills enabled
(operator schedule).

## Rollback

| change | rollback |
|---|---|
| Skills index in planner prompt | `AMOR_CODE_SKILLS_ENABLED=false` + restart app |
| `load_skill` MCP tool | unregister via `registry.clear()` or restart with skills disabled |
| Specific skill | Delete `skills/<name>/SKILL.md` (or rename to `.md.bak`) — loader silently drops it |
| Planner prompt format | Revert the `skills_block` insert in `prompts.py:planner_prompt` |

No DB migration, no breaking API.

## Caveats

* **Skills index is added to the PLANNER prompt only.**  Coder
  + Tester + Debugger don't see it directly; they react to
  the planner's structured `plan[]` output.  When the planner
  picks a skill, it MUST surface that decision in its JSON output
  (a `skill: "<name>"` field — convention; not yet enforced by the
  schema).  Adding the engine-side body injection is a Sprint 5
  follow-up after the approval flow lands (so a runaway planner
  can't auto-activate arbitrary skills).
* **`_ACTIVE_SKILL` ContextVar is currently set by the tool
  dispatch path but NOT YET READ by any other module.**  Sprint 5
  will wire it into the system-prompt assembly so the active
  skill's body is appended for the next coder call.
* **Token estimate is a 4-chars-per-token heuristic.**  Accurate
  to within ~15% for English; safe for the budget gate but not
  a substitute for a real BPE tokeniser.
* **Skills are NOT executable (no scripts/) in Sprint 4.**  The
  `scripts_dir` field exists on the `Skill` dataclass but no
  shipped skill uses it yet.  Sprint 5's approval flow gates the
  execution path.

## Wire-shape reference (planner output convention)

When the planner identifies a matching skill from the index, it
SHOULD output (convention; not schema-enforced):

```json
{
  "task_type": "generation",
  "skill": "snake_game_builder",
  "plan": [...],
  ...
}
```

The engine reads the `skill` field, dispatches `load_skill(name=
"snake_game_builder")`, and (Sprint 5+) injects the body into the
coder's next system prompt.

## Test the planner extension manually

```bash
docker exec amor-app-1 python -c "
import os
os.environ['AMOR_CODE_SKILLS_ENABLED'] = 'true'
from document_processor.code_intelligence.prompts import planner_prompt
out = planner_prompt('build a todo app with localStorage')
# Confirm the SKILLS AVAILABLE block is present:
assert 'SKILLS AVAILABLE:' in out
# Confirm todo_app appears with its trigger phrase:
assert 'todo_app' in out
print('planner prompt contains todo_app reference ✓')
"
```
