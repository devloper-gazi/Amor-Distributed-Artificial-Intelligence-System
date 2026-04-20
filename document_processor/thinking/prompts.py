"""
Prompts for every Thinking Mode phase.

All phase prompts request **JSON-only output** so the engine can feed the
result downstream without brittle regex parsing. Each prompt ends with a
strict schema the model must satisfy and a reminder not to wrap the JSON in
prose or Markdown fences.
"""

from __future__ import annotations

from textwrap import dedent
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# System prompt — persona shared across all phases
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = dedent(
    """
    You are Amor's "Thinking" engine — a senior staff-level engineer who
    approaches complex, specific problems the way a thoughtful expert would:

      1. You never guess at requirements. When a request is ambiguous, you
         ask precise, high-leverage clarifying questions before committing
         to an answer.
      2. You reason in structured phases (understand → decompose → explore
         alternatives → decide → synthesize → critique), and each phase
         produces a concrete, inspectable artifact.
      3. You explicitly state assumptions and constraints so the user can
         correct you cheaply.
      4. You consider trade-offs, not just "best" answers. Your
         recommendations come with the cost you're paying for them.
      5. You are terse where terseness helps and detailed where details
         matter. You never pad.
      6. When asked for code or an architecture, you produce something a
         teammate could actually run or review — not a hand-wavy sketch.

    Whenever a phase requires JSON, return **only** the JSON object — no
    prose, no Markdown fences, no commentary. Any deviation breaks the
    pipeline.
    """
).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 0 — "Analyze": should we ask clarifying questions?
# ─────────────────────────────────────────────────────────────────────────────

def analyze_prompt(user_prompt: str, deliverable_hint: str) -> str:
    return dedent(
        f"""
        A user sent the following request:
        ---
        {user_prompt.strip()}
        ---

        Decide whether this request is complex/specific enough that we
        should ask the user 2–5 clarifying questions BEFORE attempting an
        answer. Simple questions ("what is docker?") do not need
        clarification; concrete build/design requests ("build me a
        real-time chat", "pick a database for X") almost always do.

        Additionally, classify the intended deliverable so the downstream
        pipeline can tune its output. The user hinted: "{deliverable_hint}".

        Output a single JSON object with this exact schema:

        {{
          "complexity": "trivial" | "moderate" | "complex" | "expert",
          "needs_clarification": boolean,
          "rationale": "ONE sentence explaining the choice, max 140 chars",
          "detected_deliverable":
              "plan" | "architecture" | "code" | "analysis" |
              "decision" | "explanation",
          "questions": [
            {{
              "id": "stable-kebab-case-slug",
              "question": "the question as the user will read it",
              "why_it_matters": "ONE sentence rationale, shown under the question",
              "suggestions": ["short quick-pick", "another", "another"],
              "input_type": "text" | "choice" | "number" | "multiline",
              "placeholder": "optional hint text or null",
              "required": false
            }}
          ]
        }}

        Rules for questions:
          • Ask at most 5. Fewer is better — pick the highest-leverage ones.
          • Each question must concretely change the final answer.
          • Never ask about the user's name, emotional state, or preferences
            that are irrelevant to the technical decision.
          • Provide 3–5 `suggestions` for every question so the user can
            answer in one click. Suggestions must be short (≤ 40 chars).
          • Use `input_type: "choice"` when suggestions are truly exhaustive
            (e.g. yes/no), otherwise `"text"` or `"multiline"`.
          • `id` must be lowercase-kebab-case, unique within the set.

        If `needs_clarification` is false, `questions` must be an empty
        array. Remember: JSON only, no Markdown.
        """
    ).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Understand
# ─────────────────────────────────────────────────────────────────────────────


def _format_clarifications(clarifications: Dict[str, str]) -> str:
    if not clarifications:
        return "(no clarifications provided — infer sensible defaults and call them out)"
    lines = []
    for qid, answer in clarifications.items():
        answer = (answer or "").strip() or "(no answer)"
        lines.append(f"  • {qid}: {answer}")
    return "\n".join(lines)


def understand_prompt(user_prompt: str, clarifications: Dict[str, str]) -> str:
    return dedent(
        f"""
        The user asked:
        ---
        {user_prompt.strip()}
        ---

        They also answered our clarifying questions:
        {_format_clarifications(clarifications)}

        Restate the problem in your own words, then list what you believe
        are the hard constraints, the soft preferences, and the assumptions
        you're going to make.

        Return JSON:

        {{
          "restatement": "one tight paragraph (<= 80 words) in your own words",
          "constraints": ["hard constraint 1", "..."],
          "preferences": ["soft preference 1", "..."],
          "assumptions": ["assumption 1 (call out WHY you're making it)", "..."],
          "unknowns": ["anything still unclear that could flip the answer"]
        }}

        Keep each list to at most 6 items. Prefer specificity over length.
        """
    ).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Decompose
# ─────────────────────────────────────────────────────────────────────────────


def decompose_prompt(
    user_prompt: str,
    clarifications: Dict[str, str],
    understanding_json: Dict[str, Any],
) -> str:
    return dedent(
        f"""
        Original request:
        ---
        {user_prompt.strip()}
        ---

        Your own restatement & constraints:
        {understanding_json}

        Break the problem into the 3–7 sub-questions you will need to
        answer in sequence to solve it. Each sub-question should be
        actionable — something you can research or reason about in
        isolation.

        Return JSON:

        {{
          "sub_questions": [
            {{
              "index": 1,
              "question": "concrete sub-question",
              "why": "1 sentence on what this unlocks"
            }},
            ...
          ]
        }}
        """
    ).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Explore alternatives
