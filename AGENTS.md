# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

**Amor** is a production-ready multilingual document processing system with dual research capabilities:
- **Document Processing Pipeline**: Ingests, translates, and stores documents from multiple sources (web, PDF, databases, APIs)
- **Chat Research Interface**: Interactive research assistant with Claude API and Local AI (Ollama) modes

The system is containerized using Docker Compose and optimized for cross-platform deployment (Windows/Linux/macOS).

## Architecture

### High-Level Structure

```
FastAPI App (port 8000 via gateway)
├── Chat Research APIs (/api/chat/*, /api/local-ai/*)
│   ├── Claude API mode (cloud-based, ANTHROPIC_API_KEY)
│   └── Local AI mode (Ollama + qwen2.5:7b model)
├── Document Pipeline APIs (/process/*, /document/*, /stats)
├── Chat Persistence (/api/sessions/*, /api/folders/*)
└── Web UI (/, /static)
    ├── Monochrome chat interface (Research/Thinking/Coding modes)
    └── Static assets (CSS/JS)

Infrastructure Stack (Docker services):
├── Gateway (nginx) - Routes all traffic via port 8000
├── Ollama (amor-ollama) - Local LLM service (11434)
├── Kafka + Zookeeper - Event streaming for document pipeline
├── Redis - Cache and rate limiting
├── PostgreSQL - Document metadata
├── MongoDB - Full document storage + chat sessions
├── Prometheus + Grafana - Monitoring
└── LanceDB (volume) - Vector store for Local AI
```

### Code Organization

- **`document_processor/`** - Main Python application
  - **`api/`** - FastAPI route handlers
    - `chat_research_routes.py` - Claude API endpoints (`/api/chat/*`)
    - `local_ai_routes_simple.py` - Local AI endpoints (`/api/local-ai/*`)
    - `chat_sessions_routes.py`, `chat_folders_routes.py` - Chat persistence
    - `crawling_routes.py`, `translation_routes.py` - Pipeline features
  - **`config/`** - Settings and logging configuration
    - `settings.py` - Pydantic settings (loads from `.env`)
  - **`core/`** - Data models and utilities
  - **`processing/`** - Document processing pipeline
    - `pipeline.py` - Main orchestrator
    - `translator.py`, `language_detector.py` - Translation/detection
  - **`sources/`** - Source-specific processors
    - `web_scraper.py`, `pdf_processor.py`, `database.py`, etc.
  - **`infrastructure/`** - Core infrastructure managers
    - `cache.py` (Redis), `storage.py` (Postgres/Mongo), `queue.py` (Kafka)
    - `chat_store.py` - MongoDB-backed chat session persistence
  - **`rag/`** - Retrieval-augmented generation components
  - **`reliability/`** - Circuit breakers, rate limiters, retry logic
  - `main.py` - FastAPI app initialization and lifespan management
- **`web_ui/`** - Frontend assets
  - `templates/index.html` - Main chat UI
  - `static/css/`, `static/js/` - Styles and frontend logic
- **`local_ai/`** - Local AI implementation (CrewAI agents, scraping, vector store)
- **`scripts/`** - Utility scripts
- **`monitoring/`** - Prometheus/Grafana configuration

## Key Environment Variables

Critical variables in `.env` (see `.env.example`):

### API Keys (Research Modes)
- `ANTHROPIC_API_KEY` - Required for Claude API mode
- `GOOGLE_TRANSLATE_API_KEY`, `AZURE_TRANSLATOR_KEY` - Translation services

### Ollama Configuration (Local AI)
- `OLLAMA_BASE_URL=http://ollama:11434` - Service name in Docker network
- `OLLAMA_MODEL=qwen2.5:7b` - Default model (change to use different models)
- `OLLAMA_AUTO_PULL=true` - Auto-pull missing models on startup

### Infrastructure
- `KAFKA_BOOTSTRAP_SERVERS=kafka:9092`
- `REDIS_HOST=redis`, `POSTGRES_HOST=postgres`, `MONGO_HOST=mongo`

### Processing Tuning
- `MAX_CONCURRENT_SOURCES=1000` - Concurrent document processors
- `WORKER_COUNT=4` - FastAPI worker processes
- `BATCH_SIZE=1000` - Documents per batch

