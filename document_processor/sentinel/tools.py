"""
Sentinel — agentic tool registry.

Each tool is a small, focused function that an agent can call to
ground its answer in real code state instead of hallucinating.

Tools:

* ``read_file(path, line_start, line_end)`` — bounded slice.  Refuses
  paths that escape an allow-listed root.
* ``search_codebase(query, root, regex)`` — regex / substring search
  across the project.
* ``compile_check(code, language)`` — does the code parse / compile?
  Reuses ``ast.parse`` for Python and ``json.loads`` for JSON; falls
  through to a "skipped" verdict for compiled languages.
* ``taint_trace(variable, file)`` — best-effort AST traversal that
  tells whether a name is ever assigned from a tainted-source
  function (input(), request.args, sys.argv, ...).  Python-only.
* ``cve_lookup(package, version)`` — local-only stub.  V1 returns
  "no local DB configured" unless ``NVD_LOCAL_DB`` env var is set.
* ``exploit_sandbox(code, language, timeout_s)`` — delegates to the
  existing ``ExecutionSandbox`` with ``--network=none`` so the
  RedTeam agent can verify a payload without leaving the box.

License: MIT.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Tool result shape
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ToolResult:
    name: str
    ok: bool
    payload: Any = None
    error: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "payload": self.payload,
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


# ─────────────────────────────────────────────────────────────────────
# read_file — bounded slice with path-traversal guard
# ─────────────────────────────────────────────────────────────────────


def read_file(
    path: str,
    *,
    line_start: int = 1,
    line_end: int | None = None,
    allowed_roots: tuple[str, ...] | None = None,
    max_bytes: int = 200_000,
) -> ToolResult:
    """Read a slice of `path`, refusing to escape any of `allowed_roots`."""
    if not path:
        return ToolResult(name="read_file", ok=False, error="empty path")
    try:
        p = Path(path).resolve()
    except Exception as exc:
        return ToolResult(name="read_file", ok=False,
                          error=f"resolve failed: {exc}")
    if allowed_roots:
        roots = [Path(r).resolve() for r in allowed_roots]
        if not any(_is_within(p, root) for root in roots):
            return ToolResult(
                name="read_file", ok=False,
                error=f"path escapes allowed roots: {p}",
            )
    try:
        size = p.stat().st_size
        if size > max_bytes:
            return ToolResult(
                name="read_file", ok=False,
                error=f"file too large ({size} bytes; cap {max_bytes})",
            )
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return ToolResult(name="read_file", ok=False, error=str(exc))
    lines = text.splitlines()
    start = max(1, int(line_start))
    end = int(line_end) if line_end else len(lines)
    end = max(start, min(end, len(lines)))
    excerpt = "\n".join(lines[start - 1 : end])
    return ToolResult(
        name="read_file",
        ok=True,
        payload={
            "path": str(p),
            "line_start": start,
            "line_end": end,
            "total_lines": len(lines),
            "content": excerpt,
        },
    )


def _is_within(child: Path, root: Path) -> bool:
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


# ─────────────────────────────────────────────────────────────────────
# search_codebase
# ─────────────────────────────────────────────────────────────────────


_DEFAULT_TEXT_EXTS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".php",
    ".java", ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".sh", ".yml",
    ".yaml", ".json", ".toml", ".ini", ".cfg", ".env", ".tf",
})


def search_codebase(
    query: str,
    *,
    root: str,
    regex: bool = False,
    max_hits: int = 200,
    allowed_roots: tuple[str, ...] | None = None,
) -> ToolResult:
    """Substring or regex search across `root`'s text files."""
    if not query:
        return ToolResult(name="search_codebase", ok=False, error="empty query")
    try:
        root_p = Path(root).resolve()
    except Exception as exc:
        return ToolResult(name="search_codebase", ok=False, error=str(exc))
    if allowed_roots:
        roots = [Path(r).resolve() for r in allowed_roots]
        if not any(_is_within(root_p, r) for r in roots):
            return ToolResult(
                name="search_codebase", ok=False,
                error="root escapes allowed_roots",
            )
    try:
        pattern: re.Pattern[str] | str
        pattern = re.compile(query) if regex else query
    except re.error as exc:
        return ToolResult(name="search_codebase", ok=False,
                          error=f"bad regex: {exc}")

    hits: list[dict[str, Any]] = []
    skip_dirs = {".git", "node_modules", "venv", ".venv", "__pycache__",
                 "dist", "build", "target"}
    try:
        for dirpath, dirnames, filenames in os.walk(root_p):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fn in filenames:
                ext = Path(fn).suffix.lower()
                if ext and ext not in _DEFAULT_TEXT_EXTS:
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    text = Path(fp).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                for i, line in enumerate(text.splitlines(), start=1):
                    matched = (
                        pattern.search(line)
                        if isinstance(pattern, re.Pattern)
                        else (pattern in line)
                    )
                    if matched:
                        hits.append({
                            "file": fp,
                            "line": i,
                            "snippet": line[:240],
                        })
                        if len(hits) >= max_hits:
                            return ToolResult(
                                name="search_codebase", ok=True,
                                payload={"hits": hits, "truncated": True},
                            )
    except Exception as exc:
        return ToolResult(name="search_codebase", ok=False, error=str(exc))
    return ToolResult(
        name="search_codebase", ok=True,
        payload={"hits": hits, "truncated": False},
    )


