# Extending Code Intelligence Mode

Four recipes — Charter §6 Mandate 5 — for the most common extensions
a future engineer will want to make. Each recipe is a complete,
followable procedure that does not require re-reading the engine.

If your extension doesn't fit one of these recipes, add a new one
here in the same format and reference it from `CHANGELOG.md`.

---

## Recipe 1 — Add a new agent role

**Use case:** You want a sixth specialist (e.g., `SecurityAuditor`,
`PerformanceProfiler`) that runs alongside Planner / Coder / Tester /
Debugger / Critic.

**Steps:**

1. Create `document_processor/code_intelligence/agents/<role>.py`
   *(or just add a class to the existing `agents.py` if it's a small
   variant)*. Subclass `_BaseAgent` and define:
   ```python
   class SecurityAuditor(_BaseAgent):
       role = "security_auditor"
       system_prompt = SECURITY_AUDITOR_SYSTEM_PROMPT  # in prompts.py

       async def run(self, ctx: AgentContext) -> AgentOutput:
           prompt = security_auditor_prompt(ctx.code, ctx.plan)
           raw = await self._call(prompt)
           data = _extract_json(raw)
           return AgentOutput(raw=raw, data=data)
   ```

2. Add the system prompt + phase prompt builder to `prompts.py`,
   following the `CRITIC_SYSTEM_PROMPT` and `critic_prompt` patterns.

3. Register the new role in `registries.py:register_defaults()` so
   the singleton `agent_registry` picks it up at startup:
   ```python
   from .agents import SecurityAuditor
   agent_registry.register_role("security_auditor", SecurityAuditor)
   ```

4. Wire it into the engine:
   - Add a phase tuple to `CODE_PHASES` (engine.py) at the right
     position, e.g. `("audit", "Auditing security")` after `review`.
   - Add the phase to `PHASE_PROGRESS` with a percentage that doesn't
     collide with the others.
   - Add a `_phase_audit` async method on `CodeIntelligenceEngine`
     that constructs `SecurityAuditor(self.llm_call, max_tokens=...)`
     and calls `agent.run(AgentContext(...))`.
   - Call `await self._run_phase("audit", self._phase_audit)` in
     `run()` at the appropriate point.

5. Add a unit test under `tests/code_intelligence/`:
   ```python
   @pytest.mark.asyncio
   async def test_security_auditor_runs():
       async def mock_llm(prompt, system, max_tokens):
           return '{"verdict":"approved","issues":[]}'
       agent = SecurityAuditor(mock_llm, max_tokens=500)
       out = await agent.run(AgentContext(
           user_prompt="audit this", code="print('hi')", plan={},
       ))
       assert out.error is None
   ```

6. Update `EXTENDING.md` with a one-line note listing the new role
   in this recipe's "Currently registered roles" section below.

7. Update `ARCHITECTURE.md` Layer 4 (Agents) to mention the new role.

**Currently registered roles:** `planner`, `coder`, `tester`,
`debugger`, `critic`. (Singleton at
`document_processor.code_intelligence.registries.agent_registry`.)

---

## Recipe 2 — Add a new sandbox tier

**Use case:** You want stronger isolation than Docker for a particular
class of code (e.g., Firecracker microVMs for code that needs to test
syscalls, or a remote-run tier that ships code to a hardened
ephemeral cluster).

**Steps:**

1. Implement the sandbox class in
   `document_processor/code_intelligence/sandboxes/<name>.py`. It must
   expose the same surface as `ExecutionSandbox`:
   ```python
   class FirecrackerSandbox:
       def __init__(self, default_timeout=30, memory_limit="256m", ...):
           ...
       async def docker_available(self) -> bool: ...   # rename if needed
       async def execute(self, code, language="python", ...) -> ExecutionResult: ...
       async def image_status(self) -> dict[str, bool]: ...
   ```
   Reuse `ExecutionResult` from `sandbox.py` so callers don't need
   to special-case the return type.

2. Register the tier in `registries.py:register_default_tiers()`:
   ```python
   sandbox_tier_registry.register_tier(
       SandboxTier(
           tier=2,
           name="firecracker",
           description="microVM via Firecracker",
           isolation="microvm",
           network="none",
           factory=FirecrackerSandbox,
           available=True,         # flip from the placeholder default
       ),
       replace=True,               # overrides the v1 placeholder
   )
   ```

3. Decide selection policy: do you want the engine to pick the tier
   automatically based on language / complexity, or stay caller-
   selectable? For automatic selection, add a `tier_selection`
   strategy in `engine.py` that consults
   `sandbox_tier_registry.by_tier_number(...)`.

4. Add an integration test marked `@pytest.mark.integration` so it
   only runs when the Firecracker daemon is reachable:
   ```python
   @pytest.mark.integration
   @pytest.mark.skipif(not _firecracker_available(), reason="...")
   async def test_firecracker_executes_python():
       ...
   ```

5. Document in `RUNBOOK.md` how to install / start the new tier on
   a host.

