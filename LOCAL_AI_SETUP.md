# Local AI Research System - Setup Guide

**Complete autonomous research system optimized for RTX 4060 8GB VRAM + 16GB RAM**

---

## 🎯 System Overview

This system provides **fully offline, autonomous AI research** using:

- **Ollama** (Qwen 2.5 7B Q4) - Local LLM inference (~4.5GB VRAM)
- **CrewAI** - Multi-agent orchestration (Research, Analyst, Writer agents)
- **NLLB-200** - 200+ language translation (~600MB VRAM with INT8)
- **LanceDB** - Serverless vector database with nomic-embed (768d)
- **Playwright + Trafilatura** - Autonomous web scraping with ethics
- **Modern Web UI** - Real-time agent monitoring and research interface

**Total VRAM Usage**: ~5.1GB / 8GB (leaving 2.9GB headroom)

---

## 📋 Prerequisites

### Hardware Requirements

- **GPU**: NVIDIA RTX 4060 (8GB VRAM) or equivalent
- **RAM**: 16GB minimum
- **Storage**: 20GB free space
- **OS**: Linux, Windows 10/11 with WSL2, or macOS (with Metal)

### Software Requirements

- Docker 24.0+ with Docker Compose
- NVIDIA Container Toolkit (for GPU support)
- Python 3.11+ (for local development)
- CUDA 11.8+ drivers

---

## 🚀 Quick Start (5 Minutes)

### 1. Clone and Navigate

```bash
cd Claude-Multi-Research
```

### 2. Configure WSL2 Memory (Windows Only)

Create or edit `%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
memory=11GB          # 11GB for WSL (5GB for containers, 6GB for system)
swap=32GB            # Large swap for safety
processors=4         # Use 4 CPU cores
localhostForwarding=true
```

Restart WSL:

```powershell
wsl --shutdown
```

### 3. Start Services

```bash
# Start all services
docker-compose -f docker-compose.local-ai.yml up -d

# Check status
docker-compose -f docker-compose.local-ai.yml ps
```

### 4. Pull Ollama Model

```bash
# Wait for Ollama to be healthy (30-60 seconds)
docker-compose -f docker-compose.local-ai.yml logs -f ollama

# Pull Qwen 2.5 7B (Q4 quantized, ~4.7GB download)
docker exec amor-ollama ollama pull qwen2.5:7b
```

This will take **5-10 minutes** depending on internet speed.

### 5. Verify Installation

```bash
# Check Ollama model
docker exec amor-ollama ollama list

# Test generation
docker exec amor-ollama ollama run qwen2.5:7b "Say hello"
```

### 6. Access Web UI

Open browser to:
- **Main UI**: http://localhost:8000
- **AI Research**: http://localhost:8000/research
- **API Docs**: http://localhost:8000/docs
- **Grafana**: http://localhost:3000 (admin/admin123)

---

## 🔧 Detailed Setup

### Install NLLB Translation (Optional but Recommended)

For **local multilingual translation** supporting 200+ languages:

#### Option 1: Download Pre-Converted Model

```bash
mkdir -p ./models/nllb-200-distilled-600M
cd ./models/nllb-200-distilled-600M

# Download CTranslate2 model (faster)
wget https://huggingface.co/michaelfeil/ct2fast-nllb-200-distilled-600M/resolve/main/model.bin
wget https://huggingface.co/michaelfeil/ct2fast-nllb-200-distilled-600M/resolve/main/shared_vocabulary.txt
wget https://huggingface.co/michaelfeil/ct2fast-nllb-200-distilled-600M/resolve/main/sentencepiece.model
```

#### Option 2: Convert from HuggingFace

```bash
pip install ctranslate2 transformers sentencepiece

ct2-transformers-converter \
  --model facebook/nllb-200-distilled-600M \
  --output_dir ./models/nllb-200-distilled-600M \
  --quantization int8 \
  --force
```

**Without NLLB**: System works but translation will be disabled.

### Setup Embedding Model

The system auto-downloads **nomic-embed-text-v1.5** (~500MB) on first use. To pre-download:

```bash
python3 << 'EOF'
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("nomic-ai/nomic-embed-text-v1.5", device="cpu")
print("✓ Embedding model cached")
EOF
```

---

## 📊 Resource Allocation

### VRAM Budget (8GB Total)

| Component | VRAM Usage | Notes |
|-----------|-----------|-------|
| **Qwen 2.5 7B (Q4)** | ~4.5GB | Main LLM, auto-unloads after 5min idle |
| **NLLB-200 (INT8)** | ~600MB | Only loaded during translation |
| **System/Docker** | ~1GB | GPU overhead |
| **Headroom** | ~1.9GB | Buffer for safety |

### Memory Budget (16GB RAM)

