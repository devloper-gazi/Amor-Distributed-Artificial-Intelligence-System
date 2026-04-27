# Model Picker — Spec ↔ Implementation Audit

> Side-by-side mapping of every Part 1-8 row + every validation
> checklist row in the original *AMOR: LLM Model Selector in "More
> Settings"* spec to its current location in the codebase.
>
> **Status: every spec item is implemented.** Rounds v3, v4, and v5 went
> substantially beyond the spec — see the *Beyond-Spec Highlights*
> section at the bottom.
>
> File paths use POSIX separators throughout. Line numbers were
> captured at commit `08b36fd` + the round-6 `X-Model-Used` patch on
> top; they may shift with future refactors but the symbols stay.

---

## Part 1 — `user_model_preferences` MongoDB collection

| Spec row | Status | Where |
|---|---|---|
| New collection `user_model_preferences` | ✓ | `document_processor/infrastructure/chat_store.py:245` (collection allocation) + indexes at `:246-253` |
| Indexes on `(user_id, mode)` and `(client_id, mode)`, both unique + sparse | ✓ | `document_processor/infrastructure/chat_store.py:246-253` |
| `PREFERENCE_MODE_ALL = "__all__"` constant | ✓ | `document_processor/infrastructure/chat_store.py:288` (on the `ChatStore` class) |
| `set_preference()` upsert | ✓ | `document_processor/infrastructure/chat_store.py:301` (`set_model_preference`) |
| `get_preference()` with exact → wildcard fallback | ✓ | `document_processor/infrastructure/chat_store.py:402` (`get_model_preference`) — wildcard handled inline |
| `delete_preference()` | ✓ | `document_processor/infrastructure/chat_store.py:432` (`delete_model_preference`) |
| `get_all_preferences()` for the settings UI | ✓ | `document_processor/infrastructure/chat_store.py:453` (`get_all_model_preferences`) |
| All writes go through `_write_with_retry` (the spec's "Absolute Constraint") | ✓ | All four methods wrap their writes — see `:294`, `:434`, `:482` |

> Note: spec mentions a `UserModelPreferenceStore` *class*; we put the
> methods on the existing `ChatStore` singleton (since every other
> per-user store lives there). Functionally identical surface.

## Part 2 — `ModelManager` service

| Spec row | Status | Where |
|---|---|---|
| New file `services/model_manager.py` | ✓ | `document_processor/services/model_manager.py:1` |
| `MODE_REQUIREMENTS` map | ✓ | `document_processor/services/model_manager.py:54` |
| `EFFORT_TIER_MAP` | ✓ | `document_processor/services/model_manager.py:63` |
| `InstalledModel` dataclass | ✓ | `document_processor/services/model_manager.py:75` |
| `class ModelManager` | ✓ | `document_processor/services/model_manager.py:92` |
| `list_installed()` with Redis cache (`_PROBE_TTL = 120`) | ✓ | `document_processor/services/model_manager.py:106` |
| `auto_select(mode, effort)` with the documented 5-component score | ✓ | `document_processor/services/model_manager.py:206` |
| `resolve_model()` — user pref → wildcard → auto-select | ✓ | `document_processor/services/model_manager.py:275` |
| `pull_model_stream()` — yields `pull_start` / `pull_progress` / `pull_complete` / `pull_error` events | ✓ | `document_processor/services/model_manager.py:301` |
| `import_gguf()` — magic-byte check + Modelfile + `ollama create` | ✓ | `document_processor/services/model_manager.py:347` (uses HTTP `/api/create` not subprocess — same effect, no CLI dep) |
| `delete_custom_model()` owner-only | ✓ | `document_processor/services/model_manager.py:1055` (sidecar `.meta.json` walk for ownership) |
| `CUSTOM_MODELS_DIR`, `MAX_UPLOAD_SIZE_BYTES`, `_PROBE_CACHE_KEY` constants | ✓ | `document_processor/services/model_manager.py:46-72` |

## Part 3 — API routes

| Spec endpoint | Status | Where |
|---|---|---|
| `GET    /api/models` | ✓ | `document_processor/api/model_routes.py:194` |
| `GET    /api/models/auto-select` | ✓ | `document_processor/api/model_routes.py:366` |
| `GET    /api/models/preference` | ✓ | `document_processor/api/model_routes.py:390` |
| `PUT    /api/models/preference` | ✓ | `document_processor/api/model_routes.py:411` |
| `DELETE /api/models/preference/{mode}` | ✓ | `document_processor/api/model_routes.py:676` |
| `POST   /api/models/pull` (SSE) | ✓ | `document_processor/api/model_routes.py:696` |
| `POST   /api/models/upload` (multipart) | ✓ | `document_processor/api/model_routes.py:745` |
| `DELETE /api/models/custom/{tag:path}` | ✓ | `document_processor/api/model_routes.py:820` |
| `Content-Length` precheck on upload | ✓ | `document_processor/api/model_routes.py` (inside `upload_gguf`) |
| Router included in `main.py` | ✓ | `document_processor/main.py:368` (search for `app.include_router(model_router)`) |
| `ModelManager` instantiated on `app.state` in lifespan | ✓ | `document_processor/main.py:215-225` (lifespan startup block) |

## Part 4 — Wire model resolution into LLM call paths

| Spec row | Status | Where |
|---|---|---|
| `call_ollama()` accepts an optional model override (backwards compatible) | ✓ | `document_processor/api/local_ai_routes_simple.py:1264` (`call_ollama` delegates to `call_ollama_with`) |
| New `call_ollama_with(model, …)` model-aware variant | ✓ | `document_processor/api/local_ai_routes_simple.py:1063` |
| Cache key includes the model | ✓ | `document_processor/api/local_ai_routes_simple.py:998` (`_llm_cache_key`) |
| `_resolve_request_model` helper at the start of every AI handler | ✓ | `document_processor/services/model_resolution.py:30` (`resolve_request_model`) + `:96` (v3 `_full` variant returning the profile too) |
| `start_research` calls the resolver + stamps the session | ✓ | `document_processor/api/local_ai_routes_simple.py:1576-1592` |
| `start_think` calls the resolver | ✓ | `document_processor/api/thinking_routes.py:335-350` |
| `start_code_session` calls the resolver | ✓ | `document_processor/api/code_intelligence_routes.py:381-406` |
| Per-task `_ACTIVE_MODEL` ContextVar (so the override propagates without changing every call signature) | ✓ | `document_processor/api/local_ai_routes_simple.py:842` |
| `set_active_model` helper | ✓ | `document_processor/api/local_ai_routes_simple.py:866` |

## Part 5 — Frontend HTML in `#researchSettingsPanel`

| Spec row | Status | Where |
|---|---|---|
| New section divider for AI Model | ✓ | `web_ui/templates/index.html` (search `panel-section-title.*AI Model` — section header) |
| Auto-select info row | ✓ | `web_ui/templates/index.html` (search `model-card-auto`) |
| Source-tab strip with **Installed**, **Pull**, **Upload** tabs | ✓ | `web_ui/templates/index.html` (search `data-tab="installed"` etc.) — plus a 4th **Discover** tab (v3) |
| Pull search input + button + progress area | ✓ | `web_ui/templates/index.html` (search `customModelTag` and `modelPullProgress`) |
| Upload drop-zone + display-name field + progress area + submit + success/error | ✓ | `web_ui/templates/index.html` (search `modelDropZone` / `modelUploadForm`) |
| Scope selector (`__all__` / research / thinking / coding) | ✓ | `web_ui/templates/index.html` (search `model-scope-toggle`) — implemented as a button group rather than a `<select>` for keyboard accessibility, same semantics |

## Part 6 — Frontend JavaScript

| Spec row | Status | Where |
|---|---|---|
| `initModelSelector()` method on `ChatController` | ✓ | `web_ui/static/js/chat-research.js` (search `initModelSelector`) |
| Auth headers helper for fetch + XHR | ✓ | `web_ui/static/js/chat-research.js` (search `_authHeaders`) |
| `loadInstalledModels()` populates the model list with cards | ✓ | `web_ui/static/js/chat-research.js` (search `_renderInstalledTab` + `_makeModelCard`) |
| Tab switching | ✓ | `web_ui/static/js/chat-research.js` (search `_wireModelTabs`) |
| Toggle show/hide subpanel | ✓ | The picker is always visible inside the panel; the v3 strategy toggle (`_wireModelStrategy`) replaces the original toggle |
| Pull SSE handler | ✓ | `web_ui/static/js/chat-research.js` (search `_pullModel`) |
| Upload via XMLHttpRequest with progress | ✓ | `web_ui/static/js/chat-research.js` (search `_submitUpload`) — uses XHR for upload-progress events |
| Apply preference (PUT /preference) | ✓ | `web_ui/static/js/chat-research.js` (search `_savePreference`) |
| Settings summary updates with model name | ✓ | `web_ui/static/js/chat-research.js` (search `_renderModelChip` + `_renderPanelFooter`) |

## Part 7 — CSS

| Spec row | Status | Where |
|---|---|---|
| Section divider | ✓ | `web_ui/static/css/chat-research.css` (search `.panel-section-title`) |
| Auto-model row | ✓ | `web_ui/static/css/chat-research.css` (search `.model-card-auto`) |
| Tab strip | ✓ | `web_ui/static/css/chat-research.css` (search `.model-tabs`) |
| Model card grid | ✓ | `web_ui/static/css/chat-research.css` (search `.model-card-list`) |
| Pull progress + custom-tag input | ✓ | `web_ui/static/css/chat-research.css` (search `.panel-pull-progress`) |
| Drop-zone | ✓ | `web_ui/static/css/chat-research.css` (search `.model-drop-zone`) |
| Apply button + status | ✓ | `web_ui/static/css/chat-research.css` (search `.panel-action-btn`) |

## Part 8 — Settings & Docker

| Spec row | Status | Where |
|---|---|---|
| `CUSTOM_MODELS_DIR` env / setting | ✓ | `docker-compose.yml` (search `CUSTOM_MODELS_DIR`) + `.env.example` |
| `MAX_MODEL_UPLOAD_SIZE_GB` env | ✓ | `docker-compose.yml` + `.env.example` (`MAX_MODEL_UPLOAD_SIZE_GB`) |
| Bind/named-volume mount for custom models | ✓ | `docker-compose.yml` `custom-models-data` volume mounted on app + ollama at `/data/custom_models` |

---

## Validation Checklist (12 items)

| # | Spec criterion | Status | How verified |
|---|---|---|---|
| 1 | API response header `X-Model-Used: <tag>` on AI endpoints | ✓ | `document_processor/api/local_ai_routes_simple.py:1597`, `document_processor/api/thinking_routes.py:354`, `document_processor/api/code_intelligence_routes.py:413` (added Round 6). Tests: `tests/api/test_x_model_used_header.py` (5 tests pass) |
| 2 | "Auto-select" row shows current model + reason on settings open | ✓ | `_renderInstalledTab` + `_renderPanelFooter` in `chat-research.js`; the Auto card shows `auto.tag — auto.reason` from `/api/models` |
| 3 | Toggle on → list loads → select → Apply → next request uses that model | ✓ | `_savePreference` in `chat-research.js` PUT s `/api/models/preference`; the next request's `resolve_request_model_full` reads it from Mongo |
| 4 | Pull progress bar + auto-select on completion | ✓ | `_pullModel` in `chat-research.js` parses SSE; on `pull_complete` it auto-saves the just-pulled tag as the active preference |
| 5 | 4 GB GGUF upload with drag/drop + progress | ✓ | `_wireModelUpload` in `chat-research.js` uses `XMLHttpRequest.upload.onprogress`; backend at `model_routes.py:745` |
| 6 | Delete custom model with confirmation | ✓ | `_deleteCustomModel` in `chat-research.js`; backend at `model_routes.py:820` (owner-scoped via sidecar walk) |
| 7 | "All modes" preference applied to Thinking session | ✓ | `resolve_request_model` in `services/model_resolution.py` falls back to `__all__` wildcard when no exact-mode pref exists |
| 8 | Per-mode preference doesn't leak across modes | ✓ | `chat_store.get_model_preference(mode=...)` filters by mode; the wildcard is queried only when the exact-mode pref is absent |
| 9 | Toggle off → preference cleared → back to auto | ✓ | DELETE `/preference/{mode}` route; the Auto card click in `_clearPreference` (`chat-research.js`) calls it |
| 10 | Non-GGUF file rejected with clear error | ✓ | `model_manager.py:357` (`if file_bytes[:4] != b"GGUF"`) raises `ValueError` → 400 in the route |
| 11 | Oversized file rejected before body read with 413 | ✓ | `model_routes.py` upload_gguf (search `Content-Length`) — checks header before `await file.read()` |
| 12 | Settings summary footer includes model name when custom | ✓ | `_renderPanelFooter` in `chat-research.js` renders `Currently: <strong>{tag}</strong> · {scope}` |

---

## Beyond-Spec Highlights

These features are not in the original spec but ship in the current
implementation:

### v3 — Hardware + advanced parameters (separate commits `880da69`, `5d74a6a`, `b508407`, `e924498`)

- **Hardware panel** at the top of the picker — auto-detects GPU
  (pynvml + `CUDA_VISIBLE_DEVICES`) and Ollama version.
  - `document_processor/services/model_manager.py:577` (`detect_hardware`)
  - 4-way preference toggle: Auto / GPU full / GPU partial / CPU only
  - `num_gpu` slider for partial mode
- **Discover tab** — search Hugging Face Hub + curated catalogue.
  - `document_processor/services/model_manager.py:461` (`search_models`)
  - `document_processor/api/model_routes.py:527` (route)
- **Strategy toggle** with 4 modes — Single / Per-mode / Per-role / Ensemble
  - Persisted via new `user_model_routing` collection (`chat_store.py:268`)
- **Per-model profiles** — temperature / top_p / top_k / repeat_penalty /
  num_ctx / num_gpu / num_thread / seed / system_prompt
  - Whitelist in `model_manager.py:apply_profile_to_options`
  - Layered into Ollama `options` at request time via `_ACTIVE_PROFILE`
    ContextVar (`local_ai_routes_simple.py:850`)
- **Test endpoint** — `/api/models/test` streams a tiny generation per
  card's "Try it" button. `model_routes.py:576`

### v4 — Engine consumption + intelligence (separate commits `90b9d55`, `43655c0`, `1177aef`, `08b36fd`)

- **Per-role runtime** — `_ACTIVE_ROLE` ContextVar set at each
  CodeIntelligenceEngine phase boundary so each agent (planner/coder/
  critic/reviewer) can use a different model.
  - `local_ai_routes_simple.py:892` (`set_active_role`)
  - `code_intelligence/engine.py` (search `self._role_setter("planner")` etc.)
- **Ensemble dispatch** — parallel inference across N members + voting
  (`first` race / `weighted` longest / `majority` Jaccard consensus)
  - `local_ai_routes_simple.py:1145` (`_call_ollama_ensemble`)
- **Fallback chain** — primary → fallback list, transparent on HTTP 404/503
  - `local_ai_routes_simple.py:1100` (chain walk in `call_ollama_with`)
- **VRAM-fit awareness** — every model card gets a `fits / tight / too_big / cpu / unknown` badge against the detected GPU
  - `model_manager.py:821` (`estimate_vram_gb`) + `:855` (`fit_classification`)
- **Profile presets** — Creative / Precise / Coding / Long-form / Deterministic
  - `web_ui/static/js/chat-research.js` (search `_modelPresets`)
- **Recommendation engine** — heuristic prompt → model suggestion with
  human-readable reason
  - `model_manager.py:700` (`recommend_for_prompt`)
  - `/api/models/recommend` route — `model_routes.py:490`
  - Debounced banner in the picker
- **Live tok/s telemetry** — emitted every 8 chunks during `test_generate`
  - `model_manager.py:test_generate` — adds `tokens_per_second` per chunk
- **Quantization picker** — HF Discover cards expose Q3_K_M / Q4_K_M / Q5_K_M / Q6_K / Q8_0 / F16 dropdown
  - `web_ui/static/js/chat-research.js` (search `discover-quant-picker`)
- **Usage analytics** — per-(user, tag, mode) counter + cards show "used N×"
  - `chat_store.py:268` (collection) + `:628` (`increment_model_usage`)
- **Warmup-on-save** — Save Profile fires `/api/models/warmup` to pre-load into VRAM
  - `model_manager.py:881` (`warmup_model`) + route at `model_routes.py:338`

### v5 — Consortium meta-pipeline + CLI (separate commits `a87ed2d`, `2e15c93`, `cc3e060`, `d0080ea`)

- **Consortium Mode** — chains Code Intelligence (scope) → Research →
  Thinking → Code Intelligence (build) into a single end-to-end pipeline
  - `document_processor/consortium/orchestrator.py:87`
  - 8 endpoints under `/api/consortium/*` (`document_processor/api/consortium_routes.py`)
- **Quality gates between phases** — research citation density, thinking
  decision groundedness, implementation static-analysis severity
  - Methods `_gate_research`, `_gate_thinking`, `_gate_implementation` on the orchestrator
- **Bundle artifact** — README.md + scope.json + per-phase folders + verifications.json + bundle.json
  - Streamed as a zip from `/api/consortium/{sid}/artifact`
- **CLI** — `python -m document_processor.cli consortium "<goal>" --depth deep`
  - In-process *or* `--remote URL` against a running server
  - `document_processor/cli/__main__.py`

---

## Test coverage

| File | Tests | Status |
|---|---|---|
| `tests/services/test_model_manager.py` (v2) | 9 | Pass |
| `tests/services/test_model_resolution.py` (v2) | 4 | Pass |
| `tests/api/test_model_routes.py` (v2) | 15 | Pass |
| `tests/services/test_model_manager_v3.py` (v3) | 16 | Pass |
| `tests/api/test_model_routes_v3.py` (v3) | 11 | Pass |
| `tests/services/test_model_manager_v4.py` (v4) | 12 | Pass |
| `tests/api/test_model_routes_v4.py` (v4) | 10 | Pass |
| `tests/code_intelligence/test_preferred_model_passthrough.py` (v1) | 12 | Pass |
| `tests/consortium/test_orchestrator.py` (v5) | 16 | Pass |
| `tests/api/test_consortium_routes.py` (v5) | 9 | Pass |
| `tests/api/test_x_model_used_header.py` (Round 6) | 5 | Pass |
| **Total** | **119** | **All pass** |

Run with `python -m pytest tests/services tests/api tests/consortium tests/code_intelligence/test_preferred_model_passthrough.py`.
