"""Fast live sandbox smoke — Phase 16.5 Commit H/J.

Hits ``ExecutionSandbox.execute()`` directly inside the running
amor-app container so we can verify in seconds (not minutes) that:

* the docker CLI is on PATH (Commit H),
* /var/run/docker.sock is reachable, and
* a tiny Python snippet actually runs and returns its stdout.

This is the fastest possible regression net for the
``docker_unavailable`` failure mode the user reported.

Gated by ``AMOR_LIVE_TESTS=1``.
"""

from __future__ import annotations

import asyncio
import os

import pytest


_LIVE = os.environ.get("AMOR_LIVE_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set AMOR_LIVE_TESTS=1 to exercise the sandbox in-container",
)


def _run(coro):
    return asyncio.run(coro)


def test_sandbox_executes_python_hello_world():
    """End-to-end: ExecutionSandbox spins a python:3.11-slim
    container and runs the supplied code with --network=none."""
    from document_processor.code_intelligence.sandbox import ExecutionSandbox

    sandbox = ExecutionSandbox()
    result = _run(sandbox.execute(
        code='print("sandbox-ok-42")',
        language="python",
        timeout=30,
    ))
    assert not result.skipped, (
        f"sandbox skipped — Commit H may have regressed.  "
        f"error={result.error!r} stderr={result.stderr[:200]!r}"
    )
    assert result.error != "docker_unavailable", (
        f"sandbox claims docker unavailable inside the container "
        f"({result.error!r}).  docker-cli installation broke."
    )
    assert "sandbox-ok-42" in (result.stdout or ""), (
        f"sandbox ran but produced unexpected stdout: "
        f"{result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.exit_code == 0, (
        f"sandbox returned non-zero exit code {result.exit_code} "
        f"with stderr={result.stderr!r}"
    )


def test_sandbox_enforces_network_isolation():
    """The ``--network=none`` flag must really cut off network so
    a generated payload can't phone home."""
    from document_processor.code_intelligence.sandbox import ExecutionSandbox

    sandbox = ExecutionSandbox()
    result = _run(sandbox.execute(
        code=(
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
            "    print('NETWORK_LEAK')\n"
            "except OSError:\n"
            "    print('isolation-ok')\n"
        ),
        language="python",
        timeout=30,
    ))
    assert not result.skipped
    assert "NETWORK_LEAK" not in (result.stdout or ""), (
        f"sandbox lets out outbound network — security regression.  "
        f"stdout={result.stdout!r}"
    )
    assert "isolation-ok" in (result.stdout or "")
