"""v18.1 Step 6 (Cycle G) — coverage for the SWE-bench-Lite-25 runner.

What's being tested
-------------------
The runner wires the Cycle C scaffold into a real callable.  We exercise:

  * INSTANCE_IDS_25 has the expected 25 instances spread across 5 repos
  * Metadata loader produces well-shaped rows even without the live
    fixture file
  * Patch-generation HTTP failure surfaces as empty patch (not crash)
  * Diff extraction handles fenced + raw responses
  * SIMPLIFIED mode produces predictions JSONL with resolved=False
  * Manifest registration switches runner from None to live
  * Full-harness fallback skips cleanly when swebench library missing
  * Aggregation math matches humaneval_plus.py's percentile helper
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Force a temporary out-root so the test doesn't pollute the real
# data/eval_runs/ directory.  Set BEFORE importing the module so the
# module-level DATA_OUT_ROOT picks it up.
@pytest.fixture(autouse=True)
def isolated_eval_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AMOR_EVAL_OUT_ROOT", str(tmp_path))
    # The module computed DATA_OUT_ROOT at import time; patch it.
    from tools.eval import swebench_lite
    monkeypatch.setattr(swebench_lite, "DATA_OUT_ROOT", tmp_path)
    return tmp_path


# ─── Curated list ──────────────────────────────────────────────────


def test_instance_ids_25_count():
    from tools.eval.swebench_lite import INSTANCE_IDS_25
    assert len(INSTANCE_IDS_25) == 25


def test_instance_ids_spread_across_5_repos():
    from tools.eval.swebench_lite import INSTANCE_IDS_25
    repos = {iid.split("__")[0] for iid in INSTANCE_IDS_25}
    expected = {"django", "sympy", "pytest-dev", "scikit-learn", "psf"}
    assert repos == expected


def test_each_repo_has_5_instances():
    from tools.eval.swebench_lite import INSTANCE_IDS_25
    counts: dict[str, int] = {}
    for iid in INSTANCE_IDS_25:
        repo = iid.split("__")[0]
        counts[repo] = counts.get(repo, 0) + 1
    assert all(v == 5 for v in counts.values()), counts


# ─── Metadata loader ───────────────────────────────────────────────


def test_metadata_loader_produces_stub_when_fixture_missing(monkeypatch, tmp_path):
    """When tests/eval/swebench_lite_25_metadata.json doesn't exist,
    the loader produces a stub for every curated instance so the
    runner still has shape to work with."""
    from tools.eval import swebench_lite

    # Point the metadata path at a guaranteed-missing location.
    monkeypatch.setattr(
        swebench_lite, "INSTANCE_METADATA_PATH",
        tmp_path / "definitely_missing.json",
    )
    md = swebench_lite._load_instance_metadata()
    assert len(md) == 25
    for iid in swebench_lite.INSTANCE_IDS_25:
        assert iid in md
        assert md[iid]["instance_id"] == iid
        assert md[iid]["base_commit"] == "PENDING"


def test_metadata_loader_reads_fixture_when_present(monkeypatch, tmp_path):
    from tools.eval import swebench_lite
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps([
        {
            "instance_id": "django__django-11099",
            "repo": "django/django",
            "base_commit": "abc123",
            "problem_statement": "Fix the thing.",
            "version": "3.0",
        },
    ]), encoding="utf-8")
    monkeypatch.setattr(swebench_lite, "INSTANCE_METADATA_PATH", fixture)
    md = swebench_lite._load_instance_metadata()
    assert md["django__django-11099"]["base_commit"] == "abc123"


# ─── Diff extraction ───────────────────────────────────────────────


def test_extract_diff_block_handles_fenced_diff():
    from tools.eval.swebench_lite import _extract_diff_block
    text = "Sure:\n```diff\ndiff --git a/x b/x\n@@\n-old\n+new\n```\nDone."
    out = _extract_diff_block(text)
    assert out.startswith("diff --git")
    assert "-old" in out and "+new" in out


def test_extract_diff_block_handles_unfenced_response():
    from tools.eval.swebench_lite import _extract_diff_block
    raw = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    out = _extract_diff_block(raw)
    assert "diff --git" in out


def test_extract_diff_block_handles_empty():
    from tools.eval.swebench_lite import _extract_diff_block
    assert _extract_diff_block("") == ""
    assert _extract_diff_block(None) == ""  # defensive


# ─── LLM endpoint resolution ───────────────────────────────────────


def test_llm_base_url_skips_empty_env_vars(monkeypatch):
    """Same bug as humaneval_plus.py — empty AMOR_LLM_BACKEND_URL must
    fall through to the next fallback instead of returning ''."""
    from tools.eval import swebench_lite
    monkeypatch.setenv("AMOR_LLM_BACKEND_URL", "")
    monkeypatch.setenv("AMOR_LLAMASWAP_URL", "http://override:9100")
    assert swebench_lite._llm_base_url() == "http://override:9100"


def test_llm_base_url_falls_through_to_default(monkeypatch):
    from tools.eval import swebench_lite
    monkeypatch.delenv("AMOR_LLM_BACKEND_URL", raising=False)
    monkeypatch.delenv("AMOR_LLAMASWAP_URL", raising=False)
    assert swebench_lite._llm_base_url() == "http://amor-llama-swap:9100"


# ─── Patch generation ──────────────────────────────────────────────


def test_generate_patch_returns_empty_on_http_error(monkeypatch):
    from tools.eval import swebench_lite
    import httpx as _httpx

    class FailingClient:
        async def post(self, *a, **k):
            raise _httpx.ConnectError("conn refused")

    async def driver():
        return await swebench_lite._generate_patch(
            FailingClient(),
            "http://x:9100",
            "amor-editor",
            {"instance_id": "x", "problem_statement": "go", "base_commit": "abc"},
            timeout_s=5.0,
        )

    patch_text, ms = asyncio.run(driver())
    assert patch_text == ""
    assert ms > 0


def test_generate_patch_extracts_content_on_success(monkeypatch):
    from tools.eval import swebench_lite

    class OKResponse:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._payload

    class StubClient:
        async def post(self, *a, **k):
            return OKResponse({
                "choices": [{
                    "message": {
                        "content": "```diff\ndiff --git a/x b/x\n-old\n+new\n```",
                    },
                }],
            })

    async def driver():
        return await swebench_lite._generate_patch(
            StubClient(),
            "http://x:9100",
            "amor-editor",
            {"instance_id": "x", "problem_statement": "go", "base_commit": "abc"},
        )

    patch_text, ms = asyncio.run(driver())
    assert "diff --git" in patch_text


# ─── Simplified instance evaluation ────────────────────────────────


def test_simplified_evaluation_marks_resolved_false():
    """SIMPLIFIED mode never runs tests; every case must report
    resolved=False so the v18.1 launch gate sees a real number
    (not a placeholder) and the operator knows to flip to
    FULL_HARNESS for non-zero rates."""
    from tools.eval import swebench_lite

    class StubClient:
        async def post(self, *a, **k):
            class R:
                def raise_for_status(self):
                    pass
                def json(self):
                    return {"choices": [{"message": {"content": "```diff\nx\n```"}}]}
            return R()

    async def driver():
        return await swebench_lite._evaluate_simplified_instance(
            StubClient(),
            "http://x:9100",
            "amor-editor",
            {"instance_id": "y", "problem_statement": "go", "base_commit": "abc"},
        )

    case = asyncio.run(driver())
    assert case["resolved"] is False
    assert case["instance_id"] == "y"
    assert "model_patch" in case
    assert "wall_ms" in case
    assert isinstance(case["patch_empty"], bool)


# ─── Full-harness availability detection ───────────────────────────


def test_full_harness_skipped_when_swebench_missing(monkeypatch):
    from tools.eval import swebench_lite
    monkeypatch.setattr(swebench_lite, "_swebench_library_available", lambda: False)

    async def driver():
        return await swebench_lite._evaluate_with_harness(
            Path("/tmp/predictions.jsonl"),
            "test_run",
            AsyncMock(),
        )

    result = asyncio.run(driver())
    assert result["harness"] == "missing"


# ─── Top-level runner end-to-end (mocked LLM) ──────────────────────


def test_run_swebench_lite_simplified_end_to_end(monkeypatch, tmp_path):
    """Smoke test the top-level runner in SIMPLIFIED mode against a
    mocked LLM client and limit=3 instances.  Verifies:
      * predictions JSONL is written
      * summary has the expected shape
      * resolved=0 (simplified mode) but resolved_rate is set
      * mode field surfaces 'simplified'
    """
    from tools.eval import swebench_lite

    monkeypatch.setenv("AMOR_EVAL_LIMIT", "3")
    monkeypatch.setenv("AMOR_LLM_BACKEND_URL", "http://stub:9100")
    monkeypatch.delenv("AMOR_SWEBENCH_FULL_HARNESS", raising=False)
    monkeypatch.setattr(swebench_lite, "DATA_OUT_ROOT", tmp_path)

    class StubResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {
                "choices": [{
                    "message": {"content": "```diff\ndiff --git a/x b/x\n+new\n```"},
                }],
            }

    class StubClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **k):
            return StubResponse()

    monkeypatch.setattr(swebench_lite.httpx, "AsyncClient", StubClient)

    async def driver():
        progress_log: list[str] = []
        async def progress(msg):
            progress_log.append(msg)
        summary = await swebench_lite.run_swebench_lite(
            "test_run_id", progress,
        )
        return summary, progress_log

    summary, log = asyncio.run(driver())
    assert summary["total"] == 3
    assert summary["resolved"] == 0     # simplified mode
    assert summary["resolved_rate"] == 0.0
    assert summary["mode"] == "simplified"
    assert "p50_ms" in summary
    assert "p95_ms" in summary

    # Predictions JSONL was written.
    predictions = tmp_path / "swebench_lite" / "predictions_test_run_id.jsonl"
    assert predictions.is_file()
    lines = predictions.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        row = json.loads(line)
        assert "model_patch" in row
        assert row["resolved"] is False


# ─── Manifest registration ─────────────────────────────────────────


def test_swebench_eval_registered_with_live_runner():
    """v18.1 — the descriptor must now ship with ``runner=run_swebench_lite``
    (not None like the Cycle C scaffold).  Without this the kick endpoint
    keeps returning 503."""
    from tools.eval import swebench_lite
    from document_processor.api.admin_evals_routes import _EVAL_MANIFEST

    descriptor = _EVAL_MANIFEST.get("swebench_lite_25")
    assert descriptor is not None, "swebench_lite_25 not registered"
    assert descriptor.runner is not None, (
        "swebench_lite_25 runner is None — v18.1 Step 6 hasn't shipped, "
        "or import order broke registration"
    )
    assert descriptor.runner is swebench_lite.run_swebench_lite


def test_swebench_summary_keys_match_v18_gate():
    """The v18 launch gate reads `resolved_rate_percent` from the
    eval_runs/swebench_lite/latest.json shape.  Our summary keys must
    include it so the export_latest.py bridge picks it up."""
    from document_processor.api.admin_evals_routes import _EVAL_MANIFEST

    descriptor = _EVAL_MANIFEST.get("swebench_lite_25")
    assert descriptor is not None
    assert "resolved_rate_percent" in descriptor.summary_keys
    assert "resolved" in descriptor.summary_keys
    assert "total" in descriptor.summary_keys
