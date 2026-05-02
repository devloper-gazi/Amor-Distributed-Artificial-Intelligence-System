"""Tests for the Phase 17 Commit O per-language sandbox timeout map.

Each ``LANGUAGE_RUNNERS`` entry now ships a ``default_timeout_s``
field.  ``ExecutionSandbox.execute(timeout=None)`` picks it up;
explicit ``timeout=`` from the caller always wins.
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence.sandbox import LANGUAGE_RUNNERS


# ─── catalogue invariant ───────────────────────────────────────────


def test_every_runner_has_default_timeout():
    for lang, cfg in LANGUAGE_RUNNERS.items():
        assert "default_timeout_s" in cfg, (
            f"language {lang!r} missing default_timeout_s"
        )
        assert isinstance(cfg["default_timeout_s"], int)
        assert cfg["default_timeout_s"] > 0


def test_html_and_css_use_tight_timeout():
    """Sub-second parsers shouldn't carry a 30s ceiling."""
    assert LANGUAGE_RUNNERS["html"]["default_timeout_s"] <= 10
    assert LANGUAGE_RUNNERS["css"]["default_timeout_s"] <= 10


def test_compile_heavy_languages_use_wider_timeout():
    """Compile + run pipelines need >30s on cold caches."""
    assert LANGUAGE_RUNNERS["rust"]["default_timeout_s"] >= 60
    assert LANGUAGE_RUNNERS["go"]["default_timeout_s"] >= 60
    assert LANGUAGE_RUNNERS["cpp"]["default_timeout_s"] >= 60
    assert LANGUAGE_RUNNERS["java"]["default_timeout_s"] >= 60
    assert LANGUAGE_RUNNERS["typescript"]["default_timeout_s"] >= 60