# ─────────────────────────────────────────────────────────────────────────────


def explore_prompt(
    user_prompt: str,
    understanding_json: Dict[str, Any],
    sub_questions: List[Dict[str, Any]],
) -> str:
    return dedent(
        f"""
        Original request:
        ---
        {user_prompt.strip()}
        ---

        Restated understanding & constraints:
        {understanding_json}

        Sub-questions to address:
        {sub_questions}

        Propose 2–4 distinct *approaches* that could solve this problem.
        Each approach should be a genuinely different strategy (not
        variations of the same idea). For each one, name it, describe it
        in 1–2 sentences, list concrete pros, concrete cons, and the risk
        category.

        Return JSON:

        {{
          "alternatives": [
            {{
              "id": "kebab-case-slug",
              "name": "short human name",
              "summary": "1–2 sentences describing the approach",
              "pros": ["concrete pro", "..."],
              "cons": ["concrete con", "..."],
              "best_when": "1 sentence on when this wins",
              "risk": "low" | "medium" | "high",
              "effort": "low" | "medium" | "high"
            }},
            ...
          ]
        }}
        """
    ).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — Evaluate & decide
# ─────────────────────────────────────────────────────────────────────────────


def evaluate_prompt(
    user_prompt: str,
    understanding_json: Dict[str, Any],
    alternatives: List[Dict[str, Any]],
) -> str:
    return dedent(
        f"""
        Given the problem, the constraints, and the candidate approaches,
        pick the single best approach and explain why it wins relative to
        the others. Be direct — do not hedge.

        Original request:
        ---
        {user_prompt.strip()}
        ---

        Constraints & preferences:
        {understanding_json}

        Candidate approaches:
        {alternatives}

        Return JSON:

        {{
          "chosen_id": "id of the winning alternative",
          "justification": "2–4 sentences on why this wins given the constraints",
          "key_trade_offs": ["what we give up to get this"],
          "confidence": 0-100,
          "would_reconsider_if": ["condition that would change the answer"]
        }}
        """
    ).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5 — Synthesize deliverable
# ─────────────────────────────────────────────────────────────────────────────


_DELIVERABLE_GUIDE = {
    "plan": (
        "A step-by-step execution plan with numbered stages, each with a "
        "concrete exit criterion."
    ),
    "architecture": (
        "A system design with a component list, data flow, key interfaces, "
        "and a deployment shape. Include a simple ASCII or Mermaid diagram "
        "if it helps."
    ),
    "code": (
        "A concrete, runnable code skeleton (language inferred from the "
        "request). Include file paths, function signatures, and real logic "
        "— not TODO comments."
    ),
    "analysis": (
        "A written analysis organized by sub-question, each with the "
        "reasoning and the answer."
    ),
    "decision": (
        "A crisp recommendation memo: TL;DR, context, options considered, "
        "decision, rollback plan."
    ),
    "explanation": (
        "A clear, well-structured explanation using analogies where they "
        "help, but never at the cost of precision."
    ),
}


def synthesize_prompt(
    user_prompt: str,
    understanding_json: Dict[str, Any],
    alternatives: List[Dict[str, Any]],
    decision: Dict[str, Any],
    deliverable: str,
) -> str:
    guide = _DELIVERABLE_GUIDE.get(deliverable, _DELIVERABLE_GUIDE["explanation"])
    return dedent(
        f"""
        Write the final deliverable. The kind of deliverable is
        "{deliverable}", which means: {guide}

        Original request:
        ---
        {user_prompt.strip()}
        ---

        Restated problem & constraints:
        {understanding_json}

        All alternatives considered:
        {alternatives}

        Chosen approach:
        {decision}

        Output format: GitHub-Flavored Markdown. No JSON envelope — just
        the deliverable itself. Structure it with headers, code fences
        where appropriate, and bullet/numbered lists. Lead with a short
        TL;DR section.

        Quality bar:
          • Every claim a reviewer would question must be justified.
          • No TODOs, no placeholders, no "left as exercise".
          • Keep it self-contained — the user should be able to act on it
            without re-asking questions.
        """
    ).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6 — Self-critique
# ─────────────────────────────────────────────────────────────────────────────


def critique_prompt(
    user_prompt: str,
    understanding_json: Dict[str, Any],
    decision: Dict[str, Any],
    deliverable_markdown: str,
) -> str:
    return dedent(
        f"""
        You just produced the deliverable below. Now put on your skeptic
        hat and red-team it.

        Original request:
        ---
        {user_prompt.strip()}
        ---

        Chosen approach:
        {decision}

        Deliverable:
        ---
        {deliverable_markdown}
        ---

        Return JSON:

        {{
          "risks": [
            {{"title": "short risk name", "detail": "1–2 sentences", "severity": "low"|"medium"|"high"}}
          ],
          "open_questions": ["thing a reviewer would still want to clarify"],
          "next_steps": ["concrete next action the user could take"],
          "confidence": 0-100
        }}
        """
    ).strip()
