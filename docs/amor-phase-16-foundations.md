# Amor — Phase 16: Adapter Foundations

Phase 16 is the *foundation slice* of the Amor architecture brief
(an 18-module local-only AI desktop targeting RTX 4060 8 GB).  It
ships the primitives every later module needs — pluggable LLM
backend, OpenAI-compatible facade, RAG upgrade, MCP tool registry,
Letta-style memory hierarchy — without rewriting the existing
Sentinel / Consortium / Code Intelligence stack.

Backwards compatibility is the headline constraint.  Every commit
is purely additive except Commit B (the LLM call-site migration),
and even Commit B preserves byte-equivalent behaviour for the
default `llm_backend = "ollama"` setting.

License: MIT.

---

## Commit map

| Commit | Subject | Surface |
|--------|---------|---------|
| A | Pluggable LLM backend ABC + OllamaBackend + StubBackend | `local_ai/llm_backend/` |
| B | Migrate inference call sites + 3 new backends | `local_ai_routes_simple.py`, `rag_engine.py`, +`llama_swap`, `llama_cpp`, `openai_compat` |
| C | OpenAI-compatible `/v1` facade | `document_processor/api/openai_compat_routes.py` |
| D1 | RAG hybrid (BM25+RRF) + cross-encoder reranker | `local_ai/vector_store/lancedb_store.py` |
| D2 | BGE-M3 embedder + late-chunking + per-model tables | `local_ai/vector_store/late_chunking.py` |
| E | MCP-style typed tool registry + Sentinel adapter + `/mcp/v1` | `local_ai/tools/`, `mcp_routes.py` |
| F | Letta-style 3-tier memory + ledger audit + agent DI | `local_ai/memory/` |
| G | Docs + AGENTS.md + final sweep + push | this file |

Total: ~1196 tests passing post-sweep.

---

## Subsystem 1 — Pluggable LLM backend (`local_ai/llm_backend/`)

A strategy pattern for inference.  Today only Ollama is wired into
the running stack, but the contract is fixed for every backend the
brief recommends:

| Backend | Wire shape | Default URL | Use case |
|---------|------------|-------------|----------|
| `OllamaBackend`         | Ollama `/api/generate` + `/api/chat` | `localhost:11434` | Default, today's path |
| `LlamaSwapBackend`      | OpenAI `/v1/chat/completions`        | `localhost:11435` | Multi-model hot-swap proxy |
| `LlamaCppBackend`       | OpenAI `/v1/chat/completions`        | `localhost:8080`  | Direct `llama-server` |
| `OpenAICompatibleBackend` | OpenAI `/v1/chat/completions`      | (caller-provided) | vLLM / ExLlamaV2 / LM Studio |
| `StubBackend`           | n/a (deterministic, no I/O)          | n/a               | Tests |

### Public API

```python
from local_ai.llm_backend import (
    get_backend, make_backend,
    ChatMessage, ChatOptions, ChatResponse,
)

backend = get_backend()  # honours settings.llm_backend / $AMOR_LLM_BACKEND
resp = await backend.chat(
    [{"role": "user", "content": "hi"}],
    model="qwen2.5:7b",
    options=ChatOptions(temperature=0.2, max_tokens=64),
)
print(resp.content)
```

### Resolver order

1. Explicit `kind=` argument
2. `settings.llm_backend` (`document_processor/config/settings.py`)
3. `$AMOR_LLM_BACKEND`
4. `"ollama"` (last-resort default)

### Test seam

`_set_backend(kind, backend)` injects a `StubBackend` into the
singleton cache for tests; `_reset_backend_cache()` wipes it
between tests.

---

## Subsystem 2 — OpenAI-compatible `/v1` facade (Commit C)

Mounts a thin OpenAI-shape facade on the existing FastAPI app so
external SDKs plug in with a single env-var flip:

```bash
OPENAI_BASE_URL=http://localhost:8000/v1
OPENAI_API_KEY=any-non-empty-string
```

| Method | Path | Backed by |
|--------|------|-----------|
| GET    | `/v1/models`                      | `LLMBackend.list_models()` |
| POST   | `/v1/chat/completions`            | `LLMBackend.chat()` / `stream_chat()` |
| POST   | `/v1/completions` (legacy)        | `LLMBackend.chat()` (single user turn) |
| POST   | `/v1/embeddings`                  | Ollama-only in Phase 16; BGE-M3 unified embedder is Phase 17 |

Master gate: `settings.openai_compat_enabled: bool = True`.

### Live smoke (verified during Commit C)

```bash
curl http://localhost:8000/v1/models
# {"object":"list","data":[{"id":"qwen2.5-coder:7b","object":"model","owned_by":"ollama"},{"id":"qwen2.5:7b","...

curl -X POST http://localhost:8000/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"qwen2.5:7b","messages":[{"role":"user","content":"Say hi in 5 words."}],"max_tokens":32}'
# {"id":"chatcmpl-...","object":"chat.completion","model":"qwen2.5:7b","choices":[{"index":0,"message":{"role":"assistant","content":"Hello, nice to meet."},"finish_reason":"stop"}],"usage":...}
```

