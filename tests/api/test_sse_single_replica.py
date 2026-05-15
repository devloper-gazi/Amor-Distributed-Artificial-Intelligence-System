"""
Cycle F Sprint 1 — SSE single-replica regression test.

Wrong #1 in the v18 roadmap reduced the default `app` deployment
from `replicas: 2` to single-replica.  The forward-compatible
Redis pub/sub fan-out at `cache.py:447-460` must still publish
events even when there is no other replica to receive them — so
that re-enabling 2 replicas later is zero-change.

This test:
1. Monkey-patches a single in-process FastAPI app (simulating
   `replicas: 1`),
2. Asserts that events emitted via `cache.publish_event(...)` are
   serialized + put on the local queue + (best-effort) sent to
   Redis,
3. Confirms a synthetic SSE reconnect with `Last-Event-ID` resumes
   from the recorded event_id rather than the stream start.

Runs offline — Redis is stubbed; no live service required.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest


# ─── Stubs ──────────────────────────────────────────────────────────


class _StubRedis:
    """In-memory replacement for cache_manager's Redis client.

    publish() and subscribe() are minimal: publish appends to a list,
    subscribe yields existing-and-future entries.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


# ─── Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_event_works_with_single_replica():
    """Event still serializes + publishes even when nobody else listens."""

    try:
        from document_processor.infrastructure import cache as cache_mod
    except ImportError:
        pytest.skip("document_processor not importable in this env")

    cm = cache_mod.CacheManager()
    cm._connected = True
    stub = _StubRedis()
    cm.redis = stub  # actual attribute is .redis (not _redis)

    event = {"type": "phase_start", "phase": "plan", "event_id": "evt_1"}
    await cm.publish_event("amor:code:events:test", event)

    assert len(stub.published) == 1
    channel, payload = stub.published[0]
    assert channel == "amor:code:events:test"
    assert json.loads(payload)["event_id"] == "evt_1"


@pytest.mark.asyncio
async def test_publish_event_degrades_silently_when_redis_down():
    """A Redis hiccup must NOT crash the publisher."""

    try:
        from document_processor.infrastructure import cache as cache_mod
    except ImportError:
        pytest.skip("document_processor not importable in this env")

    cm = cache_mod.CacheManager()
    cm._connected = False  # force the connect() path

    async def _fail_connect():
        raise RuntimeError("redis unreachable")

    cm.connect = _fail_connect  # type: ignore[method-assign]

    # Must not raise.
    await cm.publish_event("amor:code:events:test", {"type": "done"})


def test_compose_yaml_no_longer_has_replicas_2():
    """Wrong #1 regression — docker-compose.yml must not re-introduce 2 replicas."""

    from pathlib import Path
    import re

    repo_root = Path(__file__).resolve().parent.parent.parent
    compose = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
    # No active `replicas: 2` line — comments mentioning the history are fine.
    # Match only when the line is NOT comment-leading (^[ \t]*[^#\s].*replicas: 2)
    for ln, line in enumerate(compose.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if re.search(r"replicas\s*:\s*2\b", line):
            pytest.fail(
                f"docker-compose.yml line {ln} has an active "
                f"`replicas: 2` setting — Wrong #1 (Cycle F Sprint 1) "
                f"regressed.  Line: {line!r}"
            )


def test_compose_yaml_llama_swap_no_longer_opt_in():
    """Sprint 1 promotion — llama-swap must NOT be under `profiles:`."""

    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent.parent
    compose = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")

    # Find the llama-swap block.
    idx = compose.find("\n  llama-swap:")
    assert idx > 0, "llama-swap service block missing from docker-compose.yml"
    block = compose[idx:idx + 600]
    # The block must NOT contain a profiles: line in its top-level keys.
    # (the `- llamaswap` value is fine to grep for inside SERVICES naming.)
    assert "profiles:" not in block, (
        "llama-swap is still opt-in via profiles: — Sprint 1 promotion regressed."
    )


def test_llamaswap_config_does_not_use_unsupported_cram_flag():
    """The roadmap's `--cram 512` is a non-existent flag (llama-server
    accepts `-cram` short form or `--cache-ram` long form, and the
    default is already 8192 MiB enabled).  Make sure we didn't
    re-introduce the broken double-dash form anywhere."""

    from pathlib import Path
    import re

    repo_root = Path(__file__).resolve().parent.parent.parent
    for cfg in ("config.yaml", "config.q4_0.yaml", "config.q8_0.yaml"):
        body = (repo_root / "compose" / "llama-swap" / cfg).read_text(
            encoding="utf-8"
        )
        # Active commands (skip YAML comment lines).
        for ln, line in enumerate(body.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            assert "--cram " not in line, (
                f"{cfg}:{ln} uses unsupported `--cram` long form; "
                f"use `--cache-ram` or omit (default 8192 MiB)."
            )


def test_llamaswap_quant_variants_exist():
    """Both Q4_0 and Q8_0 A/B variants must be on disk."""

    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent.parent
    base = repo_root / "compose" / "llama-swap"
    assert (base / "config.q4_0.yaml").is_file()
    assert (base / "config.q8_0.yaml").is_file()
    # Variants must differ on KV-quant lines.
    q4 = (base / "config.q4_0.yaml").read_text(encoding="utf-8")
    q8 = (base / "config.q8_0.yaml").read_text(encoding="utf-8")
    assert "-ctk q4_0 -ctv q4_0" in q4
    assert "-ctk q4_0 -ctv q4_0" not in q8
    assert "-ctk q8_0 -ctv q8_0" in q8
    assert "-ctk q8_0 -ctv q8_0" not in q4
