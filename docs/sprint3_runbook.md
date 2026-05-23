# Sprint 3 v18 — LoRA hot-swap runbook

> Cycle F Sprint 3 — per-request LoRA adapter routing via
> llama.cpp PR #10994 `"lora": [{"id": int, "scale": float}, ...]`
> body field.  Coder / Tester / Debugger share a single resident
> Qwen2.5-Coder-7B base; their role-specific behaviour comes from
> rank-16 LoRA adapters that hot-swap at ~1-10 ms per request
> instead of full-model llama-swap rotation (~3.5 s).

## What landed (this sprint — runtime path, OFF by default)

| Artifact | Path | Purpose |
|---|---|---|
| Settings | `config/settings.py` | `code_lora_enabled=False`, `code_lora_role_adapters="{}"`, `code_lora_default_scale=1.0` |
| Runtime helper | `tools/lora_runtime.py` (NEW) | `parse_role_adapter_map`, `lora_payload_for_role`, `disable_all_adapters_payload` |
| Backend injection | `document_processor/api/local_ai_routes_simple.py` (line ~1335) | When backend != ollama AND `code_lora_enabled=True` AND active role mapped, adds `"lora": [...]` to `ChatOptions.extra` → flows into OpenAI-compat body via existing `body.update(dict(opts.extra))` |
| Training driver | `tools/training/orpo_role_adapter.py` (NEW) | Thin wrapper around existing `orpo_qwen_coder.py` pinning the Cycle F recipe (r=16, alpha=32, dropout=0.05, lr=8e-6, beta=0.1, 1 epoch, max_seq=2048).  Routes corpus + output paths by `--role coder|tester|debugger`. |
| Adapter ID map | `tools/training/orpo_role_adapter.py:ROLE_ADAPTER_IDS` | `coder=0, tester=1, debugger=2` — MUST match llama-swap mount order |
| llama-swap config | `compose/llama-swap/config.{yaml,q4_0.yaml,q8_0.yaml}` | Commented `--lora-init-without-apply` placeholders on the editor model; operator uncomments after training |
| Tests | `tests/code_intelligence/test_lora_runtime.py` (19), `tests/training/test_orpo_role_adapter.py` (14), `tests/api/test_lora_injection.py` (7) | 40 new tests, all green |

## Architectural decisions

* **OpenAI-compat extra passthrough.**  The backend abstraction
  already has a clean `ChatOptions.extra` escape hatch
  (`openai_compat.py:_build_body` line 143 does
  `body.update(dict(opts.extra))`).  Sprint 3 attaches `lora`
  through that hatch — no changes to `LlamaSwapBackend`,
  `OpenAICompatibleBackend`, or `LLMBackend` ABC.  This means
  rolling back the feature is one line: drop the injection block
  in `local_ai_routes_simple.py`.
* **Adapter IDs are positional.**  `--lora-init-without-apply
  <file>` flags assign IDs in mount order: first flag → id 0,
  second → id 1.  `ROLE_ADAPTER_IDS` in
  `orpo_role_adapter.py` captures this convention so settings +
  llama-swap config + training script all agree.
* **OFF by default.**  `code_lora_enabled=False` means new
  deployments behave identically to Sprint 2; LoRA is opt-in
  AFTER an operator trains adapters + uncomments the
  llama-swap mounts.
* **Ollama path bypassed.**  LoRA injection only fires when the
  active backend is non-Ollama (we read `backend.name`).  Ollama
  doesn't speak the PR #10994 `lora` field; flag-flip rollback
  (`AMOR_LLM_BACKEND=ollama`) silently disables LoRA.
