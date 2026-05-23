"""Cycle I.2 — Titans test-time predictive memory coverage.

Tests focus on the similarity-based recall + bounded-window
semantics + fail-safe fallbacks.  No real embedder — we inject
a deterministic stub so tests are reproducible.
"""

from __future__ import annotations

import asyncio
import math
from typing import List, Sequence

import pytest


# ─── Stub embedder ──────────────────────────────────────────────────


def _make_word_embedder():
    """Return an EmbedderFn that maps each input text to a tiny vector
    whose components match the count of each "feature" word.  This
    keeps similarity assertions trivially predictable in tests.

    Feature dimensions: ["python", "rust", "test", "deploy", "shadow"]
    """
    features = ["python", "rust", "test", "deploy", "shadow"]

    async def _embed(text: str) -> Sequence[float]:
        text = (text or "").lower()
        vec = [float(text.count(f)) for f in features]
        return vec

    return _embed


# ─── Math primitives ────────────────────────────────────────────────


def test_cosine_similarity_identical_vectors_returns_1():
    from document_processor.memory.titans_predictive import cosine_similarity
    assert cosine_similarity([1, 0, 0], [1, 0, 0]) == 1.0


def test_cosine_similarity_orthogonal_vectors_returns_0():
    from document_processor.memory.titans_predictive import cosine_similarity
    assert cosine_similarity([1, 0], [0, 1]) == 0.0


def test_cosine_similarity_zero_vector_returns_0():
    from document_processor.memory.titans_predictive import cosine_similarity
    assert cosine_similarity([0, 0], [1, 1]) == 0.0
    assert cosine_similarity([1, 1], []) == 0.0
    assert cosine_similarity([1, 1], [1]) == 0.0       # length mismatch


def test_cosine_similarity_partial_overlap():
    """1/√2 for unit vectors at 45° → cosine = 0.707."""
    from document_processor.memory.titans_predictive import cosine_similarity
    score = cosine_similarity([1, 1, 0], [1, 0, 0])
    assert abs(score - (1 / math.sqrt(2))) < 1e-6


# ─── Memory record + recall ─────────────────────────────────────────


def test_record_appends_to_window():
    from document_processor.memory.titans_predictive import TitansPredictiveMemory
    mem = TitansPredictiveMemory(embedder=_make_word_embedder(), max_window=10)
    assert mem.size == 0
    asyncio.run(mem.record("write python tests", role="prompt"))
    asyncio.run(mem.record("deploy the rust service", role="prompt"))
    assert mem.size == 2


def test_record_caps_at_max_window():
    """Adding > max_window entries drops the oldest (deque maxlen)."""
    from document_processor.memory.titans_predictive import TitansPredictiveMemory
    mem = TitansPredictiveMemory(embedder=_make_word_embedder(), max_window=3)
    for i in range(10):
        asyncio.run(mem.record(f"entry {i} python", role="prompt"))
    assert mem.size == 3       # capped


def test_record_tolerates_embedder_failure():
    """If the embedder raises, the entry is still appended (empty
    embedding) — Plan-agent locked: best-effort writes never crash."""
    from document_processor.memory.titans_predictive import TitansPredictiveMemory

    async def _broken(_text):
        raise RuntimeError("simulated embedder failure")

    mem = TitansPredictiveMemory(embedder=_broken)
    asyncio.run(mem.record("python tests", role="prompt"))
    assert mem.size == 1


def test_recall_returns_most_similar_entries():
    from document_processor.memory.titans_predictive import TitansPredictiveMemory
    mem = TitansPredictiveMemory(embedder=_make_word_embedder(), recall_k=2)
    asyncio.run(mem.record("python python tests", role="a"))
    asyncio.run(mem.record("rust rust deploy", role="b"))
    asyncio.run(mem.record("python deploy shadow", role="c"))

    hits = asyncio.run(mem.recall("python tests"))
    assert len(hits) == 2
    # "python python tests" is the closest match.
    top_entry, top_score = hits[0]
    assert top_entry.role == "a"
    assert top_score > hits[1][1]


def test_recall_respects_min_score_threshold():
    """Entries below min_score are dropped — better to return [] than
    flood the planner prompt with weak matches."""
    from document_processor.memory.titans_predictive import TitansPredictiveMemory
    mem = TitansPredictiveMemory(
        embedder=_make_word_embedder(),
        recall_k=10,
        min_score=0.99,        # very strict
    )
    asyncio.run(mem.record("rust", role="a"))
    asyncio.run(mem.record("deploy shadow", role="b"))
    # query has no overlap with stored entries → 0 sim → drop
    hits = asyncio.run(mem.recall("python python tests"))
    assert hits == []