## Docker Compose Stack

The system uses a canonical stack defined in `docker-compose.yml` with project name **`amor`**.

### Service Names
- `gateway` - Nginx reverse proxy (exposes port 8000)
- `app` - FastAPI application (2 replicas, 4GB RAM limit)
- `ollama` (container: `amor-ollama`) - Local LLM service
- `kafka`, `zookeeper` - Message queue
- `redis`, `postgres`, `mongo` - Data stores
- `prometheus`, `grafana` - Monitoring

### Windows-Specific Configuration
On Windows, **always** use both compose files:
```powershell
docker compose -f docker-compose.yml -f docker-compose.windows.yml <command>
```

The Windows override (`docker-compose.windows.yml`):
- Adjusts Kafka networking for Docker Desktop
- Reduces resource limits (1 replica, 3GB RAM)
- Handles Windows path formats for volume mounts

### Starting the Stack

**Windows:**
```powershell
.\start.ps1  # Automated script with health checks
# OR manually:
docker compose -f docker-compose.yml -f docker-compose.windows.yml up -d
```

**Linux/Mac:**
```bash
./start.sh  # Automated script
# OR manually:
docker compose up -d
```

### Ollama Model Management

The `ollama` service requires a model to be pulled before Local AI works:

```bash
# Check installed models
docker exec amor-ollama ollama list

# Pull default model (qwen2.5:7b, ~4.7GB download)
docker exec amor-ollama ollama pull qwen2.5:7b

# Use alternative models (adjust OLLAMA_MODEL in .env)
docker exec amor-ollama ollama pull qwen2.5:3b  # Smaller, faster
docker exec amor-ollama ollama pull llama3:8b   # Alternative model
```

After pulling a new model, restart the app:
```bash
docker compose restart app
```

## Common Development Tasks

### Running Tests
```bash
docker compose exec app pytest
# Or with coverage:
docker compose exec app pytest --cov=document_processor --cov-report=html
```

### Code Quality Checks
```bash
# Format code
docker compose exec app black document_processor/

# Lint
docker compose exec app flake8 document_processor/

# Type checking
docker compose exec app mypy document_processor/
```

### Viewing Logs
```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f app
docker compose logs -f ollama

# Windows (use both compose files)
docker compose -f docker-compose.yml -f docker-compose.windows.yml logs -f app
```

### Accessing Services
- **Main UI**: http://localhost:8000
- **API Docs**: http://localhost:8000/docs
- **Prometheus**: http://localhost:8000/prometheus/ (via gateway) or http://localhost:9091 (direct)
- **Grafana**: http://localhost:8000/grafana/ (via gateway) or http://localhost:3000 (direct, admin/admin123)

### Health Checks
```bash
# Overall system health
curl http://localhost:8000/health

# API capabilities
curl http://localhost:8000/api

# Claude API status
curl http://localhost:8000/api/chat/health

# Local AI status
curl http://localhost:8000/api/local-ai/health
```

## Research API Usage

### Claude API Mode (Cloud)
Endpoints under `/api/chat/*` - requires `ANTHROPIC_API_KEY`

```bash
# Research mode
curl -X POST http://localhost:8000/api/chat/research \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Explain quantum computing", "max_tokens": 2048}'

# Thinking mode
curl -X POST http://localhost:8000/api/chat/thinking \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Analyze the pros and cons of microservices"}'

# Coding mode
curl -X POST http://localhost:8000/api/chat/coding \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write a binary search algorithm in Python"}'
```

### Local AI Mode (Offline)
Endpoints under `/api/local-ai/*` - requires Ollama with model installed

```bash
# Start research
curl -X POST http://localhost:8000/api/local-ai/research \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "Machine learning in healthcare",
    "depth": "standard",
    "use_translation": true,
    "target_language": "en"
  }'

# Check research status
curl http://localhost:8000/api/local-ai/status/{session_id}
```

### Document Pipeline APIs
For bulk document processing (separate from chat):

```bash
# Process single document
curl -X POST http://localhost:8000/process/single \
  -H "Content-Type: application/json" \
  -d '{
    "source_type": "web",
    "source_url": "https://example.com/article",
    "priority": "balanced"
  }'

# Batch process
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{
    "sources": [...],
    "async_processing": true
  }'

# Get processing stats
curl http://localhost:8000/stats
```

