"""Unit tests for Subsystem D — rule synthesis."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from document_processor.sentinel.evolution.governance import (
    LedgerStore,
    load_immutable_constraints,
)
from document_processor.sentinel.evolution.rule_synthesis import (
    FindingExample,
    RuleStore,
    RuleSynthesizer,
    ShadowMetric,
    SynthesizedRule,
    group_examples_by_pattern,
    render_examples_for_prompt,
    shadow_validate,
    strip_yaml_fences,
)


def _run(coro):
    return asyncio.run(coro)


def _make_examples(cwe: str, language: str, n: int) -> list[FindingExample]:
    return [
        FindingExample(
            cwe=cwe, language=language,
            file=f"f{i}.py", line=i + 1,
            snippet=f"snippet {i}", rule_id="X",
        )
        for i in range(n)
    ]


# ─── grouping ────────────────────────────────────────────────────────


def test_group_examples_drops_small_buckets():
    items = (
        _make_examples("CWE-89", "python", 5)
        + _make_examples("CWE-79", "javascript", 2)
    )
    groups = group_examples_by_pattern(items, min_group_size=3)
    cwes = [g[0].cwe for g in groups]
    assert "CWE-89" in cwes
    assert "CWE-79" not in cwes


def test_group_examples_returns_one_per_pattern():
    items = (
        _make_examples("CWE-89", "python", 4)
        + _make_examples("CWE-89", "javascript", 4)
        + _make_examples("CWE-22", "python", 4)
    )
    groups = group_examples_by_pattern(items, min_group_size=3)
    assert len(groups) == 3


# ─── prompt rendering + fence stripping ─────────────────────────────


def test_render_examples_truncates_to_max():
    items = _make_examples("CWE-89", "python", 12)
    text = render_examples_for_prompt(items, max_examples=3)
    assert "Example 3" in text
    assert "Example 4" not in text


def test_strip_yaml_fences_handles_fenced():
    raw = "```yaml\nrules:\n  - id: x\n```"
    assert "rules:" in strip_yaml_fences(raw)


def test_strip_yaml_fences_returns_input_when_no_fence():
    assert strip_yaml_fences("rules: []") == "rules: []"


# ─── shadow_validate ─────────────────────────────────────────────────


def test_shadow_validate_perfect_precision_recall():
    confirmed = _make_examples("CWE-89", "python", 3)
    historical = list(confirmed) + _make_examples("CWE-79", "javascript", 5)

    def predicate(f: FindingExample) -> bool:
        return f.cwe == "CWE-89"

    metric = shadow_validate(
        rule_id="r-1",
        matches_predicate=predicate,
        historical_findings=historical,
        confirmed_true_positives=confirmed,
    )
    assert metric.precision == 1.0
    assert metric.recall == 1.0
    assert metric.matches == 3


def test_shadow_validate_low_precision_when_overshooting():
    confirmed = _make_examples("CWE-89", "python", 1)
    historical = _make_examples("CWE-89", "python", 4)  # 4 hits, 1 confirmed

    def predicate(f: FindingExample) -> bool:
        return True   # fires on every finding

    metric = shadow_validate(
        rule_id="r-2",
        matches_predicate=predicate,
        historical_findings=historical,
        confirmed_true_positives=confirmed,
    )
    # Precision based on which hits matched a confirmed key.
    # Only the one confirmed example matches → precision = 1/4.
    assert metric.precision == pytest.approx(0.25)


def test_shadow_validate_recall_misses():
    confirmed = _make_examples("CWE-89", "python", 5)
    historical = list(confirmed)

    def predicate(f: FindingExample) -> bool:
        # Only fires on the first confirmed example.
        return f.line == 1

    metric = shadow_validate(
        rule_id="r-3",
        matches_predicate=predicate,
        historical_findings=historical,
        confirmed_true_positives=confirmed,
    )
    # 1 TP out of 5 confirmed → recall = 0.2
    assert metric.recall == pytest.approx(0.2)


# ─── RuleStore round-trip ────────────────────────────────────────────


def test_rule_store_promote_moves_files(tmp_path: Path):
    store = RuleStore(tmp_path)
    rule = SynthesizedRule(
        rule_id="r1", cwe="CWE-89", language="python",
        yaml="rules: []", source_examples=4,
        status="staging", created_at=1.0,
    )
    store.write(rule)
    assert any(p.name == "r1.yaml"
               for p in (tmp_path / "synthesized_rules" / "staging").glob("*"))
    store.promote(rule)
    # Now in production, gone from staging.
    assert any(p.name == "r1.yaml"
               for p in (tmp_path / "synthesized_rules" / "production").glob("*"))
    assert not any(p.name == "r1.yaml"
                   for p in (tmp_path / "synthesized_rules" / "staging").glob("*"))


def test_rule_store_archive_removes_old_status(tmp_path: Path):
    store = RuleStore(tmp_path)
    rule = SynthesizedRule(
        rule_id="r2", cwe="CWE-79", language="javascript",
        yaml="rules: []", source_examples=3,
        status="production", created_at=1.0, promoted_at=2.0,
    )
    store.write(rule)
    store.archive(rule)
    archived = store.list_status("archived")
    assert any(r.rule_id == "r2" for r in archived)


# ─── RuleSynthesizer end-to-end (mocked LLM) ────────────────────────


class _FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response

    async def __call__(self, prompt, system, max_tokens):
        return self.response


def test_synthesize_for_group_promotes_when_metrics_pass(tmp_path: Path):
    store = RuleStore(tmp_path)
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()

    examples = _make_examples("CWE-89", "python", 4)
    historical = list(examples) + _make_examples("CWE-79", "javascript", 6)

    def predicate(f: FindingExample) -> bool:
        return f.cwe == "CWE-89"

    yaml_payload = (
        "rules:\n"
        "  - id: sentinel.cwe_89.python\n"
        "    pattern: \"$EXEC = $X + $Y\"\n"
        "    severity: ERROR\n"
        "    metadata:\n"
        "      cwe: CWE-89\n"
        "      description: SQL injection via string concat\n"
    )
    syn = RuleSynthesizer(store=store, ledger=ledger, constraints=constraints)
    rule = _run(syn.synthesize_for_group(
        examples,
        llm=_FakeLLM(yaml_payload),
        matches_predicate=predicate,
        historical_findings=historical,
        confirmed_true_positives=examples,
    ))
    assert rule is not None
    assert rule.status == "production"
    kinds = [e.kind for e in ledger.entries()]
    assert "rule_synthesized" in kinds
    assert "rule_promoted" in kinds


def test_synthesize_for_group_keeps_staging_when_low_precision(tmp_path: Path):
    store = RuleStore(tmp_path)
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()

    examples = _make_examples("CWE-89", "python", 3)
    # The shadow validation uses a predicate that fires on EVERYTHING,
    # making precision low (1/N).
    historical = list(examples) + _make_examples("CWE-79", "javascript", 20)

    def predicate(f: FindingExample) -> bool:
        return True

    syn = RuleSynthesizer(store=store, ledger=ledger, constraints=constraints)
    rule = _run(syn.synthesize_for_group(
        examples,
        llm=_FakeLLM("rules: []"),
        matches_predicate=predicate,
        historical_findings=historical,
        confirmed_true_positives=examples,
    ))
    assert rule is not None
    assert rule.status == "staging"  # not promoted


def test_synthesize_for_group_blocks_violating_yaml(tmp_path: Path):
    """If the LLM emits forbidden phrases, the rule is rejected via
    constraint check + a constraint_check_failed ledger entry."""
    store = RuleStore(tmp_path)
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()

    examples = _make_examples("CWE-89", "python", 3)

    def predicate(f: FindingExample) -> bool:
        return True

    syn = RuleSynthesizer(store=store, ledger=ledger, constraints=constraints)
    rule = _run(syn.synthesize_for_group(
        examples,
        llm=_FakeLLM("rules:\n  - pattern: 'rm -rf /'\n"),
        matches_predicate=predicate,
        historical_findings=examples,
        confirmed_true_positives=examples,
    ))
    assert rule is None
    kinds = [e.kind for e in ledger.entries()]
    assert "constraint_check_failed" in kinds


def test_retire_underperforming_archives_old_rule(tmp_path: Path):
    store = RuleStore(tmp_path)
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    syn = RuleSynthesizer(store=store, ledger=ledger, constraints=constraints)

    # An existing production rule that has been below the floor for
    # 70 days.
    import time
    old = SynthesizedRule(
        rule_id="r-old", cwe="CWE-89", language="python",
        yaml="rules: []", source_examples=3,
        status="production", created_at=0.0, promoted_at=0.0,
        last_seen_precision=0.5,
        last_seen_at=time.time() - 70 * 86400,
    )
    store.write(old)

    retired = syn.retire_underperforming(retirement_days=60)
    rule_ids = [r.rule_id for r in retired]
    assert "r-old" in rule_ids
    archived = store.list_status("archived")
    assert any(r.rule_id == "r-old" for r in archived)