def test_recall_empty_window_returns_empty_list():
    from document_processor.memory.titans_predictive import TitansPredictiveMemory
    mem = TitansPredictiveMemory(embedder=_make_word_embedder())
    hits = asyncio.run(mem.recall("anything"))
    assert hits == []


def test_recall_tolerates_embedder_failure():
    """If the embedder fails on the query, recall returns [] — does
    NOT raise."""
    from document_processor.memory.titans_predictive import TitansPredictiveMemory

    call_count = {"n": 0}

    async def _flaky(text):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise RuntimeError("simulated query embed failure")
        # first call (during record) succeeds
        return [1.0, 0.0, 0.0, 0.0, 0.0]

    mem = TitansPredictiveMemory(embedder=_flaky)
    asyncio.run(mem.record("python"))
    hits = asyncio.run(mem.recall("python"))
    assert hits == []


def test_recall_skips_entries_with_empty_embeddings():
    """A record() call whose embedder failed leaves an entry with
    no embedding — recall must skip it (would otherwise produce
    spurious zero-similarity hits)."""
    from document_processor.memory.titans_predictive import TitansPredictiveMemory

    class _PartialFailEmbedder:
        def __init__(self):
            self.calls = 0

        async def __call__(self, text):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("first call fails")
            return [float(text.count("python")), 0.0]

    emb = _PartialFailEmbedder()
    mem = TitansPredictiveMemory(embedder=emb, recall_k=5)
    asyncio.run(mem.record("python", role="bad"))     # no embedding
    asyncio.run(mem.record("python", role="good"))    # has embedding
    hits = asyncio.run(mem.recall("python"))
    # Only the good one shows up.
    assert len(hits) == 1
    assert hits[0][0].role == "good"


# ─── Markdown rendering ─────────────────────────────────────────────


def test_recall_as_markdown_emits_short_block():
    from document_processor.memory.titans_predictive import TitansPredictiveMemory
    mem = TitansPredictiveMemory(embedder=_make_word_embedder(), recall_k=2)
    asyncio.run(mem.record("python tests fixtures", role="a"))
    asyncio.run(mem.record("python deploy", role="b"))
    md = asyncio.run(mem.recall_as_markdown("python tests"))
    assert "Recalled context from past sessions" in md
    assert "(a, sim=" in md


def test_recall_as_markdown_empty_returns_empty_string():
    """No hits → empty string (caller skips the injection entirely)."""
    from document_processor.memory.titans_predictive import TitansPredictiveMemory
    mem = TitansPredictiveMemory(embedder=_make_word_embedder())
    md = asyncio.run(mem.recall_as_markdown("anything"))
    assert md == ""


def test_recall_as_markdown_truncates_long_content():
    """Each entry is capped at ``per_entry_chars`` so a single huge
    past session doesn't dominate the planner prompt."""
    from document_processor.memory.titans_predictive import TitansPredictiveMemory
    big = "python " * 200       # ~1400 chars
    mem = TitansPredictiveMemory(embedder=_make_word_embedder(), recall_k=1)
    asyncio.run(mem.record(big, role="bloat"))
    md = asyncio.run(mem.recall_as_markdown("python", per_entry_chars=80))
    # Line containing the entry must be much shorter than the original.
    body = [ln for ln in md.splitlines() if "bloat" in ln][0]
    assert len(body) < 200


# ─── clear() + size ─────────────────────────────────────────────────


def test_clear_resets_window():
    from document_processor.memory.titans_predictive import TitansPredictiveMemory
    mem = TitansPredictiveMemory(embedder=_make_word_embedder())
    asyncio.run(mem.record("python", role="a"))
    asyncio.run(mem.record("rust", role="b"))
    assert mem.size == 2
    mem.clear()
    assert mem.size == 0


# ─── Factory ────────────────────────────────────────────────────────


def test_make_memory_from_settings_reads_pydantic_settings():
    """The factory pulls max_window + recall_k + min_score from the
    Pydantic settings — operator can tune them via env override."""
    from document_processor.memory.titans_predictive import make_memory_from_settings
    from document_processor.config.settings import settings
    # Sanity-check defaults are in place (Plan-agent locked).
    assert settings.code_titans_enabled is False
    assert settings.code_titans_recall_k == 3
    assert settings.code_titans_max_window == 200
    assert settings.code_titans_min_score == 0.20

    mem = make_memory_from_settings(embedder=_make_word_embedder())
    assert mem._max_window == settings.code_titans_max_window
    assert mem._recall_k == settings.code_titans_recall_k


