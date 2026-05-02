"""Unit tests for ``local_ai.tools`` — Phase 16 Commit E.

Covers:
* ``Tool`` ABC contract + Pydantic argument validation
* ``ToolRegistry`` register / get / dispatch / OpenAI + MCP exporters
* Sync + async tool dispatch
* ``MCPToolResult.to_mcp_content`` rendering
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, Field

from local_ai.tools import (
    DEFAULT_REGISTRY,
    MCPToolResult,
    Tool,
    ToolError,
    ToolRegistry,
    reset_default_registry,
)


def _run(coro):
    return asyncio.run(coro)


# ─── Sample tools used across tests ────────────────────────────────


class _EchoInput(BaseModel):
    text: str = Field(..., min_length=1, max_length=200)
    upper: bool = False


class EchoTool(Tool):
    name = "echo"
    description = "Echo the supplied text, optionally upper-cased."
    InputModel = _EchoInput

    def execute(self, args: _EchoInput) -> MCPToolResult:  # type: ignore[override]
        out = args.text.upper() if args.upper else args.text
        return MCPToolResult(name=self.name, ok=True, output=out)


class _DivideInput(BaseModel):
    a: float
    b: float


class DivideTool(Tool):
    name = "divide"
    description = "Divide a by b; raises ToolError on b=0."
    InputModel = _DivideInput

    def execute(self, args: _DivideInput) -> MCPToolResult:  # type: ignore[override]
        if args.b == 0:
            raise ToolError("division by zero", code="zero_div")
        return MCPToolResult(
            name=self.name, ok=True, output={"quotient": args.a / args.b},
        )


class _SleepInput(BaseModel):
    seconds: float = Field(0.0, ge=0.0, le=1.0)


class AsyncSleepTool(Tool):
    name = "sleep_then_pong"
    description = "Sleep n seconds then return 'pong'."
    InputModel = _SleepInput
    is_async = True

    async def execute(self, args: _SleepInput) -> MCPToolResult:  # type: ignore[override]
        await asyncio.sleep(args.seconds)
        return MCPToolResult(name=self.name, ok=True, output="pong")


class CrashingTool(Tool):
    name = "crash"
    description = "Always raises a non-ToolError exception."
    InputModel = _EchoInput

    def execute(self, args: _EchoInput):  # type: ignore[override]
        raise RuntimeError("kaboom")


# ─── Tool ABC ──────────────────────────────────────────────────────


def test_tool_is_abstract():
    with pytest.raises(TypeError):
        Tool()  # type: ignore[abstract]


def test_validate_rejects_missing_required():
    t = EchoTool()
    with pytest.raises(ToolError) as ei:
        t.validate({})
    assert ei.value.code == "invalid_arguments"


def test_validate_rejects_when_no_input_model():
    class _NoModel(Tool):
        name = "no_model"
        description = "x"
        InputModel = None

        def execute(self, args):  # type: ignore[override]
            return MCPToolResult(name=self.name, ok=True)

    with pytest.raises(ToolError) as ei:
        _NoModel().validate({})
    assert ei.value.code == "missing_schema"


# ─── ToolRegistry ──────────────────────────────────────────────────


def test_register_and_lookup():
    reg = ToolRegistry()
    reg.register(EchoTool())
    assert "echo" in reg
    assert len(reg) == 1
    assert reg.get("echo").name == "echo"
    assert reg.names() == ["echo"]


def test_register_duplicate_blocked_unless_replace():
    reg = ToolRegistry()
    reg.register(EchoTool())
    with pytest.raises(ValueError):
        reg.register(EchoTool())
    # ``replace=True`` works.
    reg.register(EchoTool(), replace=True)
    assert len(reg) == 1


def test_register_all_validates_each():
    reg = ToolRegistry()
    reg.register_all([EchoTool(), DivideTool()])
    assert reg.names() == ["divide", "echo"]


def test_register_rejects_empty_name():
    class _Empty(Tool):
        name = ""
        description = "x"
        InputModel = _EchoInput

        def execute(self, args):  # type: ignore[override]
            return MCPToolResult(name="", ok=True)

    reg = ToolRegistry()
    with pytest.raises(ValueError):
        reg.register(_Empty())


# ─── format exporters ─────────────────────────────────────────────


def test_to_openai_format_emits_function_shape():
    reg = ToolRegistry()
    reg.register(EchoTool())
    out = reg.to_openai_format()
    assert len(out) == 1
    fn = out[0]
    assert fn["type"] == "function"
    assert fn["function"]["name"] == "echo"
    assert "parameters" in fn["function"]
    params = fn["function"]["parameters"]
    assert params["type"] == "object"
    assert "text" in params["properties"]


def test_to_mcp_format_emits_tools_array():
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(DivideTool())
    out = reg.to_mcp_format()
    assert "tools" in out
    names = [t["name"] for t in out["tools"]]
    assert sorted(names) == ["divide", "echo"]
    for tool in out["tools"]:
        assert "description" in tool
        assert "inputSchema" in tool
        assert tool["inputSchema"]["type"] == "object"


# ─── dispatch ──────────────────────────────────────────────────────


def test_dispatch_sync_tool_success():
    reg = ToolRegistry()
    reg.register(EchoTool())
    r = _run(reg.dispatch("echo", {"text": "hi", "upper": True}))
    assert isinstance(r, MCPToolResult)
    assert r.ok is True
    assert r.output == "HI"
    assert r.elapsed_ms >= 0


def test_dispatch_unknown_tool():
    reg = ToolRegistry()
    r = _run(reg.dispatch("ghost", {}))
    assert r.ok is False
    assert "unknown" in r.error.lower()


def test_dispatch_invalid_arguments_returns_error():
    reg = ToolRegistry()
    reg.register(EchoTool())
    r = _run(reg.dispatch("echo", {}))  # missing required ``text``
    assert r.ok is False
    assert r.metadata.get("code") == "invalid_arguments"


def test_dispatch_tool_error_returns_clean_message():
    reg = ToolRegistry()
    reg.register(DivideTool())
    r = _run(reg.dispatch("divide", {"a": 1, "b": 0}))
    assert r.ok is False
    assert "division by zero" in r.error
    assert r.metadata.get("code") == "zero_div"


def test_dispatch_async_tool():
    reg = ToolRegistry()
    reg.register(AsyncSleepTool())
    r = _run(reg.dispatch("sleep_then_pong", {"seconds": 0.0}))
    assert r.ok is True
    assert r.output == "pong"


def test_dispatch_unhandled_exception_does_not_crash():
    reg = ToolRegistry()
    reg.register(CrashingTool())
    r = _run(reg.dispatch("crash", {"text": "x"}))
    assert r.ok is False
    assert "RuntimeError" in r.error
    assert "kaboom" in r.error


def test_dispatch_wraps_bare_value_in_result():
    """A tool returning a non-MCPToolResult is wrapped automatically."""
    class _BareTool(Tool):
        name = "bare"
        description = "Returns a bare string."
        InputModel = _EchoInput

        def execute(self, args):  # type: ignore[override]
            return f"{args.text}-bare"

    reg = ToolRegistry()
    reg.register(_BareTool())
    r = _run(reg.dispatch("bare", {"text": "hi"}))
    assert r.ok is True
    assert r.output == "hi-bare"


# ─── MCPToolResult content rendering ───────────────────────────────


def test_mcp_content_text_for_string_output():
    r = MCPToolResult(name="t", ok=True, output="hello world")
    content = r.to_mcp_content()
    assert content == [{"type": "text", "text": "hello world"}]


def test_mcp_content_json_for_dict_output():
    r = MCPToolResult(name="t", ok=True, output={"x": 1, "y": [2, 3]})
    content = r.to_mcp_content()
    assert content[0]["type"] == "text"
    assert '"x"' in content[0]["text"]


def test_mcp_content_truncates_huge_payload():
    big = "x" * 100_000
    r = MCPToolResult(name="t", ok=True, output={"data": big})
    content = r.to_mcp_content()
    assert content[0]["type"] == "text"
    assert content[0]["text"].endswith("…[truncated]")


def test_mcp_content_for_error():
    r = MCPToolResult(name="t", ok=False, error="boom")
    content = r.to_mcp_content()
    assert content == [{"type": "text", "text": "boom"}]


# ─── default registry helpers ──────────────────────────────────────


def test_default_registry_helpers():
    reset_default_registry()
    assert len(DEFAULT_REGISTRY) == 0
    from local_ai.tools.registry import register
    register(EchoTool())
    assert "echo" in DEFAULT_REGISTRY
    reset_default_registry()
    assert len(DEFAULT_REGISTRY) == 0
