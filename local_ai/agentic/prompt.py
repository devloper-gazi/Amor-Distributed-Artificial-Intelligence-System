"""
Cycle C Sprint 8 Day 2 — ReAct prompt template + parser.

Forces the agent into a ``<thought>...<action>{...}</action>`` cycle
the conversation log can ingest deterministically.  No exotic
grammar; the parser is regex + json + a small "first valid block
wins" rule so a chatty model that emits prose around the structure
still produces a usable action.

Rendered prompt (excerpt)::

    You are AMOR's agent.  Loop:
      1. Emit ONE <thought>...</thought>.
      2. Emit ONE <action>{"tool": "<name>", "arguments": {...}}</action>.
      3. Wait for an <observation>...</observation>.
      4. Repeat.  Stop when you have enough info — emit
         <action>{"tool": "finish", "arguments": {"answer": "..."}}</action>.

    Available tools:
      - sandbox-execute(language, code, timeout=30)
      - repo-symbol-search(q, limit=5)
      ...
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# ─── render ───────────────────────────────────────────────────────


SYSTEM_HEADER = """You are AMOR's agent.  Solve the user's task by alternating between
<thought>...</thought> reasoning and <action>...</action> tool calls.

Strict format:
  1. Emit exactly ONE <thought> block — short, in plain English.
  2. Emit exactly ONE <action> block whose body is a single-line
     JSON object: {"tool": "<name>", "arguments": {...}}.
  3. STOP after the </action>.  The runtime will execute the tool
     and reply with an <observation>...</observation> block.
  4. Repeat until the task is solved.  When done, emit
     <action>{"tool": "finish", "arguments": {"answer": "..."}}</action>.

Rules:
  * No commentary outside the two blocks.
  * Pick tool names verbatim from the catalogue below.
  * If a tool keeps returning the same observation (3+ identical
    pairs), STOP and emit ``finish`` with what you know so far.
"""


def render_tool_catalogue(tools: List[Dict[str, Any]]) -> str:
    """Format the registry's ``to_openai_format`` output (or any list
    of ``{name, description, inputSchema}`` dicts) into a compact
    catalogue the prompt can include."""
    lines: List[str] = []
    for t in tools:
        # OpenAI shape: {"type": "function", "function": {"name", "description", "parameters"}}
        fn = t.get("function") if isinstance(t, dict) and "function" in t else t
        if not isinstance(fn, dict):
            continue
        name = fn.get("name", "?")
        desc = fn.get("description") or ""
        schema = fn.get("parameters") or fn.get("inputSchema") or {}
        props = (schema.get("properties") if isinstance(schema, dict) else {}) or {}
        sig = ", ".join(props.keys())
        lines.append(f"  - {name}({sig}) — {desc}")
    # The ``finish`` synthetic tool is always available.
    lines.append('  - finish(answer) — emit a final answer and stop.')
    return "\n".join(lines)


def render_prompt(
    *,
    user_task: str,
    tools: List[Dict[str, Any]],
    history: Optional[str] = None,
    max_iterations: int = 10,
) -> str:
    """Compose the full ReAct system + user prompt the LLM sees."""
    catalogue = render_tool_catalogue(tools)
    parts = [
        SYSTEM_HEADER,
        f"Available tools (max {max_iterations} iterations):\n{catalogue}",
        "",
        f"User task:\n{user_task.strip()}",
    ]
    if history:
        parts.extend(["", "Conversation so far:", history.rstrip()])
    parts.append("\n---\nNow emit your next <thought> + <action>:")
    return "\n".join(parts)


# ─── parse ────────────────────────────────────────────────────────


_THOUGHT_RE = re.compile(r"<thought>\s*(.*?)\s*</thought>", re.DOTALL | re.IGNORECASE)
# Match ANY body inside <action>; the JSON parser below validates it.
# A wider regex means an invalid body produces a useful "not JSON"
# error rather than the misleading "no action block".
_ACTION_RE = re.compile(r"<action>\s*(.*?)\s*</action>", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class ParsedReAct:
    """Parser output.  ``thought`` and ``action`` are both optional —
    the agent's loop decides what to do when one is missing (e.g.
    abort or re-prompt)."""

    thought: Optional[str] = None
    action_tool: Optional[str] = None
    action_arguments: Optional[Dict[str, Any]] = None
    raw_action: Optional[str] = None
    parse_error: Optional[str] = None


def parse_react(text: str) -> ParsedReAct:
    """Extract the FIRST <thought>+<action> pair from ``text``.

    The rules are deliberately permissive:
    * Plain text outside the blocks is ignored.
    * The action body must parse as JSON; we attempt a forgiving
      retry (strip trailing commas) before giving up.
    * Missing ``tool`` ⇒ ``parse_error`` is set, downstream loop
      treats it as an LLM hallucination and retries.
    """
    if not text:
        return ParsedReAct(parse_error="empty response")

    thought_m = _THOUGHT_RE.search(text)
    thought = thought_m.group(1).strip() if thought_m else None

    action_m = _ACTION_RE.search(text)
    if not action_m:
        return ParsedReAct(thought=thought, parse_error="no <action> block found")

    raw = action_m.group(1).strip()
    parsed: Optional[Dict[str, Any]] = _try_load_json(raw)
    if parsed is None:
        return ParsedReAct(
            thought=thought,
            raw_action=raw,
            parse_error="action body is not valid JSON",
        )
    if "tool" not in parsed or not isinstance(parsed["tool"], str):
        return ParsedReAct(
            thought=thought,
            raw_action=raw,
            parse_error="action JSON missing 'tool' string",
        )
    args = parsed.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    return ParsedReAct(
        thought=thought,
        action_tool=parsed["tool"].strip(),
        action_arguments=args,
        raw_action=raw,
    )


def _try_load_json(blob: str) -> Optional[Dict[str, Any]]:
    try:
        v = json.loads(blob)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    # Forgiving retry — a few small fixups that don't risk semantic shifts.
    cleaned = re.sub(r",(\s*[}\]])", r"\1", blob)  # drop trailing commas
    try:
        v = json.loads(cleaned)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        return None


# ─── history rendering (for re-prompting) ─────────────────────────


def render_history(events) -> str:
    """Render an iterable of agentic events back into the
    ``<thought>...<observation>`` text the ReAct prompt expects."""
    lines: List[str] = []
    for ev in events:
        kind = getattr(ev, "kind", None)
        if kind == "message":
            lines.append(f"<message role={getattr(ev, 'role', '?')!r}>{getattr(ev, 'text', '')}</message>")
        elif kind == "thought":
            lines.append(f"<thought>{getattr(ev, 'text', '')}</thought>")
        elif kind == "action":
            payload = json.dumps(
                {"tool": getattr(ev, "tool", ""), "arguments": getattr(ev, "arguments", {})},
                ensure_ascii=False,
            )
            lines.append(f"<action>{payload}</action>")
        elif kind == "observation":
            output = getattr(ev, "output", None)
            text = json.dumps(output, ensure_ascii=False, default=str)[:600]
            err = "" if not getattr(ev, "is_error", False) else " error=true"
            lines.append(f"<observation tool={getattr(ev, 'tool', '?')!r}{err}>{text}</observation>")
    return "\n".join(lines)
