# ADR-0006 — FastMCP MCP-server smoke test deferred

**Date:** 2026-04-27
**Status:** Accepted
**Context:** Master Prompt §4.8 step 5 lists "MCP handshake + tools/list"
as a smoke test for capability candidates of kind `mcp_server`. Master
Prompt §9 default names FastMCP 2.x as the framework.

## Decision

The MCP-server smoke test is **not implemented in v2**. The
`CapabilityDiscoverer` correctly classifies GitHub repos with
`mcp` in their query as `CapabilityKind.MCP_SERVER`, but in
non-strict mode (default) the smoke test is deferred ("deferred,
non-strict mode"). In strict mode the gate explicitly rejects the
candidate with a clean failure message.

## Rationale

- FastMCP 2.x is not yet a transitive dep. Adding it pulls in
  `mcp` SDK, which is itself substantial.
- A real MCP smoke test means launching a subprocess (or container)
  per candidate, performing the handshake, calling `tools/list` and
  validating the response shape. That's its own substantial module.
- Without strict-mode discovery elsewhere (sandbox install, smoke,
  benchmark) the MCP smoke alone produces marginal value.

## Consequences

- v2 discovers MCP server candidates (HF/GitHub `mcp-server` query)
  and lists them via `GET /api/code/capabilities` so a human can
  audit them before plugging them into the engine.
- v2.1 will land the strict-mode discovery harness end-to-end, at
  which point this ADR will be superseded.
- A user who wants MCP servers earlier can manually register them
  via direct Mongo writes to `capabilities` collection after
  reviewing the candidate.