# ─────────────────────────────────────────────────────────────────────
# compile_check
# ─────────────────────────────────────────────────────────────────────


def compile_check(code: str, *, language: str = "python") -> ToolResult:
    if not code:
        return ToolResult(name="compile_check", ok=False, error="empty code")
    lang = (language or "").lower()
    if lang == "python":
        try:
            ast.parse(code)
            return ToolResult(name="compile_check", ok=True,
                              payload={"language": "python", "parses": True})
        except SyntaxError as exc:
            return ToolResult(name="compile_check", ok=False,
                              error=f"SyntaxError: {exc.msg} (line {exc.lineno})")
    if lang == "json":
        import json
        try:
            json.loads(code)
            return ToolResult(name="compile_check", ok=True,
                              payload={"language": "json", "parses": True})
        except json.JSONDecodeError as exc:
            return ToolResult(name="compile_check", ok=False,
                              error=f"JSON: {exc.msg}")
    return ToolResult(
        name="compile_check", ok=True,
        payload={"language": lang, "skipped": True, "reason": "no parser bundled"},
    )


# ─────────────────────────────────────────────────────────────────────
# taint_trace — Python AST best-effort
# ─────────────────────────────────────────────────────────────────────


_TAINTED_SOURCES: frozenset[str] = frozenset({
    "input", "raw_input",
    "sys.argv", "os.environ",
    "request.args", "request.form", "request.json", "request.values",
    "request.GET", "request.POST",
    "flask.request",
})


