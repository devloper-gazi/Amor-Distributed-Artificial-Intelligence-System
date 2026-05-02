"""Tests for the Phase 16.5 Commit L language sniffer.

Sniffs HTML / CSS / Python / JS from the body of a code fence so the
sandbox runs the right language even when the LLM mis-labels the
fence.
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence.agents import (
    _extract_code_and_meta,
    _sniff_language_from_content,
)


# ─── _sniff_language_from_content ─────────────────────────────────


@pytest.mark.parametrize("code, fallback, expected", [
    # HTML markers always win.
    ("<!DOCTYPE html>\n<html><body></body></html>", "javascript", "html"),
    ("<html><head></head></html>", "javascript", "html"),
    # CSS markers.
    ("body { color: red; }", "javascript", "css"),
    ("@media (max-width: 600px) { .x { } }", "html", "css"),
    ("@import url('reset.css');\nbody { margin: 0; }", "javascript", "css"),
    # Shebangs.
    ("#!/usr/bin/env python3\nprint('hi')", "javascript", "python"),
    ("#!/usr/bin/env node\nconsole.log(1)", "python", "javascript"),
    # No strong markers — pass through fallback.
    ("def hello(): pass", "python", "python"),
    ("console.log(1)", "javascript", "javascript"),
    # Empty fallback returns ``""`` for ambiguous code.
    ("def hello(): pass", "", ""),
])
def test_sniff_language_from_content(code, fallback, expected):
    assert _sniff_language_from_content(code, fallback=fallback) == expected


def test_sniff_html_with_doctype_lowercase():
    assert _sniff_language_from_content(
        "<!doctype html>\n<html></html>", fallback="javascript",
    ) == "html"


def test_sniff_html_overrides_misleading_fallback():
    """The bug the user hit: snake-game-website returned HTML in a
    fence labelled ``javascript`` and the engine ran it through Node."""
    body = (
        "<!DOCTYPE html>\n<html><body>"
        "<canvas id='c'></canvas>"
        "<script>const ctx=document.getElementById('c').getContext('2d');"
        "</script></body></html>"
    )
    assert _sniff_language_from_content(body, fallback="javascript") == "html"


def test_sniff_empty_input():
    assert _sniff_language_from_content("", fallback="python") == "python"
    assert _sniff_language_from_content(None, fallback="python") == "python"  # type: ignore


# ─── End-to-end via _extract_code_and_meta ────────────────────────


def test_extract_overrides_fence_label_for_html():
    raw = (
        "Here is your snake game website:\n\n"
        "```javascript\n"
        "<!DOCTYPE html>\n"
        "<html><head><title>Snake</title></head>\n"
        "<body><canvas id='c'></canvas>\n"
        "<script>console.log('hi')</script></body></html>\n"
        "```\n"
    )
    result = _extract_code_and_meta(raw)
    assert result["language"] == "html", (
        f"sniffer must override the fence label; "
        f"got {result['language']!r}"
    )
    assert "<!DOCTYPE" in result["code"]


def test_extract_keeps_correct_fence_label():
    """When the fence label is right, the sniffer doesn't override it."""
    raw = (
        "```python\n"
        "def hello(): print('hi')\n"
        "```\n"
    )
    result = _extract_code_and_meta(raw)
    assert result["language"] == "python"


def test_extract_picks_largest_when_multiple_fences():
    """The longest code block is the primary."""
    raw = (
        "```bash\necho hi\n```\n"
        "```python\n"
        "import os\n" * 10 +
        "```\n"
    )
    result = _extract_code_and_meta(raw)
    assert result["language"] == "python"
    assert "import os" in result["code"]
