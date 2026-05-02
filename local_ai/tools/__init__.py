"""
``local_ai.tools`` — Phase 16 typed tool registry.

Public surface:

* ``Tool`` ABC, ``MCPToolResult``, ``ToolError``
* ``ToolRegistry`` with OpenAI + MCP format exporters
* ``DEFAULT_REGISTRY`` — module-level default registry
* ``register`` / ``get_default_registry`` / ``reset_default_registry``
  — convenience wrappers
* ``sentinel_adapter`` — typed wrappers for Sentinel's six tools
  (read_file, search_codebase, compile_check, taint_trace,
  cve_lookup, exploit_sandbox)
"""

from .base import MCPToolResult, Tool, ToolError
from .registry import (
    DEFAULT_REGISTRY,
    ToolRegistry,
    get_default_registry,
    register,
    reset_default_registry,
)

__all__ = [
    "Tool",
    "ToolError",
    "MCPToolResult",
    "ToolRegistry",
    "DEFAULT_REGISTRY",
    "get_default_registry",
    "register",
    "reset_default_registry",
]
