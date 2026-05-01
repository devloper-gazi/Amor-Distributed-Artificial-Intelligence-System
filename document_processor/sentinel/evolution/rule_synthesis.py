"""
Sentinel Evolution — Subsystem D: detection-rule synthesis.

When the user marks ≥ N similar findings as true positives we ask
``RuleWriter`` (the existing Patcher LLM with a swapped system
prompt) to generate a Semgrep YAML rule + an optional tree-sitter
query that captures the pattern.  The rule then runs through:

1. **Constraint check** — output is screened by
   ``ImmutableConstraints`` so no rule can ship with a forbidden
   phrase / external URL / protected-file target.
2. **Shadow validation** — the rule is replayed against the past
   30/60/90 days of historical findings; we measure precision
   (rule hits that match a confirmed TP) and recall (TPs the rule
   would have caught).
3. **Promotion gate** — `precision >= 0.9 AND recall >= 0.5` →
   move into ``synthesized_rules/production/``.  Otherwise stays
   in ``staging`` for the user to review.
4. **Auto-retire** — production rules whose precision drops below
   `0.7` for `retirement_days` (default 60) get archived.

License: MIT.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from .governance import (
    HardConstraintViolation,
    ImmutableConstraints,
    LedgerStore,
)


logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]


RULEWRITER_SYSTEM_PROMPT = (
    "You are an expert detection engineer. Given several confirmed "
    "vulnerability findings, write a SINGLE Semgrep YAML rule that "
    "matches the same pattern across the same language.\n\n"
    "Rules:\n"
    "- Output only the YAML rule, no prose, no markdown fences.\n"
    "- Use the standard Semgrep schema with `rules:` at the top.\n"
    "- Pattern must be specific enough to avoid false positives "
    "(target the underlying bug, not just the surface tokens).\n"
    "- Set `severity` to one of WARNING / ERROR.\n"
    "- Add `metadata.cwe` and a short `metadata.description`.\n"
    "- Use `pattern-either` if multiple variants exist.\n"
)


@dataclass
class FindingExample:
    """Minimal shape of a confirmed finding the synthesiser groups
    + writes a rule for."""
    cwe: str
    language: str
    file: str
    line: int
    snippet: str
    rule_id: str = ""
    raw_message: str = ""


@dataclass
class SynthesizedRule:
    rule_id: str
    cwe: str
    language: str
    yaml: str
    source_examples: int
    status: str = "staging"      # staging | production | archived
    eval_metrics: dict[str, float] = field(default_factory=dict)
    created_at: float = 0.0
    promoted_at: float | None = None
    last_seen_precision: float | None = None
    last_seen_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# Grouping
# ─────────────────────────────────────────────────────────────────────


def group_examples_by_pattern(
    examples: Iterable[FindingExample],
    *,
    min_group_size: int = 3,
) -> list[list[FindingExample]]:
    """Group confirmed findings by ``(cwe, language)``.  Drop any
    group with fewer than ``min_group_size`` examples."""
    buckets: dict[tuple[str, str], list[FindingExample]] = {}
    for ex in examples:
        key = (ex.cwe or "UNKNOWN", ex.language or "any")
        buckets.setdefault(key, []).append(ex)
    return [g for g in buckets.values() if len(g) >= max(1, int(min_group_size))]


# ─────────────────────────────────────────────────────────────────────
# Rule writer (LLM)
# ─────────────────────────────────────────────────────────────────────


def render_examples_for_prompt(
    examples: Iterable[FindingExample],
    *,
    max_examples: int = 6,
    snippet_chars: int = 600,
) -> str:
    rows: list[str] = []
    for i, ex in enumerate(list(examples)[:max_examples], start=1):
        rows.append(f"### Example {i}")
        rows.append(f"- CWE: {ex.cwe}  Language: {ex.language}")
        rows.append(f"- File: {ex.file}  Line: {ex.line}")
        rows.append(f"- Tool message: {ex.raw_message[:240]}")
        rows.append("```")
        rows.append(ex.snippet[:snippet_chars])
        rows.append("```")
        rows.append("")
    return "\n".join(rows)


_FENCE_RE = re.compile(r"```(?:yaml|yml)?\s*(.*?)\s*```", re.S | re.I)


def strip_yaml_fences(text: str) -> str:
    if not text:
        return ""
    m = _FENCE_RE.search(text)
    return (m.group(1) if m else text).strip()


async def synthesize_rule_yaml(
    *,
    examples: list[FindingExample],
    llm: LLMCall,
    max_tokens: int = 1200,
) -> str:
    if not examples:
        return ""
    user_prompt = render_examples_for_prompt(examples)
    raw = await llm(user_prompt, RULEWRITER_SYSTEM_PROMPT, max_tokens)
    return strip_yaml_fences(raw or "")


# ─────────────────────────────────────────────────────────────────────
# Shadow validation
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ShadowMetric:
    rule_id: str
    matches: int = 0
    true_positives: int = 0
    false_positives: int = 0
    missed_true_positives: int = 0

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return (self.true_positives / denom) if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.missed_true_positives
        return (self.true_positives / denom) if denom else 0.0


def shadow_validate(
    rule_id: str,
    *,
    matches_predicate: Callable[[FindingExample], bool],
    historical_findings: Iterable[FindingExample],
    confirmed_true_positives: Iterable[FindingExample],
) -> ShadowMetric:
    """`matches_predicate(finding)` returns True iff our newly
    synthesised rule fires on the finding's snippet.

    Precision: of the rule's hits, how many were confirmed TP?
    Recall: of all confirmed TPs, how many would the rule catch?
    """
    metric = ShadowMetric(rule_id=rule_id)
    confirmed_set = {
        (f.file, f.line, f.cwe, f.snippet[:200])
        for f in confirmed_true_positives
    }
    for f in historical_findings:
        try:
            fired = bool(matches_predicate(f))
        except Exception:
            fired = False
        if fired:
            metric.matches += 1
            key = (f.file, f.line, f.cwe, f.snippet[:200])
            if key in confirmed_set:
                metric.true_positives += 1
            else:
                metric.false_positives += 1

    # Missed TPs: confirmed TPs the rule did NOT fire on.
    matched_keys: set[tuple] = set()
    for f in historical_findings:
        try:
            if matches_predicate(f):
                matched_keys.add((f.file, f.line, f.cwe, f.snippet[:200]))
        except Exception:
            continue
    for ctp in confirmed_true_positives:
        key = (ctp.file, ctp.line, ctp.cwe, ctp.snippet[:200])
        if key not in matched_keys:
            metric.missed_true_positives += 1
    return metric


# ─────────────────────────────────────────────────────────────────────
# RuleStore — staging / production / archived
# ─────────────────────────────────────────────────────────────────────


class RuleStore:
    """Disk layout:

        synthesized_rules/
        ├── staging/<rule_id>.yaml
        ├── production/<rule_id>.yaml
        └── archived/<rule_id>.yaml
    """

    STATUS_DIRS = ("staging", "production", "archived")

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "synthesized_rules"
        for sub in self.STATUS_DIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def write(self, rule: SynthesizedRule) -> Path:
        target_dir = self.root / rule.status
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{rule.rule_id}.yaml"
        try:
            import yaml  # type: ignore
            text = yaml.safe_dump(rule.to_dict(), sort_keys=True)
        except Exception:
            text = json.dumps(rule.to_dict(), indent=2, default=str)
        path.write_text(text, encoding="utf-8")
        return path

    def promote(self, rule: SynthesizedRule) -> None:
        old_status = rule.status
        rule.status = "production"
        rule.promoted_at = time.time()
        self.write(rule)
        if old_status == "staging":
            self._delete(rule.rule_id, "staging")

    def archive(self, rule: SynthesizedRule) -> None:
        old_status = rule.status
        rule.status = "archived"
        self.write(rule)
        for status in ("staging", "production"):
            if status != old_status:
                continue
            self._delete(rule.rule_id, status)

    def list_status(self, status: str) -> list[SynthesizedRule]:
        d = self.root / status
        out: list[SynthesizedRule] = []
        for p in sorted(d.glob("*.yaml")):
            out.append(self._load(p))
        return out

    def production_rules(self) -> list[SynthesizedRule]:
        return self.list_status("production")

    def _delete(self, rule_id: str, status: str) -> None:
        path = self.root / status / f"{rule_id}.yaml"
        if path.is_file():
            path.unlink()

    def _load(self, path: Path) -> SynthesizedRule:
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        return SynthesizedRule(
            rule_id=str(data.get("rule_id") or path.stem),
            cwe=str(data.get("cwe") or ""),
            language=str(data.get("language") or ""),
            yaml=str(data.get("yaml") or ""),
            source_examples=int(data.get("source_examples") or 0),
            status=str(data.get("status") or "staging"),
            eval_metrics=dict(data.get("eval_metrics") or {}),
            created_at=float(data.get("created_at") or 0.0),
            promoted_at=data.get("promoted_at"),
            last_seen_precision=data.get("last_seen_precision"),
            last_seen_at=data.get("last_seen_at"),
        )


# ─────────────────────────────────────────────────────────────────────
# RuleSynthesizer — orchestrator
# ─────────────────────────────────────────────────────────────────────


class RuleSynthesizer:
    PROMOTION_PRECISION = 0.9
    PROMOTION_RECALL = 0.5
    RETIREMENT_PRECISION = 0.7
    RETIREMENT_DAYS = 60

    def __init__(
        self,
        *,
        store: RuleStore,
        ledger: LedgerStore,
        constraints: ImmutableConstraints,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.constraints = constraints

    async def synthesize_for_group(
        self,
        examples: list[FindingExample],
        *,
        llm: LLMCall,
        matches_predicate: Callable[[FindingExample], bool],
        historical_findings: Iterable[FindingExample],
        confirmed_true_positives: Iterable[FindingExample],
        promotion_precision: float | None = None,
        promotion_recall: float | None = None,
    ) -> SynthesizedRule | None:
        """Run the full synth → constrain → shadow-validate →
        promote loop on a single group."""
        if not examples:
            return None
        first = examples[0]
        rule_id = (
            f"sentinel.{first.cwe.replace('-', '_').lower() or 'unknown'}"
            f".{first.language or 'any'}.{int(time.time())}"
        )
        try:
            yaml_text = await synthesize_rule_yaml(
                examples=examples, llm=llm,
            )
        except Exception as exc:
            logger.debug("rule synthesis llm failed: %s", exc)
            return None
        if not yaml_text.strip():
            return None

        # Constraint check first.  Reject obviously-bad output.
        try:
            self.constraints.check({
                "yaml": yaml_text,
                "rule_id": rule_id,
            })
        except HardConstraintViolation as exc:
            self.ledger.append(
                actor="rule_synthesis",
                kind="constraint_check_failed",
                payload={"rule_id": rule_id, "reason": str(exc)},
            )
            return None

        rule = SynthesizedRule(
            rule_id=rule_id,
            cwe=first.cwe,
            language=first.language,
            yaml=yaml_text,
            source_examples=len(examples),
            status="staging",
            created_at=time.time(),
        )
        self.store.write(rule)
        self.ledger.append(
            actor="rule_synthesis",
            kind="rule_synthesized",
            payload={
                "rule_id": rule_id, "cwe": first.cwe,
                "language": first.language,
                "source_examples": len(examples),
            },
        )

        # Shadow validation.
        metric = shadow_validate(
            rule_id=rule_id,
            matches_predicate=matches_predicate,
            historical_findings=historical_findings,
            confirmed_true_positives=confirmed_true_positives,
        )
        rule.eval_metrics = {
            "matches": metric.matches,
            "true_positives": metric.true_positives,
            "false_positives": metric.false_positives,
            "missed_true_positives": metric.missed_true_positives,
            "precision": metric.precision,
            "recall": metric.recall,
        }
        self.store.write(rule)

        prom_p = (promotion_precision
                  if promotion_precision is not None
                  else self.PROMOTION_PRECISION)
        prom_r = (promotion_recall
                  if promotion_recall is not None
                  else self.PROMOTION_RECALL)
        if metric.precision >= prom_p and metric.recall >= prom_r:
            self.store.promote(rule)
            self.ledger.append(
                actor="rule_synthesis",
                kind="rule_promoted",
                payload={
                    "rule_id": rule_id,
                    "metrics": rule.eval_metrics,
                },
            )
        return rule

    def retire_underperforming(
        self,
        *,
        rules: Iterable[SynthesizedRule] | None = None,
        retirement_precision: float | None = None,
        retirement_days: float | None = None,
    ) -> list[SynthesizedRule]:
        """Walk the production set + archive any rule whose
        last_seen_precision has been below the floor for more than
        ``retirement_days``."""
        floor = (retirement_precision
                 if retirement_precision is not None
                 else self.RETIREMENT_PRECISION)
        days = (retirement_days
                if retirement_days is not None
                else self.RETIREMENT_DAYS)
        cutoff = time.time() - (days * 86400)
        rules = list(rules) if rules is not None else self.store.production_rules()
        retired: list[SynthesizedRule] = []
        for r in rules:
            seen_p = r.last_seen_precision
            seen_at = r.last_seen_at
            if seen_p is None or seen_at is None:
                continue
            if seen_p < floor and seen_at < cutoff:
                self.store.archive(r)
                self.ledger.append(
                    actor="rule_synthesis",
                    kind="rule_retired",
                    payload={
                        "rule_id": r.rule_id,
                        "last_seen_precision": seen_p,
                        "days_below_floor": (time.time() - seen_at) / 86400,
                    },
                )
                retired.append(r)
        return retired


__all__ = [
    "FindingExample",
    "RULEWRITER_SYSTEM_PROMPT",
    "RuleStore",
    "RuleSynthesizer",
    "ShadowMetric",
    "SynthesizedRule",
    "group_examples_by_pattern",
    "render_examples_for_prompt",
    "shadow_validate",
    "strip_yaml_fences",
    "synthesize_rule_yaml",
]
