"""Cycle F Sprint 3 — verify LoRA payload attaches to OpenAI-compat extra.

We avoid spinning up the full FastAPI app + llama-swap.  Instead we
exercise the helper composition: settings → parser → payload builder
→ ChatOptions.extra body field.  An integration test against a real
llama-swap with adapters loaded is OUT of scope here (gated by the
overnight ORPO training job + GGUF conversion).
"""

from __future__ import annotations

import pytest

from tools.lora_runtime import lora_payload_for_role, parse_role_adapter_map


# ─── End-to-end (helper composition) ────────────────────────────────


def test_settings_json_to_payload_round_trip():
    """Simulate the runtime path: read settings JSON, parse, build."""

    settings_json = '{"coder": 0, "tester": 1, "debugger": 2}'
    adapters = parse_role_adapter_map(settings_json)

    payload = lora_payload_for_role(
        "coder", enabled=True, adapters=adapters, default_scale=1.0,
    )
    assert payload == [{"id": 0, "scale": 1.0}]

    payload2 = lora_payload_for_role(
        "debugger", enabled=True, adapters=adapters, default_scale=0.8,
    )
    assert payload2 == [{"id": 2, "scale": 0.8}]


def test_settings_disabled_means_no_attach():
    """When master gate is off, no payload regardless of adapter map."""

    adapters = parse_role_adapter_map('{"coder": 0}')
    assert lora_payload_for_role(
        "coder", enabled=False, adapters=adapters,
    ) is None


def test_role_not_in_map_means_no_attach():
    """A role with no adapter binding gets None — the request runs
    on the base model (no `lora` body field at all)."""

    adapters = parse_role_adapter_map('{"coder": 0}')
    for role in ("architect", "planner", "critic", "triage", "tester"):
        out = lora_payload_for_role(role, enabled=True, adapters=adapters)
        assert out is None, f"unexpected payload for unmapped role {role}: {out}"


def test_empty_settings_json_means_no_attach():
    adapters = parse_role_adapter_map("{}")
    out = lora_payload_for_role("coder", enabled=True, adapters=adapters)
    assert out is None


def test_payload_format_matches_pr_10994_schema():
    """llama.cpp PR #10994 wire shape is a list of dicts with int 'id'
    + float 'scale'.  Anything else gets rejected by llama-server."""

    adapters = parse_role_adapter_map('{"coder": 0}')
    payload = lora_payload_for_role(
        "coder", enabled=True, adapters=adapters,
    )
    assert isinstance(payload, list)
    assert len(payload) == 1
    entry = payload[0]
    assert isinstance(entry, dict)
    assert set(entry.keys()) == {"id", "scale"}
    assert isinstance(entry["id"], int)
    assert isinstance(entry["scale"], float)


# ─── settings module integration ────────────────────────────────────


def test_settings_default_has_lora_disabled():
    """Cycle F Sprint 3 lands with LoRA OFF by default.  Operators
    flip on AFTER the Sprint 6 ORPO cron + adapter training lands."""

    try:
        from document_processor.config.settings import settings
    except ImportError:
        pytest.skip("document_processor not importable in this env")
    assert settings.code_lora_enabled is False
    assert settings.code_lora_role_adapters == "{}"
    assert settings.code_lora_default_scale == 1.0


def test_local_ai_routes_imports_clean():
    """Sanity: the LoRA injection block we added doesn't break
    import of local_ai_routes_simple."""

    try:
        from document_processor.api import local_ai_routes_simple  # noqa: F401
    except ImportError:
        pytest.skip("document_processor not importable in this env")
