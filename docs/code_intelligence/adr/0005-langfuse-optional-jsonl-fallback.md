# ADR-0005 — Langfuse optional, JSONL traces by default

**Date:** 2026-04-27
**Status:** Accepted
**Context:** Master Prompt §9 default suggests Langfuse v3 self-hosted.

## Decision

`@traced` decorator + `emit_event()` write to **Langfuse only when
all three env vars are set** (`CODE_LANGFUSE_URL`,
`CODE_LANGFUSE_PUBLIC_KEY`, `CODE_LANGFUSE_SECRET_KEY`) AND the
`langfuse` package is importable. Otherwise spans are appended as JSON
lines to `document_processor/code_intelligence/traces/{date}.jsonl`,
one line per span.

## Rationale

- Langfuse self-hosted is a substantial deployment commitment
  (its own Postgres, ClickHouse, web UI). Many AMOR users will
  not want it.
- JSONL traces are debuggable with grep / jq — the lowest possible
  bar of "how do I see what happened during yesterday's session?".
- The decorator code paths are identical for both backends; the
  switch is a runtime check at first call.

## Consequences

- Default deploy → JSONL.
- Users who want a UI / SQL aggregation install Langfuse separately
  and set the three env vars. No code change needed in AMOR.
- v2.1 may add a Grafana dashboard that reads the JSONL files
  directly so users get a UI without Langfuse.