| Container | Memory Limit | Purpose |
|-----------|-------------|---------|
| **Ollama** | 8GB | LLM inference |
| **App** | 4GB | FastAPI, agents, processing |
| **Redis** | 768MB | Cache |
| **PostgreSQL** | 1GB | Metadata |
| **MongoDB** | 1GB | Documents |
| **Prometheus** | 512MB | Monitoring |
| **Grafana** | 256MB | Dashboards |

**Total**: ~15.5GB (leaving 500MB for system)

### CPU Allocation

- **App**: 4 cores (agent orchestration)
- **Databases**: 2 cores each
- **Total**: Uses all available cores efficiently

---

## 🎮 Using the System

### 1. Start a Research Task

**Via Web UI**:
1. Navigate to http://localhost:8000/research
2. Enter research topic: "Latest developments in quantum computing"
3. Choose depth: Quick (3 sources) / Standard (5 sources) / Deep (10 sources)
4. Optional: Provide specific source URLs
5. Click "Start AI Research"

**Via API**:

```bash
curl -X POST http://localhost:8000/api/local-ai/research \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "Latest developments in quantum computing",
    "depth": "standard",
    "use_translation": true,
    "save_to_knowledge": true
  }'
```

### 2. Monitor Progress

The UI shows **real-time agent activity**:

- **Research Specialist**: Discovers and gathers information
- **Data Analyst**: Processes and synthesizes findings
- **Technical Writer**: Creates structured reports

Progress bar updates every 2 seconds with current agent and task.

### 3. View Results

Results include:
- **Executive Summary**: Key takeaways
- **Findings**: Detailed discoveries with evidence
- **Analysis**: Synthesized insights
- **Sources**: Cited references with metadata
- **Confidence Score**: Overall reliability rating

### 4. Search Knowledge Base

Use **semantic search** to find relevant information:

```javascript
// Search previously researched topics
fetch('http://localhost:8000/api/local-ai/vector-search', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    query: "machine learning applications in healthcare",
    limit: 10
  })
})
```

---

## 🔍 Advanced Configuration

### Adjust VRAM Usage

Edit `docker-compose.local-ai.yml`:

```yaml
services:
  ollama:
    environment:
      # Unload model after 3 minutes (saves VRAM)
      - OLLAMA_KEEP_ALIVE=3m

      # Limit concurrent requests (reduces VRAM spikes)
      - OLLAMA_NUM_PARALLEL=1
```

### Change LLM Model

For **even less VRAM** (but lower quality):

```bash
# Use smaller model (3.5GB)
docker exec amor-ollama ollama pull qwen2.5:3b

# Update docker-compose.local-ai.yml
OLLAMA_MODEL=qwen2.5:3b
```

For **better quality** (if you have 12GB+ VRAM):

```bash
# Use larger model (9GB)
docker exec amor-ollama ollama pull qwen2.5:14b

OLLAMA_MODEL=qwen2.5:14b
```

### Customize Agent Behavior

Edit `local_ai/agents/research_crew.py`:

```python
# Adjust research depth
depth_config = {
    "quick": {"sources": 5, "analysis_depth": "brief"},     # More sources
    "standard": {"sources": 10, "analysis_depth": "thorough"},
    "deep": {"sources": 20, "analysis_depth": "comprehensive"},
}

# Modify agent personas
self.researcher = Agent(
    role="Research Specialist",
    goal="Your custom goal here",
    backstory="Your custom backstory",
    max_iter=10,  # More iterations = deeper research
)
```

### Enable GPU Monitoring

```bash
# Watch VRAM usage in real-time
watch -n 1 nvidia-smi

# Or use Grafana dashboard at http://localhost:3000
# Add NVIDIA GPU exporter for detailed metrics
```

---

## 🐛 Troubleshooting

### Issue: Ollama won't start

**Symptoms**: `ollama` container exits immediately

**Solution**:
```bash
# Check GPU access
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi

# If fails, reinstall NVIDIA Container Toolkit
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | \
  sudo tee /etc/apt/sources.list.d/nvidia-docker.list

sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo systemctl restart docker
```

### Issue: Out of memory errors

**Symptoms**: `OOM killed` in logs

**Solutions**:
1. **Reduce concurrent tasks**:
   ```yaml
   # docker-compose.local-ai.yml
   environment:
     - MAX_CONCURRENT_TASKS=1  # Down from 2
   ```

2. **Increase WSL memory** (Windows):
   ```ini
   # .wslconfig
   memory=13GB  # Up from 11GB
   ```

3. **Enable swap**:
   ```bash
   # Create 8GB swap file
   sudo fallocate -l 8G /swapfile
   sudo chmod 600 /swapfile
   sudo mkswap /swapfile
   sudo swapon /swapfile
   ```

### Issue: Model downloads are slow

**Solution**: Use a download manager or resume support

```bash
# Use wget with resume capability
wget -c https://ollama.ai/models/qwen2.5:7b

# Or use aria2c for faster multi-connection downloads
aria2c -x 8 -s 8 https://ollama.ai/models/qwen2.5:7b
```

### Issue: Web scraping blocked by robots.txt