---

## Subsystem 3 — RAG upgrade (Commits D1 + D2)

### D1 — hybrid + reranker

`LanceDBVectorStore.hybrid_search()` was a keyword-overlap kludge.
Phase 16 replaces it with the production `BM25` from
`document_processor/rag/rag_engine.py` plus reciprocal-rank fusion
over the dense + sparse rankings.  The optional cross-encoder
reranker (`document_processor/rag/reranker.py`) runs as a final
pass.

```python
results = await store.hybrid_search(
    "what colour is the sky?",
    limit=5,
    rrf_k=60,                # default; honours settings.rag_rrf_k
    rerank=None,             # honours settings.rag_reranker_enabled
)
# results[i] carries vector_score / bm25_score / vector_rank / bm25_rank
# alongside the fused ``score``.
```

| Setting | Default | Role |
|---------|---------|------|
| `rag_hybrid_search_enabled` | True | master gate; falls back to dense-only when off |
| `rag_rrf_k`                 | 60   | RRF constant (Cormack et al. canonical) |
| `rag_bm25_k1`               | 1.5  | BM25 term-saturation |
| `rag_bm25_b`                | 0.75 | BM25 length-norm |
| `rag_reranker_enabled`      | False| opt-in cross-encoder pass (~150 ms/query CPU) |
| `rag_reranker_top_k`        | 20   | candidate width fed to the reranker |

### D2 — BGE-M3 + late-chunking

`LanceDBVectorStore` is now dimension-aware.  Pass any
sentence-transformers checkpoint and the constructor:

* sniffs the model's `get_sentence_embedding_dimension()`;
* derives a per-model table name (`documents_<slug>_<dim>`) so
  writes never collide with the historical 768-dim schema;
* leaves the `documents` table on `nomic-embed-text-v1.5` untouched
  for backwards compat.

```python
store = LanceDBVectorStore(
    embedding_model="BAAI/bge-m3",          # 1024-dim, multilingual
    table_name="documents",                 # auto-suffixed
)
# → store.table_name == "documents_bge_m3_1024"
```

`LateChunker` (in `local_ai/vector_store/late_chunking.py`)
implements pragmatic late-chunking — char-window chunks with a
leading-window context payload prepended.  The vector store
embeds `chunk.contextual_payload` instead of the bare chunk
text, so short chunks carry document-level semantics.

| Setting | Default | Role |
|---------|---------|------|
| `rag_embedding_model`     | `nomic-ai/nomic-embed-text-v1.5` | default embedder |
| `rag_per_model_table`     | True | auto-suffix non-default models |
| `rag_chunking_strategy`   | `"naive"` | `naive` \| `late` |
| `rag_late_chunking_window`| 8192 | leading-window cap |

---

## Subsystem 4 — MCP tool registry (Commit E)

A typed `Tool` ABC plus `ToolRegistry` that emits the catalog in
both OpenAI `tools=[…]` shape and MCP `tools/list` shape.

### Public API

```python
from local_ai.tools import (
    Tool, MCPToolResult, ToolError, ToolRegistry,
    DEFAULT_REGISTRY, register,
)
from pydantic import BaseModel, Field

class _EchoInput(BaseModel):
    text: str = Field(..., min_length=1)

class EchoTool(Tool):
    name = "echo"
    description = "Echo the supplied text."
    InputModel = _EchoInput

    def execute(self, args: _EchoInput) -> MCPToolResult:
        return MCPToolResult(name=self.name, ok=True, output=args.text)

register(EchoTool())
result = await DEFAULT_REGISTRY.dispatch("echo", {"text": "hi"})
```

### Sentinel adapter