6. Update `ARCHITECTURE.md` Layer 5 (Capabilities) with the new
   tier.

**Currently registered tiers:** Tier 1 = `docker` (available);
Tier 2 = `firecracker` (placeholder); Tier 3 = `gvisor`
(placeholder).

---

## Recipe 3 — Add a new model provider

**Use case:** You want to support an inference backend other than
Ollama — e.g., vLLM, TGI, llama.cpp's server, or a self-hosted
HuggingFace TGI.

**Steps:**

1. Implement a function with the same signature as
   `local_ai_routes_simple.call_ollama`:
   ```python
   async def call_vllm(
       prompt: str,
       system: str | None = None,
       max_tokens: int = 2048,
   ) -> str: ...
   ```
   Wrap in `@retry` + `CircuitBreaker` from `reliability/`.

2. In `code_intelligence_routes.py`, replace the existing
   `_llm_call_local` import with a selection function:
   ```python
   def _select_llm_call(provider: str) -> Callable:
       if provider == "vllm":
           from ..local_ai.vllm_client import call_vllm
           return call_vllm
       from .local_ai_routes_simple import call_ollama
       return call_ollama
   ```
   Then `engine = CodeIntelligenceEngine(llm_call=_select_llm_call(payload.provider), ...)`.

3. Extend the `provider` field on `CodeStartRequest` (Pydantic) to
   include the new provider in its `Literal`. Update the route's
   503 handler so an unconfigured provider returns a clear error
   instead of crashing.

4. Add or extend `CodeModelRegistry` if the provider has its own
   model catalogue. The current registry is Ollama-specific —
   either add a `Provider` enum and per-provider catalogue, or
   create `VllmModelRegistry`. The pattern is whichever has less
   code in the routes layer.

5. Update `Settings` with `code_<provider>_base_url`, etc.

6. Add a unit test against the new provider via `respx` (or
   `pytest-httpx`).

7. Update `ARCHITECTURE.md` and `CHANGELOG.md`.

**Currently supported providers:** Ollama (the only one). The
engine is provider-agnostic at construction (`llm_call` injection)
so adding a second provider is a routes-level change, not an engine
change.

---

## Recipe 4 — Add a new capability discovery source

**Use case:** You want the autonomous discoverer to also harvest
candidates from GitLab, Gitee, internal model registries, or a
private package server.

**Steps:**

1. Implement an async source function in
   `document_processor/code_intelligence/capability_discoverer.py`
   (or a new module under `discovery_sources/`):
   ```python
   async def _discover_gitlab(
       queries: Sequence[str], limit: int = 5,
   ) -> list[CapabilityCandidate]:
       try:
           import gitlab  # or `python-gitlab`
       except ImportError:
           logger.info("capability_discoverer_gitlab_unavailable")
           return []
       try:
           gl = gitlab.Gitlab(...)
           ...
           return [CapabilityCandidate(...) for proj in results]
       except Exception as exc:
           logger.warning("capability_discoverer_gitlab_failed: %s", exc)
           return []
   ```
   The return shape is **always** `list[CapabilityCandidate]`. Be
   failure-quiet: missing SDK or auth → return `[]`.

2. Register the source in
   `registries.py:register_default_sources()`:
   ```python
   capability_source_registry.register("gitlab", _discover_gitlab)
   ```

3. Update `CapabilityDiscoverer.run_once()` to iterate the registry
   instead of hard-coding three sources. *(Optional — the v1 method
   hard-codes HF + GH + arXiv; refactor to
   `for src in capability_source_registry.items().values(): await
   src(...)` if you're touching that method anyway.)*

4. Make sure the new source declares its license-detection logic so
   the SPDX gate can run. Source should always set
   `spdx_license` on returned candidates; if the upstream API
   doesn't expose it, leave it empty and the license gate will
   reject the candidate (correct behaviour — we don't auto-register
   under unknown licenses).

5. Add a unit test that mocks the SDK and asserts a
   `CapabilityCandidate` round-trip.

6. Update `requirements.txt` if a new SDK is needed; verify the SDK's
   own license against `LICENSE_NOTES.md`.

7. Update `CAPABILITIES.md` "What's NOT discovered" section if your
   new source intentionally excludes anything.

**Currently registered sources:** `huggingface`, `github`, `arxiv`.
(Singleton at
`document_processor.code_intelligence.registries.capability_source_registry`.)

---

## When you're stuck

- Check `PATTERNS.md` for the canonical session lifecycle, error
  handling, and frontend view patterns.
- Check `INVARIANTS.md` for the hard rules that don't bend (zero
  paid APIs, sandbox containment, schema-versioning, etc.).
- Check `INTEGRATION_MAP.md` for which layer your extension belongs
  in.
- Check `adr/` for prior decisions on similar extensions.
- If your extension would require modifying `engine.py` interior
  logic (not just registering a new variant), it probably needs an
  ADR first — write it, link from `CHANGELOG.md`, and ask.
