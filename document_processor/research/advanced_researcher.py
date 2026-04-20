"""
Advanced local research orchestrator.

Produces Claude Research–style output: a planned, multi-phase pipeline that
decomposes a query into sub-questions, gathers web sources, extracts findings
from each source, and writes a professional markdown report with inline
citations.

All phases run against a local LLM (Ollama) — no external API dependency.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


EventCallback = Callable[[Dict[str, Any]], Awaitable[None]]
LLMCall = Callable[[str, Optional[str], int], Awaitable[str]]
WebSearch = Callable[[str, int], Awaitable[List[Dict[str, str]]]]
WebScrape = Callable[[List[str], int], Awaitable[List[Dict[str, str]]]]
Translator = Callable[[List[Dict[str, str]]], Awaitable[List[Dict[str, str]]]]


@dataclass
class Source:
    id: int
    url: str
    title: str
    snippet: str = ""
    content: str = ""
    domain: str = ""
    relevance: float = 0.0
    sub_question_index: int = 0
    findings: str = ""
    original_language: Optional[str] = None
    translated: bool = False


@dataclass
class Phase:
    name: str
    label: str
    status: str = "pending"  # pending | in_progress | completed | failed
    detail: Dict[str, Any] = field(default_factory=dict)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


async def _noop_event(_event: Dict[str, Any]) -> None:
    return None


class AdvancedResearcher:
    """
    Claude Research–style local orchestrator.

    Phases:
      1. planning        Decompose the query into focused sub-questions.
      2. gathering       Web search + scrape top sources per sub-question.
      3. analyzing       Per-source relevant-finding extraction & relevance scoring.
      4. synthesizing    Final markdown report with inline [n] citations.
    """

    def __init__(
        self,
        query: str,
        depth: str,
        llm_call: LLMCall,
        web_search: WebSearch,
        web_scrape: WebScrape,
        translate: Optional[Translator] = None,
        target_language: str = "en",
        on_event: Optional[EventCallback] = None,
    ) -> None:
        self.query = (query or "").strip()
        self.depth = depth if depth in {"quick", "standard", "deep"} else "standard"
        self.llm_call = llm_call
        self.web_search = web_search
        self.web_scrape = web_scrape
        self.translate = translate
        self.target_language = target_language
        self.on_event: EventCallback = on_event or _noop_event

        # Depth configuration
        if self.depth == "quick":
            self.num_sub_questions = 3
            self.sources_per_subquestion = 2
            self.max_total_sources = 6
            self.report_tokens = 1600
        elif self.depth == "deep":
            self.num_sub_questions = 6
            self.sources_per_subquestion = 3
            self.max_total_sources = 15
            self.report_tokens = 3600
        else:  # standard
            self.num_sub_questions = 4
            self.sources_per_subquestion = 2
            self.max_total_sources = 10
            self.report_tokens = 2600

        self.phases: List[Phase] = [
            Phase("planning", "Planning"),
            Phase("gathering", "Gathering sources"),
            Phase("analyzing", "Analyzing"),
            Phase("synthesizing", "Writing report"),
        ]
        self.sub_questions: List[str] = []
        self.sources: List[Source] = []
        self.report_markdown: str = ""
        self.confidence: int = 75
        self.translated_any: bool = False

    # ─── event helpers ────────────────────────────────────────────────

    async def _emit(self, event_type: str, data: Dict[str, Any]) -> None:
        event = {"type": event_type, "ts": datetime.utcnow().isoformat(), **data}
        try:
            await self.on_event(event)
        except Exception as e:  # events must never kill the pipeline
            logger.debug("event emit failed: %s", e)

    def _phase(self, name: str) -> Optional[Phase]:
        return next((p for p in self.phases if p.name == name), None)

    async def _start_phase(self, name: str, **detail: Any) -> None:
        phase = self._phase(name)
        if phase:
            phase.status = "in_progress"
            phase.started_at = datetime.utcnow().isoformat()
            phase.detail.update(detail)
        await self._emit("phase_start", {"phase": name, "detail": detail})

    async def _complete_phase(self, name: str, **detail: Any) -> None:
        phase = self._phase(name)
        if phase:
            phase.status = "completed"
            phase.completed_at = datetime.utcnow().isoformat()
            phase.detail.update(detail)
        await self._emit("phase_complete", {"phase": name, "detail": detail})

    async def _fail_phase(self, name: str, message: str) -> None:
        phase = self._phase(name)
        if phase:
            phase.status = "failed"
            phase.completed_at = datetime.utcnow().isoformat()
            phase.detail["error"] = message
        await self._emit("phase_failed", {"phase": name, "error": message})

    # ─── Phase 1 · Planning ───────────────────────────────────────────

    async def plan(self) -> List[str]:
        await self._start_phase("planning")

        system = (
            "You are an expert research planner. Given a user research query, "
            "produce a focused set of sub-questions that — answered together — "
            "yield a complete, high-quality answer. Each sub-question should "
            "target a distinct angle such as definition, history, current state, "
            "mechanism, benefits, drawbacks, examples, controversy, or outlook. "
            "Output only the sub-questions: one per line, no numbering, no preamble."
        )
        prompt = (
            f"Research query: {self.query}\n\n"
            f"Produce exactly {self.num_sub_questions} distinct, specific sub-questions "
            f"that can be answered through web research. "
            f"Avoid redundancy. Output one question per line — no numbering, no bullets."
        )

        try:
            raw = await self.llm_call(prompt, system, 500)
        except Exception as e:
            logger.warning("planning LLM call failed: %s", e)
            raw = ""

        questions = [self._clean_question(q) for q in (raw or "").split("\n")]
        questions = [q for q in questions if q and len(q) >= 10]
        # Prefer genuine questions but accept short phrasings too
        seen: set = set()
        deduped: List[str] = []
        for q in questions:
            key = q.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(q)
        questions = deduped[: self.num_sub_questions]

        if not questions:
            # Fallback: trivial decomposition so the pipeline still runs
            questions = [
                f"What is {self.query}?",
                f"Why is {self.query} important?",
                f"What are the key facts about {self.query}?",
            ][: self.num_sub_questions]

        self.sub_questions = questions

        for i, q in enumerate(questions):
            await self._emit("sub_question", {"index": i, "question": q})

        await self._complete_phase("planning", sub_questions=questions)
        return questions

    @staticmethod
    def _clean_question(line: str) -> str:
        line = line.strip()
        # Strip markdown bullets, numbering, quotes
        line = re.sub(r"^[-•*]\s+", "", line)
        line = re.sub(r"^\(?\d+[\.\)]\s+", "", line)
        line = line.strip().strip("\"'").strip()
        return line

    # ─── Phase 2 · Gathering ──────────────────────────────────────────

    async def gather(self) -> List[Source]:
        await self._start_phase(
            "gathering", sub_questions=len(self.sub_questions)
        )

        all_results: List[Dict[str, Any]] = []
        for i, sub_q in enumerate(self.sub_questions):
            await self._emit(
                "search_start",
                {"sub_question_index": i, "sub_question": sub_q},
            )
            try:
                results = await self.web_search(
                    sub_q, self.sources_per_subquestion + 1
                )
            except Exception as e:
                logger.warning("search failed for %r: %s", sub_q, e)
                results = []
            for r in results:
                if r.get("url"):
                    r["sub_question_index"] = i
                    all_results.append(r)
            await self._emit(
                "search_done",
                {"sub_question_index": i, "found": len(results)},
            )
            await asyncio.sleep(0.4)

        # Dedupe while preserving sub-question coverage
        seen_urls: set = set()
        unique: List[Dict[str, Any]] = []
        for r in all_results:
            url = r["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            unique.append(r)
            if len(unique) >= self.max_total_sources:
                break

        if not unique:
            await self._fail_phase(
                "gathering", "No usable search results were returned."
            )
            self.sources = []
            return []

        urls = [r["url"] for r in unique]
        await self._emit("scrape_start", {"total": len(urls)})
        try:
            scraped = await self.web_scrape(urls, 3)
        except Exception as e:
            logger.warning("scrape batch failed: %s", e)
            scraped = []
        scraped_by_url: Dict[str, Dict[str, str]] = {
            s["url"]: s for s in scraped if isinstance(s, dict) and s.get("url")
        }

        sources: List[Source] = []
        next_id = 1
        for r in unique:
            url = r["url"]
            scraped_item = scraped_by_url.get(url)
            if not scraped_item:
                continue
            content = (scraped_item.get("content") or "").strip()
            if len(content) < 200:
                continue
            src = Source(
                id=next_id,
                url=url,
                title=(r.get("title") or self._derive_title(url))[:180],
                snippet=self._make_snippet(content),
                content=content[:6000],
                domain=urlparse(url).netloc,
                sub_question_index=r["sub_question_index"],
            )
            sources.append(src)
            await self._emit(
                "source_added",
                {
                    "id": src.id,
                    "url": src.url,
                    "title": src.title,
                    "domain": src.domain,
                    "snippet": src.snippet,
                    "sub_question_index": src.sub_question_index,
                },
            )
            next_id += 1

        # Optional translation pass
        if self.translate and sources:
            await self._emit("translation_start", {"total": len(sources)})
            try:
                to_translate = [
                    {"url": s.url, "content": s.content} for s in sources
                ]
                translated = await self.translate(to_translate)
                by_url = {t["url"]: t for t in translated if t.get("url")}
                for s in sources:
                    t = by_url.get(s.url)
                    if not t:
                        continue
                    if t.get("translated"):
                        s.content = t.get("content", s.content)
                        s.translated = True
                        s.original_language = t.get("original_language")
                        self.translated_any = True
            except Exception as e:
                logger.debug("translation step failed: %s", e)

        self.sources = sources
        await self._complete_phase(
            "gathering", sources_collected=len(sources)
        )
        return sources

    @staticmethod
    def _derive_title(url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        if path:
            leaf = path.split("/")[-1].replace("-", " ").replace("_", " ")
            if leaf:
                return f"{parsed.netloc} — {leaf[:80]}"
        return parsed.netloc or url

    @staticmethod
    def _make_snippet(content: str, max_len: int = 260) -> str:
        s = re.sub(r"\s+", " ", content).strip()
        if len(s) <= max_len:
            return s
        return s[:max_len].rsplit(" ", 1)[0] + "…"

    # ─── Phase 3 · Analyzing ─────────────────────────────────────────

    async def analyze(self) -> List[Source]:
        await self._start_phase("analyzing", total=len(self.sources))

        if not self.sources:
            await self._complete_phase("analyzing", kept=0)
            return []

        system = (
            "You are a careful research analyst. Given a source excerpt and a "
            "research sub-question, extract only the 2–4 most relevant factual "
            "findings that directly answer the sub-question. Preserve the source's "
            "own phrasing when it adds precision. Each finding must stand alone. "
            "If the source does not meaningfully address the sub-question, reply "
            "with exactly: NOT_RELEVANT"
        )

        for i, src in enumerate(self.sources):
            sub_q = (
                self.sub_questions[src.sub_question_index]
                if 0 <= src.sub_question_index < len(self.sub_questions)
                else self.query
            )
            await self._emit(
                "analyzing_source",
                {
                    "source_id": src.id,
                    "index": i,
                    "total": len(self.sources),
                    "title": src.title,
                },
            )
            prompt = (
                f"Sub-question: {sub_q}\n\n"
                f"Source title: {src.title}\n"
                f"Source URL: {src.url}\n\n"
                f"Source excerpt:\n{src.content[:3800]}\n\n"
                f"Extract 2–4 concise, self-contained findings that directly answer "
                f"the sub-question. One finding per line, no numbering, no preamble. "
                f"If nothing relevant, reply only: NOT_RELEVANT"
            )
            try:
                raw = await self.llm_call(prompt, system, 420)
            except Exception as e:
                logger.debug("analyze failed for source %s: %s", src.id, e)
                raw = ""

            cleaned = (raw or "").strip()
            if not cleaned or cleaned.upper().startswith("NOT_RELEVANT"):
                src.relevance = 0.05
                src.findings = ""
                continue

            # Keep only bullet-like lines
            lines = [ln.strip("-•* \t") for ln in cleaned.split("\n") if ln.strip()]
            lines = [ln for ln in lines if len(ln) > 12][:5]
            if not lines:
                src.relevance = 0.1
                src.findings = ""
                continue
            src.findings = "\n".join(f"- {ln}" for ln in lines)
            src.relevance = min(1.0, 0.4 + 0.12 * len(lines))

        # Filter to relevant sources and renumber citation IDs
        kept = [s for s in self.sources if s.relevance >= 0.35 and s.findings]
        for new_id, s in enumerate(kept, start=1):
            s.id = new_id
        self.sources = kept

        for s in self.sources:
            await self._emit(
                "source_refined",
                {
                    "id": s.id,
                    "relevance": round(s.relevance, 2),
                    "findings": s.findings,
                },
            )

        await self._complete_phase("analyzing", kept=len(self.sources))
        return self.sources

    # ─── Phase 4 · Synthesizing ──────────────────────────────────────

    async def synthesize(self) -> str:
        await self._start_phase("synthesizing", sources=len(self.sources))

        if not self.sources:
            fallback = (
                f"# {self.query}\n\n"
                "## Executive Summary\n\n"
                "No usable web sources were retrieved for this query. The report "
                "cannot be grounded in cited evidence and has therefore not been "
                "produced. Consider rephrasing the query or trying again later."
            )
            self.report_markdown = fallback
            self.confidence = 20
            await self._emit(
                "report_ready", {"markdown": fallback, "confidence": self.confidence}
            )
            await self._complete_phase("synthesizing", length=len(fallback))
            return fallback

        sources_block_parts: List[str] = []
        for s in self.sources:
            block = (
                f"[{s.id}] {s.title} — {s.domain}\nURL: {s.url}\n"
                f"Key findings extracted:\n{s.findings}\n"
            )
            sources_block_parts.append(block)
        sources_block = "\n".join(sources_block_parts)

        sub_qs_block = "\n".join(f"- {q}" for q in self.sub_questions)

        system = (
            "You are a senior technical writer producing a professional research "
            "report. Your writing is precise, factual, and well-structured. "
            "You MUST cite sources inline using bracketed numbers like [1], [2]. "
            "Every non-trivial factual claim needs at least one citation. "
            "Only use information present in the supplied sources — do not invent facts. "
            "If information is missing, acknowledge the gap. Use measured, analytical "
            "language; avoid marketing tone and avoid hedging filler. Write in markdown: "
            "`#` for the report title, `##` for major sections, `###` for sub-sections, "
            "bullet points where appropriate, **bold** for key terms."
        )

        prompt = f"""Write a comprehensive, professional research report for the following query.

