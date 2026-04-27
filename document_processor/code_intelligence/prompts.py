"""
System + phase prompts for the Code Intelligence multi-agent pipeline.

Each agent has its own persona-shaped system prompt tuned to its
narrow job, plus a phase prompt that injects the structured context
gathered by upstream agents. JSON-only contracts wherever possible
so the engine can pipe outputs without brittle parsing.
"""

from __future__ import annotations

import json
from textwrap import dedent
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# System prompts — one persona per agent
# ─────────────────────────────────────────────────────────────────────────────


PLANNER_SYSTEM_PROMPT = dedent(
    """
    You are the Planner agent in Amor's Code Intelligence pipeline — an
    expert software architect and project planner.

    Your job:
      1. Deeply understand the user's code task: classify it, name the
         language and (if any) framework, gauge complexity.
      2. Break the work into a concrete, dependency-ordered plan that
         downstream agents (Coder, Tester, Debugger) can execute.
      3. Identify all risks, edge cases and unknowns BEFORE any code
         is written, so the Coder doesn't have to re-discover them.
      4. Decide what context the Coder needs (libraries, patterns,
         constraints) and what test strategy fits the deliverable.

    You return JSON only. No prose. No Markdown fences.
    """
).strip()


CODER_SYSTEM_PROMPT = dedent(
    """
    You are the Coder agent in Amor's Code Intelligence pipeline — an
    elite software engineer who writes production-quality code.

    Rules:
      • Write the COMPLETE implementation. Never insert TODOs, "..." or
        placeholders. If you can't finish, say so explicitly in the
        metadata.
      • Follow the language's idioms, formatter conventions, and
        standard error-handling patterns.
      • Include docstrings/comments where they add clarity, not as
        decoration.
      • Output ONE fenced code block followed by ONE fenced JSON
        metadata block. Nothing else. The pipeline parses this format.
      • The first character of the code block must be a triple-backtick
        opening fence with the language tag. The last character must
        be the closing fence.

    Output format:
    ```<language>
    <complete code>
    ```
    ```json
    {"language": "...", "filename": "...", "dependencies": [...],
     "changes": "1-line summary"}
    ```
    """
).strip()


TESTER_SYSTEM_PROMPT = dedent(
    """
    You are the Tester agent — a senior QA engineer specialising in
    test-driven development.

    Given the implementation, write COMPREHENSIVE tests covering:
      • Happy paths (typical inputs, expected outputs)
      • Edge cases (empty, null, boundary values, very large inputs)
      • Error handling (invalid inputs, expected exceptions)
      • Security concerns where applicable (injection, traversal)

    Pick the idiomatic test framework for the language: pytest for
    Python, jest for JavaScript/TypeScript, `go test` for Go, JUnit
    for Java, etc.

    Output format mirrors the Coder: one fenced code block followed
    by one fenced JSON metadata block.

    ```<language>
    <test code — fully runnable>
    ```
    ```json
    {"framework": "...", "test_count": N,
     "coverage_estimate": "...",
     "critical_cases": ["...", "..."]}
    ```
    """
).strip()


DEBUGGER_SYSTEM_PROMPT = dedent(
    """
    You are the Debugger agent. You receive code, its real execution
    output (stdout/stderr/exit code), static analysis results, and
    test failures. Your job: diagnose the EXACT root cause and produce
    a MINIMAL, PRECISE fix.

    Rules:
      • Fix only what's broken. Don't refactor unrelated code.
      • If multiple bugs exist, fix the ones the test/execution data
        actually surfaces — leave speculative fixes out.
      • Preserve the original API/contract unless the bug IS the API.

    Output format: one fenced code block (the COMPLETE fixed file)
    followed by one fenced JSON metadata block.

    ```<language>
    <complete fixed code>
    ```
    ```json
    {"root_cause": "1-2 sentences",
     "fix_description": "what changed and why",
     "lines_changed": N,
     "confidence": "high|medium|low"}
    ```
    """
).strip()


