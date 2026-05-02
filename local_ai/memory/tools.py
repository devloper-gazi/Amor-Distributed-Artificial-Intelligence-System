"""
Memory ops as MCP tools — Phase 16 Commit F.

Exposes ``MemoryStore`` reads / writes as ``Tool`` subclasses so an
agent driven via the OpenAI ``tools=[…]`` parameter (or the MCP
``tools/call`` endpoint) can call them as a Letta-style function.

License: MIT.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from ..tools.base import MCPToolResult, Tool, ToolError
from .store import MemoryStore


class _CoreReadInput(BaseModel):
    pass


class _CoreWriteInput(BaseModel):
    payload: dict[str, Any] = Field(..., description="Full core blob to set.")


class _CorePatchInput(BaseModel):
    updates: dict[str, Any] = Field(..., description="Keys to merge in.")


class _RecallAppendInput(BaseModel):
    role: str = Field(..., max_length=20)
    content: str = Field(..., max_length=200_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class _RecallSearchInput(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    limit: int = Field(20, ge=1, le=200)


class _ArchiveInput(BaseModel):
    text: str = Field(..., min_length=1, max_length=200_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class _ArchivalSearchInput(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    limit: int = Field(5, ge=1, le=100)
    min_score: float = Field(0.0, ge=0.0, le=1.0)


# ─── Concrete tools ────────────────────────────────────────────────


class CoreReadTool(Tool):
    name = "memory_core_read"
    description = "Read the always-in-context core memory blob."
    InputModel = _CoreReadInput

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    def execute(self, args: _CoreReadInput) -> MCPToolResult:  # type: ignore[override]
        return MCPToolResult(
            name=self.name, ok=True, output=self.memory.read_core(),
        )


class CoreWriteTool(Tool):
    name = "memory_core_write"
    description = (
        "Replace the core memory blob.  Capped at "
        "``settings.memory_core_max_bytes`` (default 2048)."
    )
    InputModel = _CoreWriteInput
    is_async = True

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    async def execute(self, args: _CoreWriteInput) -> MCPToolResult:  # type: ignore[override]
        ok, size = await self.memory.write_core(args.payload)
        return MCPToolResult(
            name=self.name,
            ok=ok,
            output={"bytes": size, "ok": ok},
            error="" if ok else f"core blob exceeds limit ({size} bytes)",
        )


class CorePatchTool(Tool):
    name = "memory_core_patch"
    description = "Merge updates into the core memory blob (read-modify-write)."
    InputModel = _CorePatchInput
    is_async = True

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    async def execute(self, args: _CorePatchInput) -> MCPToolResult:  # type: ignore[override]
        ok, size = await self.memory.patch_core(args.updates)
        return MCPToolResult(
            name=self.name,
            ok=ok,
            output={"bytes": size, "ok": ok},
            error="" if ok else f"patched core blob exceeds limit ({size} bytes)",
        )


class RecallAppendTool(Tool):
    name = "memory_recall_append"
    description = (
        "Append a turn to the recall ring buffer.  Older entries past "
        "``window_size`` are evicted automatically."
    )
    InputModel = _RecallAppendInput
    is_async = True

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    async def execute(self, args: _RecallAppendInput) -> MCPToolResult:  # type: ignore[override]
        entry = await self.memory.append_recall(
            args.role, args.content, metadata=args.metadata,
        )
        return MCPToolResult(
            name=self.name, ok=True, output=entry.to_dict(),
        )


class RecallSearchTool(Tool):
    name = "memory_recall_search"
    description = "Substring search the recall ring buffer."
    InputModel = _RecallSearchInput

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    def execute(self, args: _RecallSearchInput) -> MCPToolResult:  # type: ignore[override]
        rows = self.memory.search_recall(args.query, limit=args.limit)
        return MCPToolResult(
            name=self.name, ok=True,
            output=[r.to_dict() for r in rows],
        )


class ArchiveTool(Tool):
    name = "memory_archive"
    description = "Add a text + metadata record to long-term archival memory."
    InputModel = _ArchiveInput
    is_async = True

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    async def execute(self, args: _ArchiveInput) -> MCPToolResult:  # type: ignore[override]
        entry = await self.memory.archive(args.text, metadata=args.metadata)
        return MCPToolResult(
            name=self.name, ok=True, output=entry.to_dict(),
        )


class ArchivalSearchTool(Tool):
    name = "memory_archival_search"
    description = (
        "Search long-term archival memory by query.  Vector similarity "
        "if an embedder is wired, substring + recency otherwise."
    )
    InputModel = _ArchivalSearchInput
    is_async = True

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    async def execute(self, args: _ArchivalSearchInput) -> MCPToolResult:  # type: ignore[override]
        rows = await self.memory.search_archival(
            args.query, limit=args.limit, min_score=args.min_score,
        )
        return MCPToolResult(
            name=self.name, ok=True,
            output=[r.to_dict() for r in rows],
        )


def all_memory_tools(memory: MemoryStore) -> list[Tool]:
    return [
        CoreReadTool(memory),
        CoreWriteTool(memory),
        CorePatchTool(memory),
        RecallAppendTool(memory),
        RecallSearchTool(memory),
        ArchiveTool(memory),
        ArchivalSearchTool(memory),
    ]


__all__ = [
    "CoreReadTool",
    "CoreWriteTool",
    "CorePatchTool",
    "RecallAppendTool",
    "RecallSearchTool",
    "ArchiveTool",
    "ArchivalSearchTool",
    "all_memory_tools",
]
