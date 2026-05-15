"""
Cycle C Sprint 10 Day 4 — backend i18n + Accept-Language plumbing.

What this gives the rest of the codebase:

* :func:`t` — pure key-lookup translator with placeholder
  interpolation, mirroring the frontend's ``src/i18n/index.ts``.
* :func:`parse_accept_language` — RFC-7231 Accept-Language header
  parser (handles ``q=``-weighted entries, picks the highest-ranked
  supported locale, falls back to ``"en"``).
* :func:`get_locale` — FastAPI dependency that resolves a request's
  locale from (1) the explicit ``X-AMOR-Locale`` header, (2) the
  ``amor.locale`` cookie (set by the frontend's ``setLocale``),
  (3) ``Accept-Language``, (4) hard default ``"en"``.
* :func:`localized_http_exception` — emits an :class:`HTTPException`
  whose ``detail`` is already translated.

Why a fresh module instead of reusing the frontend strings: the
frontend table includes UI chrome (button labels, headings) the
backend never emits.  The backend table is intentionally smaller and
narrower — error / system messages only.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from fastapi import HTTPException, Request

from .messages import LOCALES, SUPPORTED


__all__ = [
    "Locale",
    "SUPPORTED",
    "t",
    "parse_accept_language",
    "get_locale",
    "localized_http_exception",
]


Locale = str  # "en" | "tr"


def t(key: str, locale: str = "en", **params: Any) -> str:
    """Look up ``key`` in ``locale``'s catalogue.  Falls back to
    ``"en"`` then to the literal key (so a missing translation is
    still visible in QA logs).

    ``params`` interpolate ``{{name}}`` placeholders the same way the
    frontend's ``t()`` does — values are coerced to strings, and
    ``None`` becomes the empty string.
    """
    table = LOCALES.get(locale) or LOCALES["en"]
    fallback = LOCALES["en"]
    template = table.get(key) or fallback.get(key) or key
    if params:
        for name, value in params.items():
            stringified = "" if value is None else str(value)
            template = template.replace(f"{{{{{name}}}}}", stringified)
    return template


# ─── Accept-Language parser ─────────────────────────────────────


def parse_accept_language(header: Optional[str]) -> str:
    """Return the highest-quality supported locale from a parsed
    ``Accept-Language`` header.  Examples::

        "tr,en-US;q=0.7"          → "tr"
        "en-US,en;q=0.9"          → "en"
        "fr,de;q=0.8"             → "en"   (no supported match)
        ""                        → "en"

    Stable ordering tie-break: when q-values match, the order in
    the header wins (matching what most browsers send).
    """
    if not header:
        return "en"
    weighted: list[tuple[float, int, str]] = []
    for idx, part in enumerate(header.split(",")):
        token = part.strip()
        if not token:
            continue
        if ";" in token:
            tag_part, q_part = token.split(";", 1)
            tag = tag_part.strip().lower()
            q = 1.0
            for kv in q_part.split(";"):
                kv = kv.strip()
                if kv.startswith("q="):
                    try:
                        q = float(kv[2:])
                    except ValueError:
                        q = 1.0
        else:
            tag = token.lower()
            q = 1.0
        if not tag:
            continue
        primary = tag.split("-")[0]
        weighted.append((q, idx, primary))
    if not weighted:
        return "en"
    # Sort by q descending, then by index ascending (stable order).
    weighted.sort(key=lambda triple: (-triple[0], triple[1]))
    for _, _, primary in weighted:
        if primary in SUPPORTED:
            return primary
    return "en"


# ─── FastAPI dependency ─────────────────────────────────────────


def get_locale(request: Request) -> str:
    """FastAPI dependency that resolves the locale per request.

    Resolution order (first hit wins):
      1. ``X-AMOR-Locale`` header (programmatic override)
      2. ``amor.locale`` cookie (the frontend sets this on
         ``setLocale``; same key as ``localStorage["amor.locale"]``).
      3. ``Accept-Language`` header.
      4. ``"en"`` default.
    """
    explicit = request.headers.get("x-amor-locale", "").strip().lower()
    if explicit and explicit in SUPPORTED:
        return explicit
    cookie = request.cookies.get("amor.locale", "").strip().lower()
    if cookie and cookie in SUPPORTED:
        return cookie
    return parse_accept_language(request.headers.get("accept-language"))


# ─── helpers for route layers ───────────────────────────────────


def localized_http_exception(
    *,
    status_code: int,
    key: str,
    locale: str,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> HTTPException:
    """Build an :class:`HTTPException` whose ``detail`` is translated
    via :func:`t`.  Routes import this so they don't have to repeat
    the lookup pattern."""
    detail = t(key, locale=locale, **(dict(params) if params else {}))
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=dict(headers) if headers else None,
    )