def taint_trace(variable: str, *, code: str) -> ToolResult:
    """Best-effort AST traversal: was `variable` ever assigned from a
    tainted-source call?  Python only.  Returns a list of evidence
    locations."""
    if not variable or not code:
        return ToolResult(name="taint_trace", ok=False, error="empty input")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return ToolResult(name="taint_trace", ok=False,
                          error=f"parse: {exc.msg}")

    evidence: list[dict[str, Any]] = []

    def _is_tainted_callable(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in _TAINTED_SOURCES
        if isinstance(node, ast.Attribute):
            return _full_name(node) in _TAINTED_SOURCES
        return False

    def _full_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return _full_name(node.value) + "." + node.attr
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            if not any(t.id == variable for t in targets):
                continue
            if isinstance(node.value, ast.Call) and _is_tainted_callable(node.value.func):
                evidence.append({
                    "line": node.lineno,
                    "source": _full_name(node.value.func),
                })
            elif isinstance(node.value, ast.Subscript):
                # request.args["foo"] etc.
                base = _full_name(node.value.value)
                if base in _TAINTED_SOURCES:
                    evidence.append({"line": node.lineno, "source": base})

    return ToolResult(
        name="taint_trace", ok=True,
        payload={
            "variable": variable,
            "tainted": bool(evidence),
            "evidence": evidence,
        },
    )


# ─────────────────────────────────────────────────────────────────────
# cve_lookup — local-only stub
# ─────────────────────────────────────────────────────────────────────


def cve_lookup(package: str, version: str = "") -> ToolResult:
    """V1 ships only the local-only stub: returns ``no_local_db``
    unless the ``NVD_LOCAL_DB`` env var points at a local mirror.
    Real lookups land in V1.1."""
    if not package:
        return ToolResult(name="cve_lookup", ok=False, error="empty package")
    db_path = os.environ.get("NVD_LOCAL_DB", "")
    if not db_path:
        return ToolResult(
            name="cve_lookup", ok=True,
            payload={
                "package": package,
                "version": version,
                "status": "no_local_db",
                "advisories": [],
            },
        )
    # Future: lookup local NVD mirror.  V1 just acknowledges the config.
    return ToolResult(
        name="cve_lookup", ok=True,
        payload={
            "package": package, "version": version,
            "status": "stubbed",
            "db_path": db_path,
            "advisories": [],
        },
    )


# ─────────────────────────────────────────────────────────────────────
# exploit_sandbox — delegates to ExecutionSandbox
# ─────────────────────────────────────────────────────────────────────


async def exploit_sandbox(
    code: str,
    *,
    language: str = "python",
    timeout_s: int = 15,
    sandbox: Any | None = None,
) -> ToolResult:
    """Run `code` in the existing ``ExecutionSandbox``.  The sandbox
    already enforces ``--network=none`` + ``--memory`` + ``--read-only``
    so the RedTeam agent's exploit code is safe to execute."""
    if not code:
        return ToolResult(name="exploit_sandbox", ok=False, error="empty code")
    if sandbox is None:
        try:
            from ..code_intelligence.sandbox import ExecutionSandbox  # noqa: PLC0415
            sandbox = ExecutionSandbox(
                default_timeout=timeout_s, memory_limit="256m",
            )
        except Exception as exc:
            return ToolResult(name="exploit_sandbox", ok=False,
                              error=f"sandbox unavailable: {exc}")
    try:
        result = await sandbox.execute(
            code, language=language, timeout=timeout_s,
        )
    except Exception as exc:
        return ToolResult(name="exploit_sandbox", ok=False, error=str(exc))
    payload = {
        "language": language,
        "exit_code": getattr(result, "exit_code", None),
        "stdout": getattr(result, "stdout", "")[:4000],
        "stderr": getattr(result, "stderr", "")[:4000],
        "skipped": getattr(result, "skipped", False),
    }
    return ToolResult(name="exploit_sandbox", ok=True, payload=payload)


# ─────────────────────────────────────────────────────────────────────
# ToolRegistry
# ─────────────────────────────────────────────────────────────────────


class ToolRegistry:
    """Holds the JSON schema + callable for each tool.  Agents
    receive this registry and pick by name."""

    SCHEMAS: dict[str, dict[str, Any]] = {
        "read_file": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "line_start": {"type": "integer", "minimum": 1},
                "line_end": {"type": "integer"},
            },
            "required": ["path"],
        },
        "search_codebase": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "root": {"type": "string"},
                "regex": {"type": "boolean"},
            },
            "required": ["query", "root"],
        },
        "compile_check": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "language": {"type": "string"},
            },
            "required": ["code"],
        },
        "taint_trace": {
            "type": "object",
            "properties": {
                "variable": {"type": "string"},
                "code": {"type": "string"},
            },
            "required": ["variable", "code"],
        },
        "cve_lookup": {
            "type": "object",
            "properties": {
                "package": {"type": "string"},
                "version": {"type": "string"},
            },
            "required": ["package"],
        },
        "exploit_sandbox": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "language": {"type": "string"},
                "timeout_s": {"type": "integer", "minimum": 1, "maximum": 60},
            },
            "required": ["code"],
        },
    }

    def __init__(self, *, allowed_roots: tuple[str, ...] = ()) -> None:
        self.allowed_roots = allowed_roots

    async def invoke(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name == "read_file":
            return read_file(
                args.get("path", ""),
                line_start=int(args.get("line_start", 1)),
                line_end=args.get("line_end"),
                allowed_roots=self.allowed_roots,
            )
        if name == "search_codebase":
            return search_codebase(
                args.get("query", ""),
                root=args.get("root", ""),
                regex=bool(args.get("regex", False)),
                allowed_roots=self.allowed_roots,
            )
        if name == "compile_check":
            return compile_check(
                args.get("code", ""),
                language=args.get("language", "python"),
            )
        if name == "taint_trace":
            return taint_trace(
                args.get("variable", ""),
                code=args.get("code", ""),
            )
        if name == "cve_lookup":
            return cve_lookup(
                args.get("package", ""),
                args.get("version", ""),
            )
        if name == "exploit_sandbox":
            return await exploit_sandbox(
                args.get("code", ""),
                language=args.get("language", "python"),
                timeout_s=int(args.get("timeout_s", 15)),
            )
        return ToolResult(name=name, ok=False, error=f"unknown tool: {name}")


__all__ = [
    "ToolRegistry",
    "ToolResult",
    "compile_check",
    "cve_lookup",
    "exploit_sandbox",
    "read_file",
    "search_codebase",
    "taint_trace",
]
