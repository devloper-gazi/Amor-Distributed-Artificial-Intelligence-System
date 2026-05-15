"""
Tests for AdvancedResearcher's three-tier resilience layer:

1. ``gather()`` last-ditch search variants when the planned variants
   return zero results (e.g. abstract / underspecified queries).
2. ``analyze()`` relevance rescue — keep findings even when every
   source scores below the relevance threshold, rather than dropping
   to a zero-source state.
3. ``synthesize()`` knowledge-only fallback when no sources survive
   to synthesize from — produce a disclaimer-prefixed knowledge-based
   report from the local LLM rather than the legacy brick-wall
   "(no sources)" error.

These three guards ensure abstract queries like "a c++ system for user
guide" never produce the canned "No usable web sources were retrieved"
message the user reported.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

import pytest

from document_processor.research.advanced_researcher import (
    AdvancedResearcher,
    Source,
)


# ─── Test scaffolding ─────────────────────────────────────────────


class _ScriptedLLM:
    """LLM stub that returns canned responses keyed by call order."""

    def __init__(self, responses: List[str]):
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def __call__(
        self, prompt: str, system: Optional[str], max_tokens: int
    ) -> str:
        self.calls.append(
            {"prompt": prompt, "system": system, "max_tokens": max_tokens}
        )
        if not self.responses:
            return ""
        return self.responses.pop(0)


def _events_collector() -> tuple[List[Dict[str, Any]], Callable]:
    events: List[Dict[str, Any]] = []

    async def on_event(ev: Dict[str, Any]) -> None:
        events.append(ev)

    return events, on_event


# ─── 1. Gather rescue ─────────────────────────────────────────────


class TestGatherRescue:
    @pytest.mark.asyncio
    async def test_rescue_search_invoked_when_variants_empty(self):
        """When ALL planned variants return [], the rescue path tries
        the raw query + Wikipedia variant and succeeds."""

        search_calls: List[str] = []

        async def search(q: str, n: int) -> List[Dict[str, str]]:
            search_calls.append(q)
            # Variants 1, 2, 3 return nothing; the FIRST rescue (the raw
            # query) returns one result so the rescue loop short-circuits.
            if q == "a c++ system for user guide":
                return [
                    {"url": "https://example.com/x",
                     "title": "C++ Sys", "snippet": "..."}
                ]
            return []

        async def scrape(urls, n):
            return [
                {"url": u, "content": "C" * 300} for u in urls
            ]

        events, on_event = _events_collector()
        llm = _ScriptedLLM(["q1\nq2\nq3"])

        r = AdvancedResearcher(
            query="a c++ system for user guide",
            depth="medium",
            llm_call=llm,
            web_search=search,
            web_scrape=scrape,
            on_event=on_event,
        )

        await r.plan()
        await r.gather()

        # Rescue search was invoked
        assert any("search_retry" in (e.get("type") or "") for e in events), \
            "expected search_retry event when variants returned empty"
        # Raw-query rescue produced a source
        assert len(r.sources) == 1
        assert r.sources[0].url == "https://example.com/x"

    @pytest.mark.asyncio
    async def test_rescue_emits_phase_warning_when_all_fail(self):
        """When even the rescue queries return [], we emit phase_warning
        (NOT phase_failed) so synthesize() gets to run its knowledge
        fallback instead of the pipeline blowing up."""

        async def search(q, n): return []  # nothing ever found
        async def scrape(urls, n): return []

        events, on_event = _events_collector()
        llm = _ScriptedLLM(["q1\nq2\nq3"])

        r = AdvancedResearcher(
            query="some abstract phrase",
            depth="medium",
            llm_call=llm,
            web_search=search,
            web_scrape=scrape,
            on_event=on_event,
        )
        await r.plan()
        await r.gather()

        # Old behaviour was phase_failed; new behaviour is phase_warning
        assert any(e.get("type") == "phase_warning"
                   and e.get("phase") == "gathering"
                   for e in events)
        # Should NOT emit phase_failed for gathering
        assert not any(e.get("type") == "phase_failed"
                       and e.get("phase") == "gathering"
                       for e in events)
        # gathering still completes (so synthesize is reachable)
        assert any(e.get("type") == "phase_complete"
                   and e.get("phase") == "gathering"
                   for e in events)
        assert r.sources == []


# ─── 2. Analyze relevance rescue ─────────────────────────────────


class TestAnalyzeRescue:
    @pytest.mark.asyncio
    async def test_findings_rescued_when_below_threshold(self):
        """When EVERY source scores below the 0.22 threshold but they
        all DID extract findings, we keep them rather than dropping to
        zero sources."""

        async def search(q, n): return []
        async def scrape(urls, n): return []

        events, on_event = _events_collector()
        # extract returns NOT_RELEVANT → relevance stays at 0.1 (below 0.22)
        # We'll seed self.sources directly to bypass gather().
        llm = _ScriptedLLM(["one finding line"])

        r = AdvancedResearcher(
            query="x",
            depth="medium",
            llm_call=llm,
            web_search=search,
            web_scrape=scrape,
            on_event=on_event,
        )
        # Seed: 1 source with content + sub_question_index
        r.sub_questions = ["What is x?"]
        r.sources = [
            Source(id=1, url="u", title="t", content="c" * 500,
                   sub_question_index=0)
        ]
        await r.analyze()

        # Source kept (rescue path) — 1 finding line yields relevance=0.48
        assert len(r.sources) == 1
        assert r.sources[0].findings  # non-empty

    @pytest.mark.asyncio
    async def test_snippet_stub_when_no_findings_at_all(self):
        """When extract returns NOT_RELEVANT for every source, the
        snippet-stub rescue still produces a usable corpus."""

        async def search(q, n): return []
        async def scrape(urls, n): return []

        events, on_event = _events_collector()
        # First call: planning. After that: NOT_RELEVANT for every analyze
        # call (and any retry).
        llm = _ScriptedLLM(["NOT_RELEVANT"] * 10)

        r = AdvancedResearcher(
            query="x",
            depth="medium",  # medium skips retry pass per existing logic
            llm_call=llm,
            web_search=search,
            web_scrape=scrape,
            on_event=on_event,
        )
        r.sub_questions = ["What is x?"]
        r.sources = [
            Source(id=1, url="u1", title="t1",
                   snippet="snippet1 about x",
                   content="c" * 500, sub_question_index=0),
            Source(id=2, url="u2", title="t2",
                   snippet="snippet2 about x",
                   content="d" * 500, sub_question_index=0),
        ]
        await r.analyze()

        # Snippet-stub rescue: at least 1 source survived with a stub
        assert len(r.sources) >= 1
        assert all(s.findings for s in r.sources)
        assert any("snippet" in s.findings for s in r.sources)
        # Emitted relevance_filter event with rescue reason
        assert any(
            e.get("type") == "relevance_filter"
            and e.get("reason") == "no_findings_use_snippet_stubs"
            for e in events
        )


# ─── 3. Synthesize knowledge fallback ────────────────────────────


class TestKnowledgeFallback:
    @pytest.mark.asyncio
    async def test_zero_source_emits_knowledge_report(self):
        """The legacy 'No usable web sources' message is GONE — we now
        produce a real knowledge-based report with a disclaimer."""

        async def search(q, n): return []
        async def scrape(urls, n): return []

        events, on_event = _events_collector()
        # First: planning sub-questions. Second: knowledge-fallback report.
        llm = _ScriptedLLM([
            "What is x?\nWhy x?\nHow x?",
            "# x\n\n> **Note:** disclaimer.\n\n## Executive Summary\n\n"
            "x is a thing.\n",
        ])

        r = AdvancedResearcher(
            query="x",
            depth="medium",
            llm_call=llm,
            web_search=search,
            web_scrape=scrape,
            on_event=on_event,
        )
        await r.plan()
        await r.gather()
        await r.analyze()
        await r.synthesize()

        # Legacy text is GONE
        assert "No usable web sources were retrieved" not in r.report_markdown
        # Disclaimer present
        assert "Note:" in r.report_markdown
        # Confidence capped low (knowledge-only)
        assert r.confidence == 30

        # report_ready event carries knowledge_only=True
        report_evs = [e for e in events if e.get("type") == "report_ready"]
        assert len(report_evs) == 1
        assert report_evs[0].get("knowledge_only") is True

    @pytest.mark.asyncio
    async def test_disclaimer_injected_if_model_drops_it(self):
        """If the local model returns a report WITHOUT the disclaimer
        block, we inject one so the user always sees the warning."""

        async def search(q, n): return []
        async def scrape(urls, n): return []

        events, on_event = _events_collector()
        # Model returns a heading + body but NO Note block
        llm = _ScriptedLLM([
            "Q1\nQ2\nQ3",
            "# x\n\n## Executive Summary\nx is real and useful.\n",
        ])

        r = AdvancedResearcher(
            query="x",
            depth="medium",
            llm_call=llm,
            web_search=search,
            web_scrape=scrape,
            on_event=on_event,
        )
        await r.plan()
        await r.gather()
        await r.analyze()
        await r.synthesize()

        # Disclaimer was injected
        assert "Note:" in r.report_markdown
        assert "training knowledge" in r.report_markdown

    @pytest.mark.asyncio
    async def test_hard_floor_when_llm_call_fails(self):
        """If the LLM call itself raises, the synthesize step still
        produces a minimal but non-empty fallback markdown."""

        async def search(q, n): return []
        async def scrape(urls, n): return []
        events, on_event = _events_collector()

        async def failing_llm(prompt, system, max_tokens):
            if "research planner" in (system or ""):
                return "Q1\nQ2\nQ3"  # planning succeeds
            raise RuntimeError("boom")  # synthesize fails

        r = AdvancedResearcher(
            query="x",
            depth="medium",
            llm_call=failing_llm,
            web_search=search,
            web_scrape=scrape,
            on_event=on_event,
        )
        await r.plan()
        await r.gather()
        await r.analyze()
        await r.synthesize()

        # Hard floor markdown reached
        assert "AMOR could not retrieve" in r.report_markdown
        assert "Executive Summary" in r.report_markdown
        # Still emits report_ready
        assert any(e.get("type") == "report_ready" for e in events)

    @pytest.mark.asyncio
    async def test_legacy_string_not_emitted_anywhere(self):
        """Belt-and-suspenders: across the full pipeline, the old brick-
        wall string MUST NOT appear in any event payload."""

        async def search(q, n): return []
        async def scrape(urls, n): return []
        events, on_event = _events_collector()
        llm = _ScriptedLLM(["Q1\nQ2\nQ3", "# x\n\n## Executive Summary\nFoo."])

        r = AdvancedResearcher(
            query="x",
            depth="medium",
            llm_call=llm,
            web_search=search,
            web_scrape=scrape,
            on_event=on_event,
        )
        await r.run()

        legacy = "No usable web sources were retrieved"
        for ev in events:
            for v in ev.values():
                if isinstance(v, str) and legacy in v:
                    pytest.fail(
                        f"legacy string surfaced in event: {ev.get('type')}"
                    )


# ─── 4. Empty-LLM synthesize fallback (sources exist but LLM bailed) ─


class TestEmptyLLMSynthesizeFallback:
    @pytest.mark.asyncio
    async def test_compact_retry_invoked_on_empty_first_call(self):
        """When the first synthesize LLM call returns empty, the compact
        retry is invoked with a tighter prompt."""

        async def search(q, n): return []
        async def scrape(urls, n): return []
        events, on_event = _events_collector()

        # synthesize() is called DIRECTLY (no plan/analyze), so only the
        # synthesize-stack LLM calls fire: main → "" (empty), then
        # compact retry → valid report.
        llm = _ScriptedLLM([
            "",  # main synthesize → empty
            "# x\n\n## Executive Summary\nA detailed report grounded "
            "in [1] and [2] with extensive findings spanning the "
            "entire knowledge corpus collected for this query.\n\n"
            "## Conclusion\nFinal synthesis covering both "
            "sub-questions.",  # compact retry → real report
        ])

        r = AdvancedResearcher(
            query="x",
            depth="medium",
            llm_call=llm,
            web_search=search,
            web_scrape=scrape,
            on_event=on_event,
        )
        r.sub_questions = ["Q1", "Q2"]
        r.sources = [
            Source(id=1, url="u1", title="t1", content="c"*500,
                   findings="- f1", relevance=0.5,
                   sub_question_index=0),
            Source(id=2, url="u2", title="t2", content="d"*500,
                   findings="- f2", relevance=0.6,
                   sub_question_index=1),
        ]
        await r.synthesize()

        # Compact retry produced a real report (not the empty placeholder)
        assert "(The model produced an empty report.)" not in r.report_markdown
        assert "Executive Summary" in r.report_markdown
        # Both LLM calls were made (main + compact retry)
        # (last 2 of the 4 scripted responses consumed)
        assert len(llm.calls) >= 2

    @pytest.mark.asyncio
    async def test_deterministic_fallback_when_both_llm_calls_empty(self):
        """When BOTH the main synthesize AND the compact retry return
        empty, the deterministic source-list fallback fires."""

        async def search(q, n): return []
        async def scrape(urls, n): return []
        events, on_event = _events_collector()

        # All synthesize attempts return empty
        llm = _ScriptedLLM(["", "", "", "", ""])

        r = AdvancedResearcher(
            query="abstract phrase",
            depth="medium",
            llm_call=llm,
            web_search=search,
            web_scrape=scrape,
            on_event=on_event,
        )
        r.sub_questions = ["Q1?", "Q2?"]
        r.sources = [
            Source(id=1, url="https://example.com/a",
                   title="Source A title",
                   content="x"*500, findings="- finding A1\n- finding A2",
                   relevance=0.5, sub_question_index=0,
                   domain="example.com"),
            Source(id=2, url="https://example.com/b",
                   title="Source B title",
                   content="y"*500, findings="- finding B",
                   relevance=0.4, sub_question_index=1,
                   domain="example.com"),
        ]
        await r.synthesize()

        # Deterministic source-list fallback rendered
        assert "(The model produced an empty report.)" not in r.report_markdown
        # Has the source titles + URLs (markdown link format)
        assert "Source A title" in r.report_markdown
        assert "Source B title" in r.report_markdown
        assert "example.com/a" in r.report_markdown
        # Has the findings
        assert "finding A1" in r.report_markdown
        # Has the recommendation block
        assert "Re-run the query" in r.report_markdown
        # Sources grouped by sub-question (### headings)
        assert "### Q1?" in r.report_markdown
        assert "### Q2?" in r.report_markdown

    @pytest.mark.asyncio
    async def test_normal_synthesis_unchanged_when_llm_succeeds(self):
        """Sanity check: the new fallbacks are no-ops when the first
        LLM call returns a valid report."""

        async def search(q, n): return []
        async def scrape(urls, n): return []
        events, on_event = _events_collector()

        valid_report = (
            "# x\n\n## Executive Summary\nA solid first-pass report "
            "with citations [1] and [2]. This is long enough to "
            "exceed the 80-char threshold for empty-detection, by a "
            "comfortable margin.\n\n## Conclusion\nDone."
        )
        llm = _ScriptedLLM([valid_report])

        r = AdvancedResearcher(
            query="x",
            depth="medium",
            llm_call=llm,
            web_search=search,
            web_scrape=scrape,
            on_event=on_event,
        )
        r.sub_questions = ["Q1"]
        r.sources = [
            Source(id=1, url="u", title="t", content="c"*500,
                   findings="- f", relevance=0.6,
                   sub_question_index=0),
        ]
        await r.synthesize()

        # Only 1 LLM call (no retry, no deterministic fallback)
        assert len(llm.calls) == 1
        assert "first-pass report" in r.report_markdown
