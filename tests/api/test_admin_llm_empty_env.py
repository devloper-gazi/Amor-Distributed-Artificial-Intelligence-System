"""v18.1.1 hotfix — falsy-empty-env bug coverage for the LLM admin
route resolvers.

Reproduces the "backend kind '' has no live probe" screenshot from
2026-05-16:

  $ docker exec amor-app-2 env | grep AMOR_LLM
  AMOR_LLM_BACKEND=          # SET but empty
  AMOR_LLM_BACKEND_URL=      # SET but empty

  $ docker exec amor-app-2 python -c "from document_processor.config.settings import settings; print(repr(settings.llm_backend))"
  ''

  Result on the UI: backend kind '' has no live probe.

The fix: ``os.environ.get(KEY, default)`` returns ``""`` (not
``default``) when ``KEY`` exists but is empty.  Replace with a
falsy-skip helper that falls through to the explicit default.
Same pattern as ``tools/eval/humaneval_plus.py:_llm_base_url`` and
``tools/eval/swebench_lite.py:_llm_base_url`` fixes from v18.1.
"""

from __future__ import annotations

import pytest


def test_env_or_default_returns_default_on_empty(monkeypatch):
    """The core helper — empty env var returns explicit default."""
    from document_processor.api.admin_llm_routes import _env_or_default
    monkeypatch.setenv("AMOR_TEST_KEY", "")
    assert _env_or_default("AMOR_TEST_KEY", "fallback") == "fallback"


def test_env_or_default_returns_default_on_unset(monkeypatch):
    from document_processor.api.admin_llm_routes import _env_or_default
    monkeypatch.delenv("AMOR_TEST_KEY", raising=False)
    assert _env_or_default("AMOR_TEST_KEY", "fallback") == "fallback"


def test_env_or_default_returns_value_when_set(monkeypatch):
    from document_processor.api.admin_llm_routes import _env_or_default
    monkeypatch.setenv("AMOR_TEST_KEY", "explicit")
    assert _env_or_default("AMOR_TEST_KEY", "fallback") == "explicit"


def test_env_or_default_strips_whitespace(monkeypatch):
    """Common compose pitfall — `AMOR_LLM_BACKEND= ollama  ` with
    extra whitespace.  The fix strips it before the truthiness test."""
    from document_processor.api.admin_llm_routes import _env_or_default
    monkeypatch.setenv("AMOR_TEST_KEY", "  spaced  ")
    assert _env_or_default("AMOR_TEST_KEY", "fallback") == "spaced"


def test_resolve_active_backend_returns_ollama_when_env_empty(monkeypatch):
    """Reproduces the screenshot bug — `AMOR_LLM_BACKEND=` (empty)
    must resolve to 'ollama', not to ''."""
    from document_processor.api import admin_llm_routes

    # Force the settings-side fallthrough.
    monkeypatch.setattr(
        admin_llm_routes.settings, "llm_backend", "",
        raising=False,
    )
    monkeypatch.setenv("AMOR_LLM_BACKEND", "")
    assert admin_llm_routes._resolve_active_backend() == "ollama"


def test_resolve_active_backend_returns_llamaswap_when_explicit(monkeypatch):
    from document_processor.api import admin_llm_routes
    monkeypatch.setattr(
        admin_llm_routes.settings, "llm_backend", "llama-swap",
        raising=False,
    )
    assert admin_llm_routes._resolve_active_backend() == "llama-swap"


def test_resolve_active_backend_strips_settings_value(monkeypatch):
    from document_processor.api import admin_llm_routes
    monkeypatch.setattr(
        admin_llm_routes.settings, "llm_backend", "  LLAMA-SWAP  ",
        raising=False,
    )
    assert admin_llm_routes._resolve_active_backend() == "llama-swap"


def test_llamaswap_base_url_uses_env_default_when_settings_empty(monkeypatch):
    from document_processor.api import admin_llm_routes
    monkeypatch.setattr(
        admin_llm_routes.settings, "llm_backend_url", "",
        raising=False,
    )
    monkeypatch.setenv("AMOR_LLAMASWAP_URL", "")
    assert admin_llm_routes._llamaswap_base_url() == "http://amor-llama-swap:9100"


def test_llamaswap_base_url_uses_env_override_when_set(monkeypatch):
    from document_processor.api import admin_llm_routes
    monkeypatch.setattr(
        admin_llm_routes.settings, "llm_backend_url", "",
        raising=False,
    )
    monkeypatch.setenv("AMOR_LLAMASWAP_URL", "http://swap:9999")
    assert admin_llm_routes._llamaswap_base_url() == "http://swap:9999"


def test_llamaswap_base_url_prefers_settings_over_env(monkeypatch):
    """When settings.llm_backend_url is set, it wins over the env."""
    from document_processor.api import admin_llm_routes
    monkeypatch.setattr(
        admin_llm_routes.settings, "llm_backend_url", "http://settings-pin:9100",
        raising=False,
    )
    monkeypatch.setenv("AMOR_LLAMASWAP_URL", "http://env-loser:9100")
    assert admin_llm_routes._llamaswap_base_url() == "http://settings-pin:9100"


def test_local_ai_resolve_kind_falsy_skips_empty_env(monkeypatch):
    """The factory in local_ai/llm_backend/__init__.py had the SAME
    falsy-empty-env bug.  Verifies the matching fix."""
    import local_ai.llm_backend as llm_backend_pkg
    # Force the settings-side fallthrough.
    try:
        from document_processor.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "llm_backend", "", raising=False)
    except Exception:
        pass
    monkeypatch.setenv("AMOR_LLM_BACKEND", "")
    assert llm_backend_pkg._resolve_kind() == "ollama"


def test_local_ai_resolve_url_falsy_skips_empty_env(monkeypatch):
    """Same bug in `_resolve_url` — empty env must fall through to
    the next env var, then to the localhost default."""
    import local_ai.llm_backend as llm_backend_pkg
    try:
        from document_processor.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "llm_backend_url", "", raising=False)
    except Exception:
        pass
    monkeypatch.setenv("AMOR_LLM_BACKEND_URL", "")
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    assert llm_backend_pkg._resolve_url() == "http://localhost:11434"


def test_local_ai_resolve_url_prefers_first_non_empty_env(monkeypatch):
    import local_ai.llm_backend as llm_backend_pkg
    try:
        from document_processor.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "llm_backend_url", "", raising=False)
    except Exception:
        pass
    monkeypatch.setenv("AMOR_LLM_BACKEND_URL", "")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama-host:11434")
    assert llm_backend_pkg._resolve_url() == "http://ollama-host:11434"