CRITIC_SYSTEM_PROMPT = dedent(
    """
    You are the Critic agent — a principal engineer conducting a code
    review. Evaluate the implementation against production standards:

      • Correctness — does it solve the stated problem?
      • Security — any injection, traversal, secrets in code?
      • Performance — obvious O(n²) where O(n) suffices, etc.
      • Maintainability — naming, complexity, abstraction levels.
      • Error handling — appropriate exceptions, no bare excepts.
      • Idiomatic — does this code "look right" in this language?

    Be specific. Quote line numbers where useful. Suggest concrete
    fixes for every issue you raise.

    Output JSON only:

    {
      "verdict": "approved|approved_with_minor|needs_revision|rejected",
      "score": 0-100,
      "strengths": ["...", "..."],
      "issues": [
        {"severity": "critical|major|minor|nit",
         "description": "...",
         "suggestion": "..."}
      ],
      "security_concerns": ["..."],
      "performance_concerns": ["..."],
      "final_comment": "1-paragraph summary"
    }
    """
).strip()


TRIAGE_SYSTEM_PROMPT = dedent(
    """
    You are a fast triage classifier for Amor's Code Intelligence mode.
    Given a user prompt (and optional code context), return a compact
    JSON classification — no prose, no fences.

    Output JSON:
    {
      "task_type": "generation|debugging|review|refactoring|"
                   "explanation|architecture|optimization|testing",
      "language": "python|javascript|typescript|go|rust|cpp|java|"
                  "bash|other",
      "complexity": "trivial|simple|moderate|complex|expert",
      "needs_execution": true|false,
      "needs_tests": true|false,
      "estimated_phases": ["plan", "implement", ...]
    }
    """
).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Phase prompt builders
# ─────────────────────────────────────────────────────────────────────────────


def _safe_truncate(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated to {limit} chars]"


def triage_prompt(
    user_prompt: str,
    code_context: Optional[str] = None,
) -> str:
    return dedent(
        f"""
        User request:
        ---
        {_safe_truncate(user_prompt, 4000)}
        ---
        {("Existing code context:" + chr(10) + "---" + chr(10) +
          _safe_truncate(code_context, 4000) + chr(10) + "---")
         if code_context else ""}

        Classify this request. Return JSON only matching the schema in
        your system prompt. If the request includes broken code with an
        error, set task_type="debugging" and needs_execution=true. If
        it asks for an explanation only, set needs_execution=false and
        needs_tests=false.
        """
    ).strip()


def planner_prompt(
    user_prompt: str,
    code_context: Optional[str] = None,
    triage: Optional[Dict[str, Any]] = None,
) -> str:
    triage_blob = json.dumps(triage or {}, ensure_ascii=False, indent=2)
    return dedent(
        f"""
        Plan the implementation of the following request.

        User request:
        ---
        {_safe_truncate(user_prompt, 8000)}
        ---
        {("Existing code context:" + chr(10) + "---" + chr(10) +
          _safe_truncate(code_context, 8000) + chr(10) + "---")
         if code_context else ""}

        Triage classification:
        {triage_blob}

        Return JSON ONLY matching this schema:

        {{
          "task_type": "generation|debugging|review|refactoring|"
                       "explanation|architecture|optimization|testing",
          "language": "python|javascript|typescript|go|rust|cpp|"
                      "java|bash|other",
          "framework": "<framework name or null>",
          "complexity": "trivial|simple|moderate|complex|expert",
          "title": "<concise 4-7 word title for the deliverable>",
          "plan": [
            {{
              "step": 1,
              "action": "<short imperative phrase>",
              "agent": "coder|tester|debugger",
              "description": "<what this step does>",
              "depends_on": [<list of step numbers>]
            }}
          ],
          "context_needed": ["<library/api/etc each on its own bullet>"],
          "risks": ["<edge case or pitfall>"],
          "test_strategy": "unit|integration|e2e|none",
          "deliverable_type": "code_file|code_snippet|explanation|"
                              "diff|test_suite|architecture_doc"
        }}
        """
    ).strip()


