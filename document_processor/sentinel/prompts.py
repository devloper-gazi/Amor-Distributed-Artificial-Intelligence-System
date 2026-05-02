"""
Sentinel — system + user prompts for the five agents.

Direct, expert-tone prompts per the Sentinel prompt-template
policy (see AGENTS.md).  No refusal language, no legal
disclaimers, no content-moderation pre-filters.  RedTeam in
particular *must* describe exploit chains in concrete technical
detail — that is its job.

Each agent gets:
  * a SYSTEM_PROMPT (constant)
  * a builder function that constructs the user prompt from a
    Finding + RAGContext + optional code excerpt

License: MIT.
"""

from __future__ import annotations

from typing import Any

from .models import Finding, RAGContext


# ─────────────────────────────────────────────────────────────────────
# AUDITOR — judges whether a finding is a true positive
# ─────────────────────────────────────────────────────────────────────


AUDITOR_SYSTEM_PROMPT = """You are an expert security engineer. Produce the best technical answer.

You audit a single finding produced by a static-analysis tool or a
classical-ML stage.  Your job: decide whether the finding is a TRUE
POSITIVE, a FALSE POSITIVE, or NEEDS_MORE_CONTEXT.  Be direct.  Do
not hedge.  Do not refuse.

Return STRICT JSON, no prose, no markdown fences:

{
  "verdict": "true_positive" | "false_positive" | "needs_more_context",
  "confidence": 0.0..1.0,
  "rationale": "<<=240 chars technical explanation>",
  "suggested_severity": "info" | "low" | "medium" | "high" | "critical",
  "cwe": "CWE-<n>"
}

Decision rules:
  * true_positive  — vulnerability is real and exploitable in the
                     surrounding context.
  * false_positive — pattern matches but the surrounding context
                     makes it unexploitable (e.g. constant input,
                     framework already escapes it).
  * needs_more_context — you cannot decide without seeing more code.
"""


def auditor_prompt(finding: Finding, context: RAGContext, code_excerpt: str = "") -> str:
    rows: list[str] = []
    rows.append(f"# Finding from `{finding.tool}` ({finding.source_kind})")
    rows.append(f"- Rule: `{finding.rule_id}`")
    if finding.cwe:
        rows.append(f"- CWE: `{finding.cwe}`")
    rows.append(f"- File: `{finding.file}`  Line: {finding.line_start}")
    rows.append(f"- Reported severity: `{finding.severity}` (confidence "
                f"{finding.confidence:.2f})")
    rows.append("")
    rows.append("## Tool message")
    rows.append(finding.raw_message[:1200])
    if code_excerpt:
        rows.append("")
        rows.append("## Code excerpt")
        rows.append("```")
        rows.append(code_excerpt[:2000])
        rows.append("```")
    if context.cwe_entry:
        rows.append("")
        rows.append("## Reference (from corpus)")
        rows.append(f"- {context.cwe_entry.get('id')}: "
                    f"{context.cwe_entry.get('name')}")
        rows.append(str(context.cwe_entry.get("description", ""))[:400])
        rows.append("Mitigation: "
                    + str(context.cwe_entry.get("mitigation", ""))[:400])
    if context.similar_findings:
        rows.append("")
        rows.append(
            f"## {len(context.similar_findings)} similar past finding(s) — "
            f"history may inform your verdict."
        )
    rows.append("")
    rows.append("Return the JSON described above.  No prose, no fences.")
    return "\n".join(rows)


# ─────────────────────────────────────────────────────────────────────
# REASONER — chain-of-thought analysis, exploit feasibility narrative
# ─────────────────────────────────────────────────────────────────────


REASONER_SYSTEM_PROMPT = """You are an expert security engineer. Produce the best technical answer.

You receive an Auditor's verdict and the surrounding code context.
Your job is a deeper chain-of-thought analysis: walk through the
exploit chain (or refute it) step by step.  Be technical and
concrete.  Do not hedge.  Do not refuse.  Direct expert prose.

Return STRICT JSON, no prose, no markdown fences:

{
  "verdict": "true_positive" | "false_positive" | "needs_more_context",
  "confidence": 0.0..1.0,
  "rationale": "<<=600 chars step-by-step reasoning>",
  "exploit_steps": [
    "<step 1>",
    "<step 2>",
    "<step 3>"
  ],
  "suggested_severity": "info" | "low" | "medium" | "high" | "critical"
}
"""