def test_make_memory_from_settings_falls_back_when_settings_broken(monkeypatch):
    """If settings raises mid-resolution, the factory returns a
    default-config TitansPredictiveMemory (max_window=200, k=3)."""
    import document_processor.memory.titans_predictive as mod
    # Force the import to raise so the fallback path fires.
    import sys
    monkeypatch.setitem(sys.modules, "document_processor.config.settings", None)
    mem = mod.make_memory_from_settings(embedder=_make_word_embedder())
    assert mem._max_window == 200
    assert mem._recall_k == 3


# ─── Engine wiring (Cycle I.2 hook) ──────────────────────────────────


def _build_engine_with_titans(memory):
    """Mirror the cortex-routing test fixture for Titans engine hook."""
    from document_processor.code_intelligence.engine import CodeIntelligenceEngine

    async def _stub_llm(prompt, system, max_tokens):
        return (
            '{"language":"python","title":"x",'
            '"summary":"x","steps":["a"],"spec":{"dependencies":[]}}'
        )

    eng = CodeIntelligenceEngine(
        prompt="how do I write python tests?",
        code_context=None,
        language="python",
        effort="medium",
        provider="local",
        llm_call=_stub_llm,
        sandbox=None,
        static_harness=None,
        enable_execution=False,
        enable_static_analysis=False,
        enable_testing=False,
        max_debug_iterations=0,
    )
    eng._role_setter = lambda _role: None
    eng.triage = {"domain": None}
    eng.attach_titans_memory(memory)
    return eng


def test_attach_titans_memory_sets_handle():
    """``attach_titans_memory(mem)`` is the route-layer setter."""
    from document_processor.memory.titans_predictive import TitansPredictiveMemory
    from document_processor.code_intelligence.engine import CodeIntelligenceEngine

    async def _llm(p, s, m):
        return "{}"

    eng = CodeIntelligenceEngine(
        prompt="x", code_context=None, language="python",
        effort="medium", provider="local", llm_call=_llm,
    )
    assert eng._titans_memory is None
    mem = TitansPredictiveMemory(embedder=_make_word_embedder())
    eng.attach_titans_memory(mem)
    assert eng._titans_memory is mem


def test_engine_hook_no_op_when_disabled(monkeypatch):
    """``code_titans_enabled=False`` (default) → recall path is a
    no-op; ``code_context`` stays untouched."""
    from document_processor.config.settings import settings
    from document_processor.memory.titans_predictive import TitansPredictiveMemory

    monkeypatch.setattr(settings, "code_titans_enabled", False)

    mem = TitansPredictiveMemory(embedder=_make_word_embedder())
    asyncio.run(mem.record("python python tests prior context", role="prompt"))

    eng = _build_engine_with_titans(mem)
    original_ctx = eng.code_context
    asyncio.run(eng._phase_plan())
    assert eng.code_context == original_ctx       # no injection


def test_engine_hook_injects_when_enabled(monkeypatch):
    """With Titans enabled + a recall hit, the plan phase prepends
    the recall markdown to ``code_context``."""
    from document_processor.config.settings import settings
    from document_processor.memory.titans_predictive import TitansPredictiveMemory

    monkeypatch.setattr(settings, "code_titans_enabled", True)
    monkeypatch.setattr(settings, "code_titans_min_score", 0.0)

    mem = TitansPredictiveMemory(
        embedder=_make_word_embedder(),
        recall_k=2,
        min_score=0.0,
    )
    asyncio.run(mem.record(
        "earlier session about python tests with pytest fixtures",
        role="prior",
    ))

    eng = _build_engine_with_titans(mem)
    asyncio.run(eng._phase_plan())
    assert eng.code_context is not None
    assert "Recalled context from past sessions" in eng.code_context


def test_engine_hook_failsafe_when_recall_explodes(monkeypatch):
    """If the Titans recall raises mid-call, the engine MUST still
    proceed with the plan phase — Plan-agent locked: never fail
    closed on best-effort augmentation."""
    from document_processor.config.settings import settings
    from document_processor.memory.titans_predictive import TitansPredictiveMemory

    monkeypatch.setattr(settings, "code_titans_enabled", True)

    class _Boom(TitansPredictiveMemory):
        async def recall_as_markdown(self, *a, **kw):
            raise RuntimeError("simulated recall failure")

    mem = _Boom(embedder=_make_word_embedder())
    eng = _build_engine_with_titans(mem)
    original_ctx = eng.code_context
    # The plan phase must NOT raise even though recall is broken.
    asyncio.run(eng._phase_plan())
    # No injection, but the plan still produced a result.
    assert eng.plan is not None
    assert eng.code_context == original_ctx
