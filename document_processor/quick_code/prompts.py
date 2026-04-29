"""
QuickCode Mode — system + user prompt templates.

The engine speaks to the LLM at three points:
  1. ``REASONING_*``   — propose 2-3 distinct approaches, score each
                         on clarity / math soundness / performance /
                         edge-case coverage, return strict JSON.
  2. (implementation + refinement reuse the existing CoderAgent /
     DebuggerAgent prompts from ``code_intelligence.prompts``)
  3. ``DELIVERABLE_*`` — synthesize the final markdown bundle text
                         (chosen alternative + rationale + verification
                         summary).
"""

from __future__ import annotations

# ─── Reasoning phase ──────────────────────────────────────────────────────────


REASONING_SYSTEM_PROMPT = """You are a code reasoning agent.

Before any code is written you propose 2 or 3 *distinct* approaches to
the user's task and score each on four axes (each 0..1):

  - clarity         readability, naming, surface area
  - math_soundness  numerical stability, big-O correctness, proof structure
  - performance     runtime / memory under realistic input sizes
  - edge_cases      coverage of nulls, boundaries, error paths

Return STRICT JSON, no prose, no markdown fences:

{
  "alternatives": [
    {
      "label": "A",
      "summary": "<=200 chars one-line description of the approach",
      "scores": {
        "clarity": 0.0,
        "math_soundness": 0.0,
        "performance": 0.0,
        "edge_cases": 0.0
      },
      "complexity_estimate": "O(n log n)",
      "perf_notes": "<=200 chars",
      "edge_cases": ["bullet 1", "bullet 2"]
    }
  ],
  "chosen": "A",
  "rationale": "<=160 words explaining the trade-off between alternatives"
}

Rules:
  • Always emit at least 2 alternatives unless the task admits exactly one.
  • Scores are floats in [0,1] — never use percentages.
  • Pick the highest-scoring alternative under your weighting; the
    engine recomputes a 0.30/0.30/0.20/0.20 composite locally and may
    override your `chosen` field.
  • Keep `summary` and `perf_notes` short — they go straight into
    SSE events and a markdown report.
"""


def reasoning_prompt(
    user_prompt: str,
    code_context: str | None = None,
    triage: dict | None = None,
) -> str:
    """Build the reasoning user prompt.

    Includes the user's request, any pre-existing code context, and
    the triage hint (language / complexity) so the model doesn't waste
    tokens classifying again.
    """
    rows: list[str] = ["# Task", user_prompt.strip()]
    if triage:
        lang = triage.get("language") or "python"
        complexity = triage.get("complexity") or "moderate"
        task_type = triage.get("task_type") or "generation"
        rows.append("\n# Triage hints")
        rows.append(f"- language: {lang}")
        rows.append(f"- complexity: {complexity}")
        rows.append(f"- task_type: {task_type}")
    if code_context and code_context.strip():
        rows.append("\n# Existing code (context only — do not rewrite verbatim)")
        rows.append("```")
        rows.append(code_context.strip()[:6000])
        rows.append("```")
    rows.append(
        "\n# Output\n"
        "Return the JSON described in the system prompt. No prose, no fences."
    )
    return "\n".join(rows)


# ─── Deliverable synthesis ────────────────────────────────────────────────────


DELIVERABLE_SYSTEM_PROMPT = (
    "You write concise technical READMEs in markdown. "
    "Section headings are short (Title Case). "
    "Code references stay in fenced blocks. No emoji decoration."
)


def deliverable_prompt(
    user_prompt: str,
    chosen_summary: str,
    rationale: str,
    code_chars: int,
    tests_chars: int,
    verification_summary: str,
) -> str:
    """Optional final synthesis call. The engine may skip this and
    build the markdown deterministically — kept here for parity with
    the consortium's deliverable_markdown pattern."""
    return (
        f"# Task\n{user_prompt.strip()}\n\n"
        f"# Chosen approach\n{chosen_summary}\n\n"
        f"# Rationale\n{rationale}\n\n"
        f"# Verification\n{verification_summary}\n\n"
        f"# Stats\n- code: {code_chars} chars\n- tests: {tests_chars} chars\n\n"
        "Write a 200-400 word markdown summary suitable for a project README's "
        "'Implementation' section. Plain prose, no bullets unless they help."
    )