## Troubleshooting

### "Claude API not configured"
**Cause**: `ANTHROPIC_API_KEY` not set
**Fix**:
1. Add key to `.env`: `ANTHROPIC_API_KEY=sk-...`
2. Restart: `docker compose restart app`
3. Verify: `curl http://localhost:8000/api/chat/health`

### "Local AI unavailable" or 503 errors
**Cause**: Ollama service unhealthy or model not installed
**Fix**:
1. Check service: `docker compose ps ollama`
2. View logs: `docker compose logs ollama`
3. Verify model: `docker exec amor-ollama ollama list`
4. Pull if missing: `docker exec amor-ollama ollama pull qwen2.5:7b`
5. Test generation: `docker exec amor-ollama ollama run qwen2.5:7b "Hello"`

### Windows Kafka connection issues
**Fix**: Always use Windows compose override:
```powershell
docker compose -f docker-compose.yml -f docker-compose.windows.yml up -d
```

### Out of memory errors
**Windows WSL2**: Edit `%USERPROFILE%\.wslconfig`:
```ini
[wsl2]
memory=11GB
swap=32GB
```
Then: `wsl --shutdown` and restart Docker Desktop

**Docker limits**: Adjust resource limits in `docker-compose.windows.yml` (reduce `deploy.replicas` or memory limits)

### Build failures
**Package version issues**: Check that `requirements.txt` versions are current and compatible
**Platform-specific issues**: Dockerfile handles CRLF conversion automatically; verify Docker Desktop WSL2 backend is enabled (Windows)

## Frontend Development

The UI is a monochrome chat interface with three modes (Research/Thinking/Coding).

**Key files:**
- `web_ui/templates/index.html` - Main template
- `web_ui/static/css/tokens.css` - Design system colors/spacing
- `web_ui/static/css/chat-research.css` - Chat-specific styles
- `web_ui/static/js/app.js` - App initialization and routing
- `web_ui/static/js/chat-research.js` - Chat logic and API calls

**To modify the UI:**
1. Edit files in `web_ui/`
2. Rebuild image: `docker compose build app`
3. Restart: `docker compose up -d app`

Static assets are versioned using `STATIC_VERSION` environment variable (defaults to current timestamp).

## Platform-Specific Notes

### Windows
- Use PowerShell scripts: `.\start.ps1`, `.\validate_setup.ps1`
- Always include `-f docker-compose.windows.yml` in compose commands
- Ensure WSL 2 backend enabled in Docker Desktop for better performance
- Docker Desktop must have access to project directory for volume mounts

### Linux
- Use bash scripts: `./start.sh`
- Add user to docker group: `sudo usermod -aG docker $USER && newgrp docker`
- Make scripts executable: `chmod +x start.sh`
- SELinux users may need `:z` on volume mounts

### macOS
- Allocate at least 8GB RAM in Docker Desktop preferences
- Named volumes preferred over bind mounts for performance

## Important Implementation Details

### Chat Session Persistence
- MongoDB collections: `chat_sessions`, `chat_folders`
- Managed by `chat_store.py` (infrastructure layer)
- Indexes created automatically on app startup

### Translation Routing
- **Quality priority**: Claude 3.5 Sonnet (requires API key)
- **Balanced**: Mix of providers
- **Volume**: Google/Azure Translate
- Translation results cached in Redis with 5-minute TTL

### Async Processing Pattern
- Pipeline uses `asyncio.Semaphore` for concurrency control (max 1000 concurrent sources)
- Streaming extraction via `async for chunk in processor.extract_content(source)`
- All infrastructure managers (cache, storage, queue) are async

### Monitoring & Observability
- Prometheus metrics exposed at `/metrics`
- Grafana dashboards pre-configured for crawl/translation stats
- Structured logging with `structlog` (JSON format)
- OpenTelemetry tracing enabled when `ENABLE_TRACING=true`

### Circuit Breakers & Reliability
- Per-provider circuit breakers (5 failures trigger open state, 60s recovery)
- Exponential backoff retry (2-60 seconds, max 5 attempts)
- Rate limiting via token bucket algorithm
- Dead letter queue for failed messages (`documents-dlq` topic)

