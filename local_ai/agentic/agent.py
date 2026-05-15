"""
Cycle C Sprint 8 Day 3 — ReActAgent + StuckDetector.

Driver of the think → act → observe → repeat loop.  Pluggable LLM
caller (so the agent unit-tests against a stub and the route layer
plugs in the real ``LLMBackend.generate``).  Tool dispatch goes
through ``local_ai.tools.DEFAULT_REGISTRY`` — so the agent can use
every tool the existing MCP facade exposes.

Termination conditions (in order checked):

* The agent emits ``finish`` — its ``arguments.answer`` becomes the
  final answer and the conversation finishes with ``reason="finish"``.
* Iteration count hits ``max_iterations`` — finish reason
  ``"max-iterations"``.
* Stuck detector trips (3+ identical action+observation pairs) —
  finish reason ``"stuck"``.
* The LLM emits an unparseable response 3 times in a row —
  finish reason ``"parse-failure"``.

Every state transition appends a typed ``Event`` to the
:class:`Conversation`; the route layer streams those events over SSE.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

from .conversation import Conversation
from .events import (
    ActionEvent,
    Event,
    MessageEvent,
    ObservationEvent,
    ThoughtEvent,
)
from .prompt import (
    ParsedReAct,
    parse_react,
    render_history,
    render_prompt,
)


logger = logging.getLogger(__name__)


# ─── pluggable LLM caller ─────────────────────────────────────────


# An LLMCaller takes the rendered prompt and returns the completion text.
# Sync or async — the agent awaits whichever it gets.
LLMCaller = Callable[[str], Union[str, Awaitable[str]]]


# A ToolDispatcher takes (name, arguments) and returns a result dict.
# The default dispatcher wraps ``local_ai.tools.DEFAULT_REGISTRY`` —
# tests can swap in a stub.
ToolResult = Dict[str, Any]
ToolDispatcher = Callable[[str, Dict[str, Any]], Union[ToolResult, Awaitable[ToolResult]]]


@dataclass
class AgentConfig:
    max_iterations: int = 10
    max_parse_retries: int = 3
    stuck_window: int = 3  # 3+ identical action/observation pairs ⇒ stuck
    finish_tool: str = "finish"


# ─── stuck detector ───────────────────────────────────────────────


class StuckDetector:
    """Checks the conversation's action/observation tail for ``window``
    identical pairs.  Equality is structural over (tool, arguments,
    output_summary) so a numeric jitter doesn't spuriously trip it."""

    def __init__(self, *, window: int = 3) -> None:
        self.window = max(2, int(window))

    def is_stuck(self, conv: Conversation) -> bool:
        pairs = conv.action_observation_pairs()
        if len(pairs) < self.window:
            return False
        tail = pairs[-self.window:]
        first_key = self._key(tail[0][0], tail[0][1])
        return all(self._key(a, o) == first_key for a, o in tail)

    @staticmethod
    def _key(action: ActionEvent, observation: ObservationEvent) -> Tuple[str, str, str]:
        # Use canonical JSON-ish reprs so dict ordering doesn't trip
        # equality.  ``output`` is summarised down to its first 240
        # characters to absorb timestamp / counter noise.
        import json as _json
        try:
            args_key = _json.dumps(action.arguments, sort_keys=True, default=str)
        except Exception:
            args_key = repr(action.arguments)
        out = observation.output
        if isinstance(out, dict):
            try:
                out_key = _json.dumps(out, sort_keys=True, default=str)
            except Exception:
                out_key = repr(out)
        else:
            out_key = repr(out)
        return (action.tool, args_key, out_key[:240])


# ─── default tool dispatcher (wraps DEFAULT_REGISTRY) ─────────────


async def default_tool_dispatcher(name: str, arguments: Dict[str, Any]) -> ToolResult:
    """Dispatch via ``local_ai.tools.DEFAULT_REGISTRY``.  Returns the
    normalised ``MCPToolResult`` as a plain dict so the route layer
    can re-serialise without touching the registry."""
    from local_ai.tools import DEFAULT_REGISTRY  # noqa: PLC0415
    res = await DEFAULT_REGISTRY.dispatch(name, arguments or {})
    return {
        "name": res.name,
        "ok": bool(res.ok),
        "output": res.output,
        "error": res.error,
        "elapsed_ms": float(res.elapsed_ms or 0.0),
        "metadata": dict(res.metadata or {}),
    }


# ─── agent ────────────────────────────────────────────────────────


@dataclass
class AgentRunResult:
    answer: Optional[str]
    reason: str   # "finish" | "max-iterations" | "stuck" | "parse-failure"
    iterations: int