QUERY
{self.query}

SUB-QUESTIONS THIS REPORT SHOULD ADDRESS
{sub_qs_block}

SOURCES (cite these inline using [n] where n is the source number)
{sources_block}

STRUCTURE — follow this exact structure, using markdown headings:

# {self.query}

## Executive Summary
Three to five sentences that capture the essential answer. Include 2–4 citations.

## Background
Context the reader needs: definitions, history, scope, why this topic matters.
Cite sources.

## Key Findings
Organize as `###` sub-sections, one per sub-question listed above. Be specific.
Cite heavily — at least one citation per factual claim.

## Analysis
What the findings mean when taken together. Patterns, tensions, contrasts between
sources. Where sources disagree, cite each position.

## Limitations & Open Questions
What the sources do not answer. Biases, gaps, or remaining uncertainty.

## Conclusion
Two to four sentences that directly answer the original query.

RULES
- Every factual claim gets at least one citation [n].
- Do not invent citation numbers; use only those in the SOURCES block.
- No "As an AI" disclaimers, no preamble before the `#` title.
- Length target: 700–1200 words.
- Use clear, precise, analytical prose.
"""

        try:
            report = await self.llm_call(prompt, system, self.report_tokens)
        except Exception as e:
            logger.exception("synthesis LLM call failed: %s", e)
            report = ""

        report = self._postprocess_report(report, self.query)
        self.report_markdown = report

        # Confidence heuristic from citation coverage
        cited_ids = {int(m) for m in re.findall(r"\[(\d+)\]", report) if m.isdigit()}
        valid_ids = {s.id for s in self.sources}
        used_valid = cited_ids & valid_ids
        coverage = len(used_valid) / len(self.sources) if self.sources else 0
        self.confidence = int(55 + 40 * coverage)
        if self.confidence > 95:
            self.confidence = 95

        await self._emit(
            "report_ready",
            {"markdown": report, "confidence": self.confidence},
        )
        await self._complete_phase(
            "synthesizing", length=len(report), confidence=self.confidence
        )
        return report

    @staticmethod
    def _postprocess_report(report: str, query: str) -> str:
        text = (report or "").strip()
        # Strip any preamble before the first markdown title
        lines = text.split("\n")
        while lines and not lines[0].lstrip().startswith("#"):
            lines.pop(0)
        text = "\n".join(lines).strip()
        if not text:
            text = f"# {query}\n\n*(The model produced an empty report.)*"
        return text

    # ─── Orchestration ───────────────────────────────────────────────

    async def run(self) -> Dict[str, Any]:
        try:
            await self.plan()
            await self.gather()
            await self.analyze()
            await self.synthesize()
            await self._emit("done", {})
            return self.to_dict()
        except Exception as e:
            logger.exception("research orchestrator failed: %s", e)
            await self._emit("error", {"message": str(e)})
            raise

    # ─── Serialization ───────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "depth": self.depth,
            "sub_questions": self.sub_questions,
            "phases": [
                {
                    "name": p.name,
                    "label": p.label,
                    "status": p.status,
                    "detail": p.detail,
                    "started_at": p.started_at,
                    "completed_at": p.completed_at,
                }
                for p in self.phases
            ],
            "citations": [
                {
                    "id": s.id,
                    "url": s.url,
                    "title": s.title,
                    "domain": s.domain,
                    "snippet": s.snippet,
                    "relevance": round(s.relevance, 2),
                    "translated": s.translated,
                    "original_language": s.original_language,
                    "findings": s.findings,
                    "sub_question_index": s.sub_question_index,
                }
                for s in self.sources
            ],
            "report_markdown": self.report_markdown,
            "confidence": self.confidence,
            "translated_any": self.translated_any,
        }