## Quick Code V2 — prompt-template policy (no content filters)

The Quick Code V2 adapter (`document_processor/quick_code/`) ships
with strict prompt-template rules.  Anyone editing or adding a
system prompt under that package follows them:

- **No refusal phrases.**  Do NOT add `I cannot help`,
  `I'm sorry, but`, `as an AI`, `It is not appropriate`, `I won't`,
  or any equivalent.
- **No legal disclaimers.**  Do NOT add `if illegal in your
  jurisdiction`, `consult a lawyer`, `educational purposes only`,
  `not legal advice`, or similar footers.
- **No content-moderation pre-filters.**  Do NOT insert refusal
  blocks (e.g. `rejecting requests for X`) in any system prompt.
- **Required tone.**  Each system prompt opens with
  `You are an expert engineer. Produce the best technical answer.`
  Direct, specific, no hedging.
- **Carve-out.**  Infrastructure security
  (`document_processor/code_intelligence/adversarial_reviewer.py`)
  stays intact and is the **only** layer allowed to block output.
  It checks prompt injection, shell injection, and secret leakage
  — not content moderation.

Enforcement: `tests/quick_code/test_prompts_no_filters.py`
greps every V2 prompt template for the banned phrase set on every
build.  Adding a new V2 prompt without updating that test will
fail CI.

## Sentinel — prompt-template policy (no content filters)

