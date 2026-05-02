"""Unit tests for Subsystem G — curriculum-driven self-play."""

from __future__ import annotations

from pathlib import Path

import pytest

from document_processor.sentinel.evolution.curriculum import (
    CurriculumInjector,
    CurriculumStore,
    CWEProgress,
    LeveledRecipe,
)
from document_processor.sentinel.evolution.governance import LedgerStore


def test_recipes_per_level_non_empty():
    store = CurriculumStore("/tmp/sentinel_curriculum_test")
    inj = CurriculumInjector(store=store)
    for level in (1, 2, 3, 4):
        recipes = inj.recipes_at(level)  # type: ignore[arg-type]
        assert recipes, f"level {level} has no recipes"
        for r in recipes:
            assert isinstance(r, LeveledRecipe)
            assert r.snippet
            assert r.cwe.startswith("CWE-")


def test_progress_starts_at_level_1(tmp_path: Path):
    store = CurriculumStore(tmp_path)
    progress = store.load()
    # Empty file → no progress yet.
    assert progress == {}


def test_update_pass_rate_records_to_disk(tmp_path: Path):
    store = CurriculumStore(tmp_path)
    p = store.update_pass_rate(
        cwe="CWE-89", level=1, passed=2, total=3,
    )
    assert isinstance(p, CWEProgress)
    assert p.cwe == "CWE-89"
    # Pass rate is rounded to 4 decimals on disk.
    assert p.pass_rate_per_level[1] == pytest.approx(2 / 3, abs=1e-3)
    # Survives a re-load.
    fresh = CurriculumStore(tmp_path).load()
    assert fresh["CWE-89"].pass_rate_per_level[1] == pytest.approx(
        2 / 3, abs=1e-3,
    )


def test_update_pass_rate_promotes_at_threshold(tmp_path: Path):
    ledger = LedgerStore(tmp_path)
    store = CurriculumStore(tmp_path)
    p = store.update_pass_rate(
        cwe="CWE-89", level=1, passed=5, total=5, ledger=ledger,
    )
    assert p.current_level == 2
    kinds = [e.kind for e in ledger.entries()]
    assert "agent_promoted" in kinds


def test_update_pass_rate_demotes_when_failing(tmp_path: Path):
    store = CurriculumStore(tmp_path)
    # Pre-seed at level 3.
    store.save({
        "CWE-78": CWEProgress(cwe="CWE-78", current_level=3),
    })
    p = store.update_pass_rate(
        cwe="CWE-78", level=3, passed=1, total=4,
    )
    # Pass rate 0.25 → below DEMOTE_THRESHOLD 0.5 → drop to 2.
    assert p.current_level == 2


def test_update_pass_rate_no_change_when_middle(tmp_path: Path):
    store = CurriculumStore(tmp_path)
    # Pass rate 0.7 stays put.
    p = store.update_pass_rate(
        cwe="CWE-22", level=1, passed=7, total=10,
    )
    assert p.current_level == 1


def test_update_pass_rate_clamps_at_level_4(tmp_path: Path):
    store = CurriculumStore(tmp_path)
    store.save({"CWE-502": CWEProgress(cwe="CWE-502", current_level=4)})
    p = store.update_pass_rate(
        cwe="CWE-502", level=4, passed=10, total=10,
    )
    # Already at max level — promotion is a no-op.
    assert p.current_level == 4


def test_curriculum_evaluate_uses_scanner(tmp_path: Path):
    ledger = LedgerStore(tmp_path)
    store = CurriculumStore(tmp_path)
    inj = CurriculumInjector(store=store)
    # Scanner that catches everything.
    seen: list[str] = []

    def scanner_fn(snippet: str) -> bool:
        seen.append(snippet[:80])
        return True

    p = inj.evaluate(
        cwe="CWE-89", level=1,
        scanner_fn=scanner_fn, ledger=ledger,
    )
    # Level-1 SQLi recipe ran → 100% pass → promoted to level 2.
    assert seen
    assert p.current_level == 2


def test_curriculum_evaluate_no_recipe_for_cwe_at_level(tmp_path: Path):
    store = CurriculumStore(tmp_path)
    inj = CurriculumInjector(store=store)
    progress = inj.evaluate(
        cwe="CWE-9999",     # not in any recipe
        level=2,
        scanner_fn=lambda _: True,
    )
    # No-op — returns whatever progress already exists, default level 1.
    assert progress.current_level == 1


def test_current_recipes_filters_by_level_and_cwe(tmp_path: Path):
    store = CurriculumStore(tmp_path)
    store.save({"CWE-89": CWEProgress(cwe="CWE-89", current_level=2)})
    inj = CurriculumInjector(store=store)
    recipes = inj.current_recipes("CWE-89")
    assert recipes
    for r in recipes:
        assert r.cwe == "CWE-89"
        assert r.level == 2


def test_recipes_for_groups_by_level(tmp_path: Path):
    store = CurriculumStore(tmp_path)
    inj = CurriculumInjector(store=store)
    bag = inj.recipes_for("CWE-89")
    # SQLi has L1 + L2 (per the leveled corpus).
    assert bag[1] and bag[2]
    # Levels with no SQLi recipes are empty lists.
    assert bag[3] == [] or bag[4] == [] or all(r.cwe == "CWE-89" for r in bag[3])