* **Single adapter per request.**  We attach exactly ONE adapter
  matching the active role.  Stacking multiple adapters per
  request is supported by PR #10994 but unmotivated for AMOR
  today (each role's behaviour comes from its own adapter).

## How to turn it on (after training)

```bash
# 1. Curate preference pairs per role.  500 (prompt, chosen,
#    rejected) triples per role is the plan-file recommended floor.
#    Format: data/preference_pairs/{coder,tester,debugger}.jsonl
#    Each line: {"prompt": "...", "chosen": "...", "rejected": "..."}

# 2. Train one adapter per role (RTX 4060 8 GiB; ~30-60 min each).
python tools/training/orpo_role_adapter.py --role coder --convert-gguf
python tools/training/orpo_role_adapter.py --role tester --convert-gguf
python tools/training/orpo_role_adapter.py --role debugger --convert-gguf

# 3. The script outputs:
#      models/lora/coder-r16/        (PEFT directory)
#      models/lora/coder-r16.gguf    (GGUF for llama-server)
#    (same for tester + debugger).

# 4. Uncomment the matching --lora-init-without-apply lines in
#    compose/llama-swap/config.yaml (or .q4_0.yaml / .q8_0.yaml).
#    Order MUST be coder, tester, debugger (matches ROLE_ADAPTER_IDS).

# 5. Update .env:
echo 'AMOR_CODE_LORA_ENABLED=true' >> .env
echo 'AMOR_CODE_LORA_ROLE_ADAPTERS={"coder":0,"tester":1,"debugger":2}' >> .env

# 6. Restart.
docker compose restart llama-swap app

# 7. Verify.
python -m tools.setup verify         # 7/7 ✓ expected
# Make a Build session and inspect /api/code/diagnostics for the
# `lora_attached role=coder payload=[{id:0, scale:1.0}]` log line.
```

## A/B testing a candidate adapter against the in-production one

The plan file Sprint 6 ORPO weekly cron lands `tools/lora/promote.py`
that does this end-to-end.  Until that ships, manual A/B:

1. Train `models/lora/coder-r16-cand.gguf`
2. Add it to `compose/llama-swap/config.yaml` as the 4th `--lora-init-without-apply`
3. Restart llama-swap (NEW adapter gets id 3; existing IDs 0/1/2 unchanged)
4. Run a small eval (e.g. Sprint-0 corpus subset) twice — once with
   `code_lora_role_adapters={"coder":0}` (production) and once
   with `code_lora_role_adapters={"coder":3}` (candidate)
5. Pick the winner; remove the loser's mount line + reload llama-swap

## Sprint 3 exit criteria status

| # | criterion | status |
|---|---|---|
| 1 | Three adapters (coder/tester/debugger-r16.gguf) loaded with `--lora-init-without-apply` | gated by operator ORPO training (~30-60 min × 3 = ~2-3 h) |
| 2 | Per-request scale switching confirmed via /slots zero re-prefill on identical prefixes | gated by #1 |
| 3 | A/B vs system-prompt-only baseline: ≥3 pp role-adherence lift | gated by #1 + held-out eval set |
| 4 | CI test sweep delta: positive | **landed (+40 tests, 172/172 isolated gate green)** |

## Rollback

| change | rollback |
|---|---|
| LoRA injection | `AMOR_CODE_LORA_ENABLED=false` + restart app  |
| Specific role's adapter | Edit `AMOR_CODE_LORA_ROLE_ADAPTERS` JSON to drop that role's entry |
| Inference backend | `AMOR_LLM_BACKEND=ollama` (LoRA injection only fires for non-Ollama backends) |
| Adapter swap | `disable_all_adapters_payload([0,1,2])` flips all to scale=0.0 in one call (until next reload) |
| Trained adapter (full unwind) | Remove the `--lora-init-without-apply` line from `config.yaml` + restart llama-swap |

No DB migration, no schema change.

## Caveats

* **Adapter ID drift is the #1 failure mode.**  If the
  `--lora-init-without-apply` mount order changes (e.g. operator
  uncomments tester but not coder), settings IDs become wrong and
  the wrong adapter gets activated.  `orpo_role_adapter.py` prints
  the expected mapping at exit; check it after every config edit.
* **The training step is OPERATOR work**, not Claude work — needs
  GPU + Unsloth + TRL + 30 GB disk.  Sprint 3 lands the recipe + the
  runtime injection; the actual weights come from running the
  recipe overnight.
* **Adapter VRAM cost** is ~30 MB per rank-16 adapter (per the plan
  file).  Three adapters ≈ 90 MB total.  Fits trivially in the 8 GiB
  budget after Q4 KV decision (Sprint 1 A/B running NOW will pin
  the KV quant).
* **Per-request `lora` field is forward-compat with stacking.**  If a
  future "ensemble" mode wants multiple adapters mixed, just return
  a longer list from `lora_payload_for_role`.  No backend change.

## Wire-shape reference (PR #10994)

llama-server accepts on the OpenAI-compat `chat/completions` body:

```json
{
  "model": "amor-editor",
  "messages": [...],
  "lora": [
    {"id": 0, "scale": 1.0}
  ]
}
```

A `scale=0.0` entry effectively disables that adapter for the request.
A request that omits the `lora` field entirely runs on the base model.
The `/lora-adapters` endpoint (POST) is a SEPARATE mechanism for
changing the global default — Sprint 3 doesn't use it; we operate
purely per-request.
