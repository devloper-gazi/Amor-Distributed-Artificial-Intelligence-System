# Production-Ready Multi-Lingual Document Processing System

A complete, production-ready Python system for real-time processing, translation, and consolidation of hundreds of thousands of documents across multiple formats and languages.

### 🌕 Development Notice ☀️
-- **It is actively being developed and may contain bugs. Please check the Version History.**
 - **Link:** https://github.com/devloper-gazi/Amor-Distributed-Artificial-Intelligence-System/blob/main/Version%20History.md


## Features

### 🌍 Multi-Source Support
- **Web Pages**: HTTP/HTTPS with JavaScript rendering (Playwright)
- **PDF Documents**: Native text + OCR for scanned documents (150+ languages)
- **Databases**: PostgreSQL, MySQL, MongoDB
- **APIs**: REST and GraphQL
- **Files**: CSV, JSON, XML, Excel, Word, Text

### 🗣️ Language Processing
- **Automatic Detection**: FastText-based detection (150+ languages)
- **Multi-Tier Translation**:
  - Tier 1 (Quality): Claude 3.5 Sonnet
  - Tier 2 (Balanced): Google Cloud Translation
  - Tier 3 (Volume): Azure Translator
- **Translation Memory**: Redis-based caching
- **Quality Assurance**: Automated quality scoring

### ⚡ Performance & Scalability
- **Real-Time Processing**: Event-driven architecture
- **High Throughput**: 10,000-50,000+ documents/second
- **Distributed**: Kafka message queue with 50+ partitions
- **Concurrent**: Async Python with 1000+ concurrent sources
- **Memory Efficient**: Streaming, chunked processing

### 🛡️ Production-Ready Features
- **Reliability**: Circuit breakers, exponential backoff, retry logic
- **Rate Limiting**: Token bucket algorithm per provider
- **Deduplication**: Bloom filter (1M capacity, 1% error rate)
- **Error Handling**: Dead letter queue for failed messages
- **Monitoring**: Prometheus + Grafana dashboards
- **Observability**: Structured logging (JSON), OpenTelemetry tracing
- **Storage**: PostgreSQL (metadata) + MongoDB (documents)

## Quick Start

### Prerequisites

