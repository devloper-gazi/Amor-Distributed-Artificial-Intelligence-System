"""
Pre-LLM relevance gate for the advanced research pipeline.

Phase 1 optimization (`fancy-swinging-karp.md`): a deterministic,
lightweight filter that runs *between* `gather()` and `analyze()`. Its
only job is to drop sources that would obviously waste an LLM call —
login walls, navigation-only pages, off-topic results, runaway domain
over-representation, duplicate URLs.

Design constraints (intentional):

* Pure-Python stdlib only. No sklearn, no rank-bm25, no numpy. The
  scorer here is the cheap-and-deterministic baseline; the expensive
  embedding/cross-encoder layer arrives in Phase 2.
* Fail-open: if anything in the scoring path raises, return the input
  list unchanged with `fallback_used=True`. Better to make a wasted
  LLM call than to lose a good source.
* Source duck-typing: callers may pass dataclasses (the current
  AdvancedResearcher path) or dicts (other future callers). Field
  access goes through `_field()` so both shapes work.
* Tier-aware: basic just passes through (already small), medium is
  light, deep/expert/ultra apply progressively stronger caps. Caps
  are deliberately *higher* than the current effective LLM-survival
  count so the filter never gets blamed for losing good content —
  the existing post-LLM keep-filter (`relevance >= 0.22 and
  findings`) is still the second-line gate.

The public surface is small — `RelevanceFilter(...)` and
`RelevanceResult(...)`. See `advanced_researcher._apply_relevance_filter`
for the wiring.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass
class RelevanceResult:
    """
    Outcome of one filter pass. `selected_sources` holds the SAME
    Source instances that were passed in — never copies, never
    mutated — so downstream code (citations, IDs) is unchanged.
    """
    selected_sources: List[Any]
    original_count: int
    selected_count: int
    rejected_count: int
    method_summary: str = ""
    fallback_used: bool = False
    errors: List[str] = field(default_factory=list)
    # Populated only when the caller's config has `debug=True`. Kept
    # off by default to bound memory: at ultra tier this can be 1000
    # entries × dict-per-source.
    score_debug: Optional[Dict[str, Dict[str, float]]] = None


@dataclass
class RelevanceConfig:
    """
    Snapshot of the active settings for a single filter call. The
    AdvancedResearcher hydrates this from `pydantic_settings` plus
    the run's tier; the filter never touches the global settings
    object itself.
    """
    enabled: bool = True
    fail_open: bool = True
    debug: bool = False
    min_score: float = 0.15
    max_sources: int = 60          # tier-derived cap
    tier: str = "medium"


# ─── Tunables (private constants, not config flags) ─────────────────────────

# Domains we trust enough to give a small bonus. Boosts are *gentle*
# (≤+0.05) so good content from arbitrary domains isn't drowned out.
_TRUSTED_DOMAIN_PATTERNS: Tuple[Tuple[re.Pattern, float], ...] = (
    (re.compile(r"\.gov(?:\.|$)"),         0.05),
    (re.compile(r"\.edu(?:\.|$)"),         0.05),
    (re.compile(r"^en\.wikipedia\.org$"),  0.04),
    (re.compile(r"\.wikipedia\.org$"),     0.03),
    (re.compile(r"^arxiv\.org$"),          0.05),
    (re.compile(r"^pubmed\.ncbi\.nlm\.nih\.gov$"), 0.05),
    (re.compile(r"^pmc\.ncbi\.nlm\.nih\.gov$"),    0.05),
    (re.compile(r"^scholar\.google\.com$"),0.03),
    (re.compile(r"\.org(?:\.|$)"),         0.02),
)

# Markers that make a page almost certainly useless to an LLM. Each
# adds a penalty to the source's score (cumulative).
_BAD_PAGE_MARKERS: Tuple[Tuple[re.Pattern, float], ...] = (
    (re.compile(r"\b(404|not found|page (?:not )?found|page does not exist)\b", re.I), 0.40),
    (re.compile(r"\b(403|forbidden|access denied)\b", re.I),                            0.40),
    (re.compile(r"\b(sign[\- ]?in|log[\- ]?in to continue|please sign in)\b", re.I),    0.30),
    (re.compile(r"\b(captcha|i'?m not a robot|robot check)\b", re.I),                   0.25),
    (re.compile(r"\b(this site can'?t be reached|connection (?:timed out|refused))\b", re.I), 0.50),
)

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9'-]{1,}")
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "for", "to", "in", "on",
    "at", "by", "with", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had", "this", "that", "these",
    "those", "it", "its", "as", "from", "about", "into", "than", "then",
    "if", "so", "such", "only", "can", "could", "should", "would", "may",
    "might", "will", "shall", "what", "which", "who", "whom", "whose",
    "why", "how", "when", "where",
})

_SCORING_VERSION = "lexical+domain v1"
_CONTENT_SCORE_WINDOW = 3000   # cap content scanned per source
_TITLE_BOOST_PER_HIT = 0.04
_PHRASE_BOOST_PER_HIT = 0.05
_IDEAL_CONTENT_LEN = 1200       # words above this don't add quality
_MIN_USABLE_CONTENT_LEN = 80    # below this we apply a length penalty
_DOMAIN_OVERREP_RATIO = 0.30    # >30% of corpus from one domain → penalize

# Score weights (must sum roughly to 1.0 BEFORE penalties).
_W_TOKEN_OVERLAP = 0.45
_W_TITLE_OVERLAP = 0.20
_W_PHRASE_MATCH  = 0.15
_W_DOMAIN_QUAL   = 0.10
_W_CONTENT_QUAL  = 0.10


# ─── Source duck-typing helper ──────────────────────────────────────────────


def _field(src: Any, name: str, default: str = "") -> str:
    """
    Read a string field from a Source-like object. Handles dataclass
    instances (the AdvancedResearcher path), dicts, and Pydantic
    models with the same call shape. Always returns a str.
    """
    val = None
    if hasattr(src, name):
        val = getattr(src, name, None)
    elif isinstance(src, dict):
        val = src.get(name)
    if val is None:
        return default
    if not isinstance(val, str):
        try:
            val = str(val)
        except Exception:                           # pragma: no cover
            return default
    return val or default


# ─── Tokenization & text utils ──────────────────────────────────────────────


def _normalize(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def _tokens(text: str) -> List[str]:
    """Lowercase tokens, stopwords removed, length>=2."""
    if not text:
        return []
    return [
        m.group(0).lower()
        for m in _TOKEN_RE.finditer(text)
        if m.group(0).lower() not in _STOPWORDS and len(m.group(0)) > 1
    ]


def _phrase_candidates(query: str, sub_questions: Iterable[str]) -> List[str]:
    """
    Build a small set of 2–3-word phrases from query + sub-questions
    used as exact-match boosters in the scorer. We keep this small
    (cap 12) to stay cheap; phrase match is a tiebreaker, not the
    primary signal.
    """
    phrases: List[str] = []
    seen: set = set()
    parts: List[str] = [query] + [q for q in sub_questions if q]
    for part in parts:
        toks = [t for t in _TOKEN_RE.findall(part.lower()) if t not in _STOPWORDS]
        for n in (3, 2):
            for i in range(0, max(0, len(toks) - n + 1)):
                ph = " ".join(toks[i : i + n])
                if ph not in seen and len(ph) >= 5:
                    seen.add(ph)
                    phrases.append(ph)
                    if len(phrases) >= 12:
                        return phrases
    return phrases


# ─── Scoring ────────────────────────────────────────────────────────────────


def _domain_bonus(domain: str) -> float:
    if not domain:
        return 0.0
    d = domain.lower()
    # NOTE: must use prefix slicing, NOT str.lstrip("www.") — lstrip strips
    # any character in the argument set, so "wikipedia.org".lstrip("www.")
    # silently mangles to "ikipedia.org" and the trust patterns never match.
    if d.startswith("www."):
        d = d[4:]
    for pattern, bonus in _TRUSTED_DOMAIN_PATTERNS:
        if pattern.search(d):
            return bonus
    return 0.0


def _bad_page_penalty(text: str, title: str = "") -> float:
    """
    Cumulative penalty for known bad-page markers. Inspects the page
    title (always full) AND the first 600 chars of the body — login
    walls / 404 pages usually announce themselves in the head, but
    sometimes ONLY in the title (e.g. "404 — Not Found" with a body
    that starts with normal navigation text). Capped at 0.7 so even
    a multi-marker page can still show a tiny residual score if the
    rest of the signal is unusually strong.
    """
    if not text and not title:
        return 0.0
    head = (title + "\n" + (text[:600] if text else "")).lower()
    total = 0.0
    for pattern, pen in _BAD_PAGE_MARKERS:
        if pattern.search(head):
            total += pen
    return min(total, 0.7)


def _content_quality(content: str) -> float:
    """
    Cheap content-shape signal. Length within a sane range = full
    credit; boilerplate-short content gets penalized. Doesn't try to
    detect navigation menus etc. — the bad-page detector handles the
    obvious failure modes.
    """
    if not content:
        return 0.0
    n = len(content)
    if n < _MIN_USABLE_CONTENT_LEN:
        # logarithmic ramp: 80 chars → 0.4, 200 → 0.7, 1200+ → 1.0
        return max(0.0, math.log10(max(1, n)) / math.log10(_IDEAL_CONTENT_LEN))
    return min(1.0, math.log10(n) / math.log10(_IDEAL_CONTENT_LEN))


def _overlap_score(query_tokens: set, candidate_tokens: List[str]) -> float:
    """
    Asymmetric overlap: how much of the (deduped) query vocabulary
    appears in the candidate. Saturates at 1.0 — extra hits beyond
    matching every query token don't keep adding signal.
    """
    if not query_tokens or not candidate_tokens:
        return 0.0
    cand_set = set(candidate_tokens)
    hits = sum(1 for t in query_tokens if t in cand_set)
    return min(1.0, hits / len(query_tokens))


def _phrase_score(phrases: Iterable[str], blob: str) -> float:
    if not phrases or not blob:
        return 0.0
    haystack = blob.lower()
    hits = 0
    for ph in phrases:
        if ph in haystack:
            hits += 1
    # Phrase hits are scarce; cap at 5 hits worth of credit.
    return min(1.0, hits * _PHRASE_BOOST_PER_HIT * 5)


def _title_overlap_score(query_tokens: set, title_tokens: List[str]) -> float:
    if not title_tokens:
        return 0.0
    hits = sum(1 for t in title_tokens if t in query_tokens)
    return min(1.0, hits * _TITLE_BOOST_PER_HIT * 5)


def _score_one(
    src: Any,
    query_tokens: set,
    phrases: List[str],
    domain_overrep: Dict[str, int],
    domain_total: int,
    seen_urls: set,
) -> Tuple[float, Dict[str, float]]:
    title = _field(src, "title")
    content = _field(src, "content")[:_CONTENT_SCORE_WINDOW]
    url = _field(src, "url")
    domain = _field(src, "domain") or (urlparse(url).netloc if url else "")

    title_n = _normalize(title)
    content_n = _normalize(content)
    blob = (title_n + " " + content_n).strip()

    body_tokens = _tokens(blob)
    title_tokens = _tokens(title_n)

    # Core dimensions
    s_token = _overlap_score(query_tokens, body_tokens)
    s_title = _title_overlap_score(query_tokens, title_tokens)
    s_phrase = _phrase_score(phrases, blob)
    s_domain = min(1.0, _domain_bonus(domain) / 0.05)  # normalize 0–1 within bonus range
    s_content = _content_quality(content)

    base = (
        _W_TOKEN_OVERLAP * s_token
        + _W_TITLE_OVERLAP * s_title
        + _W_PHRASE_MATCH * s_phrase
        + _W_DOMAIN_QUAL * s_domain
        + _W_CONTENT_QUAL * s_content
    )

    # Penalties
    bad_pen = _bad_page_penalty(content, title=title)
    dup_pen = 0.20 if (url and url in seen_urls) else 0.0

    # Domain over-representation: every source from a domain whose
    # share of the corpus exceeds _DOMAIN_OVERREP_RATIO takes a small
    # penalty. (Earlier comment claimed "SECOND+ source only" — that's
    # NOT what the code does, and the current behaviour is the right
    # one: penalising all of them lets a single high-quality source
    # from a different domain leapfrog past a Wikipedia-only corpus
    # in the post-cap sort.) The `>= 2` guard prevents penalising the
    # only source from a domain that simply happens to be 100% of a
    # tiny corpus.
    overrep_pen = 0.0
    if domain and domain_total > 0:
        domain_n = domain_overrep.get(domain, 0)
        share = domain_n / domain_total
        if share > _DOMAIN_OVERREP_RATIO and domain_n >= 2:
            overrep_pen = 0.10

    final = max(0.0, min(1.0, base - bad_pen - dup_pen - overrep_pen))

    debug = {
        "token": round(s_token, 3),
        "title": round(s_title, 3),
        "phrase": round(s_phrase, 3),
        "domain": round(s_domain, 3),
        "content": round(s_content, 3),
        "bad_pen": round(bad_pen, 3),
        "dup_pen": round(dup_pen, 3),
        "overrep_pen": round(overrep_pen, 3),
        "final": round(final, 3),
    }
    return final, debug


# ─── Public filter class ────────────────────────────────────────────────────


class RelevanceFilter:
    """
    Deterministic pre-LLM source gate. Fail-open by contract.

    Usage:
        rfilter = RelevanceFilter(config=RelevanceConfig(tier="deep", max_sources=60))
        result = await rfilter.filter_sources(
            query="how does photosynthesis work",
            sub_questions=[...],
            sources=[...],
        )
        for src in result.selected_sources:
            ...
    """

    def __init__(
        self,
        *,
        config: Optional[RelevanceConfig] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config or RelevanceConfig()
        self.log = logger or globals()["logger"]

    async def filter_sources(
        self,
        *,
        query: str,
        sub_questions: List[str],
        sources: List[Any],
        tier: Optional[str] = None,
    ) -> RelevanceResult:
        original_count = len(sources or [])
        if not self.config.enabled:
            return RelevanceResult(
                selected_sources=list(sources or []),
                original_count=original_count,
                selected_count=original_count,
                rejected_count=0,
                method_summary="disabled",
            )

        # `basic` tier passes everything through — corpus is already
        # tiny; filtering buys little and risks losing rare hits.
        active_tier = (tier or self.config.tier or "medium").lower()
        if active_tier == "basic":
            return RelevanceResult(
                selected_sources=list(sources or []),
                original_count=original_count,
                selected_count=original_count,
                rejected_count=0,
                method_summary=f"passthrough (tier={active_tier})",
            )

        if not sources:
            return RelevanceResult(
                selected_sources=[],
                original_count=0,
                selected_count=0,
                rejected_count=0,
                method_summary=f"empty input (tier={active_tier})",
            )

        try:
            return await asyncio.to_thread(
                self._run_blocking,
                query=query,
                sub_questions=sub_questions or [],
                sources=list(sources),
                tier=active_tier,
            )
        except Exception as exc:                # ← fail-open contract
            if not self.config.fail_open:
                raise
            self.log.warning(
                "research.relevance_filter_failed (tier=%s, n=%d): %s",
                active_tier, original_count, exc,
            )
            return RelevanceResult(
                selected_sources=list(sources),
                original_count=original_count,
                selected_count=original_count,
                rejected_count=0,
                method_summary="fallback (filter failed)",
                fallback_used=True,
                errors=[repr(exc)],
            )

    # ─── Internals (pure, sync — runs in a worker thread for safety) ──

    def _run_blocking(
        self,
        *,
        query: str,
        sub_questions: List[str],
        sources: List[Any],
        tier: str,
    ) -> RelevanceResult:
        # 1) Build query vocabulary + phrases
        full_query_text = " ".join([query] + list(sub_questions))
        query_tokens = set(_tokens(full_query_text))
        phrases = _phrase_candidates(query, sub_questions)

        # 2) Pre-pass: domain frequency + URL set (used by penalty stage)
        domain_count: Dict[str, int] = {}
        for s in sources:
            d = _field(s, "domain") or (urlparse(_field(s, "url")).netloc if _field(s, "url") else "")
            if d:
                domain_count[d] = domain_count.get(d, 0) + 1
        domain_total = sum(domain_count.values())

        # 3) Score every source. Deduplicate URLs as we go so the
        #    SECOND occurrence carries the dup penalty (the first
        #    one keeps its full score).
        seen_urls: set = set()
        scored: List[Tuple[float, Any, Dict[str, float]]] = []
        for s in sources:
            url = _field(s, "url")
            score, debug = _score_one(
                s, query_tokens, phrases,
                domain_count, domain_total, seen_urls,
            )
            scored.append((score, s, debug))
            if url:
                seen_urls.add(url)

        # 4) Filter on min_score and dedupe URLs (keep highest-scoring
        #    instance per URL).
        url_best: Dict[str, Tuple[float, Any, Dict[str, float]]] = {}
        no_url_buf: List[Tuple[float, Any, Dict[str, float]]] = []
        for score, src, debug in scored:
            if score < self.config.min_score:
                continue
            url = _field(src, "url")
            if not url:
                no_url_buf.append((score, src, debug))
                continue
            if url not in url_best or url_best[url][0] < score:
                url_best[url] = (score, src, debug)

        merged: List[Tuple[float, Any, Dict[str, float]]] = list(url_best.values()) + no_url_buf
        merged.sort(key=lambda t: t[0], reverse=True)

        # 5) Apply tier cap
        cap = max(1, int(self.config.max_sources))
        capped = merged[:cap]

        selected: List[Any] = [t[1] for t in capped]
        debug_dump: Optional[Dict[str, Dict[str, float]]] = None
        if self.config.debug:
            debug_dump = {}
            for score, src, dbg in capped:
                key = _field(src, "url") or f"src#{id(src)}"
                debug_dump[key] = {**dbg, "score": round(score, 3)}

        method = (
            f"{_SCORING_VERSION}, tier={tier}, "
            f"min={self.config.min_score:.2f}, cap={cap}"
        )
        return RelevanceResult(
            selected_sources=selected,
            original_count=len(sources),
            selected_count=len(selected),
            rejected_count=len(sources) - len(selected),
            method_summary=method,
            fallback_used=False,
            score_debug=debug_dump,
        )


__all__ = ["RelevanceFilter", "RelevanceResult", "RelevanceConfig"]