class ReActAgent:
    """ReAct-style think → act → observe loop.

    The agent doesn't own the LLM or the tool registry — both are
    injected so the loop is testable without spinning up a real
    backend.  Every state transition appends to the conversation's
    event log; the route layer is the SSE producer that watches the
    log.
    """

    def __init__(
        self,
        *,
        conversation: Conversation,
        llm_caller: LLMCaller,
        tools_catalogue: List[Dict[str, Any]],
        config: Optional[AgentConfig] = None,
        tool_dispatcher: Optional[ToolDispatcher] = None,
        on_event: Optional[Callable[[Event], Awaitable[None]]] = None,
    ) -> None:
        self.conv = conversation
        self.llm_caller = llm_caller
        self.tools_catalogue = list(tools_catalogue)
        self.config = config or AgentConfig()
        self.tool_dispatcher: ToolDispatcher = (
            tool_dispatcher or default_tool_dispatcher
        )
        self.on_event = on_event
        self.stuck = StuckDetector(window=self.config.stuck_window)

    # ── public ────────────────────────────────────────────────

    async def run(self, *, user_task: str) -> AgentRunResult:
        """Drive the loop until termination.  Returns the final
        answer + reason."""
        await self._emit(self.conv.append_message("user", user_task))

        parse_failures = 0
        for _ in range(self.config.max_iterations):
            self.conv.start_iteration()
            history = render_history(self.conv.events)
            prompt = render_prompt(
                user_task=user_task,
                tools=self.tools_catalogue,
                history=history,
                max_iterations=self.config.max_iterations,
            )

            completion = await self._call_llm(prompt)
            parsed = parse_react(completion)

            # Thought (always emit if present, even when action fails).
            if parsed.thought:
                await self._emit(self.conv.append_thought(parsed.thought))

            if parsed.action_tool is None:
                parse_failures += 1
                if parse_failures >= self.config.max_parse_retries:
                    self.conv.finish("parse-failure")
                    return AgentRunResult(
                        answer=None,
                        reason="parse-failure",
                        iterations=self.conv.iteration,
                    )
                # Re-enter loop with the failed completion appended as
                # an observation so the next render carries context.
                await self._emit(
                    self.conv.append_observation(
                        tool="<parser>",
                        arguments={},
                        output={"raw": completion[:240]},
                        is_error=True,
                        error_message=parsed.parse_error or "parse error",
                    ),
                )
                continue

            parse_failures = 0  # reset on a clean parse

            # finish — early exit.
            if parsed.action_tool == self.config.finish_tool:
                answer = ""
                if isinstance(parsed.action_arguments, dict):
                    raw = parsed.action_arguments.get("answer")
                    answer = str(raw) if raw is not None else ""
                action = self.conv.append_action(
                    tool=parsed.action_tool,
                    arguments=parsed.action_arguments or {},
                )
                await self._emit(action)
                self.conv.finish("finish")
                return AgentRunResult(
                    answer=answer,
                    reason="finish",
                    iterations=self.conv.iteration,
                )

            # Real tool dispatch.
            action = self.conv.append_action(
                tool=parsed.action_tool,
                arguments=parsed.action_arguments or {},
            )
            await self._emit(action)

            t0 = time.monotonic()
            result = await self._call_tool(parsed.action_tool, parsed.action_arguments or {})
            elapsed = (time.monotonic() - t0) * 1000.0

            obs = self.conv.append_observation(
                tool=parsed.action_tool,
                arguments=parsed.action_arguments or {},
                output=result.get("output"),
                is_error=not bool(result.get("ok", True)),
                error_message=result.get("error"),
                elapsed_ms=float(result.get("elapsed_ms") or elapsed),
            )
            await self._emit(obs)

            if self.stuck.is_stuck(self.conv):
                self.conv.finish("stuck")
                return AgentRunResult(
                    answer=None,
                    reason="stuck",
                    iterations=self.conv.iteration,
                )

        # Hit the max-iterations ceiling.
        self.conv.finish("max-iterations")
        return AgentRunResult(
            answer=None,
            reason="max-iterations",
            iterations=self.conv.iteration,
        )

    # ── helpers ───────────────────────────────────────────────

    async def _emit(self, event: Event) -> None:
        if not self.on_event:
            return
        try:
            res = self.on_event(event)
            if inspect.isawaitable(res):
                await res
        except Exception as exc:  # pragma: no cover
            logger.warning("on_event hook raised: %s", exc)

    async def _call_llm(self, prompt: str) -> str:
        try:
            res = self.llm_caller(prompt)
            if inspect.isawaitable(res):
                res = await res
            return str(res or "")
        except Exception as exc:
            logger.warning("LLM call raised: %s", exc)
            return ""

    async def _call_tool(self, name: str, arguments: Dict[str, Any]) -> ToolResult:
        try:
            res = self.tool_dispatcher(name, arguments)
            if inspect.isawaitable(res):
                res = await res
        except Exception as exc:
            logger.warning("tool dispatcher raised: %s", exc)
            return {
                "name": name,
                "ok": False,
                "output": None,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": 0.0,
            }
        # Normalise — accept dict or MCPToolResult.
        if isinstance(res, dict):
            return res
        return {
            "name": name,
            "ok": True,
            "output": res,
            "error": None,
            "elapsed_ms": 0.0,
        }
