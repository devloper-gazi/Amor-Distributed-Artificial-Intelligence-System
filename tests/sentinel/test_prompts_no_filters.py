"""Build-gate test: no Sentinel prompt template may contain refusal
language, legal disclaimers, or content-moderation pre-filters.

The user's "no filter" policy applies equally to Sentinel — its
RedTeam agent in particular MUST be allowed to describe exploit
chains in concrete technical detail.  A refusal-prone prompt
would be a UX disaster.

Mirrors ``tests/quick_code/test_prompts_no_filters.py``.
"""

from __future__ import annotations

import pytest

from document_processor.sentinel.prompts import (
    AUDITOR_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    PATCHER_SYSTEM_PROMPT,
    REASONER_SYSTEM_PROMPT,
    REDTEAM_SYSTEM_PROMPT,
)


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


PROMPT_FIXTURES: dict[str, str] = {
    "AUDITOR_SYSTEM_PROMPT":  AUDITOR_SYSTEM_PROMPT,
    "REASONER_SYSTEM_PROMPT": REASONER_SYSTEM_PROMPT,
    "REDTEAM_SYSTEM_PROMPT":  REDTEAM_SYSTEM_PROMPT,
    "PATCHER_SYSTEM_PROMPT":  PATCHER_SYSTEM_PROMPT,
    "JUDGE_SYSTEM_PROMPT":    JUDGE_SYSTEM_PROMPT,
}


@pytest.mark.parametrize("name,text", list(PROMPT_FIXTURES.items()))
def test_prompt_has_no_refusal_language(name: str, text: str) -> None:
    lower = (text or "").lower()
    for banned in BANNED_REFUSALS:
        assert banned not in lower, (
            f"{name} contains refusal phrase: {banned!r}"
        )


@pytest.mark.parametrize("name,text", list(PROMPT_FIXTURES.items()))
def test_prompt_has_no_legal_disclaimers(name: str, text: str) -> None:
    lower = (text or "").lower()
    for banned in BANNED_LEGAL:
        assert banned not in lower, (
            f"{name} contains legal disclaimer: {banned!r}"
        )


@pytest.mark.parametrize("name,text", list(PROMPT_FIXTURES.items()))
def test_prompt_has_no_moderation_language(name: str, text: str) -> None:
    lower = (text or "").lower()
    for banned in BANNED_MODERATION:
        assert banned not in lower, (
            f"{name} contains moderation phrase: {banned!r}"
        )


def test_redteam_prompt_explicitly_allows_concrete_exploits():
    """RedTeam must explicitly invite concrete exploit detail —
    otherwise it falls back to generic hedging on its own."""
    text = REDTEAM_SYSTEM_PROMPT.lower()
    assert "concrete" in text
    assert "exploit" in text
    assert "do not refuse" in text or "do not hedge" in text


def test_required_expert_tone():
    """Every Sentinel system prompt must open with the canonical
    "expert engineer" line."""
    for name, text in PROMPT_FIXTURES.items():
        first = (text or "").splitlines()[0]
        assert "expert" in first.lower(), (
            f"{name} does not start with the expert-tone line: {first!r}"
        )
