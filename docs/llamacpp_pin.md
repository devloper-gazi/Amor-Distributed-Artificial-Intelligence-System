# llama.cpp + llama-swap build pins (Cycle F Sprint 1)

> The roadmap requires llama.cpp pinned to the **b8500-b8700** range
> because `--cache-reuse` regressed silently outside this window
> (issues #15082, #14163 fix, post-Aug-2025 patches landed by Sept 2025).
> This doc captures the exact image digests the AMOR v18 reference
> deployment is validated against.

## Current pins

| component | tag / digest | notes |
|---|---|---|
| `ghcr.io/mostlygeek/llama-swap:cuda` | uses upstream "latest cuda" alias — pin to digest when AMOR runs its first Sprint 1 A/B baseline | digest captured in `docs/sprint1_decision.md` post-run |
| llama.cpp inside the llama-swap image | commit hash discoverable via `docker exec amor-llama-swap llama-server --version` | record the commit hash alongside the image digest |

## How to capture + pin

After `compose up -d llama-swap`:

```bash
# 1. Capture llama-swap image digest
docker inspect ghcr.io/mostlygeek/llama-swap:cuda \
  --format='{{index .RepoDigests 0}}' \
  | tee docs/llamaswap_digest.txt

# 2. Capture llama.cpp commit hash from the running container
docker exec amor-llama-swap sh -c 'llama-server --version 2>&1 | head -3' \
  | tee docs/llamacpp_version.txt
```

Then edit `docker-compose.yml`:

```yaml
llama-swap:
  image: ghcr.io/mostlygeek/llama-swap@sha256:<digest>  # PIN
```

## Cache-reuse regression history

| commit range | status | notes |
|---|---|---|
| pre-#15082 (≤ Jun 2025) | regressed | prefix cache silently disabled |
| #15082 → #14163 patches | unstable | partial fixes |
| **b8500-b8700 (current pin range)** | **stable** | post-Aug-2025 patches landed |
| > b9000 (Apr 2026+) | re-evaluate | EAGLE-3 PR #18039 may land; `--cram` semantics may evolve |

## Verification (`probe_cache_reuse.py`)

`tools/llamaswap/probe_cache_reuse.py` sends the same ~1000-token
prompt twice and asserts:

* second-call wall-clock ≤ 0.2 × first-call wall-clock (±10% jitter)
* `/slots[0].n_cached_tokens > 0` on second call

This probe is the **Sprint 1 gate**: run it before every overnight
A/B baseline; a regression here aborts cheaply rather than after
six hours of Mistral-judging.

## Re-evaluation triggers

* llama.cpp ships a `--cram`-replacement flag — re-bench host-memory
  cache effectiveness.
* llama.cpp adds native LoRA adapter hot-swap with rank-aware
  scoring (Sprint 3 dependency).
* llama-swap publishes a versioned, semver-pinned tag — switch off
  digest pinning to tag pinning.

## Rollback

Revert `docker-compose.yml` to `ghcr.io/mostlygeek/llama-swap:cuda`
(unpinned).  `AMOR_LLM_BACKEND=ollama` rolls back inference entirely.
