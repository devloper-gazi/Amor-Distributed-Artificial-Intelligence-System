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

## Related Documentation

- **`README.md`** - Main project documentation, quickstart, and deployment
- **`CHAT_RESEARCH_GUIDE.md`** - Detailed chat interface and research API usage
- **`LOCAL_AI_SETUP.md`** - Local AI setup, VRAM optimization, and CrewAI agents
- **`RESEARCH_GUIDE.md`** - Document pipeline API usage and examples
- **`WEB_UI_GUIDE.md`** - Frontend architecture and customization
- **`QUICK_START.md`** - Fast setup instructions
- **`DOCKER_FIX_SUMMARY.md`** - Docker troubleshooting and fixes
- **`example_usage.py`** - Python client examples for pipeline APIs
