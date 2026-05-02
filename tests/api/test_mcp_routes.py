"""Smoke tests for the MCP server routes — Phase 16 Commit E."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from document_processor.api.mcp_routes import router
from document_processor.config.settings import settings
from local_ai.tools import (
    DEFAULT_REGISTRY,
    MCPToolResult,
    Tool,
    reset_default_registry,
)


class _PingInput(BaseModel):
    msg: str = Field("pong", min_length=1)


class PingTool(Tool):
    name = "ping"
    description = "Echo the supplied msg back."
    InputModel = _PingInput

    def execute(self, args: _PingInput) -> MCPToolResult:  # type: ignore[override]
        return MCPToolResult(name=self.name, ok=True, output=args.msg)


@pytest.fixture
def app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app_with_router: FastAPI, monkeypatch) -> TestClient:
    reset_default_registry()
    DEFAULT_REGISTRY.register(PingTool())
    monkeypatch.setattr(settings, "enable_mcp_server", True)
    yield TestClient(app_with_router)
    reset_default_registry()


# ─── master gate ───────────────────────────────────────────────────


def test_disabled_returns_503(monkeypatch, app_with_router: FastAPI):
    monkeypatch.setattr(settings, "enable_mcp_server", False)
    c = TestClient(app_with_router)
    r = c.get("/mcp/v1/tools/list")
    assert r.status_code == 503


# ─── tools/list ────────────────────────────────────────────────────


def test_tools_list_emits_mcp_shape(client: TestClient):
    r = client.get("/mcp/v1/tools/list")
    assert r.status_code == 200
    body = r.json()
    assert "tools" in body
    names = [t["name"] for t in body["tools"]]
    assert "ping" in names
    # Each tool has the MCP fields.
    ping = next(t for t in body["tools"] if t["name"] == "ping")
    assert ping["description"] == "Echo the supplied msg back."
    assert ping["inputSchema"]["type"] == "object"
    assert "msg" in ping["inputSchema"]["properties"]


def test_tools_list_includes_sentinel_adapters(client: TestClient):
    r = client.get("/mcp/v1/tools/list")
    body = r.json()
    names = {t["name"] for t in body["tools"]}
    # Adapter auto-registers on first call.
    for expected in (
        "read_file", "search_codebase", "compile_check",
        "taint_trace", "cve_lookup", "exploit_sandbox",
    ):
        assert expected in names


# ─── tools/call ────────────────────────────────────────────────────


def test_tools_call_dispatches_and_returns_content(client: TestClient):
    r = client.post(
        "/mcp/v1/tools/call",
        json={"name": "ping", "arguments": {"msg": "hi"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["isError"] is False
    assert body["content"] == [{"type": "text", "text": "hi"}]
    assert body["metadata"]["name"] == "ping"
    assert "elapsed_ms" in body["metadata"]


def test_tools_call_unknown_name_404(client: TestClient):
    r = client.post(
        "/mcp/v1/tools/call",
        json={"name": "ghost", "arguments": {}},
    )
    assert r.status_code == 404


def test_tools_call_invalid_arguments_returns_isError_true(client: TestClient):
    # Missing required ``msg`` (default present, but min_length=1
    # prevents empty string).
    r = client.post(
        "/mcp/v1/tools/call",
        json={"name": "ping", "arguments": {"msg": ""}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["isError"] is True
    assert body["metadata"]["code"] == "invalid_arguments"


def test_openai_tools_endpoint(client: TestClient):
    r = client.get("/mcp/v1/openai-tools")
    assert r.status_code == 200
    body = r.json()
    assert "tools" in body
    fns = [t["function"]["name"] for t in body["tools"]]
    assert "ping" in fns
    # OpenAI shape: {"type": "function", "function": {...}}.
    for entry in body["tools"]:
        assert entry["type"] == "function"
        assert "parameters" in entry["function"]
