"""
MCP (Model Context Protocol) facade — Phase 16 Commit E.

Mounts ``/mcp/v1/*`` endpoints so MCP-aware clients (Letta,
OpenHands, Claude Desktop's MCP host, …) can discover and call
the typed tools registered in ``local_ai.tools.DEFAULT_REGISTRY``.

Endpoints
---------

* ``GET  /mcp/v1/tools/list``  — catalog (name, description, inputSchema)
* ``POST /mcp/v1/tools/call``  — ``{"name": ..., "arguments": {...}}``
                                  → ``{"content": [...], "isError": ...}``

Settings gate
-------------

The router is mounted unconditionally but every endpoint checks
``settings.enable_mcp_server`` first.  Default ``False`` — operators
flip on per-host once they've vetted what the registry exposes.

License: MIT.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth.dependencies import get_optional_user
from ..auth.models import User
from ..config.settings import settings


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/mcp/v1", tags=["mcp"])


def _check_enabled() -> None:
    if not getattr(settings, "enable_mcp_server", False):
        raise HTTPException(
            status_code=503,
            detail="MCP server disabled (settings.enable_mcp_server=False)",
        )


def _registry():
    """Lazy import so the route module loads even if local_ai isn't
    on the path (mirrors openai_compat_routes pattern)."""
    try:
        from local_ai.tools import (  # noqa: PLC0415
            DEFAULT_REGISTRY,
            sentinel_adapter,
        )
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=503,
            detail=f"local_ai.tools unavailable: {type(exc).__name__}: {exc}",
        )
    # Idempotent: if the Sentinel adapters are not yet registered,
    # do it now so a fresh process exposes the catalog out of the box.
    if not any(name == "read_file" for name in DEFAULT_REGISTRY.names()):
        try:
            sentinel_adapter.register_into(DEFAULT_REGISTRY, replace=False)
        except Exception as exc:  # pragma: no cover
            logger.warning("sentinel adapter register failed: %s", exc)
    return DEFAULT_REGISTRY


# ─── Models ─────────────────────────────────────────────────────────


class ToolCallRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    arguments: Optional[dict] = Field(default_factory=dict)


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/tools/list")
async def list_tools(
    user: Optional[User] = Depends(get_optional_user),  # noqa: ARG001
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),  # noqa: ARG001
) -> JSONResponse:
    _check_enabled()
    registry = _registry()
    return JSONResponse(registry.to_mcp_format())


@router.post("/tools/call")
async def call_tool(
    body: ToolCallRequest,
    user: Optional[User] = Depends(get_optional_user),  # noqa: ARG001
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),  # noqa: ARG001
) -> JSONResponse:
    _check_enabled()
    registry = _registry()
    if body.name not in registry:
        raise HTTPException(
            status_code=404,
            detail=f"unknown tool: {body.name!r}",
        )
    result = await registry.dispatch(body.name, body.arguments or {})
    return JSONResponse({
        "content": result.to_mcp_content(),
        "isError": not result.ok,
        "metadata": {
            "name": result.name,
            "elapsed_ms": result.elapsed_ms,
            **(result.metadata or {}),
        },
    })


# OpenAI ``/v1/chat/completions`` ``tools=[…]`` discovery.  Useful
# when an SDK wants to splice the registry into a chat call without
# implementing MCP discovery itself.
@router.get("/openai-tools")
async def openai_tools(
    user: Optional[User] = Depends(get_optional_user),  # noqa: ARG001
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),  # noqa: ARG001
) -> JSONResponse:
    _check_enabled()
    registry = _registry()
    return JSONResponse({"tools": registry.to_openai_format()})
