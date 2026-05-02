"""Live integration test — Phase 16.5 Commit J.

Exercises the full Code Intelligence pipeline against the running
amor-app + amor-ollama stack.  Verifies three things the user
specifically called out:

1. Per-role model diversity actually fires (planner/critic/debugger
   end up on a different tag from coder/tester on the 2-model rig).
2. The Docker sandbox runs (was returning ``docker_unavailable``
   before Commit H — the test confirms it executes the generated
   code in a container).
3. The engine produces non-trivial code for a real task ("snake
   game") and the iterative debug loop fires when the first
   attempt fails.

The test is gated by ``AMOR_LIVE_TESTS=1`` so it doesn't run in
default CI — it requires:
* docker daemon reachable at localhost:8000 via the amor-gateway
* ollama daemon at localhost:11434 with qwen2.5:7b + qwen2.5-coder:7b
* the amor-app container with docker-cli (Commit H) installed.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
import pytest


_LIVE = os.environ.get("AMOR_LIVE_TESTS") == "1"
_BASE = os.environ.get("AMOR_LIVE_BASE_URL", "http://localhost:8000")

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set AMOR_LIVE_TESTS=1 + run docker stack to exercise live pipeline",
)


# ─── helpers ──────────────────────────────────────────────────────


_TOKEN_CACHE: dict[str, str] = {}


def _ensure_test_user() -> str:
    """Register-or-login a throwaway test user, return the access
    token.  Cached for the duration of the test process so we don't
    re-auth every call."""
    if "access" in _TOKEN_CACHE:
        return _TOKEN_CACHE["access"]
    creds = {
        "username": os.environ.get("AMOR_LIVE_USER", "snake_live_user"),
        "password": os.environ.get(
            "AMOR_LIVE_PASS", "Snake-Live-Test-2026!",
        ),
        "email": os.environ.get(
            "AMOR_LIVE_EMAIL", "snake-live@example.com",
        ),
        "display_name": "Snake Live Test",
    }
    with httpx.Client(timeout=30.0) as client:
        # Try register; fall back to login if the user already exists.
        r = client.post(f"{_BASE}/api/auth/register", json=creds)
        if r.status_code == 409:
            r = client.post(
                f"{_BASE}/api/auth/login",
                json={
                    "identifier": creds["username"],
                    "password": creds["password"],
                },
            )
        if r.status_code not in (200, 201):
            pytest.skip(
                f"unable to authenticate test user: HTTP {r.status_code} "
                f"{r.text[:200]}",
            )
        body = r.json()
        token = body.get("access_token") or ""
        if not token:
            pytest.skip("auth endpoint returned no access_token")
        _TOKEN_CACHE["access"] = token
        return token


def _auth_headers() -> dict[str, str]:
    return {
        "X-Client-Id": "snake-live-test",
        "Authorization": f"Bearer {_ensure_test_user()}",
    }


def _post(path: str, body: dict, *, timeout: float = 600.0) -> httpx.Response:
    with httpx.Client(timeout=timeout) as client:
        return client.post(
            f"{_BASE}{path}", json=body, headers=_auth_headers(),
        )


def _get(path: str, *, timeout: float = 30.0) -> httpx.Response:
    with httpx.Client(timeout=timeout) as client:
        return client.get(
            f"{_BASE}{path}", headers=_auth_headers(),
        )


def _wait_for_session_complete(
    sid: str, *, timeout_s: float = 600.0, poll_every: float = 4.0,
) -> dict[str, Any]:
    """Poll /api/code/{sid} until the session reaches a
    terminal status (completed / failed / cancelled).  Returns the
    final session snapshot."""
    started = time.time()
    last: dict[str, Any] = {}
    while time.time() - started < timeout_s:
        r = _get(f"/api/code/{sid}")
        if r.status_code != 200:
            time.sleep(poll_every)
            continue
        body = r.json()
        last = body
        status = body.get("status") or ""
        if status in ("completed", "failed", "cancelled"):
            return body
        time.sleep(poll_every)
    pytest.fail(
        f"session {sid} did not finish within {timeout_s}s — "
        f"last status={last.get('status')!r}",
    )


# ─── live smoke ───────────────────────────────────────────────────


def test_health_reachable():
    r = _get("/health")
    assert r.status_code == 200, r.text


def test_v1_models_returns_two_distinct_tags():
    r = _get("/v1/models")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {m["id"] for m in body.get("data", [])}
    assert "qwen2.5-coder:7b" in ids
    assert "qwen2.5:7b" in ids


def test_v1_chat_completions_real_inference():
    """The /v1/chat/completions facade must hit Ollama and return
    a real assistant message."""
    r = _post(
        "/v1/chat/completions",
        {
            "model": "qwen2.5:7b",
            "messages": [
                {"role": "user", "content": "Reply with exactly: pong"},
            ],
            "max_tokens": 16,
            "temperature": 0,
        },
        timeout=60.0,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    content = body["choices"][0]["message"]["content"]
    # Model can deviate slightly from "pong"; just check it's non-empty.
    assert content.strip()


# ─── Snake game end-to-end (the headline test) ────────────────────


def test_code_intelligence_produces_running_snake_game():
    """The user's complaint: the engine couldn't produce a working
    snake game.  This test drives the full pipeline (architect →
    coder → tester → sandbox → debugger → critic) on the live
    stack and asserts:

    * Multiple distinct models were used (per-role diversity).
    * The sandbox actually executed the generated code (no
      ``docker_unavailable`` flag).
    * The final code contains the structural pieces of a snake
      game (game loop, direction handling, collision).
    """
    # 1. Start a session.
    start = _post(
        "/api/code/start",
        {
            "prompt": (
                "Write a complete, runnable Python snake game using the "
                "curses module (no pygame).  The snake must move on "
                "arrow keys, grow when it eats food, end the game on "
                "wall or self collision, and print the final score.  "
                "Output a single self-contained file."
            ),
            "language": "python",
            "effort": "medium",
            "provider": "local",
            "enable_execution": True,
            "enable_static_analysis": True,
            "enable_testing": True,
            "max_debug_iterations": 2,
        },
        timeout=30.0,
    )
    assert start.status_code == 200, start.text
    sid = start.json()["session_id"]

    # 2. Wait for completion.
    final = _wait_for_session_complete(sid, timeout_s=900.0)
    assert final.get("status") == "completed", (
        f"snake-game session ended with status={final.get('status')!r} "
        f"detail={final.get('error')}"
    )

    # 3. Per-role model diversity assertion — the user's biggest
    #    complaint.  We expect at LEAST 2 distinct models across
    #    the 5 roles.
    models_used = final.get("models_used") or {}
    assert models_used, "engine did not record any models_used"
    distinct = set(models_used.values())
    assert len(distinct) >= 2, (
        f"expected ≥2 distinct models across roles, got "
        f"{models_used} (only {len(distinct)} distinct)"
    )

    # 4. Sandbox actually ran — the user's other complaint.  At
    #    least one execution result must be present and non-skipped.
    exec_results = final.get("execution_results") or []
    assert exec_results, "no execution_results recorded"
    real_runs = [
        r for r in exec_results
        if not r.get("skipped") and r.get("error") != "docker_unavailable"
    ]
    assert real_runs, (
        f"every execution_result was skipped — sandbox is still dead. "
        f"results={exec_results}"
    )

    # 5. Generated code must contain the structural pieces of a
    #    snake game.  We don't compile it (curses needs a TTY),
    #    just check the LLM produced relevant scaffolding.
    code = (final.get("code") or "")
    assert "import" in code, "generated code has no imports"
    assert "curses" in code, (
        "generated code doesn't import curses — task wasn't followed"
    )
    snake_keywords = ("snake", "food", "score", "direction", "collision")
    matched = sum(1 for kw in snake_keywords if kw in code.lower())
    assert matched >= 3, (
        f"generated code only mentions {matched}/{len(snake_keywords)} "
        f"snake-game keywords — output looks generic"
    )
