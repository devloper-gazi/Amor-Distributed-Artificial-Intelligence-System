"""
Sentinel tool adapters — Phase 16 Commit E.

Wraps each function in ``document_processor/sentinel/tools.py`` as a
typed ``local_ai.tools.Tool`` so MCP clients and the OpenAI tools
parameter both see them.  The original Sentinel callables are
*not* modified — Sentinel's own engine continues to use them via
their existing module-level entrypoints; this adapter is purely
additive.

Each adapter:

* Declares a Pydantic ``InputModel`` derived from the wrapped
  function's signature.
* Forwards arguments to the underlying callable.
* Maps Sentinel's ``ToolResult`` (in ``sentinel/tools.py``) onto
  the new ``MCPToolResult`` shape.

License: MIT.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .base import MCPToolResult, Tool, ToolError
from .registry import ToolRegistry


# ─── Input schemas ─────────────────────────────────────────────────


class _ReadFileInput(BaseModel):
    path: str = Field(..., description="Filesystem path to read.")
    line_start: int = Field(
        1, ge=1, description="1-indexed first line to include.",
    )
    line_end: Optional[int] = Field(
        None, ge=1, description="Inclusive last line; None reads to EOF.",
    )
    max_bytes: int = Field(
        200_000, ge=1, le=2_000_000,
        description="Hard cap on bytes returned.",
    )


class _SearchCodebaseInput(BaseModel):
    query: str = Field(..., min_length=1)
    root: str = Field(".", description="Directory to scan from.")
    regex: bool = Field(False, description="Treat query as a Python regex.")
    max_matches: int = Field(50, ge=1, le=500)
    max_file_bytes: int = Field(2_000_000, ge=1)


class _CompileCheckInput(BaseModel):
    code: str
    language: str = Field("python", max_length=20)


class _TaintTraceInput(BaseModel):
    variable: str = Field(..., min_length=1)
    code: str


class _CveLookupInput(BaseModel):
    package: str
    version: str = ""


class _ExploitSandboxInput(BaseModel):
    code: str
    language: str = "python"
    timeout_s: float = Field(8.0, ge=1.0, le=120.0)


# ─── Tool wrappers ─────────────────────────────────────────────────


def _coerce_result(name: str, raw) -> MCPToolResult:
    """Translate a Sentinel ``ToolResult`` into ``MCPToolResult``.

    Sentinel ToolResult fields: name, ok, payload, error, elapsed_ms.
    """
    if raw is None:
        return MCPToolResult(name=name, ok=False, error="no result")
    return MCPToolResult(
        name=getattr(raw, "name", name),
        ok=bool(getattr(raw, "ok", False)),
        output=getattr(raw, "payload", None),
        error=str(getattr(raw, "error", "") or ""),
        elapsed_ms=float(getattr(raw, "elapsed_ms", 0.0) or 0.0),
    )


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a 1-indexed line slice of a file.  Refuses paths that "
        "escape the configured allowed-roots."
    )
    InputModel = _ReadFileInput

    def execute(self, args: _ReadFileInput) -> MCPToolResult:  # type: ignore[override]
        from document_processor.sentinel.tools import read_file  # noqa: PLC0415
        raw = read_file(
            args.path,
            line_start=args.line_start,
            line_end=args.line_end,
            max_bytes=args.max_bytes,
        )
        return _coerce_result(self.name, raw)


class SearchCodebaseTool(Tool):
    name = "search_codebase"
    description = (
        "Search the codebase for a substring or regex match.  Returns "
        "(file, line, snippet) triples up to ``max_matches``."
    )
    InputModel = _SearchCodebaseInput

    def execute(self, args: _SearchCodebaseInput) -> MCPToolResult:  # type: ignore[override]
        from document_processor.sentinel.tools import search_codebase  # noqa: PLC0415
        raw = search_codebase(
            args.query,
            root=args.root,
            regex=args.regex,
            max_matches=args.max_matches,
            max_file_bytes=args.max_file_bytes,
        )
        return _coerce_result(self.name, raw)


class CompileCheckTool(Tool):
    name = "compile_check"
    description = (
        "Does the supplied code parse / compile?  ``ast.parse`` for "
        "Python, ``json.loads`` for JSON; compiled languages return "
        "a 'skipped' verdict."
    )
    InputModel = _CompileCheckInput

    def execute(self, args: _CompileCheckInput) -> MCPToolResult:  # type: ignore[override]
        from document_processor.sentinel.tools import compile_check  # noqa: PLC0415
        return _coerce_result(
            self.name, compile_check(args.code, language=args.language),
        )


class TaintTraceTool(Tool):
    name = "taint_trace"
    description = (
        "Best-effort Python AST traversal: is ``variable`` ever "
        "assigned from a tainted source (input, request.args, …)?"
    )
    InputModel = _TaintTraceInput

    def execute(self, args: _TaintTraceInput) -> MCPToolResult:  # type: ignore[override]
        from document_processor.sentinel.tools import taint_trace  # noqa: PLC0415
        return _coerce_result(
            self.name, taint_trace(args.variable, code=args.code),
        )


class CveLookupTool(Tool):
    name = "cve_lookup"
    description = (
        "Local-only CVE lookup against the bundled NVD snapshot.  "
        "Returns 'no local DB configured' when ``NVD_LOCAL_DB`` is unset."
    )
    InputModel = _CveLookupInput

    def execute(self, args: _CveLookupInput) -> MCPToolResult:  # type: ignore[override]
        from document_processor.sentinel.tools import cve_lookup  # noqa: PLC0415
        return _coerce_result(
            self.name, cve_lookup(args.package, version=args.version),
        )


class ExploitSandboxTool(Tool):
    name = "exploit_sandbox"
    description = (
        "Run code inside the network-isolated ExecutionSandbox so the "
        "RedTeam agent can verify a payload without leaving the box."
    )
    InputModel = _ExploitSandboxInput
    is_async = True

    async def execute(self, args: _ExploitSandboxInput) -> MCPToolResult:  # type: ignore[override]
        from document_processor.sentinel.tools import exploit_sandbox  # noqa: PLC0415
        raw = await exploit_sandbox(
            args.code, language=args.language, timeout_s=args.timeout_s,
        )
        return _coerce_result(self.name, raw)


# ─── public registration helper ────────────────────────────────────


def all_tools() -> list[Tool]:
    return [
        ReadFileTool(),
        SearchCodebaseTool(),
        CompileCheckTool(),
        TaintTraceTool(),
        CveLookupTool(),
        ExploitSandboxTool(),
    ]


def register_into(registry: ToolRegistry, *, replace: bool = False) -> None:
    """Register every Sentinel tool adapter into ``registry``."""
    registry.register_all(all_tools(), replace=replace)


__all__ = [
    "ReadFileTool",
    "SearchCodebaseTool",
    "CompileCheckTool",
    "TaintTraceTool",
    "CveLookupTool",
    "ExploitSandboxTool",
    "all_tools",
    "register_into",
]
