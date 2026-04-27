# ADR-0007 — Strict-mode capability discovery gates deferred

**Date:** 2026-04-27
**Status:** Accepted
**Context:** Master Prompt §4.8 specifies six gates: license → metadata →
sandbox install → smoke test → benchmark → registration. Gates 3-5
require infrastructure not yet built.

## Decision

`CODE_CAPABILITY_STRICT=false` (default). In default (non-strict)
mode, gates 3-5 are marked passed with detail `"deferred"` after
gates 1-2 (license + metadata) succeed. In strict mode
(`CODE_CAPABILITY_STRICT=true`) gate 3 is marked **failed** with
explicit detail "strict mode requires sandboxed install harness — not
implemented in this revision", causing the candidate to be rejected
without registration.

## Rationale

The Tier-2 install harness (gate 3) requires:
- A fresh ephemeral Docker container per candidate
- A `uv venv` + `uv pip install --strict --no-cache` with 5-min
  timeout + 2 GB disk cap
- Pip log capture into MongoDB collection `capability_install_logs`
- A way to reach the registry's package index (PyPI, GitHub releases,
  HF Hub) from inside the gated network namespace

That's its own substantial module — easily a 500+-line implementation
+ orchestration. Per Charter Discipline 1 (pre-flight before any code),
it belongs in its own design pass, not bolted onto v2.

## Consequences

- Today: a user can opt into strict mode and get clean failures with
  actionable error messages (no false-positive registrations).
- Default mode: candidates pass gates 1-2 (cheap, deterministic) and
  land in the registry as advisory entries. The
  `GET /api/code/capabilities` endpoint surfaces them for human
  audit before any agent uses them.
- v2.1 will implement gates 3-5 and flip the default to strict.
