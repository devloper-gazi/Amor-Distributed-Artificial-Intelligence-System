# ADR-0004 — Bare asyncio for the discoverer, not Taskiq

**Date:** 2026-04-27
**Status:** Accepted
**Context:** Master Prompt §9 default suggests Taskiq with the existing Redis broker.

## Decision

`CapabilityDiscoverer.run_forever()` is a plain `asyncio.create_task()`
spawned from the FastAPI lifespan. No Taskiq / Celery introduced in v2.

## Rationale

- The discoverer is single-instance per process. Cross-replica
  coordination is not currently required (registry writes are upserted
  to MongoDB on `name`, so duplicate cycles produce identical end
  state).
- The FastAPI lifespan gives us clean cancellation on shutdown — the
  same pattern used by `_sse_queue_sweeper`.
- Taskiq would add a worker container + broker config + monitoring
  story without a current gain.

## Consequences

- A future need for cross-replica cron (e.g., one cycle per cluster
  rather than per process) will trigger a Taskiq adoption ADR. The
  switch is mechanical: change `asyncio.create_task` to
  `taskiq_redis.RedisStreamBroker.task()` and the cycle method
  becomes a Taskiq task.
- Today's deployment runs 2 replicas → 2 cycles/hour against HF / GH /
  arXiv. Within rate limits and idempotent on the registry, so OK.
