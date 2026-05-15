"""
Cycle C Sprint 4 Day 2 — repo symbol discovery route tests.

Validates:
1. ``GET /api/repo/symbols`` returns the canonical envelope shape
2. Empty ``q`` returns the first ``limit`` rows (bootstrapping the
   @-mention picker on first keystroke)
3. Non-empty ``q`` substring-matches the ``Tag.name`` column
4. ``kind`` filter narrows the result list
5. ``GET /api/repo/stats`` returns a non-empty manifest
6. RepoMap singleton is cached across calls (1 scan, not N)

The RepoMap layer uses an actual SQLite cache against a tiny pytest-
managed source tree — so this is an integration test, not a mock.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def fake_user():
    from document_processor.auth.models import User
    return User(
        id="test-user-repo",
        username="tester",
        email="t@example.com",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        is_active=True,
    )


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    """A 3-file repo with predictable symbols.  Names chosen so we
    can assert specific substrings deterministically."""
    (tmp_path / "alpha.py").write_text(
        textwrap.dedent(
            """
            \"\"\"Alpha module.\"\"\"

            class Apple:
                def slice_apple(self):
                    return 1

            def helper_alpha():
                return 0
            """
        ).strip()
    )
    (tmp_path / "beta.py").write_text(
        textwrap.dedent(
            """
            class Banana:
                pass

            def helper_beta(x):
                return x + 1
            """
        ).strip()
    )
    (tmp_path / "gamma.ts").write_text(
        textwrap.dedent(
            """
            export function helperGamma() {
              return 1;
            }

            export class Grape {}

            export interface IConfig {
              foo: string;
            }
            """
        ).strip()
    )
    return tmp_path


def _build_app(tiny_repo: Path, fake_user, monkeypatch) -> FastAPI:
    """Spin up a FastAPI with only the repo router + auth bypass +
    AMOR_REPOMAP_ROOT pointing at the tiny fixture repo."""
    monkeypatch.setenv("AMOR_REPOMAP_ROOT", str(tiny_repo))

    from document_processor.api import repo_routes as r

    # Reset the module-level singleton so this test sees a fresh map.
    r._REPOMAP_INSTANCE = None  # type: ignore[attr-defined]
    r._REPOMAP_LAST_SCAN_TS = 0.0  # type: ignore[attr-defined]

    app = FastAPI()
    app.include_router(r.router)

    from document_processor.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: fake_user
    return app


def test_symbols_substring_match_python(tiny_repo, fake_user, monkeypatch):
    app = _build_app(tiny_repo, fake_user, monkeypatch)
    with TestClient(app) as client:
        r = client.get("/api/repo/symbols", params={"q": "apple"})
        assert r.status_code == 200, r.text
        body = r.json()
        names = {item["name"] for item in body["items"]}
        # Both ``Apple`` (class) and ``slice_apple`` (method) must hit.
        assert "Apple" in names
        assert "slice_apple" in names
        # Banana has nothing to do with apple.
        assert "Banana" not in names


def test_symbols_empty_q_returns_first_page(tiny_repo, fake_user, monkeypatch):
    app = _build_app(tiny_repo, fake_user, monkeypatch)
    with TestClient(app) as client:
        r = client.get("/api/repo/symbols", params={"limit": 5})
        assert r.status_code == 200
        body = r.json()
        assert body["q"] == ""
        assert body["count"] >= 1
        assert len(body["items"]) <= 5


def test_symbols_kind_filter(tiny_repo, fake_user, monkeypatch):
    app = _build_app(tiny_repo, fake_user, monkeypatch)
    with TestClient(app) as client:
        r = client.get(
            "/api/repo/symbols",
            params={"q": "helper", "kind": "def", "limit": 50},
        )
        assert r.status_code == 200
        body = r.json()
        assert all(item["kind"] == "def" for item in body["items"])
        names = {item["name"] for item in body["items"]}
        assert "helper_alpha" in names
        assert "helper_beta" in names


def test_symbols_label_format(tiny_repo, fake_user, monkeypatch):
    """``label`` is the ready-to-insert ``@[name](path:line)`` token
    the composer drops into the textarea."""
    app = _build_app(tiny_repo, fake_user, monkeypatch)
    with TestClient(app) as client:
        r = client.get("/api/repo/symbols", params={"q": "Apple"})
        item = next(it for it in r.json()["items"] if it["name"] == "Apple")
        assert item["label"].startswith("@[Apple](")
        assert ":" in item["label"]
        assert item["path"].endswith(".py")


def test_stats_endpoint(tiny_repo, fake_user, monkeypatch):
    app = _build_app(tiny_repo, fake_user, monkeypatch)
    with TestClient(app) as client:
        r = client.get("/api/repo/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["repo_root"] == str(tiny_repo)
        assert body["files"] >= 1
        assert body["tags"] >= 1


def test_singleton_repo_map_cached(tiny_repo, fake_user, monkeypatch):
    """Two consecutive symbol calls in the same process must reuse
    the same RepoMap instance — that's the whole point of the
    module-level singleton.  We verify by calling ``_get_repo_map``
    directly twice."""
    monkeypatch.setenv("AMOR_REPOMAP_ROOT", str(tiny_repo))
    from document_processor.api import repo_routes as r
    r._REPOMAP_INSTANCE = None  # type: ignore[attr-defined]
    r._REPOMAP_LAST_SCAN_TS = 0.0  # type: ignore[attr-defined]
    a = r._get_repo_map()
    b = r._get_repo_map()
    assert a is b


def test_symbols_typescript_export(tiny_repo, fake_user, monkeypatch):
    """The TS regex sniffer should pick up ``export function`` and
    ``export class`` declarations."""
    app = _build_app(tiny_repo, fake_user, monkeypatch)
    with TestClient(app) as client:
        r = client.get("/api/repo/symbols", params={"q": "Grape", "limit": 20})
        body = r.json()
        names = {item["name"] for item in body["items"]}
        assert "Grape" in names

        r2 = client.get("/api/repo/symbols", params={"q": "helperGamma", "limit": 20})
        names2 = {item["name"] for item in r2.json()["items"]}
        assert "helperGamma" in names2
