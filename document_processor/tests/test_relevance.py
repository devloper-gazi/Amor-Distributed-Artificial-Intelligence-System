"""
Unit tests for `document_processor.research.relevance`.

These tests are offline by design — no Ollama, no Redis, no network.
Run them with:

    docker exec amor-app-1 python -m pytest document_processor/tests/test_relevance.py -v

(Or locally with the project's virtualenv activated; pytest-asyncio is
already in requirements.txt.)
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from dataclasses import dataclass
from typing import Optional

import pytest

from document_processor.research.relevance import (
    RelevanceConfig,
    RelevanceFilter,
    RelevanceResult,
)


# A miniature stand-in for the production Source dataclass — we don't
# import the real one because we want these tests to remain unit-pure
# (no dependency on advanced_researcher or its imports).
@dataclass
class FakeSource:
    id: int
    url: str
    title: str
    content: str = ""
    snippet: str = ""
    domain: str = ""
    relevance: float = 0.0
    sub_question_index: int = 0
    findings: str = ""
    original_language: Optional[str] = None
    translated: bool = False


# ─── helpers ────────────────────────────────────────────────────────────────


def _run(coro):
    """
    Run an async coroutine to completion in a fresh event loop.
    asyncio.run() is the supported sync→async bridge in 3.10+ and
    avoids the DeprecationWarning that asyncio.get_event_loop() now
    emits when no loop is running (will become a hard error in 3.14).
    """
    return asyncio.run(coro)


def _highly_relevant_source(qid: int = 1) -> FakeSource:
    return FakeSource(
        id=qid,
        url=f"https://en.wikipedia.org/wiki/Photosynthesis_{qid}",
        title="Photosynthesis — biological process in plants",
        domain="en.wikipedia.org",
        content=(
            "Photosynthesis is the biological process by which plants, algae and "
            "some bacteria convert light energy into chemical energy. The process "
            "captures sunlight and uses water and carbon dioxide to produce glucose "
            "and oxygen as byproducts. The light-dependent reactions occur in the "
            "thylakoid membranes; the Calvin cycle uses ATP and NADPH to fix CO2."
        ) * 4,
    )


def _irrelevant_source(qid: int = 99) -> FakeSource:
    return FakeSource(
        id=qid,
        url=f"https://example.com/celebrity_news_{qid}",
        title="Top 10 celebrity gossip moments of 2024",
        domain="example.com",
        content=(
            "Hollywood was buzzing this year with red carpet drama, "
            "viral tweets and a string of awards-night surprises. "
            "Reality TV stars made appearances on late-night shows."
        ) * 3,
    )


# ─── tests ──────────────────────────────────────────────────────────────────


def test_keeps_highly_relevant_source():
    """1. Highly relevant source clears the threshold."""
    rfilter = RelevanceFilter(config=RelevanceConfig(
        tier="deep", max_sources=10, min_score=0.10,
    ))
    result = _run(rfilter.filter_sources(
        query="how does photosynthesis work in plants",
        sub_questions=[
            "What is the role of sunlight in photosynthesis?",
            "What molecules are produced by photosynthesis?",
        ],
        sources=[_highly_relevant_source()],
    ))

    assert isinstance(result, RelevanceResult)
    assert result.fallback_used is False
    assert result.original_count == 1
    assert result.selected_count == 1
    assert len(result.selected_sources) == 1


def test_rejects_irrelevant_source():
    """2. Obviously off-topic source is dropped."""
    rfilter = RelevanceFilter(config=RelevanceConfig(
        tier="deep", max_sources=10, min_score=0.10,
    ))
    result = _run(rfilter.filter_sources(
        query="how does photosynthesis work in plants",
        sub_questions=[
            "What is the role of sunlight in photosynthesis?",
        ],
        sources=[_irrelevant_source()],
    ))

    assert result.selected_count == 0
    assert result.rejected_count == 1
    assert len(result.selected_sources) == 0


def test_handles_missing_fields_gracefully():
    """3. Missing title / content / url do not raise; weak source is excluded."""
    bare = FakeSource(id=1, url="", title="", content="", domain="")
    rfilter = RelevanceFilter(config=RelevanceConfig(
        tier="deep", max_sources=10, min_score=0.10,
    ))
    result = _run(rfilter.filter_sources(
        query="quantum entanglement",
        sub_questions=[],
        sources=[bare],
    ))

    assert result.fallback_used is False
    assert result.selected_count == 0
    assert result.rejected_count == 1


def test_dedupes_duplicate_urls():
    """4. Two sources with identical URL collapse to one."""
    a = _highly_relevant_source(qid=1)
    b = _highly_relevant_source(qid=2)
    b.url = a.url  # force collision
    rfilter = RelevanceFilter(config=RelevanceConfig(
        tier="deep", max_sources=10, min_score=0.05,
    ))
    result = _run(rfilter.filter_sources(
        query="how does photosynthesis work in plants",
        sub_questions=[],
        sources=[a, b],
    ))

    urls = {s.url for s in result.selected_sources}
    assert len(urls) == 1
    assert result.selected_count == 1


def test_fail_open_on_scorer_exception(monkeypatch):
    """5. If the scoring path explodes, we return all sources untouched."""
    rfilter = RelevanceFilter(config=RelevanceConfig(
        tier="deep", max_sources=10, fail_open=True,
    ))
    # Patch the internal scorer to blow up on every call.
    import document_processor.research.relevance as rel
    monkeypatch.setattr(
        rel,
        "_score_one",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    sources = [_highly_relevant_source(qid=i) for i in range(3)]
    result = _run(rfilter.filter_sources(
        query="anything",
        sub_questions=[],
        sources=sources,
    ))

    assert result.fallback_used is True
    assert result.selected_count == 3
    assert len(result.errors) == 1
    # Identity preserved: same Source instances came back.
    assert all(a is b for a, b in zip(result.selected_sources, sources))


def test_tier_cap_is_applied_in_score_order():
    """6. With 30 sources and cap=8, exactly 8 are kept (top scores)."""
    relevant = [_highly_relevant_source(qid=i) for i in range(20)]
    irrelevant = [_irrelevant_source(qid=100 + i) for i in range(10)]

    rfilter = RelevanceFilter(config=RelevanceConfig(
        tier="deep", max_sources=8, min_score=0.05,
    ))
    result = _run(rfilter.filter_sources(
        query="how does photosynthesis work in plants",
        sub_questions=[],
        sources=relevant + irrelevant,
    ))

    assert result.selected_count == 8
    # All kept sources should be from the relevant pool — irrelevant
    # ones either fall under min_score or get sorted below the cap.
    assert all(
        "photosynthesis" in s.title.lower() or "wikipedia" in s.url
        for s in result.selected_sources
    )


def test_works_without_sklearn(monkeypatch):
    """7. Project intentionally has no sklearn — verify import is clean."""
    monkeypatch.delitem(sys.modules, "sklearn", raising=False)
    monkeypatch.delitem(sys.modules, "document_processor.research.relevance", raising=False)
    rel = importlib.import_module("document_processor.research.relevance")

    rfilter = rel.RelevanceFilter(config=rel.RelevanceConfig(
        tier="deep", max_sources=10, min_score=0.10,
    ))
    src = FakeSource(
        id=1,
        url="https://en.wikipedia.org/wiki/Photosynthesis",
        title="Photosynthesis",
        content="Photosynthesis is a biological process in plants.",
        domain="en.wikipedia.org",
    )
    result = _run(rfilter.filter_sources(
        query="photosynthesis",
        sub_questions=[],
        sources=[src],
    ))
    assert result.fallback_used is False
    assert result.selected_count >= 0  # smoke: just doesn't crash


def test_domain_bonus_recognizes_wikipedia_without_www():
    """
    9. Regression: `lstrip("www.")` is a character-set strip, not a
    prefix strip. With the buggy version, "wikipedia.org" was mangled
    to "ikipedia.org" and the trust pattern never matched. This test
    asserts the .wikipedia.org bonus actually fires for both the
    bare and www-prefixed forms.
    """
    from document_processor.research.relevance import _domain_bonus

    assert _domain_bonus("wikipedia.org") > 0
    assert _domain_bonus("en.wikipedia.org") > 0
    assert _domain_bonus("www.wikipedia.org") > 0
    assert _domain_bonus("wikipedia.org") >= _domain_bonus("example.com")
    # Other trusted domains should also work whether or not they're
    # prefixed with www.
    assert _domain_bonus("nasa.gov") > 0
    assert _domain_bonus("www.nasa.gov") > 0
    assert _domain_bonus("mit.edu") > 0
    # Untrusted domain still gets zero.
    assert _domain_bonus("example.com") == 0
    assert _domain_bonus("") == 0


def test_bad_page_penalty_catches_404_in_title_only():
    """
    11. Regression: bad-page detector originally only inspected the
    first 600 chars of body. A page titled "404 — Not Found" with
    a normal-looking body (e.g. "Search results for ...") slipped
    through unscathed. Now the title is folded into the inspected
    head so title-only signals also trigger the penalty.
    """
    from document_processor.research.relevance import _bad_page_penalty

    # Title-only marker triggers penalty.
    assert _bad_page_penalty("Some search results unrelated", title="404 — Not Found") > 0
    assert _bad_page_penalty("Continue to your dashboard", title="Sign in to MyService") > 0
    # Body-only still works (existing behaviour).
    assert _bad_page_penalty("Page Not Found. The requested page does not exist.") > 0
    # Clean page → zero penalty.
    assert _bad_page_penalty(
        "Photosynthesis converts light into chemical energy.",
        title="Photosynthesis",
    ) == 0
    # Both empty → zero.
    assert _bad_page_penalty("", title="") == 0


def test_llm_cache_key_no_delimiter_collision():
    """
    12. Regression: previous cache-key composition was
    f"{model}|{system or ''}|{prompt}|{max_tokens}|{temp}" — a `|`
    inside `prompt` or `system` could compose to the same string as
    a different (system, prompt) pair, returning a wrong-cache-hit.
    The JSON-list serialization fix makes that impossible. This
    test asserts two such pathologically-shaped (but distinct)
    inputs yield DIFFERENT keys.
    """
    from document_processor.api.local_ai_routes_simple import _llm_cache_key

    # Old impl: both produce "model|A|B|C|256|0.7" → collision.
    # New impl: JSON list embeds string boundaries → distinct hashes.
    k1 = _llm_cache_key(prompt="B|C", system="A",   max_tokens=256)
    k2 = _llm_cache_key(prompt="C",   system="A|B", max_tokens=256)
    assert k1 != k2, "cache key collision: prompt vs system boundary leaked"

    # Same inputs → same key (determinism).
    assert _llm_cache_key("hello", "you are helpful", 100) == \
           _llm_cache_key("hello", "you are helpful", 100)

    # Different max_tokens or system → different keys.
    assert _llm_cache_key("hello", "sys", 100) != _llm_cache_key("hello", "sys", 200)
    assert _llm_cache_key("hello", "sys-a", 100) != _llm_cache_key("hello", "sys-b", 100)

    # Key always begins with the documented prefix.
    assert _llm_cache_key("x", None, 1).startswith("llm:")


def test_basic_tier_passes_through():
    """13. tier=basic short-circuits to passthrough regardless of score."""
    rfilter = RelevanceFilter(config=RelevanceConfig(
        tier="basic", max_sources=8, min_score=0.99,  # impossible threshold
    ))
    sources = [_irrelevant_source(qid=i) for i in range(4)]
    result = _run(rfilter.filter_sources(
        query="utterly different topic",
        sub_questions=[],
        sources=sources,
        tier="basic",
    ))

    assert result.selected_count == 4
    assert result.rejected_count == 0
    assert "passthrough" in result.method_summary
