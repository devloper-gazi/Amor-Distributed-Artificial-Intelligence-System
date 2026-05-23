"""Shared fixtures for tools/setup tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make sure `tools.setup` imports work even when pytest is invoked from
# a different directory.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _no_color(monkeypatch):
    """Force NO_COLOR so test output isn't littered with ANSI escapes."""

    monkeypatch.setenv("NO_COLOR", "1")
    yield


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A pristine 'repo root' fixture — no .env, no compose files."""

    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.fixture
def tmp_repo_with_compose(tmp_path: Path) -> Path:
    """A repo fixture with a minimal docker-compose.yml + .env.example."""

    compose_yaml = """\
services:
  app:
    image: amor/app
  redis:
    image: redis:7
  postgres:
    image: postgres:16
"""
    (tmp_path / "docker-compose.yml").write_text(compose_yaml, encoding="utf-8")
    (tmp_path / ".env.example").write_text(
        "LOG_LEVEL=INFO\n"
        "DEBUG=true\n"
        "GOOGLE_TRANSLATE_API_KEY=your-google-api-key-here\n"
        "POSTGRES_PASSWORD=docpass123\n",
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    return tmp_path
