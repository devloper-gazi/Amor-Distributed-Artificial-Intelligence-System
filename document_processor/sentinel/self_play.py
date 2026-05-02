"""
Sentinel — self-play / RLAIF-lite loop (V1).

Two pieces:

1. **SyntheticInjector** — given clean code, inject a CWE pattern
   (SQL injection, hardcoded key, eval, weak-crypto, etc.).  Output
   is a (vuln_code, expected_finding) tuple the engine can run a
   self-test on (`amor sentinel self-test`).
2. **DebateRunner** — when Auditor and RedTeam disagree on a
   finding, run up to ``max_turns`` rounds of argumentation.  Each
   side gets to rebut the other.  Judge then picks the winner.

These are opt-in entry points (off by default) — running
``DebateRunner.run()`` only happens when ``settings.
sentinel_self_play_enabled`` is True OR the user asks for the
``paranoid`` scan profile.

License: MIT.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .agents import AuditorAgent, JudgeAgent, RedTeamAgent
from .models import AgentVerdict, Finding, RAGContext

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# SyntheticInjector
# ─────────────────────────────────────────────────────────────────────


@dataclass
class InjectionRecipe:
    """One canned vulnerability we can inject into clean code."""
    cwe: str
    name: str
    language: str
    pattern_start: re.Pattern[str]
    snippet: str
    severity: str = "high"


_RECIPES: tuple[InjectionRecipe, ...] = (
    InjectionRecipe(
        cwe="CWE-89", name="SQL Injection (concat)",
        language="python",
        pattern_start=re.compile(r"^def\s+\w+", re.M),
        snippet=(
            "\n    # SYNTHETIC-INJECT CWE-89 SQLi\n"
            "    query = \"SELECT * FROM users WHERE id = '\" + user_id + \"'\"\n"
            "    cursor.execute(query)\n"
        ),
    ),
    InjectionRecipe(
        cwe="CWE-798", name="Hardcoded API Key",
        language="python",
        pattern_start=re.compile(r"^(import|from|#)", re.M),
        snippet="\n# SYNTHETIC-INJECT CWE-798 hardcoded credential\n"
                "OPENAI_KEY = \"sk-AAAA1234567890BBBBCCCCDDDDEEEEFFFFGGGGHHHHIIII\"\n",
    ),
    InjectionRecipe(
        cwe="CWE-94", name="eval() of user input",
        language="python",
        pattern_start=re.compile(r"^def\s+\w+", re.M),
        snippet=(
            "\n    # SYNTHETIC-INJECT CWE-94 code injection\n"
            "    expr = input('expr: ')\n"
            "    return eval(expr)\n"
        ),
    ),
    InjectionRecipe(
        cwe="CWE-78", name="OS command injection",
        language="python",
        pattern_start=re.compile(r"^def\s+\w+", re.M),
        snippet=(
            "\n    # SYNTHETIC-INJECT CWE-78 command injection\n"
            "    import os\n"
            "    os.system('echo ' + user_input)\n"
        ),
    ),
    InjectionRecipe(
        cwe="CWE-502", name="pickle.loads of user input",
        language="python",
        pattern_start=re.compile(r"^def\s+\w+", re.M),
        snippet=(
            "\n    # SYNTHETIC-INJECT CWE-502 unsafe deserialization\n"
            "    import pickle\n"
            "    return pickle.loads(payload)\n"
        ),
    ),
)


@dataclass
class InjectionResult:
    cwe: str
    name: str
    expected_severity: str
    original_code: str
    injected_code: str
    injection_marker: str
    recipe_index: int
    line_inserted_after: int


class SyntheticInjector:
    """Injects canned CWE patterns into clean code for self-test."""

    @property
    def recipes(self) -> tuple[InjectionRecipe, ...]:
        return _RECIPES

    def inject(
        self,
        clean_code: str,
        *,
        recipe_index: int = 0,
    ) -> InjectionResult | None:
        if not clean_code:
            return None
        if recipe_index < 0 or recipe_index >= len(_RECIPES):
            return None
        recipe = _RECIPES[recipe_index]
        # Find a hook line and insert after it.
        match = recipe.pattern_start.search(clean_code)
        if not match:
            return None
        # Insert at end of the matched line.
        line_end = clean_code.find("\n", match.end())
        if line_end < 0:
            line_end = len(clean_code)
        injected = clean_code[: line_end + 1] + recipe.snippet + clean_code[line_end + 1 :]
        marker = f"SYNTHETIC-INJECT {recipe.cwe}"
        return InjectionResult(
            cwe=recipe.cwe,
            name=recipe.name,
            expected_severity=recipe.severity,
            original_code=clean_code,
            injected_code=injected,
            injection_marker=marker,
            recipe_index=recipe_index,
            line_inserted_after=clean_code[: line_end].count("\n") + 1,
        )

    def inject_all(self, clean_code: str) -> list[InjectionResult]:
        out: list[InjectionResult] = []
        for i in range(len(_RECIPES)):
            r = self.inject(clean_code, recipe_index=i)
            if r is not None:
                out.append(r)
        return out

    @staticmethod
    def evaluate_findings(
        injection: InjectionResult,
        findings: list[Finding],
    ) -> dict[str, Any]:
        """Did the engine catch the injected CWE?"""
        catches = [f for f in findings if f.cwe == injection.cwe]
        on_inserted_line = [
            f for f in catches
            if abs(f.line_start - injection.line_inserted_after) <= 5
        ]
        return {
            "expected_cwe": injection.cwe,
            "found": bool(catches),
            "found_on_correct_line": bool(on_inserted_line),
            "match_count": len(catches),
            "evidence": [
                {"tool": f.tool, "line": f.line_start, "severity": f.severity}
                for f in catches[:10]
            ],
        }


# ─────────────────────────────────────────────────────────────────────
# DebateRunner
# ─────────────────────────────────────────────────────────────────────


@dataclass
class DebateTurn:
    speaker: str             # "auditor" or "redteam"
    statement: str
    confidence: float


@dataclass
class DebateResult:
    finding: Finding
    turns: list[DebateTurn] = field(default_factory=list)
    final_verdict: str = "needs_more_context"
    final_confidence: float = 0.0
    judge_rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_fingerprint": self.finding.fingerprint,
            "cwe": self.finding.cwe,
            "file": self.finding.file,
            "line": self.finding.line_start,
            "turns": [
                {"speaker": t.speaker, "statement": t.statement,
                 "confidence": t.confidence}
                for t in self.turns
            ],
            "final_verdict": self.final_verdict,
            "final_confidence": self.final_confidence,
            "judge_rationale": self.judge_rationale,
        }


class DebateRunner:
    """Auditor ↔ RedTeam structured debate, capped at ``max_turns``."""

    def __init__(
        self,
        *,
        auditor: AuditorAgent,
        redteam: RedTeamAgent,
        judge: JudgeAgent,
        max_turns: int = 5,
    ) -> None:
        self._auditor = auditor
        self._redteam = redteam
        self._judge = judge
        self._max_turns = max(2, min(7, int(max_turns)))

    @property
    def max_turns(self) -> int:
        return self._max_turns

    async def run(
        self,
        *,
        finding: Finding,
        initial_auditor: AgentVerdict,
        initial_redteam: AgentVerdict,
        context: RAGContext,
        code_excerpt: str = "",
    ) -> DebateResult:
        """Only fires when initial verdicts disagree.  When they
        agree, returns a 0-turn DebateResult immediately."""
        result = DebateResult(finding=finding)

        # Quick agreement check — both assert true_positive /
        # exploitable / true_positive / false_positive consistently.
        agreed = self._verdicts_agree(initial_auditor, initial_redteam)
        if agreed:
            result.final_verdict = initial_auditor.verdict
            result.final_confidence = round(
                (initial_auditor.confidence + initial_redteam.confidence) / 2.0,
                4,
            )
            result.judge_rationale = "agents already agree; debate skipped"
            return result

        # Seed the debate with the two opening statements.
        result.turns.append(DebateTurn(
            speaker="auditor",
            statement=initial_auditor.rationale[:600] or initial_auditor.verdict,
            confidence=initial_auditor.confidence,
        ))
        result.turns.append(DebateTurn(
            speaker="redteam",
            statement=initial_redteam.exploit_scenario[:600]
                      or initial_redteam.rationale[:600]
                      or initial_redteam.verdict,
            confidence=initial_redteam.confidence,
        ))

        # Up to max_turns - 2 rebuttal rounds (each round = 1 audit + 1 redteam).
        rebuttals = max(0, self._max_turns - 2) // 2
        for _ in range(rebuttals):
            try:
                # Fresh audit informed by the current debate transcript.
                fresh_audit_list = await self._auditor.audit(
                    finding=finding,
                    context=context,
                    code_excerpt=code_excerpt + "\n# DEBATE TRANSCRIPT\n"
                                 + _format_transcript(result.turns),
                )
                fresh_audit = AuditorAgent.majority_verdict(fresh_audit_list)
                result.turns.append(DebateTurn(
                    speaker="auditor",
                    statement=fresh_audit.rationale[:600],
                    confidence=fresh_audit.confidence,
                ))
            except Exception as exc:  # pragma: no cover
                logger.debug("debate auditor turn failed: %s", exc)
                break

            try:
                fresh_redteam = await self._redteam.attack(
                    finding=finding, context=context,
                    code_excerpt=code_excerpt + "\n# DEBATE TRANSCRIPT\n"
                                 + _format_transcript(result.turns),
                )
                result.turns.append(DebateTurn(
                    speaker="redteam",
                    statement=(fresh_redteam.exploit_scenario[:600]
                               or fresh_redteam.rationale[:600]),
                    confidence=fresh_redteam.confidence,
                ))
            except Exception as exc:  # pragma: no cover
                logger.debug("debate redteam turn failed: %s", exc)
                break

        # Judge synthesises.
        try:
            judge_verdict = await self._judge.synthesize(
                finding=finding,
                auditor_results=[
                    {"verdict": t.speaker, "confidence": t.confidence,
                     "rationale": t.statement}
                    for t in result.turns if t.speaker == "auditor"
                ],
                reasoner_result=None,
                redteam_result={
                    "verdict": "exploitable",
                    "confidence": max(
                        (t.confidence for t in result.turns if t.speaker == "redteam"),
                        default=0.0,
                    ),
                    "exploit_scenario": "; ".join(
                        t.statement for t in result.turns if t.speaker == "redteam"
                    )[:1200],
                },
            )
            result.final_verdict = judge_verdict.verdict
            result.final_confidence = judge_verdict.confidence
            result.judge_rationale = judge_verdict.rationale[:800]
        except Exception as exc:  # pragma: no cover
            logger.debug("debate judge synthesis failed: %s", exc)
            result.final_verdict = "needs_more_context"
            result.judge_rationale = f"judge error: {type(exc).__name__}"
        return result

    @staticmethod
    def _verdicts_agree(a: AgentVerdict, b: AgentVerdict) -> bool:
        positive = {"true_positive", "exploitable", "approved"}
        negative = {"false_positive", "not_exploitable", "rejected"}
        if a.verdict in positive and b.verdict in positive:
            return True
        if a.verdict in negative and b.verdict in negative:
            return True
        return False


def _format_transcript(turns: list[DebateTurn]) -> str:
    return "\n".join(
        f"[{t.speaker} conf={t.confidence:.2f}] {t.statement[:240]}"
        for t in turns
    )


__all__ = [
    "DebateResult",
    "DebateRunner",
    "DebateTurn",
    "InjectionRecipe",
    "InjectionResult",
    "SyntheticInjector",
]
