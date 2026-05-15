"""
Cycle C Sprint 10 Day 4 — backend i18n module tests.

Three concerns to pin:

* ``parse_accept_language`` handles RFC-7231 weights + falls back to
  English when nothing supported matches.
* ``t()`` looks up keys per locale, falls back en→key, interpolates
  ``{{name}}`` placeholders.
* ``get_locale`` resolves request locale from
  ``X-AMOR-Locale`` → ``amor.locale`` cookie → ``Accept-Language`` →
  ``"en"`` default, in that order.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from document_processor.i18n import (
    get_locale,
    localized_http_exception,
    parse_accept_language,
    t,
)


# ─── translator ─────────────────────────────────────────────────


def test_t_returns_locale_specific_string():
    assert t("common.not_found", "en") == "not found"
    assert t("common.not_found", "tr") == "bulunamadı"


def test_t_interpolates_placeholders():
    out = t("common.invalid_mode", "en", mode="bogus")
    assert out == "invalid mode: bogus"
    out_tr = t("common.invalid_mode", "tr", mode="bogus")
    assert out_tr == "geçersiz mod: bogus"


def test_t_falls_back_to_english_when_locale_missing_key(monkeypatch):
    """Pretend a TR table is missing a key and verify English wins."""
    from document_processor.i18n import messages as msgs
    saved = msgs.LOCALES["tr"].pop("common.not_found", None)
    try:
        assert t("common.not_found", "tr") == "not found"
    finally:
        if saved is not None:
            msgs.LOCALES["tr"]["common.not_found"] = saved


def test_t_falls_back_to_key_when_both_tables_miss():
    assert t("totally.nonexistent.key", "tr") == "totally.nonexistent.key"


def test_t_handles_unknown_locale():
    """Pass a locale we don't ship — should silently use English."""
    assert t("common.not_found", "fr") == "not found"


def test_t_renders_none_param_as_empty_string():
    out = t("common.invalid_mode", "en", mode=None)
    assert out == "invalid mode: "


# ─── parse_accept_language ──────────────────────────────────────


def test_parse_simple_header():
    assert parse_accept_language("tr") == "tr"
    assert parse_accept_language("en") == "en"


def test_parse_weighted_header():
    # tr has higher q than en — Turkish wins.
    assert parse_accept_language("tr;q=0.9,en;q=0.7") == "tr"
    # en wins (default q=1).
    assert parse_accept_language("tr;q=0.5,en") == "en"


def test_parse_falls_back_when_unsupported():
    # Locale we don't ship → fall back to en.
    assert parse_accept_language("fr,de;q=0.8") == "en"
    assert parse_accept_language("ja-JP;q=1.0") == "en"


def test_parse_handles_empty():
    assert parse_accept_language("") == "en"
    assert parse_accept_language(None) == "en"


def test_parse_normalises_subtags():
    # ``tr-TR`` should resolve to ``tr``.
    assert parse_accept_language("tr-TR,en;q=0.5") == "tr"
    # ``en-US`` resolves to ``en``.
    assert parse_accept_language("en-US,en;q=0.9") == "en"


def test_parse_picks_first_for_q_ties():
    """When q-values match, the order in the header wins (matching
    what most browsers send)."""
    assert parse_accept_language("en,tr") == "en"
    assert parse_accept_language("tr,en") == "tr"


def test_parse_ignores_malformed_q():
    # ``q=garbage`` falls back to q=1.0 for that entry.
    assert parse_accept_language("tr;q=garbage,en;q=0.5") == "tr"


# ─── get_locale FastAPI dep ─────────────────────────────────────


def _build_probe_app() -> FastAPI:
    """Tiny FastAPI app that just echoes the resolved locale."""
    app = FastAPI()
    r = APIRouter()

    @r.get("/probe")
    def probe(locale: str = Depends(get_locale)) -> dict[str, Any]:
        return {"locale": locale}

    app.include_router(r)
    return app


def test_get_locale_default_is_english():
    app = _build_probe_app()
    with TestClient(app) as c:
        r = c.get("/probe")
        assert r.status_code == 200
        assert r.json()["locale"] == "en"


def test_get_locale_uses_accept_language():
    app = _build_probe_app()
    with TestClient(app) as c:
        r = c.get("/probe", headers={"Accept-Language": "tr"})
        assert r.json()["locale"] == "tr"


def test_get_locale_cookie_overrides_accept_language():
    app = _build_probe_app()
    with TestClient(app) as c:
        # Accept-Language says English; cookie says Turkish; cookie wins.
        c.cookies.set("amor.locale", "tr")
        r = c.get("/probe", headers={"Accept-Language": "en"})
        assert r.json()["locale"] == "tr"


def test_get_locale_explicit_header_overrides_cookie():
    app = _build_probe_app()
    with TestClient(app) as c:
        c.cookies.set("amor.locale", "en")
        r = c.get(
            "/probe",
            headers={"X-AMOR-Locale": "tr", "Accept-Language": "en"},
        )
        assert r.json()["locale"] == "tr"


def test_get_locale_ignores_unsupported_header():
    app = _build_probe_app()
    with TestClient(app) as c:
        r = c.get(
            "/probe",
            headers={"X-AMOR-Locale": "klingon", "Accept-Language": "tr"},
        )
        # X-AMOR-Locale is unsupported → fall through to Accept-Language.
        assert r.json()["locale"] == "tr"


# ─── localized_http_exception ──────────────────────────────────


def test_localized_http_exception_translates_detail():
    exc_en = localized_http_exception(
        status_code=404, key="common.not_found", locale="en",
    )
    exc_tr = localized_http_exception(
        status_code=404, key="common.not_found", locale="tr",
    )
    assert exc_en.status_code == 404
    assert exc_en.detail == "not found"
    assert exc_tr.detail == "bulunamadı"


def test_localized_http_exception_with_params():
    exc = localized_http_exception(
        status_code=422,
        key="common.invalid_mode",
        locale="tr",
        params={"mode": "X"},
    )
    assert exc.detail == "geçersiz mod: X"


# ─── integration: real route with localized error ─────────────


@pytest.fixture
def fake_user():
    from document_processor.auth.models import User
    return User(
        id="00000000-0000-0000-0000-000000000001",
        username="i18ner",
        email="i@example.com",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        is_active=True,
    )


def test_repo_routes_localize_503(monkeypatch, fake_user):
    """Hit /api/repo/symbols with the index helper raising — verify
    the 503 detail respects Accept-Language."""
    from document_processor.api import repo_routes as r
    from document_processor.auth.dependencies import get_current_user

    class _Boom:
        def search(self, *_args, **_kwargs): raise RuntimeError("boom")
        def all_tags(self): raise RuntimeError("boom")
        def stats(self): raise RuntimeError("boom")

    monkeypatch.setattr(r, "_get_repo_map", lambda: _Boom())

    app = FastAPI()
    app.include_router(r.router)
    app.dependency_overrides[get_current_user] = lambda: fake_user

    with TestClient(app) as c:
        r_en = c.get(
            "/api/repo/symbols",
            params={"q": "x"},
            headers={"Accept-Language": "en"},
        )
        r_tr = c.get(
            "/api/repo/symbols",
            params={"q": "x"},
            headers={"Accept-Language": "tr"},
        )
    assert r_en.status_code == 503
    assert r_tr.status_code == 503
    assert r_en.json()["detail"] == "repo index unavailable"
    assert r_tr.json()["detail"] == "repo dizini kullanılamıyor"
