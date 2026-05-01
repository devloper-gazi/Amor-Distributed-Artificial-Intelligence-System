"""
Shared fixtures for ``tests/quick_code/``.

By default we disable the V2 master gate (``quick_v2_enabled``) and
the V2 ORPO exporter so the pre-V2 contract tests in
``test_engine.py`` / ``test_engine_mesh.py`` / ``test_engine_phase1b.py``
/ ``test_engine_reactor.py`` keep observing the byte-identical
behaviour they were written for.  V2-specific test files
(``test_engine_v2_*``) opt back in via their own fixture.

We also clear any Striatum cache state so that one test's
``store(prompt, bundle)`` cannot leak into another test's
``lookup(prompt)`` via the shared Redis layer.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _disable_quick_v2(monkeypatch):
    """Disable the V2 master gate for every test in this directory.

    V2-specific test files override this by calling::

        @pytest.fixture(autouse=True)
        def _enable_v2(monkeypatch):
            from document_processor.config import settings as s
            monkeypatch.setattr(s.settings, "quick_v2_enabled", True)
    """
    try:
        from document_processor.config import settings as s

        monkeypatch.setattr(s.settings, "quick_v2_enabled", False)
        # ORPO is already off by default but pin it explicitly so a
        # local .env can't flip it on inside a test.
        monkeypatch.setattr(s.settings, "quick_v2_orpo_enabled", False)
    except Exception:
        # If settings can't be loaded we let individual tests handle it.
        pass


@pytest.fixture(autouse=True)
def _clean_striatum_cache():
    """Best-effort wipe of the shared Striatum Redis key.  Does
    nothing if Redis isn't reachable."""
    yield
    try:
        from document_processor.infrastructure.cache import cache_manager

        async def _wipe():
            try:
                await cache_manager.delete_pattern(
                    "amor:quick_code:striatum:*"
                )
            except Exception:
                pass

        try:
            asyncio.run(_wipe())
        except RuntimeError:
            # Already inside a running loop (e.g. pytest-asyncio);
            # skip the wipe — the next test's monkeypatched salt
            # will isolate it anyway.
            pass
    except Exception:
        # Cache layer or Redis missing — fine, nothing to clean.
        pass