The Sentinel security module (`document_processor/sentinel/`) is
held to the **same** prompt-template rules as Quick Code V2.  The
five Sentinel agents (Auditor / Reasoner / RedTeam / Patcher /
Judge) audit *any* code the user submits — including code that
discusses or implements security primitives.  A refusal-prone
prompt would be a UX disaster ("I can't review this code because
it contains a hash function") and is forbidden.

- All five Sentinel system prompts open with `You are an expert
  security engineer. Produce the best technical answer.`
- The RedTeam agent in particular *must* describe exploit chains
  in concrete technical detail — that is its job.  No hedging.
- The infrastructure-security carve-out in
  `code_intelligence/adversarial_reviewer.py` still applies and is
  still the only layer allowed to block output.

Enforcement: `tests/sentinel/test_prompts_no_filters.py` greps
every Sentinel prompt template for the banned phrase set on every
build, mirroring the Quick Code V2 test.

## Amor — Phase 17 Strong Code Intelligence

Phase 17 closes the user-reported "Code Intelligence çok zayıf …
hatasız çalıştığından emin olabilir misin? Mevcut sistem düzgün
çalışmıyor" complaint with six commits:

1. **`GET /api/code/diagnostics`** — single-call snapshot of
   backend, models + role assignment, sandbox health + cold-start
   telemetry, RAG config, Phase 15 ledger integrity, Phase 16
   facade gates, recent sessions + failures.  Operator visibility
   the user explicitly asked for.
2. **`_publish` cross-replica Redis fallback + invert engine→
   routes layer violation** — surgical hardening when
   `_sessions[sid]` is empty on a polling replica + decouple
   engine from the routes module via injected `routing_setter`.
3. **Strict planner `spec` block + engine forwards
   `dependencies` to sandbox** — fixes the user's
   `ModuleNotFoundError: No module named 'flask'`.  Planner
   prompts now require an authoritative `spec` (invariants,
   signatures, preconditions, postconditions, error_cases,
   dependencies); engine sanitises against an allow-list regex
   and pipes to `install_packages=`.
4. **Per-language sandbox timeout map + pip/npm bridge network
   install path** — HTML/CSS drop to 5s; compile-heavy widen to
   60-90s; `--network=bridge` only when packages requested;
   pip/npm install into `/tmp/pip-prefix` so tmpfs writes don't
   poison the base image's site-packages tree.
5. **Diff-mode DebuggerAgent** — SEARCH/REPLACE block format
   (Aider / Cline / OpenHands convention), 3-5x token savings on
   500-LOC outputs, fewer regressions in untouched lines.  Falls
   back to whole-file rewrite when the diff doesn't apply
   cleanly.
6. **Phase 17 docs** — `docs/amor-phase-17-strong-code-intelligence.md`.

Three immutable rules for every Phase 17 commit:

1. **Backwards compatibility is non-negotiable.**  Every flag
   defaults to "current behaviour".  `_publish` Redis fallback
   only fires when the in-memory miss already happened (silent
   failure before).  Diff-mode debugger falls back to whole-file
   when the patch doesn't apply.  Engine works without
   `routing_setter`.
2. **Visibility before optimisation.**  Diagnostics endpoint
   (Commit R) shipped first because the user can't tell if
   anything else worked without it.  Future perf work
   (pre-warmed sandbox pool, 3-vote planner) gates on diagnostics
   numbers, not speculation.
3. **Layer cleanliness.**  Engine doesn't import from routes.
   Sandbox doesn't import from engine.  Each subsystem has a
   stable contract that the next phase can build on.

The test surface lives at `tests/code_intelligence/test_diagnostics.py`,
`test_routing_setter.py`, `test_dependency_forwarding.py`,
`test_per_language_timeout.py`, and `test_diff_mode_debugger.py`
(63 tests total).

See `docs/amor-phase-17-strong-code-intelligence.md` for the full
subsystem map, settings reference, and live-verified commands.

## Amor — Phase 16.5 Code Intelligence repair (sandbox + role diversity)

Phase 16.5 fixes three separate bugs the user reported all
collapsing into "Code Intelligence can't even produce a snake
game":

1. **Sandbox dead** — ``ExecutionSandbox`` short-circuited to
   ``docker_unavailable`` because the bind-mounted
   ``/var/run/docker.sock`` had no client binary to talk to.  The
   Dockerfile now pulls the static Docker 27.3.1 client into
   ``/usr/local/bin/docker`` so ``docker --version`` works inside
   the app container.  Live-verified by
   ``tests/code_intelligence/test_sandbox_live.py`` (gated by
   ``AMOR_LIVE_TESTS=1``).
2. **All five roles → same model** — ``_tag_installed`` did
   prefix-match by base name, so ``qwen2.5-coder:14b`` and
   ``qwen2.5-coder:32b`` were treated as installed whenever
   ``qwen2.5-coder:7b`` was.  The +50 install bonus then dragged
   every role onto the highest-scoring (uninstalled) flagship.
   Fixed: tightened to exact-tag + suffix-tolerant variation on
   the *same parameter size*.  Strength-match weight raised
   2.0 → 6.0 so role-specific strength lists actually flip the
   choice on a 2-model rig.
3. **No session-level model diversity** — even with the scorer
   fixed, ``ensure_model`` was looped per role and could still
   land everyone on the same tag.  New
   ``select_models_for_session(roles, effort)`` distributes roles
   across distinct installed models with a 12-point degradation
   cap so a role never drops to a meaningfully worse model just
   for visual diversity.  ``CodeIntelligenceEngine._phase_model_prep``
   promotes the resolved ``{role: tag}`` map into the active
   routing ContextVar.

Verified split on the production 2-model rig (qwen2.5:7b +
qwen2.5-coder:7b)::

    planner  → qwen2.5:7b           (reasoning)
    critic   → qwen2.5:7b           (reasoning, kept by degradation cap)
    debugger → qwen2.5:7b           (reasoning)
    coder    → qwen2.5-coder:7b     (code generation)
    tester   → qwen2.5-coder:7b     (code generation)

Catalogue gained the brief-recommended fits for 8 GB VRAM:
``deepseek-r1:7b`` (reasoning specialist), ``qwen3:8b`` /
``qwen3:4b`` (Qwen3 instruct), ``josiefied-qwen3:8b`` (uncensored),
``qwen2.5-coder:14b`` (borderline 8 GB w/ offload).  When any of
these are pulled, the session selector slots them into reasoning-
heavy roles automatically.

See ``docs/amor-phase-16-foundations.md`` (Subsystem 1 + Pluggable
LLM backend) for the underlying primitives.

## Amor — Phase 16 adapter foundations

Phase 16 introduces the AMOR architecture brief's foundation
primitives: a pluggable LLM backend, an OpenAI-compatible `/v1`
facade, a RAG upgrade pack (BM25+RRF hybrid + cross-encoder
reranker + BGE-M3 + late-chunking), an MCP-style typed tool
registry, and a Letta-style 3-tier memory hierarchy.  All under
`local_ai/` (top-level package).  Three rules:

1. **Backwards compatibility is non-negotiable.**  Default
   `llm_backend = "ollama"` preserves byte-equivalent behaviour
   for the existing test suite.  The keyword-overlap hybrid path
   becomes BM25+RRF (controlled by `rag_hybrid_search_enabled`,
   default True); the existing `documents` corpus on
   nomic-embed-text-v1.5 is never touched.  Opt-in features
   (BGE-M3, late-chunking, MCP server, cross-encoder reranker,
   memory ledger audit) all default off or behind a flag.
2. **Every external SDK integration goes through the
   abstraction.**  Letta, OpenHands V1 SDK, Aider, the OpenAI
   Python SDK — all plug in via `OPENAI_BASE_URL=http://
   localhost:8000/v1`, never by importing AMOR internals.
3. **Memory writes append a Phase 15 ledger entry.**  When
   `memory_ledger_audit_enabled` is on, every `MemoryStore`
   write fires a `memory_core_written` / `memory_recall_appended`
   / `memory_archival_written` ledger entry so the immutable
   trail covers conversation history.

The test surface lives at `tests/local_ai/`,
`tests/api/test_openai_compat_routes.py`, and
`tests/api/test_mcp_routes.py` (132 tests total).

See `docs/amor-phase-16-foundations.md` for the full architecture
map, settings reference, and live-smoke recipes.

## Sentinel — Phase 15 evolution policy

Phase 15 adds nine self-improvement subsystems (governance,
preferences, prompt evolution, rule synthesis, agent spawning,
distillation, curriculum, LoRA, DAG mutation) under
`document_processor/sentinel/evolution/`.  Three rules:

1. **Nothing promotes without an immutable ledger entry.**  Every
   `*_promoted` / `*_rolled_back` / `agent_spawned` mutation must
   call `LedgerStore.append(actor, kind, payload)` with a real
   actor (chat user id or X-Client-Id, never `"system"` for a
   user-initiated change).  The ledger is hash-chained — entries
   are immutable post-write.
2. **Every mutation payload runs through `ImmutableConstraints.
   check()` first.**  A violation appends a
   `constraint_check_failed` entry and returns 400 from the route
   layer; the production pointer never moves.
3. **Promotion requires either a Pareto improvement or explicit
   user consent (or both).**  Auto-promotion happens only when the
   candidate strictly dominates the parent on all measured axes
   (precision / recall / latency band).  The Console UI never
   auto-confirms a promote — the operator clicks the button.

The test surface lives at `tests/sentinel/evolution/` (151 tests)
and gates every commit that touches Phase 15 code.

See `docs/sentinel-evolution.md` for the full subsystem map,
configuration reference, and disk layout.

## Related Documentation

- **`README.md`** - Main project documentation, quickstart, and deployment
- **`CHAT_RESEARCH_GUIDE.md`** - Detailed chat interface and research API usage
- **`LOCAL_AI_SETUP.md`** - Local AI setup, VRAM optimization, and CrewAI agents
- **`RESEARCH_GUIDE.md`** - Document pipeline API usage and examples
- **`WEB_UI_GUIDE.md`** - Frontend architecture and customization
- **`QUICK_START.md`** - Fast setup instructions
- **`DOCKER_FIX_SUMMARY.md`** - Docker troubleshooting and fixes
- **`docs/sentinel-architecture.md`** - Sentinel V1 multi-agent pipeline
- **`docs/sentinel-agent-prompts.md`** - All five Sentinel role prompts
- **`docs/sentinel-ml-models.md`** - Classical ML pipeline + RAG layout
- **`docs/sentinel-evolution.md`** - Phase 15 Evolution Engine (9 subsystems + Console)
- **`docs/amor-phase-16-foundations.md`** - Phase 16 Adapter Foundations (LLM backend, /v1 facade, RAG upgrade, MCP, memory)
- **`docs/amor-phase-17-strong-code-intelligence.md`** - Phase 17 Strong Code Intelligence (diagnostics endpoint, planner spec block, dependency forwarding, diff-mode debugger)
- **`example_usage.py`** - Python client examples for pipeline APIs
