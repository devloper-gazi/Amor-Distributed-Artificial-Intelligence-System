"""Unit tests for ``document_processor/sentinel/tools.py``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from document_processor.sentinel.tools import (
    ToolRegistry,
    ToolResult,
    compile_check,
    cve_lookup,
    exploit_sandbox,
    read_file,
    search_codebase,
    taint_trace,
)


def _run(coro):
    return asyncio.run(coro)


# ─── read_file ──────────────────────────────────────────────────────


def test_read_file_basic(tmp_path: Path):
    p = tmp_path / "x.py"
    p.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    res = read_file(str(p), line_start=2, line_end=4)
    assert res.ok
    assert res.payload["content"] == "b\nc\nd"
    assert res.payload["total_lines"] == 5


def test_read_file_path_traversal_blocked(tmp_path: Path):
    p = tmp_path / "x.py"
    p.write_text("ok", encoding="utf-8")
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    res = read_file(str(p), allowed_roots=(str(elsewhere),))
    assert not res.ok
    assert "escapes allowed" in res.error


def test_read_file_too_large(tmp_path: Path):
    p = tmp_path / "big.py"
    p.write_text("x" * 5000, encoding="utf-8")
    res = read_file(str(p), max_bytes=1000)
    assert not res.ok
    assert "too large" in res.error


def test_read_file_missing():
    res = read_file("/nope/does_not_exist.py")
    assert not res.ok


# ─── search_codebase ────────────────────────────────────────────────


def test_search_codebase_basic_substring(tmp_path: Path):
    (tmp_path / "a.py").write_text("def foo(): pass\nbar = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("nothing here", encoding="utf-8")
    res = search_codebase("foo", root=str(tmp_path))
    assert res.ok
    files = {h["file"] for h in res.payload["hits"]}
    assert any(p.endswith("a.py") for p in files)


def test_search_codebase_regex(tmp_path: Path):
    (tmp_path / "a.py").write_text("password = 'secret'", encoding="utf-8")
    res = search_codebase(r"password\s*=", root=str(tmp_path), regex=True)
    assert res.ok
    assert len(res.payload["hits"]) == 1


def test_search_codebase_bad_regex_returns_error(tmp_path: Path):
    res = search_codebase("[invalid", root=str(tmp_path), regex=True)
    assert not res.ok
    assert "bad regex" in res.error


# ─── compile_check ──────────────────────────────────────────────────


def test_compile_check_valid_python():
    res = compile_check("def f(): return 1", language="python")
    assert res.ok
    assert res.payload["parses"] is True


def test_compile_check_invalid_python():
    res = compile_check("def f(:: bad", language="python")
    assert not res.ok
    assert "SyntaxError" in res.error


def test_compile_check_json_parses():
    res = compile_check('{"x": 1}', language="json")
    assert res.ok


def test_compile_check_unsupported_language_skipped():
    res = compile_check("int main(){return 0;}", language="cpp")
    assert res.ok
    assert res.payload.get("skipped") is True


# ─── taint_trace ────────────────────────────────────────────────────


def test_taint_trace_input_call_flagged():
    code = "name = input('hi')\nprint(name)\n"
    res = taint_trace("name", code=code)
    assert res.ok
    assert res.payload["tainted"] is True
    assert res.payload["evidence"][0]["source"] == "input"


def test_taint_trace_constant_assignment_clean():
    code = "name = 'alice'\n"
    res = taint_trace("name", code=code)
    assert res.ok
    assert res.payload["tainted"] is False


def test_taint_trace_request_args_subscript():
    code = "user = request.args['user']\n"
    res = taint_trace("user", code=code)
    assert res.ok
    assert res.payload["tainted"] is True


# ─── cve_lookup ─────────────────────────────────────────────────────


def test_cve_lookup_no_local_db(monkeypatch):
    monkeypatch.delenv("NVD_LOCAL_DB", raising=False)
    res = cve_lookup("requests", "2.6.0")
    assert res.ok
    assert res.payload["status"] == "no_local_db"


def test_cve_lookup_with_db_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("NVD_LOCAL_DB", str(tmp_path))
    res = cve_lookup("requests", "2.6.0")
    assert res.ok
    assert res.payload["status"] == "stubbed"


# ─── exploit_sandbox ────────────────────────────────────────────────


def test_exploit_sandbox_with_fake():
    class _Fake:
        async def execute(self, code: str, *, language="python", timeout=10):
            class _R:
                exit_code = 0
                stdout = "ok"
                stderr = ""
                skipped = False
            return _R()

    res = _run(exploit_sandbox("print(1)", language="python",
                               timeout_s=5, sandbox=_Fake()))
    assert res.ok
    assert res.payload["exit_code"] == 0


def test_exploit_sandbox_skipped_path():
    class _Fake:
        async def execute(self, code: str, *, language="python", timeout=10):
            class _R:
                exit_code = 0
                stdout = ""
                stderr = "docker missing"
                skipped = True
            return _R()

    res = _run(exploit_sandbox("anything", sandbox=_Fake()))
    assert res.ok
    assert res.payload["skipped"] is True


def test_exploit_sandbox_propagates_error():
    class _Broken:
        async def execute(self, *a, **kw):
            raise RuntimeError("docker daemon")

    res = _run(exploit_sandbox("x", sandbox=_Broken()))
    assert not res.ok
    assert "docker" in res.error


# ─── ToolRegistry ───────────────────────────────────────────────────


def test_registry_invoke_unknown():
    reg = ToolRegistry()
    res = _run(reg.invoke("nothing", {}))
    assert not res.ok
    assert "unknown tool" in res.error


def test_registry_invoke_compile_check():
    reg = ToolRegistry()
    res = _run(reg.invoke("compile_check", {"code": "x = 1", "language": "python"}))
    assert res.ok


def test_registry_schemas_have_required_fields():
    for name, schema in ToolRegistry.SCHEMAS.items():
        assert "required" in schema or schema.get("type") == "object"
