# Invariants — Code Intelligence Mode

Hard, non-negotiable constraints. A violation is a build failure.

## I1 — Zero paid AI APIs

No code path under `document_processor/code_intelligence/**` or
`web_ui/static/js/code-view.js` may import or call:
- `anthropic`, `anthropic_client`, `api.anthropic.com`
- `openai`, `api.openai.com`
- `cohere`, `api.cohere.com`
- `voyageai`, `api.voyageai.com`
- Any other paid AI vendor SDK or HTTP endpoint

The `chat_research_routes.py` legacy Claude path is excluded and may
keep its `anthropic_client` import. The new code MUST NOT touch it.

## I2 — Permissive licensing

Every Python dependency added by Code Intelligence Mode must carry one
of: Apache-2.0, MIT, BSD-2-Clause, BSD-3-Clause, MPL-2.0, ISC,
PostgreSQL. AGPL is permitted only when invoked over a network boundary
AND flagged in `LICENSE_NOTES.md`.

## I3 — Sandbox containment

Every LLM-generated code execution runs in a Docker container with:
- `--network none`
- `--read-only`
- `--security-opt no-new-privileges`
- `--memory 256m --memory-swap 256m`
- `--cpu-quota 50000` (50% of one core)
- Hard timeout enforced by `asyncio.wait_for`
- `--rm` + defensive `docker rm -f` in a `finally` block
- The host Docker socket is mounted **read-only** into the app
  container only — never propagated into agent-spawned containers.

## I4 — Existing-codebase invariants

- All session IDs are `str(uuid4())`.
- All timestamps are `datetime.now(timezone.utc).isoformat()`.
- All async Mongo writes pass through `_write_with_retry()`.
- Every SSE event has `event_id: uuid4().hex`.
- Every endpoint declares `user: User = Depends(get_current_user)` and
  enforces ownership with HTTP 404 on mismatch (never 403).
- `_normalize_mode()` accepts `"code"` (already added in v1).
- Redis key prefix for code sessions is `"code_session:"`. Never
  `"thinking_session:"`.

## I5 — Reproducibility

Every install instruction must be expressible in `requirements.txt`,
`docker-compose.yml`, or `.env.example`. No hand-rolled `pip install`
outside scripts, no manual setup steps that aren't captured in a
committed file.

## I6 — Truthfulness

When uncertain about a convention, query the existing codebase or ask.
Never fabricate. Distinguish verified facts from assumptions in the
code comments. When this prompt's spec disagrees with a discovered
convention, document the deviation in `PROMPT_DEVIATIONS.md` and pick
the most defensible interpretation.

## I7 — Engine LLM-agnosticism

The `CodeIntelligenceEngine` module must not import `call_ollama` or
any other LLM bridge directly. The bridge is injected at construction
via the `llm_call` parameter. Routes wire it up:
```python
engine = CodeIntelligenceEngine(..., llm_call=_llm_call_local)
```
This mirrors `ThinkingEngine` exactly.

## I8 — Adversarial Reviewer is mandatory

Every SSE event (in both directions) MUST flow through the
`AdversarialReviewer` before being persisted/published. The reviewer
may halt the engine via the existing cancellation flag. Skipping the
filter — even for performance — breaks the security guarantee.

## I9 — Capability registration is gate-locked

A new capability discovered by the `CapabilityDiscoverer` must pass
ALL six gates (license → metadata → sandbox install → smoke test →
benchmark → registration) before being added to the registry. A skip
or partial pass is a build failure. Failed candidates are logged but
not registered.

## I10 — Test coverage gate

`pytest --cov=document_processor.code_intelligence
--cov-fail-under=85` must pass before the v2 PR is marked ready.
Frontend tests are snapshot-only (`code-view.js` visual diff via
manual smoke test).