**Expected behavior**: The scraper respects robots.txt by default

**Override** (only if authorized):
```python
# local_ai/scraping/web_scraper.py
async def _check_robots_txt(self, url: str) -> bool:
    # WARNING: Only disable if you have permission!
    return True  # Skip robots.txt check
```

### Issue: Translation not working

**Check**:
```bash
# Verify NLLB model files exist
ls -lh ./models/nllb-200-distilled-600M/

# Should show:
# model.bin (~600MB)
# sentencepiece.model
# shared_vocabulary.txt (or vocabulary.txt)
```

**Fix missing files**:
```bash
cd ./models/nllb-200-distilled-600M
wget https://huggingface.co/michaelfeil/ct2fast-nllb-200-distilled-600M/resolve/main/model.bin
wget https://huggingface.co/michaelfeil/ct2fast-nllb-200-distilled-600M/resolve/main/sentencepiece.model
```

---

## 📈 Performance Optimization

### 1. Batch Processing

For multiple research topics:

```python
from local_ai.agents import ResearchCrew

async with ResearchCrew() as crew:
    topics = [
        "Quantum computing advances",
        "AI in healthcare",
        "Renewable energy trends"
    ]

    results = await crew.batch_research(topics, depth="quick")
```

### 2. Pre-warm Models

Load models on startup to avoid cold-start delays:

```bash
# Add to startup script
docker exec amor-ollama ollama run qwen2.5:7b ""
```

### 3. Optimize Chunk Size

For better vector search:

```python
# local_ai/vector_store/lancedb_store.py
await vector_store.add_document(
    text=text,
    chunk_size=500,    # Smaller chunks = more precise search
    chunk_overlap=100  # More overlap = better context
)
```

### 4. Use Hybrid Search

Combine vector + keyword search for best results:

```python
results = await vector_store.hybrid_search(
    query="quantum computing",
    vector_weight=0.7,  # 70% semantic similarity
    text_weight=0.3     # 30% keyword matching
)
```

---

## 🔒 Security Considerations

### Ethical Web Scraping

The system implements:
- ✅ **robots.txt compliance** (automatic)
- ✅ **Rate limiting** (2s delay between requests)
- ✅ **Descriptive user agent** (identifies as research bot)
- ✅ **Respectful crawling** (max 3 concurrent requests)

**Never**:
- Scrape disallowed content
- Overload servers with requests
- Ignore rate limits
- Use for malicious purposes

### API Security

For production deployment:

```python
# Add to document_processor/main.py
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware

app.add_middleware(HTTPSRedirectMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://yourdomain.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
```

---

## 📚 API Reference

### Research Endpoint

```http
POST /api/local-ai/research
Content-Type: application/json

{
  "topic": "Research topic or question",
  "depth": "quick|standard|deep",
  "sources": ["https://optional-url1.com", "https://optional-url2.com"],
  "use_translation": true,
  "save_to_knowledge": true
}
```

**Response**:
```json
{
  "success": true,
  "session_id": "uuid",
  "message": "Research started"
}
```

### Vector Search Endpoint

```http
POST /api/local-ai/vector-search
Content-Type: application/json

{
  "query": "Search query",
  "limit": 10
}
```

**Response**:
```json
[
  {
    "id": "chunk_id",
    "text": "Relevant text chunk",
    "score": 0.95,
    "document_id": "doc_id",
    "title": "Document title",
    "source_url": "https://source.com"
  }
]
```

### Health Check

```http
GET /api/local-ai/health
```

**Response**:
```json
{
  "status": "healthy",
  "ollama_status": "healthy",
  "model_loaded": true,
  "vram_usage_mb": 4500,
  "translator_available": true,
  "scraper_available": true,
  "vector_store_available": true
}
```

---

## 🎯 Next Steps

1. **Test the system**: Run a sample research query
2. **Monitor VRAM**: Use `nvidia-smi` to verify usage stays under 8GB
3. **Customize agents**: Modify CrewAI agents for your use case
4. **Build knowledge base**: Add documents to vector store
5. **Integrate with workflows**: Use API endpoints in your applications

---

## 📞 Support

- **Documentation**: See `RESEARCH_GUIDE.md` for usage examples
- **Issues**: Report bugs at GitHub issues
- **Discord**: Join community for help

---

## 🏆 System Capabilities

What you can do with this system:

✅ **Autonomous research** on any topic
✅ **Multi-source analysis** with automatic aggregation
✅ **Multilingual translation** (200+ languages)
✅ **Semantic search** across knowledge base
✅ **Ethical web scraping** with robots.txt compliance
✅ **Agent-based orchestration** with specialized roles
✅ **Real-time monitoring** via web UI and Grafana
✅ **Fully offline operation** (no external API calls)
✅ **GPU-accelerated** LLM and translation
✅ **Production-ready** with Docker and observability

**All running on a single RTX 4060 8GB GPU!**

---

**Ready to research! 🚀**