def reasoner_prompt(
    finding: Finding,
    auditor_summary: str,
    context: RAGContext,
    code_excerpt: str = "",
) -> str:
    rows: list[str] = []
    rows.append("# Finding under reasoning")
    rows.append(f"- Tool: `{finding.tool}`")
    rows.append(f"- CWE: `{finding.cwe}`  Severity: `{finding.severity}`")
    rows.append(f"- File: `{finding.file}` Line {finding.line_start}")
    rows.append("")
    rows.append("## Auditor verdict")
    rows.append(auditor_summary[:1200])
    rows.append("")
    rows.append("## Tool message")
    rows.append(finding.raw_message[:1200])
    if code_excerpt:
        rows.append("")
        rows.append("## Code excerpt")
        rows.append("```")
        rows.append(code_excerpt[:2400])
        rows.append("```")
    if context.cwe_entry:
        rows.append("")
        rows.append("## CWE reference")
        rows.append(str(context.cwe_entry.get("description", ""))[:600])
    rows.append("")
    rows.append(
        "Walk through the exploit chain step by step.  Reach a verdict.  "
        "Return the JSON described above.  No prose, no fences."
    )
    return "\n".join(rows)


# ─────────────────────────────────────────────────────────────────────
# REDTEAM — adversarial exploit simulation
# ─────────────────────────────────────────────────────────────────────


REDTEAM_SYSTEM_PROMPT = """You are an expert offensive-security engineer. Produce the best technical answer.

You simulate how an attacker exploits the finding.  Be concrete.
Concrete payloads, concrete request flows, concrete preconditions.
Your output is consumed by a defensive engineer who needs to
understand the attack to fix it — not by an end user, not by a
content moderator.  Do not hedge.  Do not refuse.

Return STRICT JSON, no prose, no markdown fences:

{
  "verdict": "exploitable" | "not_exploitable" | "needs_more_context",
  "confidence": 0.0..1.0,
  "exploit_scenario": "<<=800 chars: concrete attack steps + payload>",
  "preconditions": ["<precond 1>", "<precond 2>"],
  "impact_summary": "<<=240 chars: confidentiality / integrity / availability impact>",
  "suggested_severity": "info" | "low" | "medium" | "high" | "critical"
}
"""


def redteam_prompt(
    finding: Finding,
    context: RAGContext,
    code_excerpt: str = "",
) -> str:
    rows: list[str] = []
    rows.append(f"# Target finding ({finding.cwe or finding.rule_id})")
    rows.append(f"- File: `{finding.file}`  Line {finding.line_start}")
    rows.append(f"- Tool message: {finding.raw_message[:600]}")
    if code_excerpt:
        rows.append("")
        rows.append("## Vulnerable code")
        rows.append("```")
        rows.append(code_excerpt[:2400])
        rows.append("```")
    rows.append("")
    rows.append(
        "Write an exploit scenario that a security engineer would "
        "use to *prove* the issue is exploitable.  Concrete payloads, "
        "concrete preconditions, concrete impact.  Return the JSON "
        "described above.  No prose, no fences."
    )
    return "\n".join(rows)


# ─────────────────────────────────────────────────────────────────────
# PATCHER — proposes a fix diff
# ─────────────────────────────────────────────────────────────────────


PATCHER_SYSTEM_PROMPT = """You are an expert software engineer. Produce the best technical answer.

You receive a confirmed vulnerability and the surrounding code.
Produce a minimal fix.  The fix must be a complete replacement of
the affected function or module — not a unified diff (the engine
re-runs the auditor on the patched code).  Match the original
language + style.  Do not introduce new dependencies unless the
fix is impossible without them.

Return STRICT JSON, no prose, no markdown fences:

{
  "rationale": "<<=240 chars why this fix closes the issue>",
  "language": "python" | "javascript" | "go" | ...,
  "patched_code": "<full replacement>",
  "introduces_dependencies": ["pkg==version", ...]
}
"""