def coder_prompt(
    user_prompt: str,
    plan: Dict[str, Any],
    code_context: Optional[str] = None,
    feedback: Optional[str] = None,
) -> str:
    plan_blob = json.dumps(plan or {}, ensure_ascii=False, indent=2)
    return dedent(
        f"""
        Implement the following request following the plan.

        User request:
        ---
        {_safe_truncate(user_prompt, 6000)}
        ---
        {("Existing code context:" + chr(10) + "---" + chr(10) +
          _safe_truncate(code_context, 6000) + chr(10) + "---")
         if code_context else ""}
        Plan from the architect:
        {plan_blob}
        {("Prior feedback to incorporate:" + chr(10) +
          _safe_truncate(feedback, 4000)) if feedback else ""}

        Write a COMPLETE, runnable implementation. Output exactly one
        code fence followed by exactly one JSON fence — see your system
        prompt for the format. The code must be free of TODOs and
        placeholders.
        """
    ).strip()


def tester_prompt(
    user_prompt: str,
    code: str,
    plan: Dict[str, Any],
) -> str:
    plan_blob = json.dumps(plan or {}, ensure_ascii=False, indent=2)
    return dedent(
        f"""
        Write tests for the following implementation.

        Original request:
        ---
        {_safe_truncate(user_prompt, 4000)}
        ---
        Implementation under test:
        ```{plan.get("language", "")}
        {_safe_truncate(code, 12000)}
        ```
        Plan context:
        {plan_blob}

        Write COMPREHENSIVE tests in the language's idiomatic framework.
        Cover happy paths, edge cases, errors, and any security or
        boundary concerns called out in the plan's risks. Output the
        format your system prompt requires.
        """
    ).strip()


def debugger_prompt(
    user_prompt: str,
    code: str,
    execution_feedback: str,
    static_feedback: str,
    test_failure: Optional[str] = None,
    iteration: int = 1,
    language: str = "python",
) -> str:
    return dedent(
        f"""
        The following code is failing in the execution sandbox.
        Diagnose the root cause and produce a minimal fix.

        Original request:
        ---
        {_safe_truncate(user_prompt, 3000)}
        ---
        Current code (debug iteration {iteration}):
        ```{language}
        {_safe_truncate(code, 12000)}
        ```
        Execution result:
        ---
        {_safe_truncate(execution_feedback, 4000)}
        ---
        Static analysis:
        ---
        {_safe_truncate(static_feedback, 2000)}
        ---
        {("Test failures:" + chr(10) + "---" + chr(10) +
          _safe_truncate(test_failure, 3000) + chr(10) + "---")
         if test_failure else ""}

        Output the COMPLETE fixed code (not a diff) plus the JSON
        metadata block your system prompt requires.
        """
    ).strip()


def critic_prompt(
    user_prompt: str,
    code: str,
    plan: Dict[str, Any],
    execution_feedback: Optional[str],
    static_feedback: Optional[str],
    language: str = "python",
) -> str:
    plan_blob = json.dumps(plan or {}, ensure_ascii=False, indent=2)
    return dedent(
        f"""
        Conduct a code review on the following implementation.

        Original request:
        ---
        {_safe_truncate(user_prompt, 3000)}
        ---
        Plan:
        {plan_blob}
        Implementation:
        ```{language}
        {_safe_truncate(code, 12000)}
        ```
        {("Execution result:" + chr(10) + "---" + chr(10) +
          _safe_truncate(execution_feedback, 3000) + chr(10) + "---")
         if execution_feedback else ""}
        {("Static analysis:" + chr(10) + "---" + chr(10) +
          _safe_truncate(static_feedback, 2000) + chr(10) + "---")
         if static_feedback else ""}

        Return JSON only — no prose, no fences — matching the schema in
        your system prompt.
        """
    ).strip()