`local_ai/tools/sentinel_adapter.py` wraps Sentinel's six tools —
`read_file`, `search_codebase`, `compile_check`, `taint_trace`,
`cve_lookup`, `exploit_sandbox` — without modifying the original
callables (Sentinel's own engine continues to work unchanged).

### `/mcp/v1/*` server

| Method | Path | Body | Response |
|--------|------|------|----------|
| GET    | `/mcp/v1/tools/list`     | —                                  | `{"tools": [...]}` |
| POST   | `/mcp/v1/tools/call`     | `{"name": "...", "arguments": {...}}` | `{"content": [...], "isError": ..., "metadata": {...}}` |
| GET    | `/mcp/v1/openai-tools`   | —                                  | `{"tools": [{"type": "function", ...}]}` |

Master gate: `settings.enable_mcp_server: bool = False`.

---

## Subsystem 5 — Letta-style 3-tier memory (Commit F)

A persistent memory hierarchy modelled on Letta / MemGPT.  Each
tier is a separate SQLite database under `settings.memory_root`.

| Tier | Backend | Capacity | Use case |
|------|---------|----------|----------|
| Core | `CoreMemoryBackend` | ~2 KB single-row JSON | Always-in-context (persona, current task) |
| Recall | `RecallMemoryBackend` | last N=50 turns ring buffer | Recent-window scratchpad |
| Archival | `ArchivalMemoryBackend` | long-term (no cap) | Vector / substring search |

### Public API

```python
from local_ai.memory import MemoryStore

store = MemoryStore(
    root="data/amor_memory",
    scope="auditor",
    embedder=None,  # optional — vector ranking when set
    ledger_hook=lambda kind, p: ledger.append("user:auditor", kind, p),
)

await store.write_core({"persona": "Sentinel"})
await store.append_recall("user", "audit this snippet")
await store.archive("Past zero-day report from 2026-Q1")
hits = await store.search_archival("zero-day")
```

### Memory ops as Tools

Seven Tool ABC subclasses in `local_ai/memory/tools.py` —
`CoreReadTool`, `CoreWriteTool`, `CorePatchTool`,
`RecallAppendTool`, `RecallSearchTool`, `ArchiveTool`,
`ArchivalSearchTool` — letting an agent driven via OpenAI
`tools=[…]` or MCP `tools/call` manage its own memory the
Letta way.

### Sentinel agent DI

`document_processor/sentinel/agents.py:_BaseAgent` gained an
optional `memory: Any | None = None` field plus a lazy
`_default_memory()` fallback.  Existing agents keep working
unchanged; opt in by passing `memory=MemoryStore(...)` to the
constructor.

| Setting | Default | Role |
|---------|---------|------|
| `memory_root`                | `data/amor_memory` | filesystem root |
| `memory_recall_window`       | 50                 | ring-buffer cap |
| `memory_core_max_bytes`      | 2048               | core-tier byte cap |
| `memory_archival_table`      | `amor_archival`    | reserved for vector-indexed upgrade |
| `memory_ledger_audit_enabled`| True               | append `memory_*` ledger entries on every write |

---

## Backwards compatibility matrix

| Default behaviour | Phase 16 default | Effect |
|-------------------|------------------|--------|
| `llm_backend`           | `ollama`            | Ollama hot path unchanged |
| `openai_compat_enabled` | True                | `/v1/*` mounted; doesn't affect existing routes |
| `rag_hybrid_search_enabled` | True            | replaces keyword-overlap kludge with BM25+RRF |
| `rag_reranker_enabled`  | False               | reranker only runs when explicitly asked |
| `rag_embedding_model`   | `nomic-ai/nomic-embed-text-v1.5` | existing `documents` corpus untouched |
| `rag_chunking_strategy` | `naive`             | late-chunking is opt-in |
| `enable_mcp_server`     | False               | `/mcp/v1/*` returns 503 until opted-in |
| `memory_*`              | (lazy)              | agents construct memory only on first read/write |

Hard rollback: revert the seven Phase 16 commits.  Sentinel V1 +
Phase 15 Evolution Engine continue to work unchanged.

---

## Test surface

| File | Tests |
|------|-------|
| `tests/local_ai/test_llm_backend.py`              | 25 |
| `tests/local_ai/test_llm_backend_openai.py`       | 16 |
| `tests/local_ai/test_llm_backend_migration.py`    | 4  |
| `tests/local_ai/test_lancedb_hybrid.py`           | 11 |
| `tests/local_ai/test_late_chunking.py`            | 14 |
| `tests/local_ai/test_tools_registry.py`           | 21 |
| `tests/local_ai/test_memory_store.py`             | 24 |
| `tests/api/test_openai_compat_routes.py`          | 10 |
| `tests/api/test_mcp_routes.py`                    | 7  |
| **Phase 16 total**                                | **132** |

Plus the existing 1133-test baseline (Sentinel V1 + Phase 15
Evolution Engine + the rest of the repo).

---

## What's deferred to subsequent phases

* **Tauri 2.0 desktop shell** — Phase 17.  The `local_ai`
  primitives Phase 16 ships are exactly what Tauri's Python
  sidecar pattern consumes.
* **llama-swap deployment scripts** — Phase 17.  The *backend*
  is in Commit B; the deployment story (subprocess supervisor,
  YAML config, model-cache locality) is separate.
* **Image / video / audio generative modules** — ComfyUI sidecar,
  Wan2GP, ACE-Step v1.5, F5-TTS, Kokoro-82M.
* **Auto-research agent** — sits on top of memory + tools + LLM
  backend.  Phase 18 candidate.
* **HippoRAG2 / GraphRAG / LightRAG** — significant indexers.
* **OpenHands V1 SDK adapter** — sits on top of MCP + memory + LLM
  backend, all of which Phase 16 provides.
* **Letta full integration** — the *primitives* are in Phase 16;
  running Letta alongside Amor is a deployment concern.
* **Activation steering library** (ACE / SRA / Angular).
* **Mercury Coder draft-and-verify pipeline**.
* **LLaDA reversal-task experiments**.

---

## Where to read next

* `AGENTS.md` — Phase 16 prompt-policy section
* `docs/sentinel-architecture.md` — V1 Sentinel pipeline
* `docs/sentinel-evolution.md` — Phase 15 evolution engine
* `local_ai/llm_backend/__init__.py` — public surface
* `local_ai/memory/store.py` — memory orchestrator
* `document_processor/api/openai_compat_routes.py` — `/v1/*` facade
* `document_processor/api/mcp_routes.py` — MCP server
