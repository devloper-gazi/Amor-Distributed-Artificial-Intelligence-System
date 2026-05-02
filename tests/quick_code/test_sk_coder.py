"""
Unit tests for ``document_processor/quick_code/sk_coder.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from document_processor.quick_code.contracts import CodeSnippet, TaskIR
from document_processor.quick_code.sk_coder import SkCoder


def _run(coro):
    return asyncio.run(coro)


def _ir(prompt: str) -> TaskIR:
    return TaskIR(id="t1", prompt=prompt)


# ─────────────────────────────────────────────────────────────────────
# Empty corpus → fill-here hint
# ─────────────────────────────────────────────────────────────────────


def test_empty_corpus_returns_fill_here_hint():
    sk = SkCoder(corpus=[])
    snippets, hint = _run(sk.retrieve_or_hint(_ir("reverse a string")))
    assert snippets == []
    assert hint is not None
    assert hint.startswith("<FILL_HERE:")


def test_empty_prompt_returns_fill_here_hint():
    sk = SkCoder(
        corpus=[
            {"source": "def reverse(s): return s[::-1]"},
        ]
    )
    snippets, hint = _run(sk.retrieve_or_hint(_ir("   ")))
    assert snippets == []
    assert hint is not None


# ─────────────────────────────────────────────────────────────────────
# Above-floor retrieval
# ─────────────────────────────────────────────────────────────────────


def test_high_overlap_prompt_returns_snippets():
    corpus = [
        {
            "source": "def reverse(s): return s[::-1]",
            "summary": "reverse a string",
            "source_path": "examples/reverse.py",
        },
        {
            "source": "def merge(a, b): return sorted(a + b)",
            "summary": "merge two sorted lists",
        },
    ]
    sk = SkCoder(corpus=corpus, alpha_floor=0.10, top_k=2, beta=1.0)
    snippets, hint = _run(sk.retrieve_or_hint(_ir("reverse a string")))
    assert hint is None
    assert len(snippets) >= 1
    # The top hit should be the reverse snippet.
    assert snippets[0].source.startswith("def reverse")
    assert isinstance(snippets[0], CodeSnippet)


def test_high_floor_filters_weak_matches():
    """With α floor 0.95, no snippet should be returned for a
    poorly-matching prompt.  Forces the FILL_HERE path."""
    corpus = [{"source": "def foo(): pass", "summary": "foo helper"}]
    sk = SkCoder(corpus=corpus, alpha_floor=0.95)
    snippets, hint = _run(sk.retrieve_or_hint(_ir("design a kafka pipeline")))
    assert snippets == []
    assert hint is not None
    assert "design a kafka pipeline" in hint


# ─────────────────────────────────────────────────────────────────────
# Top-K cap
# ─────────────────────────────────────────────────────────────────────


def test_top_k_cap_respected():
    corpus = [
        {"source": f"def reverse_{i}(s): return s[::-1]", "summary": "reverse a string"}
        for i in range(10)
    ]
    sk = SkCoder(corpus=corpus, top_k=3, alpha_floor=0.0)
    snippets = _run(sk.retrieve(_ir("reverse a string")))
    assert len(snippets) == 3


# ─────────────────────────────────────────────────────────────────────
# Score fields populated
# ─────────────────────────────────────────────────────────────────────


def test_snippet_carries_individual_scores():
    corpus = [
        {"source": "def reverse(s): return s[::-1]", "summary": "reverse a string"},
    ]
    sk = SkCoder(corpus=corpus, alpha_floor=0.0)
    snippets = _run(sk.retrieve(_ir("reverse a string")))
    assert snippets
    s = snippets[0]
    assert s.bm25_score >= 0
    assert 0.0 <= s.cosine_score <= 1.0
    assert s.score >= 0


# ─────────────────────────────────────────────────────────────────────
# No-filter prompt sanity
# ─────────────────────────────────────────────────────────────────────


def test_module_docstring_no_refusal_language():
    import document_processor.quick_code.sk_coder as mod

    text = (mod.__doc__ or "").lower()
    for token in ("i cannot", "i won't", "consult a lawyer", "if illegal"):
        assert token not in text