- **Docker Desktop**:
  - [Windows](https://www.docker.com/products/docker-desktop)
  - [Mac](https://www.docker.com/products/docker-desktop)
  - [Linux](https://docs.docker.com/engine/install/)
- **Docker Compose**: Included with Docker Desktop (Windows/Mac) or [install separately](https://docs.docker.com/compose/install/) (Linux)
- Minimum 8GB RAM, 20GB free disk space

### Installation & Setup

#### Option 1: Automated Startup (Recommended)

**Linux/Mac:**
```bash
git clone https://github.com/devloper-gazi/Claude-Multi-Research.git
cd Claude-Multi-Research
cp .env.example .env
# Edit .env and add your API keys

# Run the startup script
./start.sh
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/devloper-gazi/Claude-Multi-Research.git
cd Claude-Multi-Research
Copy-Item .env.example .env
# Edit .env and add your API keys

# Run the startup script
.\start.ps1
```

#### Option 2: Manual Docker Compose

**Linux/Mac:**
```bash
git clone https://github.com/devloper-gazi/Claude-Multi-Research.git
cd Claude-Multi-Research
cp .env.example .env
# Edit .env and add your API keys

# Start all services
docker-compose up -d
```

**Windows:**
```powershell
git clone https://github.com/devloper-gazi/Claude-Multi-Research.git
cd Claude-Multi-Research
Copy-Item .env.example .env
# Edit .env and add your API keys

# Start all services with Windows configuration
docker-compose -f docker-compose.yml -f docker-compose.windows.yml up -d
```

### Access the Services

Once started, access the following URLs:
- **Amor Web UI**: `http://localhost:8000`
- **API Docs**: `http://localhost:8000/docs`
- **Metrics**: `http://localhost:8000/metrics`

If you can only expose a single port (8000), monitoring UIs are also available via:
- **Grafana (via 8000)**: `http://localhost:8000/grafana/` (admin/admin123)
- **Prometheus (via 8000)**: `http://localhost:8000/prometheus/`

Direct access (optional, if those ports are allowed):
- **Grafana**: `http://localhost:3000` (admin/admin123)
- **Prometheus**: `http://localhost:9091`

---

## Amor Research Modes & API Overview

Amor exposes two research providers that share a unified chat-first UI (research / thinking / coding modes) and common UX:

- **Claude API mode** (cloud):
  - Uses Anthropic’s Claude models via `ANTHROPIC_API_KEY`.
  - Endpoints are under `"/api/chat/*"` (for example `POST /api/chat/research`, `/api/chat/thinking`, `/api/chat/coding`).
  - Best when you want highest quality answers and are online.

- **Local AI mode** (offline):
  - Uses the `ollama` service (container name `amor-ollama`) running a local model such as `qwen2.5:7b`.
  - Endpoints are under `"/api/local-ai/*"` (for example `POST /api/local-ai/research`, `/api/local-ai/thinking`, `/api/local-ai/coding`).
  - Can optionally use the local NLLB translator and LanceDB vector store for multilingual, retrieval-augmented research.

The main API entrypoints for checking research availability are:

- `GET /api` – high-level flags: `chat_research_available`, `local_ai_available`, `crawling_available`, `translation_available`.
- `GET /api/chat/health` – Claude API configuration and connectivity.
- `GET /api/local-ai/health` – Local AI + Ollama + translation readiness.

See `CHAT_RESEARCH_GUIDE.md`, `RESEARCH_GUIDE.md`, and `LOCAL_AI_SETUP.md` for mode-specific payloads and examples.

---

## Docker Stack (Amor)

The Docker Compose file (`docker-compose.yml`) defines the canonical Amor stack:

- **Project name**: `amor` (top-level `name` in `docker-compose.yml`).
- **Gateway (`gateway`)**:
  - Nginx entrypoint that exposes **port 8000**.
  - Routes `/` to the FastAPI app, `/grafana` to Grafana, `/prometheus` to Prometheus.
- **Application (`app`)**:
  - FastAPI document processor and chat research API.
  - Talks to Kafka, Redis, PostgreSQL, MongoDB, Ollama, and LanceDB.
- **Local AI (`ollama`)**:
  - Service name: `ollama`
  - Container name: `amor-ollama`
  - Exposes port `11434` inside the Docker network; the app uses `OLLAMA_BASE_URL=http://ollama:11434`.
- **Datastores**:
  - `postgres` – metadata (documents, stats, etc.).
  - `mongo` – full document content.
  - `redis` – cache and rate limiting.
  - `lancedb-data` volume – vector store path mounted at `/data/vectors`.
- **Streaming & Monitoring**:
  - `kafka` + `zookeeper` – ingestion/event pipeline.
  - `prometheus` – metrics at `/prometheus` (or `http://localhost:9091` directly).
  - `grafana` – dashboards exposed via `/grafana` (or `http://localhost:3000` directly).

Key research-related environment variables (set via `.env` and consumed by the `app` service):

- `ANTHROPIC_API_KEY` – required for Claude API research mode.
- `OLLAMA_BASE_URL` – defaults to `http://ollama:11434` (service name `ollama`).
- `OLLAMA_MODEL` – default `qwen2.5:7b`, can be changed to any installed Ollama model.
- `OLLAMA_AUTO_PULL` – when `true`, the app may attempt to pull the model automatically on first use (requires internet).

## Usage

### REST API Example

```python
import requests

# Process a web page
response = requests.post('http://localhost:8000/process/single', json={
    "source_type": "web",
    "source_url": "https://example.com/article",
    "priority": "balanced"
})

# Batch processing
response = requests.post('http://localhost:8000/process', json={
    "sources": [
        {"source_type": "web", "source_url": "https://example1.com"},
        {"source_type": "pdf", "source_path": "/path/to/doc.pdf"}
    ],
    "async_processing": true
})
```

## Configuration

Key environment variables in `.env`:

```bash
# Translation API Keys (Required)
ANTHROPIC_API_KEY=your-key
GOOGLE_TRANSLATE_API_KEY=your-key
AZURE_TRANSLATOR_KEY=your-key

# Processing
MAX_CONCURRENT_SOURCES=1000
BATCH_SIZE=1000

# Infrastructure
KAFKA_BOOTSTRAP_SERVERS=kafka:9092
REDIS_HOST=redis
```

## Monitoring

- **Metrics**: http://localhost:9091/metrics
- **Grafana**: http://localhost:3000 (admin/admin123)
- **Logs**: `docker-compose logs -f app`

## Architecture

```
FastAPI REST API (8000)
    ↓
Processing Pipeline
    ↓
[Ingestion] → [Language Detection] → [Translation] → [Storage]
    ↓
Infrastructure: Kafka + Redis + PostgreSQL + MongoDB
```

## Platform-Specific Notes

### Windows

- **Docker Desktop**: Ensure WSL 2 backend is enabled for better performance
- **File Sharing**: Docker Desktop should have access to the project directory
- **Line Endings**: The Dockerfile automatically handles CRLF to LF conversion
- **Volume Mounts**: Use the provided `docker-compose.windows.yml` for proper path handling
- **Networking**: Kafka is configured for Windows Docker Desktop networking

### Linux

- **Permissions**: Ensure your user is in the `docker` group: `sudo usermod -aG docker $USER`
- **SELinux**: If using SELinux, you may need to add `:z` to volume mounts
- **Resource Limits**: Adjust Docker resource limits in `/etc/docker/daemon.json`

### macOS

- **Docker Desktop**: Allocate at least 8GB RAM in Docker Desktop preferences
- **File Sharing**: Ensure the project directory is in Docker's file sharing list
- **Performance**: Use named volumes instead of bind mounts for better performance

## Troubleshooting

### Research & Chat-Specific Issues

#### Claude API not configured
- **Symptom**: Claude mode is unavailable in the UI, or `/api/chat/health` reports `claude_api_configured: false`.
- **Fix**:
  - Set `ANTHROPIC_API_KEY` in `.env` (see `.env.example`).
  - Restart the app: `docker compose -f docker-compose.yml -f docker-compose.windows.yml restart app`.
  - Re-check with `curl http://localhost:8000/api/chat/health`.

#### Local AI (Ollama) unavailable
- **Symptoms**:
  - `GET /api/local-ai/health` returns 503 or `ollama_status != "healthy"`.
  - UI shows messages like “Local AI is unavailable” when using Local mode.
- **Quick checks**:
  - Confirm the container is running: `docker compose ps ollama`.
  - Inspect logs: `docker compose logs ollama`.
  - From the host, list models: `docker exec amor-ollama ollama list`.

If the expected model is missing:

```bash
docker exec amor-ollama ollama pull qwen2.5:7b
```

You can change the default model by updating `OLLAMA_MODEL` in `.env` and restarting the `app` service.

#### Research endpoints failing
- **Check core health**:
  - `curl http://localhost:8000/health`
  - `curl http://localhost:8000/api`
- **Check specific provider**:
  - `curl http://localhost:8000/api/chat/health`
  - `curl http://localhost:8000/api/local-ai/health`
- Review logs:
  - `docker compose logs app`
  - `docker compose logs ollama`

### General Docker & Platform Issues

#### Build fails with "Could not find a version that satisfies the requirement"

#### Build fails with "Could not find a version that satisfies the requirement"
- **Solution**: Ensure you're using the latest version of the repository. Package versions have been updated to use available versions.

#### "Permission denied" on Linux
- **Solution**: Make startup script executable: `chmod +x start.sh`
- Ensure your user is in the docker group: `sudo usermod -aG docker $USER && newgrp docker`

#### Kafka connection issues on Windows
- **Solution**: Use the Windows-specific docker-compose configuration:
  ```powershell
  docker-compose -f docker-compose.yml -f docker-compose.windows.yml up -d
  ```

#### Services fail to start with "port already in use"
- **Solution**: Check if ports are available:
  ```bash
  # Linux/Mac
  lsof -i :8000,9090,6379,5432,27017,9092

  # Windows
  netstat -ano | findstr ":8000 :9090 :6379 :5432 :27017 :9092"
  ```
  Stop conflicting services or modify ports in `docker-compose.yml`

#### Out of memory errors
- **Solution**:
  - Increase Docker memory allocation (Docker Desktop → Settings → Resources)
  - Reduce `deploy.replicas` in docker-compose.yml
  - Adjust `WORKER_COUNT` and `MAX_CONCURRENT_SOURCES` in `.env`

#### Volume mount issues on Windows
- **Solution**:
  - Ensure Docker Desktop has access to the drive
  - Use absolute paths or let docker-compose handle relative paths
  - Check Windows file sharing settings in Docker Desktop

### Viewing Logs

**All services:**
```bash
# Linux/Mac
docker-compose logs -f

# Windows
docker-compose -f docker-compose.yml -f docker-compose.windows.yml logs -f
```

**Specific service:**
```bash
docker-compose logs -f app
```

### Stopping Services

**Stop all services:**
```bash
# Linux/Mac
docker-compose down

# Windows
docker-compose -f docker-compose.yml -f docker-compose.windows.yml down
```

**Stop and remove volumes:**
```bash
# Linux/Mac
docker-compose down -v

# Windows
docker-compose -f docker-compose.yml -f docker-compose.windows.yml down -v
```

### Health Checks

Check if all services are healthy:
```bash
docker-compose ps
curl http://localhost:8000/health
```

## Development

### Running Tests
```bash
docker-compose exec app pytest
```

### Code Quality
```bash
docker-compose exec app black .
docker-compose exec app flake8
docker-compose exec app mypy document_processor
```

## Contributing

Contributions are welcome! Please ensure:
- Code passes all tests and linting
- Updates work on both Windows and Linux
- Documentation is updated for any new features

## License

MIT License

Built for production-scale multilingual document processing with cross-platform compatibility.
