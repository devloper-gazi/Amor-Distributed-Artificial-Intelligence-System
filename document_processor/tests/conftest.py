"""
Shared pytest fixtures for the document_processor test suite.

Kept deliberately minimal — Phase 1 only ships unit tests for the
`research/relevance.py` module, which is offline-safe by design (no
Redis, no Ollama, no network). New fixtures should preserve that
property unless a test is explicitly tagged as integration.
"""

from __future__ import annotations

import asyncio

import pytest


# pytest-asyncio < 0.23 used class-scoped event loops by default; on
# 0.23+ the default became function-scoped, which is what we want
# here. The fixture below is a safe override either way.
@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()