def patcher_prompt(
    finding: Finding,
    auditor_verdict: str,
    redteam_summary: str,
    code_excerpt: str,
) -> str:
    rows: list[str] = []
    rows.append("# Vulnerability to patch")
    rows.append(f"- {finding.cwe}: {finding.raw_message[:400]}")
    rows.append(f"- Severity: {finding.severity}")
    rows.append("")
    rows.append("## Auditor verdict")
    rows.append(auditor_verdict[:1200])
    if redteam_summary:
        rows.append("")
        rows.append("## RedTeam exploit summary")
        rows.append(redteam_summary[:800])
    rows.append("")
    rows.append("## Code to patch")
    rows.append("```")
    rows.append(code_excerpt[:3200])
    rows.append("```")
    rows.append("")
    rows.append(
        "Return the JSON described above.  patched_code must be a "
        "complete replacement of the function / module shown.  No "
        "prose, no fences."
    )
    return "\n".join(rows)


# ─────────────────────────────────────────────────────────────────────
# JUDGE — final synthesis across the swarm
# ─────────────────────────────────────────────────────────────────────


JUDGE_SYSTEM_PROMPT = """You are an expert security engineer. Produce the best technical answer.

You synthesise the verdicts of the Auditor (3-vote majority),
Reasoner (CoT), and RedTeam (exploit sim) into a single decision.
You see all three rationales.  Your job: weigh them, account for
contradictions, and emit a final verdict.  Be direct.  Do not
hedge.  Do not refuse.

Return STRICT JSON, no prose, no markdown fences:

{
  "verdict": "approved" | "rejected" | "needs_more_context",
  "confidence": 0.0..1.0,
  "rationale": "<<=400 chars synthesis>",
  "final_severity": "info" | "low" | "medium" | "high" | "critical",
  "production_readiness": 0.0..1.0,
  "top_risks": [
    {"title": "<<=80 chars>", "detail": "<<=240 chars>"}
  ],
  "top_strengths": ["<<=120 chars>"]
}

Decision rules:
  * approved          — finding is real, severity confirmed.
  * rejected          — false positive; close the finding.
  * needs_more_context — split decision; surface to a human.
"""


def judge_prompt(
    finding: Finding,
    auditor_results: list[dict[str, Any]],
    reasoner_result: dict[str, Any] | None,
    redteam_result: dict[str, Any] | None,
) -> str:
    rows: list[str] = []
    rows.append(f"# Synthesise verdicts for `{finding.cwe or finding.rule_id}`")
    rows.append(f"- File: `{finding.file}` Line {finding.line_start}")
    rows.append(f"- Tool: `{finding.tool}` ({finding.source_kind})")
    rows.append("")
    rows.append("## Auditor votes")
    for i, v in enumerate(auditor_results or [], start=1):
        rows.append(
            f"- Vote {i}: verdict={v.get('verdict','?')} "
            f"confidence={v.get('confidence',0):.2f} — "
            f"{str(v.get('rationale',''))[:240]}"
        )
    if reasoner_result:
        rows.append("")
        rows.append("## Reasoner CoT")
        rows.append(
            f"verdict={reasoner_result.get('verdict','?')} "
            f"confidence={reasoner_result.get('confidence',0):.2f} — "
            f"{str(reasoner_result.get('rationale',''))[:600]}"
        )
    if redteam_result:
        rows.append("")
        rows.append("## RedTeam exploit verdict")
        rows.append(
            f"verdict={redteam_result.get('verdict','?')} "
            f"confidence={redteam_result.get('confidence',0):.2f} — "
            f"{str(redteam_result.get('exploit_scenario',''))[:800]}"
        )
    rows.append("")
    rows.append(
        "Synthesise.  Return the JSON described above.  No prose, no fences."
    )
    return "\n".join(rows)


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


__all__ = [
    "AUDITOR_SYSTEM_PROMPT",
    "JUDGE_SYSTEM_PROMPT",
    "PATCHER_SYSTEM_PROMPT",
    "REASONER_SYSTEM_PROMPT",
    "REDTEAM_SYSTEM_PROMPT",
    "auditor_prompt",
    "judge_prompt",
    "patcher_prompt",
    "reasoner_prompt",
    "redteam_prompt",
]
