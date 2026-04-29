"""
Tests for the AMOR CLI's `quickcode` subcommand and the
`consortium --implementation-engine` flag pass-through.

The QuickCodeEngine is replaced with an in-test stub so we never hit
Ollama / Docker. The remote path is verified with a stubbed httpx
async client that intercepts the POST + SSE stream.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from document_processor.cli import __main__ as cli


# ─── argparse layer ─────────────────────────────────────────────────


def test_parser_exposes_quickcode_subcommand():
    p = cli._build_parser()
    args = p.parse_args(["quickcode", "build a thing"])
    assert args.command == "quickcode"
    assert args.prompt == "build a thing"
    # Defaults match the plan.
    assert args.effort == "medium"
    assert args.max_refine == 2
    assert args.no_refine is False
    assert args.remote is None


def test_parser_no_refine_overrides_max_refine_at_dispatch_time():
    """argparse layer keeps --max-refine; the dispatcher clamps after."""
    p = cli._build_parser()
    args = p.parse_args(["quickcode", "x", "--max-refine", "3", "--no-refine"])
    # argparse stores both verbatim — no_refine wins inside _dispatch_quickcode.
    assert args.no_refine is True
    assert args.max_refine == 3


def test_parser_clamps_max_refine_via_dispatch(monkeypatch):
    """_dispatch_quickcode normalizes max_refine to [0,3]."""
    p = cli._build_parser()
    args = p.parse_args(["quickcode", "x", "--max-refine", "99"])

    # Stub the in-process runner so we just check the normalization.
    async def fake_run(a):
        assert a.max_refine == 3  # clamped
        return 0

    monkeypatch.setattr(cli, "_run_quickcode_in_process", fake_run)
    rc = asyncio.get_event_loop().run_until_complete(cli._dispatch_quickcode(args))
    assert rc == 0


def test_parser_no_refine_zeros_max_refine_via_dispatch(monkeypatch):
    p = cli._build_parser()
    args = p.parse_args(["quickcode", "x", "--max-refine", "2", "--no-refine"])

    async def fake_run(a):
        assert a.max_refine == 0
        return 0

    monkeypatch.setattr(cli, "_run_quickcode_in_process", fake_run)
    rc = asyncio.get_event_loop().run_until_complete(cli._dispatch_quickcode(args))
    assert rc == 0


def test_parser_consortium_implementation_engine_flag():
    """The new --implementation-engine flag lands on the consortium parser."""
    p = cli._build_parser()
    args = p.parse_args([
        "consortium", "build me a CSV diff tool",
        "--implementation-engine", "quick_code",
    ])
    assert args.implementation_engine == "quick_code"


def test_parser_consortium_engine_default():
    p = cli._build_parser()
    args = p.parse_args(["consortium", "build me a thing"])
    assert args.implementation_engine == "code_intelligence"


def test_parser_rejects_invalid_engine_choice():
    p = cli._build_parser()
    with pytest.raises(SystemExit):
        p.parse_args([
            "consortium", "build a thing",
            "--implementation-engine", "made_up_engine",
        ])


# ─── in-process runner ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_quickcode_in_process_dispatches_engine(monkeypatch, tmp_path):
    """_run_quickcode_in_process constructs a QuickCodeEngine, awaits
    its run(), and writes an artifact bundle through the route helper."""
    constructed: dict[str, Any] = {}

    class _SpyEngine:
        def __init__(self, *, session_id, request, on_event=None, **kw):
            constructed["session_id"] = session_id
            constructed["request"] = request

        async def run(self):
            from document_processor.quick_code.models import (
                QuickCodeAlternative, QuickCodeBundle, QuickCodeGate,
                QuickCodeReasoning, QuickCodeRequest, QuickCodeVerification,
            )
            return QuickCodeBundle(
                session_id="qc-1",
                request=QuickCodeRequest(prompt="x"),
                reasoning=QuickCodeReasoning(
                    alternatives=[QuickCodeAlternative(
                        label="A",
                        scores={"clarity": 0.9, "math_soundness": 0.9,
                                "performance": 0.7, "edge_cases": 0.8},
                    )],
                    chosen_label="A",
                ),
                code="print('hi')\n",
                tests="def test(): assert 1\n",
                verification=QuickCodeVerification(score=85.0),
                gates=[QuickCodeGate(
                    phase="verify", status="passed", score=85.0, summary="ok",
                )],
            )

    # Patch the lazy import target.
    import document_processor.quick_code as qc_mod
    monkeypatch.setattr(qc_mod, "QuickCodeEngine", _SpyEngine)

    # No-op the artifact writer — its plumbing is tested elsewhere.
    async def _no_artifact(*a, **kw):
        return None
    from document_processor.api import quick_code_routes
    monkeypatch.setattr(quick_code_routes, "_write_artifact", _no_artifact)

    p = cli._build_parser()
    args = p.parse_args([
        "quickcode", "implement merge sort",
        "--output", str(tmp_path / "out"),
        "--quiet",  # don't pollute test output
    ])
    args.max_refine = 2  # mimic _dispatch_quickcode normalisation
    rc = await cli._run_quickcode_in_process(args)
    assert rc == 0
    # Engine was constructed with the user's prompt.
    assert constructed["request"].prompt == "implement merge sort"


@pytest.mark.asyncio
async def test_run_quickcode_in_process_returns_1_when_gate_failed(
    monkeypatch, tmp_path,
):
    """A failed gate must surface as a non-zero exit so CI picks it up."""
    class _FailingEngine:
        def __init__(self, **kw):
            self._on_event = kw.get("on_event")

        async def run(self):
            from document_processor.quick_code.models import (
                QuickCodeBundle, QuickCodeGate, QuickCodeRequest,
            )
            return QuickCodeBundle(
                session_id="qc-2",
                request=QuickCodeRequest(prompt="x"),
                gates=[QuickCodeGate(
                    phase="verify", status="failed", score=30.0,
                    summary="exec failed",
                )],
            )

    import document_processor.quick_code as qc_mod
    monkeypatch.setattr(qc_mod, "QuickCodeEngine", _FailingEngine)

    async def _no_artifact(*a, **kw): return None
    from document_processor.api import quick_code_routes
    monkeypatch.setattr(quick_code_routes, "_write_artifact", _no_artifact)

    p = cli._build_parser()
    args = p.parse_args([
        "quickcode", "x", "--output", str(tmp_path / "out"), "--quiet",
    ])
    args.max_refine = 0
    rc = await cli._run_quickcode_in_process(args)
    assert rc == 1


@pytest.mark.asyncio
async def test_run_quickcode_emit_json(monkeypatch, tmp_path, capsys):
    """--json prints the bundle as a single JSON envelope to stdout."""
    class _Engine:
        def __init__(self, **kw): pass

        async def run(self):
            from document_processor.quick_code.models import (
                QuickCodeBundle, QuickCodeRequest,
            )
            return QuickCodeBundle(
                session_id="qc-j",
                request=QuickCodeRequest(prompt="x"),
            )

    import document_processor.quick_code as qc_mod
    monkeypatch.setattr(qc_mod, "QuickCodeEngine", _Engine)

    async def _no_artifact(*a, **kw): return None
    from document_processor.api import quick_code_routes
    monkeypatch.setattr(quick_code_routes, "_write_artifact", _no_artifact)

    p = cli._build_parser()
    args = p.parse_args([
        "quickcode", "x", "--output", str(tmp_path / "out"),
        "--json", "--quiet",
    ])
    args.max_refine = 0
    rc = await cli._run_quickcode_in_process(args)
    assert rc == 0
    captured = capsys.readouterr()
    # Stdout is a single JSON envelope (json.dump uses indent=2 so it's
    # multi-line — strip + parse the whole thing).
    payload = json.loads(captured.out.strip())
    assert payload["session_id"] == "qc-j"


# ─── _print_quick_event ─────────────────────────────────────────────


def test_print_quick_event_renders_reasoning_alternatives(capsys):
    """The reason phase event must show one row per alternative with
    score badges and a ← chosen tick on the picked one."""
    cli._print_quick_event({
        "type": "quick_code_phase_complete",
        "phase": "reason",
        "reasoning": {
            "alternatives": [
                {"label": "A",
                 "scores": {"clarity": 0.8, "math_soundness": 0.9,
                            "performance": 0.6, "edge_cases": 0.7},
                 "composite_score": 0.78},
                {"label": "B",
                 "scores": {"clarity": 0.65, "math_soundness": 0.95,
                            "performance": 0.85, "edge_cases": 0.80},
                 "composite_score": 0.81},
            ],
            "chosen_label": "B",
            "rationale": "B has stronger numerical guarantees.",
        },
    })
    out = capsys.readouterr().out
    assert "◆ A" in out
    assert "◆ B" in out
    # The chosen row carries the tick.
    assert "← chosen" in out
    # Composite scores rendered.
    assert "0.78" in out and "0.81" in out
    # Rationale visible.
    assert "stronger numerical" in out


def test_print_quick_event_completed_summary(capsys):
    cli._print_quick_event({
        "type": "quick_code_completed",
        "code_chars": 120, "tests_chars": 80,
    })
    out = capsys.readouterr().out
    assert "done" in out
    assert "120" in out and "80" in out


def test_print_quick_event_refine_iteration(capsys):
    cli._print_quick_event({
        "type": "quick_code_refine_iteration",
        "iteration": 1, "improved": True,
    })
    out = capsys.readouterr().out
    assert "refine iter 1" in out
    assert "improved" in out


# ─── remote runner (stubbed httpx) ──────────────────────────────────


@pytest.mark.asyncio
async def test_run_quickcode_remote_returns_zero_on_completed(
    monkeypatch, tmp_path,
):
    """The remote runner POSTs to /start, then streams /events. A
    quick_code_completed event must yield exit code 0."""
    # Build a fake httpx async client that:
    #   1. responds 200 to POST /start with a session_id
    #   2. streams a single SSE block ending in quick_code_completed
    sse_body = (
        'data: {"type":"quick_code_started","session_id":"r1","event_id":"e1"}'
        "\n\n"
        'data: {"type":"quick_code_completed","status":"ok","event_id":"e2",'
        '"code_chars":10,"tests_chars":5}'
        "\n\n"
    )

    class _PostResp:
        status_code = 200
        text = '{"session_id":"r1"}'
        headers = {"x-model-used": "qwen2.5:7b"}

        def json(self): return {"session_id": "r1"}

    class _StreamResp:
        status_code = 200

        async def aiter_text(self):
            yield sse_body

        async def __aenter__(self): return self

        async def __aexit__(self, *a): return None

    class _FakeClient:
        def __init__(self, *a, **kw): pass

        async def __aenter__(self): return self

        async def __aexit__(self, *a): return None

        async def post(self, url, json=None, headers=None):
            assert url.endswith("/api/quick-code/start")
            return _PostResp()

        def stream(self, method, url, headers=None):
            assert method == "GET"
            assert "/events" in url
            return _StreamResp()

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeClient
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    p = cli._build_parser()
    args = p.parse_args([
        "quickcode", "implement softmax", "--remote",
        "http://example.com", "--quiet",
    ])
    args.max_refine = 0
    rc = await cli._run_quickcode_remote(args)
    assert rc == 0


# ─── consortium --implementation-engine pass-through ────────────────


@pytest.mark.asyncio
async def test_consortium_in_process_passes_engine_to_scope(
    monkeypatch, tmp_path,
):
    """The --implementation-engine flag must reach ConsortiumScope."""
    captured: dict[str, Any] = {}

    class _StubOrch:
        def __init__(self, *, session_id, scope, on_event=None, artifact_dir=None):
            captured["scope"] = scope

        async def run(self):
            from document_processor.consortium.models import ConsortiumBundle
            return ConsortiumBundle(
                session_id="cons-1", scope=captured["scope"],
            )

    from document_processor import consortium as cons_pkg
    monkeypatch.setattr(cons_pkg, "ConsortiumOrchestrator", _StubOrch)

    p = cli._build_parser()
    args = p.parse_args([
        "consortium", "Build a tiny CSV diff CLI in pure Python",
        "--implementation-engine", "quick_code",
        "--no-research",
        "--output", str(tmp_path / "out"),
        "--quiet",
    ])
    rc = await cli._run_in_process(args)
    assert rc == 0
    assert captured["scope"].implementation_engine == "quick_code"


@pytest.mark.asyncio
async def test_consortium_remote_includes_engine_in_body_when_quick_code(
    monkeypatch, tmp_path,
):
    """The remote consortium body must include implementation_engine
    when the user passed --implementation-engine quick_code."""
    seen_body: dict[str, Any] = {}

    class _PostResp:
        status_code = 200
        text = '{"session_id":"cr1"}'
        headers = {}

        def json(self): return {"session_id": "cr1"}

    class _StreamResp:
        status_code = 200

        async def aiter_text(self):
            yield ('data: {"type":"consortium_completed","status":"ok",'
                   '"event_id":"e1"}\n\n')

        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

    class _FakeClient:
        def __init__(self, *a, **kw): pass

        async def __aenter__(self): return self

        async def __aexit__(self, *a): return None

        async def post(self, url, json=None, headers=None):
            seen_body.update(json or {})
            return _PostResp()

        def stream(self, method, url, headers=None):
            return _StreamResp()

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeClient
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    p = cli._build_parser()
    args = p.parse_args([
        "consortium", "Implement a token bucket rate limiter cleanly",
        "--implementation-engine", "quick_code",
        "--remote", "http://example.com", "--quiet",
    ])
    rc = await cli._run_remote(args)
    assert rc == 0
    assert seen_body.get("implementation_engine") == "quick_code"


@pytest.mark.asyncio
async def test_consortium_remote_omits_engine_when_default(
    monkeypatch, tmp_path,
):
    """Omitting --implementation-engine (or passing the default) must
    NOT include the field in the wire body — keeps backward-compat
    with older servers that don't know about the field."""
    seen_body: dict[str, Any] = {}

    class _PostResp:
        status_code = 200
        text = '{"session_id":"cr2"}'
        headers = {}

        def json(self): return {"session_id": "cr2"}

    class _StreamResp:
        status_code = 200

        async def aiter_text(self):
            yield ('data: {"type":"consortium_completed","status":"ok"}\n\n')

        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

    class _FakeClient:
        def __init__(self, *a, **kw): pass

        async def __aenter__(self): return self

        async def __aexit__(self, *a): return None

        async def post(self, url, json=None, headers=None):
            seen_body.update(json or {})
            return _PostResp()

        def stream(self, method, url, headers=None):
            return _StreamResp()

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeClient
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    p = cli._build_parser()
    args = p.parse_args([
        "consortium", "Build a thing",
        "--remote", "http://example.com", "--quiet",
    ])
    rc = await cli._run_remote(args)
    assert rc == 0
    assert "implementation_engine" not in seen_body
