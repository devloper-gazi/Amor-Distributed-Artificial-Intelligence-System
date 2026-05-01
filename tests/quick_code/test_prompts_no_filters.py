"""
Build-gate test: no V2 prompt template may contain refusal language,
legal disclaimers, or content-moderation pre-filters.

The user explicitly asked for this when commissioning the V2 work:

    "Filtre vb. ekleme … herhangi bir filtresi olmasın."
    (no content filters / legal limits)

Infrastructure-security filtering (prompt injection / shell injection
/ secret leakage) lives in
``document_processor/code_intelligence/adversarial_reviewer.py`` and
is intentionally exempt from this test — that subsystem is required
by the spec itself.
"""

from __future__ import annotations

import pytest

from document_processor.quick_code.anton_brain import AntonBrain
from document_processor.quick_code.parsel import PARSEL_SYSTEM_PROMPT
from document_processor.quick_code.router import (
    _LLM_SYSTEM_PROMPT as ROUTER_SYSTEM_PROMPT,
)
from document_processor.quick_code.seeker import PREDATOR_SYSTEM_PROMPT


# ─────────────────────────────────────────────────────────────────────
# Banned phrases.  Lowercased — comparisons are case-insensitive.
# ─────────────────────────────────────────────────────────────────────


BANNED_REFUSALS: tuple[str, ...] = (
    "i cannot help",
    "i cannot assist",
    "i won't",
    "i will not",
    "i'm sorry, but",
    "i am sorry, but",
    "as an ai",
    "it is not appropriate",
    "i'm not able to",
    "i am not able to",
)


BANNED_LEGAL: tuple[str, ...] = (
    "if illegal",
    "consult a lawyer",
    "consult an attorney",
    "educational purposes only",
    "for educational purposes",
    "not legal advice",
    "this is not legal",
    "in your jurisdiction",
)


BANNED_MODERATION: tuple[str, ...] = (
    "rejecting requests for",
    "decline if harmful",
    "refuse if harmful",
    "decline this request",
    "i cannot create content",
    "harmful content",
    "violates our policies",
    "violates anthropic",
    "refuses to provide",
)


ALL_BANNED: tuple[str, ...] = BANNED_REFUSALS + BANNED_LEGAL + BANNED_MODERATION


# ─────────────────────────────────────────────────────────────────────
# Prompt templates we audit
# ─────────────────────────────────────────────────────────────────────


PROMPT_FIXTURES: dict[str, str] = {
    "router._LLM_SYSTEM_PROMPT": ROUTER_SYSTEM_PROMPT,
    "parsel.PARSEL_SYSTEM_PROMPT": PARSEL_SYSTEM_PROMPT,
    "seeker.PREDATOR_SYSTEM_PROMPT": PREDATOR_SYSTEM_PROMPT,
    "anton_brain.DEFAULT_IDENTITY": AntonBrain.DEFAULT_IDENTITY,
    "anton_brain.DEFAULT_GLOBAL_RULES": AntonBrain.DEFAULT_GLOBAL_RULES,
}


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,text", list(PROMPT_FIXTURES.items()))
def test_prompt_has_no_refusal_language(name: str, text: str) -> None:
    lowered = (text or "").lower()
    for banned in BANNED_REFUSALS:
        assert banned not in lowered, (
            f"{name} contains refusal phrase: {banned!r}"
        )


@pytest.mark.parametrize("name,text", list(PROMPT_FIXTURES.items()))
def test_prompt_has_no_legal_disclaimers(name: str, text: str) -> None:
    lowered = (text or "").lower()
    for banned in BANNED_LEGAL:
        assert banned not in lowered, (
            f"{name} contains legal disclaimer: {banned!r}"
        )


@pytest.mark.parametrize("name,text", list(PROMPT_FIXTURES.items()))
def test_prompt_has_no_moderation_language(name: str, text: str) -> None:
    lowered = (text or "").lower()
    for banned in BANNED_MODERATION:
        assert banned not in lowered, (
            f"{name} contains moderation phrase: {banned!r}"
        )


def test_anton_brain_default_render_clean():
    """The full default Anton-Brain render (identity + rules) must
    be free of banned phrases."""
    ab = AntonBrain(budget_tokens=4000)
    rendered = ab.shape().lower()
    for banned in ALL_BANNED:
        assert banned not in rendered, (
            f"Anton-Brain default render contains banned phrase: {banned!r}"
        )


def test_router_prompt_does_not_mention_refusal_buckets():
    """The router only classifies tasks — it must not invent a
    'refused' or 'forbidden' bucket."""
    text = ROUTER_SYSTEM_PROMPT.lower()
    for forbidden_bucket in ("refused", "forbidden", "denied", "blocked"):
        # The classifier emits one of trivial/simple/complex/math.
        # Anything else is policy creep we explicitly forbid.
        assert forbidden_bucket not in text, (
            f"router prompt mentions forbidden bucket: {forbidden_bucket!r}"
        )
