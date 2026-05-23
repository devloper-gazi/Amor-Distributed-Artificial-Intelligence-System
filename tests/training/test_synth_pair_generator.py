"""Cycle G G5 — coverage for the synthetic preference-pair generator.

The Plan-agent flagged corpus famine as the HIGH-risk dependency of
G5 LoRA training.  This generator is the Day-1 contingency: re-run
the Sprint-0 prompts at temp=0.0 (chosen) + temp=0.7 (rejected) to
synthesise a starter corpus while real MessageActions ratings
accumulate.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tools.training import synth_pair_generator as spg


# ─── Endpoint resolver (falsy-skip pattern) ────────────────────────


def test_llm_base_url_skips_empty_env(monkeypatch):
    monkeypatch.setenv("AMOR_LLM_BACKEND_URL", "")
    monkeypatch.setenv("AMOR_LLAMASWAP_URL", "http://override:9100")
    assert spg._llm_base_url() == "http://override:9100"


def test_llm_base_url_default(monkeypatch):
    monkeypatch.delenv("AMOR_LLM_BACKEND_URL", raising=False)
    monkeypatch.delenv("AMOR_LLAMASWAP_URL", raising=False)
    assert spg._llm_base_url() == "http://amor-llama-swap:9100"


def test_llm_model_default(monkeypatch):
    monkeypatch.delenv("AMOR_SYNTH_MODEL", raising=False)
    assert spg._llm_model() == "amor-editor"


def test_llm_model_env_override(monkeypatch):
    monkeypatch.setenv("AMOR_SYNTH_MODEL", "qwen2.5:7b")
    assert spg._llm_model() == "qwen2.5:7b"


# ─── Corpus loader ─────────────────────────────────────────────────


def test_load_corpus_handles_sprint0_shape(tmp_path):
    corpus_file = tmp_path / "corpus.json"
    corpus_file.write_text(json.dumps({
        "prompts": [
            {"prompt": "write fizzbuzz"},
            {"prompt": "reverse a string"},
        ],
    }), encoding="utf-8")
    prompts = spg._load_corpus(corpus_file)
    assert prompts == ["write fizzbuzz", "reverse a string"]


def test_load_corpus_handles_flat_list(tmp_path):
    corpus_file = tmp_path / "flat.json"
    corpus_file.write_text(json.dumps([
        {"prompt": "task A"},
        {"prompt": "task B"},
        "task C",
    ]), encoding="utf-8")
    prompts = spg._load_corpus(corpus_file)
    assert prompts == ["task A", "task B", "task C"]


def test_load_corpus_missing_file_returns_empty(tmp_path):
    assert spg._load_corpus(tmp_path / "nonexistent.json") == []


def test_load_corpus_unparseable_returns_empty(tmp_path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not-json-content", encoding="utf-8")
    assert spg._load_corpus(bad_file) == []


def test_load_corpus_skips_items_without_prompt_field(tmp_path):
    corpus_file = tmp_path / "mixed.json"
    corpus_file.write_text(json.dumps([
        {"prompt": "real prompt"},
        {"id": "missing-prompt-field"},   # gets skipped
        {"text": "alt key"},              # 'text' is accepted as alt
    ]), encoding="utf-8")
    prompts = spg._load_corpus(corpus_file)
    assert "real prompt" in prompts
    assert "alt key" in prompts
    assert len(prompts) == 2


# ─── synth_one_pair shape ──────────────────────────────────────────


def test_synth_one_pair_shape_with_stubbed_completions(monkeypatch):
    """A valid pair has prompt + chosen + rejected + temps + model
    + hash; synthetic=True flag tags it for downstream filtering."""

    class StubClient:
        async def post(self, *a, **k):
            body = k.get("json") or {}
            class R:
                def raise_for_status(self): pass
                def json(self):
                    return {
                        "choices": [{
                            "message": {
                                "content": (
                                    "chosen-output"
                                    if body.get("temperature") == 0.0
                                    else "rejected-output"
                                ),
                            },
                        }],
                    }
            return R()

    async def driver():
        return await spg.synth_one_pair(
            StubClient(), "http://x", "amor-editor",
            "write a function",
        )

    row = asyncio.run(driver())
    assert row["prompt"] == "write a function"
    assert row["chosen"] == "chosen-output"
    assert row["rejected"] == "rejected-output"
    assert row["synthetic"] is True
    assert row["chosen_temp"] == 0.0
    assert row["rejected_temp"] == 0.7
    assert row["model"] == "amor-editor"
    assert len(row["hash"]) == 32


def test_synth_one_pair_handles_http_error(monkeypatch):
    """When the LLM call fails, return empty strings — caller decides
    whether to skip writing this pair."""
    import httpx as _httpx

    class FailingClient:
        async def post(self, *a, **k):
            raise _httpx.ConnectError("conn refused")

    async def driver():
        return await spg.synth_one_pair(
            FailingClient(), "http://x", "m", "prompt",
        )

    row = asyncio.run(driver())
    assert row["chosen"] == ""
    assert row["rejected"] == ""
    # Still tagged for downstream filtering
    assert row["synthetic"] is True


# ─── generate_pairs end-to-end ─────────────────────────────────────


def test_generate_pairs_writes_jsonl(monkeypatch, tmp_path):
    """End-to-end: corpus on disk + mocked LLM → JSONL appears with
    the right shape + row count."""
    corpus_file = tmp_path / "corpus.json"
    corpus_file.write_text(json.dumps({
        "prompts": [{"prompt": "task A"}, {"prompt": "task B"}],
    }), encoding="utf-8")
    out_file = tmp_path / "pairs.jsonl"

    class StubResp:
        def __init__(self, content): self._c = content
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": self._c}}]}

    class StubClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **k):
            body = k.get("json") or {}
            if body.get("temperature") == 0.0:
                return StubResp("ans-clean")
            return StubResp("ans-varied")

    monkeypatch.setattr(spg.httpx, "AsyncClient", StubClient)

    summary = asyncio.run(spg.generate_pairs(
        corpus_path=corpus_file,
        out_path=out_file,
        pairs_per_prompt=3,
    ))
    assert summary["ok"] is True
    assert summary["pairs_written"] == 6   # 2 prompts × 3 pairs
    assert summary["pairs_failed"] == 0

    rows = [json.loads(l) for l in out_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 6
    for row in rows:
        assert row["chosen"] == "ans-clean"
        assert row["rejected"] == "ans-varied"
        assert row["synthetic"] is True


def test_generate_pairs_skips_failed_completions(monkeypatch, tmp_path):
    """When one completion fails (empty string), the pair is dropped
    from the JSONL but failed count is recorded."""
    corpus_file = tmp_path / "corpus.json"
    corpus_file.write_text(
        json.dumps({"prompts": [{"prompt": "p"}]}),
        encoding="utf-8",
    )
    out_file = tmp_path / "out.jsonl"

    class FailingResp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": ""}}]}

    class StubClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **k): return FailingResp()

    monkeypatch.setattr(spg.httpx, "AsyncClient", StubClient)

    summary = asyncio.run(spg.generate_pairs(
        corpus_path=corpus_file,
        out_path=out_file,
        pairs_per_prompt=2,
    ))
    assert summary["ok"] is True
    assert summary["pairs_written"] == 0
    assert summary["pairs_failed"] == 2
    # File NOT created in append mode when no writes? — it IS opened
    # but no content written.  Either is fine; assert nothing.


def test_generate_pairs_returns_error_when_corpus_empty(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    out_file = tmp_path / "out.jsonl"
    summary = asyncio.run(spg.generate_pairs(
        corpus_path=empty, out_path=out_file,
    ))
    assert summary["ok"] is False
    assert "no prompts" in summary["error"]


def test_generate_pairs_respects_max_prompts(monkeypatch, tmp_path):
    """``max_prompts`` caps the corpus for smoke runs."""
    corpus_file = tmp_path / "corpus.json"
    corpus_file.write_text(json.dumps({
        "prompts": [{"prompt": f"t{i}"} for i in range(10)],
    }), encoding="utf-8")
    out_file = tmp_path / "out.jsonl"

    class StubResp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class StubClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **k): return StubResp()

    monkeypatch.setattr(spg.httpx, "AsyncClient", StubClient)

    summary = asyncio.run(spg.generate_pairs(
        corpus_path=corpus_file, out_path=out_file,
        pairs_per_prompt=1, max_prompts=3,
    ))
    assert summary["prompts"] == 3
    assert summary["pairs_written"] == 3
