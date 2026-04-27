# Registered Capabilities

This file tracks capabilities discovered + registered by the
`CapabilityDiscoverer`. The discoverer runs once at startup (after a
60s settle delay) and then every
`CODE_CAPABILITY_DISCOVERY_INTERVAL_SECONDS` (default 3600s = hourly).

Each entry passed all six gates:
1. **License gate** — SPDX in {Apache-2.0, MIT, BSD-2/3, MPL-2.0, ISC,
   PostgreSQL}; AGPL/GPL rejected unless human-flagged.
2. **Metadata gate** — stars ≥ 50, last commit ≤ 18 months,
   parseable timestamps.
3. **Sandboxed install** — strict mode only (deferred in default
   non-strict mode).
4. **Smoke test** — strict mode only.
5. **Benchmark** — strict mode only.
6. **Registration** — written to MongoDB collection `capabilities`.

## Bootstrap entries

The discoverer starts empty. After the first cycle (typically within
1 minute of the app coming up), entries appear here. Inspect via:

```bash
curl -s http://localhost:8000/api/code/capabilities \
  -H "Authorization: Bearer $JWT" | jq .
```

To force a discovery cycle on demand:

```bash
curl -s -X POST http://localhost:8000/api/code/capabilities/discover \
  -H "Authorization: Bearer $JWT" | jq .
```

## Adding manual overrides

If you want to whitelist an AGPL package that the discoverer would
otherwise reject:

1. Create `document_processor/code_intelligence/capabilities_overrides.yaml`
   *(not yet committed — first override creates it)*.
2. Add: `overrides: ["acme/agpl-thing"]`.
3. Restart the app or call the admin reload endpoint.

The discoverer's `license_overrides` constructor parameter ingests
that list.

## Removing a capability

```python
from document_processor.code_intelligence import CapabilityRegistry
reg = CapabilityRegistry()
await reg.unregister("acme/example")
```

## What's NOT discovered

The discoverer does **not** harvest:
- Anything from paid AI vendors (`anthropic-sdk-python`, `openai`,
  `cohere`, `voyageai`).
- Anything tagged as a closed-source agent runtime.
- Anything older than 18 months without commits.
