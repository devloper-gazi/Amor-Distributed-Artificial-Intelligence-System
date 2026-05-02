"""
Tool ABC — Phase 16 Commit E.

A typed wrapper that every tool the AMOR runtime exposes implements,
regardless of whether the caller is:

* an internal Sentinel / Consortium agent (in-process via the
  ``ToolRegistry``),
* an external SDK over the OpenAI ``/v1/chat/completions`` ``tools``
  parameter, or
* an MCP-aware client over ``/mcp/v1/tools/{list,call}``.

Design notes
------------
* ``Tool.execute()`` accepts a Pydantic-validated args object and
  returns ``MCPToolResult`` — a fresh dataclass that intentionally
  differs from ``document_processor.sentinel.tools.ToolResult`` so
  the two namespaces don't collide.
* ``mime_type`` lets a tool emit non-text payloads (JSON,
  markdown, html, image bytes); MCP's ``content`` array carries the
  same idea.
* Subclasses declare ``InputModel: type[pydantic.BaseModel]`` to
  describe their argument schema.  The registry uses
  ``model_json_schema()`` to produce both the OpenAI tool schema
  and the MCP ``inputSchema``.

License: MIT.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional, Type

from pydantic import BaseModel


@dataclass
class MCPToolResult:
    """Normalised tool-call result.  One per ``Tool.execute()`` invocation."""

    name: str
    ok: bool
    output: Any = None              # str | dict | list | bytes
    error: str = ""
    elapsed_ms: float = 0.0
    mime_type: str = "text/plain"   # MCP content type
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "mime_type": self.mime_type,
            "metadata": dict(self.metadata or {}),
        }

    def to_mcp_content(self) -> list[dict[str, Any]]:
        """Render the result as an MCP ``content`` array.  Errors are
        rendered as a single ``text`` block; successes are rendered as
        either a ``text`` or ``json`` block depending on payload type."""
        if not self.ok:
            return [{
                "type": "text",
                "text": self.error or "tool execution failed",
            }]
        if isinstance(self.output, (dict, list)):
            return [{
                "type": "text",
                "text": _safe_json(self.output),
            }]
        if isinstance(self.output, bytes):
            return [{
                "type": "blob",
                "blob": self.output.hex(),
                "mimeType": self.mime_type,
            }]
        return [{"type": "text", "text": str(self.output or "")}]


class ToolError(Exception):
    """Raised by a Tool to surface a structured error to the caller."""

    def __init__(self, message: str, *, code: str = "tool_error") -> None:
        super().__init__(message)
        self.code = code


class Tool(ABC):
    """Strongly typed tool surface.

    Subclasses declare:

    * ``name`` — short identifier (snake_case).
    * ``description`` — one-line summary used by both the OpenAI
      schema and MCP discovery.
    * ``InputModel`` — Pydantic model describing the arguments.
    * ``execute(args: InputModel) -> MCPToolResult`` — the work.

    The base class wires up sync/async dispatch so subclasses can
    be either ``def`` or ``async def``.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    InputModel: ClassVar[Optional[Type[BaseModel]]] = None
    is_async: ClassVar[bool] = False

    def validate(self, raw: dict | None) -> BaseModel:
        """Coerce ``raw`` into ``InputModel`` instance, or raise
        ``ToolError`` with a clean message."""
        if self.InputModel is None:
            raise ToolError(
                f"tool {self.name!r} has no InputModel declared",
                code="missing_schema",
            )
        try:
            return self.InputModel.model_validate(raw or {})
        except Exception as exc:
            raise ToolError(
                f"invalid arguments for {self.name!r}: {exc}",
                code="invalid_arguments",
            )

    @abstractmethod
    def execute(self, args: BaseModel) -> Any:
        """Run the tool.  ``args`` is already validated.  Returns
        ``MCPToolResult`` for sync tools or a coroutine yielding
        ``MCPToolResult`` for async tools.  See ``is_async``."""
        ...


# ─── helpers ────────────────────────────────────────────────────────


def _safe_json(obj: Any, *, max_chars: int = 8000) -> str:
    """Stable JSON dump with a hard byte cap so a runaway payload
    can't overwhelm the MCP wire."""
    import json as _json  # noqa: PLC0415
    try:
        text = _json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        text = repr(obj)
    if len(text) > max_chars:
        text = text[:max_chars] + "…[truncated]"
    return text


__all__ = ["Tool", "ToolError", "MCPToolResult"]
