"""Cycle F Sprint 3 — tests for tools/lora_runtime.py.

Covers the JSON-settings parser, the per-role payload builder, and
the disable-all helper.  All offline — zero llama-swap dependency.
"""

from __future__ import annotations

import pytest

from tools.lora_runtime import (
    disable_all_adapters_payload,
    lora_payload_for_role,
    parse_role_adapter_map,
)


# ─── parse_role_adapter_map ─────────────────────────────────────────


def test_parse_empty_string_returns_empty_dict():
    assert parse_role_adapter_map("") == {}
    assert parse_role_adapter_map("  ") == {}


def test_parse_none_returns_empty_dict():
    assert parse_role_adapter_map(None) == {}


def test_parse_valid_json():
    out = parse_role_adapter_map('{"coder": 0, "tester": 1, "debugger": 2}')
    assert out == {"coder": 0, "tester": 1, "debugger": 2}


def test_parse_accepts_dict_directly():
    out = parse_role_adapter_map({"Coder": 0, "TESTER": 1})
    # Keys lowercased.
    assert out == {"coder": 0, "tester": 1}


def test_parse_skips_bad_entries():
    out = parse_role_adapter_map(
        '{"coder": "not-an-int", "tester": 1, "debugger": null, "x": 9}'
    )
    assert out == {"tester": 1, "x": 9}


def test_parse_malformed_json_returns_empty_dict():
    assert parse_role_adapter_map("not json {") == {}
    assert parse_role_adapter_map("[1,2,3]") == {}  # not a mapping
    assert parse_role_adapter_map('"just a string"') == {}


def test_parse_non_string_keys_are_skipped():
    # Defensive; we declare type as Mapping[str, Any] but bad data is
    # tolerated rather than raising.
    out = parse_role_adapter_map({1: 0, "coder": 1})
    assert out == {"coder": 1}


# ─── lora_payload_for_role ──────────────────────────────────────────


def test_payload_returns_none_when_disabled():
    out = lora_payload_for_role("coder", enabled=False, adapters={"coder": 0})
    assert out is None


def test_payload_returns_none_when_no_role():
    out = lora_payload_for_role(None, enabled=True, adapters={"coder": 0})
    assert out is None


def test_payload_returns_none_when_role_not_mapped():
    out = lora_payload_for_role(
        "architect", enabled=True, adapters={"coder": 0},
    )
    assert out is None


def test_payload_emits_default_scale():
    out = lora_payload_for_role(
        "coder", enabled=True, adapters={"coder": 0}, default_scale=1.0,
    )
    assert out == [{"id": 0, "scale": 1.0}]


def test_payload_emits_custom_default_scale():
    out = lora_payload_for_role(
        "coder", enabled=True, adapters={"coder": 5}, default_scale=0.6,
    )
    assert out == [{"id": 5, "scale": 0.6}]


def test_payload_respects_role_scale_override():
    out = lora_payload_for_role(
        "coder",
        enabled=True,
        adapters={"coder": 0},
        default_scale=1.0,
        role_scales={"coder": 0.5},
    )
    assert out == [{"id": 0, "scale": 0.5}]


def test_payload_role_lookup_is_case_insensitive():
    out = lora_payload_for_role(
        "CODER", enabled=True, adapters={"coder": 0},
    )
    assert out == [{"id": 0, "scale": 1.0}]


def test_payload_with_empty_adapters_returns_none():
    out = lora_payload_for_role("coder", enabled=True, adapters={})
    assert out is None


# ─── disable_all_adapters_payload ───────────────────────────────────


def test_disable_all_emits_zero_scale_per_id():
    out = disable_all_adapters_payload([0, 1, 2])
    assert out == [
        {"id": 0, "scale": 0.0},
        {"id": 1, "scale": 0.0},
        {"id": 2, "scale": 0.0},
    ]


def test_disable_all_handles_empty_sequence():
    assert disable_all_adapters_payload([]) == []


def test_disable_all_coerces_ids_to_int():
    out = disable_all_adapters_payload(["0", "1"])
    assert out == [
        {"id": 0, "scale": 0.0},
        {"id": 1, "scale": 0.0},
    ]
