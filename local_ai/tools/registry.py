"""
ToolRegistry — Phase 16 Commit E.

A keyed catalog of ``Tool`` instances.  The registry exposes:

* ``register(tool)`` / ``register_all(tools)`` — add tools.  Refuses
  duplicate names so adapter packages can opt-in idempotently.
* ``get(name)`` / ``list()`` — read-only access.
* ``to_openai_format()`` — emits the catalog in the OpenAI
  ``tools=[{"type": "function", "function": {...}}, …]`` shape used
  by ``/v1/chat/completions``.
* ``to_mcp_format()`` — emits the catalog in MCP shape (``tools``
  array with ``inputSchema``) for ``/mcp/v1/tools/list``.
* ``dispatch(name, arguments)`` — validate args, run the tool,
  return ``MCPToolResult``.  Sync + async tools handled.

The default global registry lives at ``DEFAULT_REGISTRY``; adapter
packages (sentinel_adapter, consortium_adapter, …) register into
it on import.

License: MIT.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Iterable

from .base import MCPToolResult, Tool, ToolError


logger = logging.getLogger(__name__)


class ToolRegistry:
    """Keyed catalog of ``Tool`` instances."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ─── add / read ─────────────────────────────────────────────

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        if not tool.name:
            raise ValueError("tool has empty name")
        if tool.name in self._tools and not replace:
            raise ValueError(f"tool already registered: {tool.name!r}")
        self._tools[tool.name] = tool

    def register_all(self, tools: Iterable[Tool], *, replace: bool = False) -> None:
        for t in tools:
            self.register(t, replace=replace)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def clear(self) -> None:
        self._tools.clear()

    # ─── format exporters ──────────────────────────────────────

    def to_openai_format(self) -> list[dict[str, Any]]:
        """OpenAI ``/v1/chat/completions`` ``tools=[…]`` shape."""
        out: list[dict[str, Any]] = []
        for tool in self._tools.values():
            schema = self._input_schema(tool)
            out.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": schema,
                },
            })
        return out

    def to_mcp_format(self) -> dict[str, Any]:
        """MCP ``tools/list`` response shape."""
        return {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": self._input_schema(tool),
                }
                for tool in self._tools.values()
            ],
        }

    # ─── dispatch ──────────────────────────────────────────────

    async def dispatch(
        self, name: str, arguments: dict | None,
    ) -> MCPToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return MCPToolResult(
                name=name, ok=False,
                error=f"unknown tool: {name!r}",
            )
        started = time.monotonic()
        try:
            args = tool.validate(arguments)
        except ToolError as exc:
            return MCPToolResult(
                name=name, ok=False, error=str(exc),
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                metadata={"code": exc.code},
            )

        try:
            result = tool.execute(args)
            if inspect.isawaitable(result):
                result = await result
        except ToolError as exc:
            return MCPToolResult(
                name=name, ok=False, error=str(exc),
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                metadata={"code": exc.code},
            )
        except Exception as exc:
            logger.exception("tool %s crashed", name)
            return MCPToolResult(
                name=name, ok=False,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        if isinstance(result, MCPToolResult):
            if result.elapsed_ms <= 0:
                result.elapsed_ms = (time.monotonic() - started) * 1000.0
            return result
        # Allow tools to return a bare value; we wrap it.
        return MCPToolResult(
            name=name, ok=True, output=result,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )

    # ─── helpers ───────────────────────────────────────────────

    @staticmethod
    def _input_schema(tool: Tool) -> dict[str, Any]:
        if tool.InputModel is None:
            return {"type": "object", "properties": {}}
        try:
            schema = tool.InputModel.model_json_schema()
        except Exception:
            return {"type": "object", "properties": {}}
        # Drop Pydantic-internal keys; keep what the OpenAI / MCP
        # consumer expects.
        for key in ("$defs", "definitions", "title"):
            schema.pop(key, None)
        return schema


# Module-level default registry — adapter packages register here.
DEFAULT_REGISTRY = ToolRegistry()


def register(tool: Tool, *, replace: bool = False) -> None:
    """Convenience wrapper for the default registry."""
    DEFAULT_REGISTRY.register(tool, replace=replace)


def get_default_registry() -> ToolRegistry:
    return DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """Test helper — wipe the default registry."""
    DEFAULT_REGISTRY.clear()


__all__ = [
    "ToolRegistry",
    "DEFAULT_REGISTRY",
    "register",
    "get_default_registry",
    "reset_default_registry",
]
