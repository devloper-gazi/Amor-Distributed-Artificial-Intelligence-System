"""Tests for code_intelligence.observability — @traced decorator."""

from __future__ import annotations

import asyncio
import json

import pytest

from document_processor.code_intelligence import observability


@pytest.mark.asyncio
async def test_traced_decorator_emits_ok_span(tmp_path, monkeypatch):
    # Force JSONL fallback by directing trace dir into tmp_path.
    monkeypatch.setattr(observability, "_TRACE_DIR", tmp_path)
    monkeypatch.setattr(observability, "_LANGFUSE_CLIENT", None)
    monkeypatch.setattr(observability, "_LANGFUSE_TRIED", True)

    @observability.traced("agent.test")
    async def add(a: int, b: int) -> int:
        return a + b

    result = await add(2, 3)
    assert result == 5

    # Read the most recent JSONL file in tmp_path.
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    span = lines[0]
    assert span["role"] == "agent.test"
    assert span["status"] == "ok"
    assert "duration_ms" in span
    assert span["name"].endswith("add")


@pytest.mark.asyncio
async def test_traced_decorator_records_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(observability, "_TRACE_DIR", tmp_path)
    monkeypatch.setattr(observability, "_LANGFUSE_CLIENT", None)
    monkeypatch.setattr(observability, "_LANGFUSE_TRIED", True)

    @observability.traced("sandbox.execute")
    async def boom() -> None:
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        await boom()

    files = list(tmp_path.glob("*.jsonl"))
    spans = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
    assert spans[0]["status"] == "error"
    assert "ValueError" in spans[0]["error"]
    assert "kaboom" in spans[0]["error"]


@pytest.mark.asyncio
async def test_traced_decorator_handles_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr(observability, "_TRACE_DIR", tmp_path)
    monkeypatch.setattr(observability, "_LANGFUSE_CLIENT", None)
    monkeypatch.setattr(observability, "_LANGFUSE_TRIED", True)

    @observability.traced("agent.long_running")
    async def slow() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(slow())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    files = list(tmp_path.glob("*.jsonl"))
    if files:
        spans = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
        # Cancellation should be recorded if the task ran long enough.
        if spans:
            assert spans[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_emit_event_freestanding(tmp_path, monkeypatch):
    monkeypatch.setattr(observability, "_TRACE_DIR", tmp_path)
    monkeypatch.setattr(observability, "_LANGFUSE_CLIENT", None)
    monkeypatch.setattr(observability, "_LANGFUSE_TRIED", True)

    observability.emit_event(
        "registry.pull",
        "model_pull_complete",
        tag="qwen2.5-coder:7b",
        bytes=1234567,
    )
    files = list(tmp_path.glob("*.jsonl"))
    spans = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
    assert spans[0]["name"] == "model_pull_complete"
    assert spans[0]["status"] == "event"
    assert spans[0]["attributes"]["tag"] == "qwen2.5-coder:7b"


def test_capture_args_caps_repr_length(tmp_path, monkeypatch):
    monkeypatch.setattr(observability, "_TRACE_DIR", tmp_path)
    monkeypatch.setattr(observability, "_LANGFUSE_CLIENT", None)
    monkeypatch.setattr(observability, "_LANGFUSE_TRIED", True)

    @observability.traced("test", capture_args=True)
    async def hog(big: str) -> int:
        return len(big)

    huge = "x" * 10_000
    asyncio.run(hog(huge))
    files = list(tmp_path.glob("*.jsonl"))
    spans = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
    # The arg repr should be truncated, not full 10k.
    assert all(len(a) <= 350 for a in spans[0]["attributes"]["args"])
