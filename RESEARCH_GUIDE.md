# 📚 Research Guide: Using the Multilingual Document Processor

## Overview

This system helps you conduct research across multiple languages by automatically:
- **Scraping** web content
- **Detecting** languages automatically
- **Translating** content to English using Claude (cloud) or local models
- **Storing** processed documents for future retrieval
- **Caching** results to avoid re-processing

There are two main ways to use the system:

- **Pipeline APIs** (document processor):
  - Endpoints such as `/process/single`, `/process`, `/document/{id}`, `/stats`.
  - Optimized for large-scale ingestion and translation of documents.

- **Chat Research APIs** (Amor chat UI):
  - Claude-based endpoints under `/api/chat/*`.
  - Local AI endpoints under `/api/local-ai/*`.
  - Optimized for interactive research, thinking, and coding workflows.

---

## 🎯 Use Cases for Research

### 1. **Academic Research**
- Translate foreign research papers
- Process multiple papers from different countries
- Compare methodologies across languages

### 2. **Market Research**
- Analyze foreign competitors' websites
- Process international product reviews
- Translate market reports

### 3. **News & Media Analysis**
- Track international news stories
- Translate foreign journalism
- Monitor global trends

### 4. **Legal & Compliance**
- Translate legal documents
- Process international regulations
- Review foreign case law

---

## 🚀 Quick Start Examples

### Example 1: Process a Web Page (Pipeline API)

```bash
curl -X POST http://localhost:8000/process/single \
  -H "Content-Type: application/json" \
  -d '{
    "source_type": "web",
    "source_url": "https://www.example.com/article",
    "priority": "quality",
    "metadata": {
      "research_topic": "AI Ethics",
      "date_collected": "2025-11-23"
    }
  }'
```

**Response:**
```json
{
  "id": "abc-123-def",
  "source_id": "xyz-789",
  "original_language": {
    "code": "es",
    "name": "Spanish",
    "confidence": 0.98
  },
  "original_text": "El texto original en español...",
  "translated_text": "The original text in Spanish...",
  "translation_provider": "claude",
  "translation_quality_score": 0.95,
  "processing_time_ms": 1234.56,
  "status": "completed"
}
```

### Example 2: Batch Process Multiple Sources (Pipeline API)

```bash
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{
    "sources": [
      {
        "source_type": "web",
        "source_url": "https://example.fr/article1",
        "priority": "quality"
      },
      {
        "source_type": "web",
        "source_url": "https://example.de/article2",
        "priority": "balanced"
      },
      {
        "source_type": "web",
        "source_url": "https://example.jp/article3",
        "priority": "volume"
      }
    ],
    "async_processing": true
  }'
```

**Response:**
```json
{
  "batch_id": "batch-abc-123",
  "submitted": 3,
  "estimated_completion_time_seconds": 6.0
}
```

### Example 3: Retrieve Processed Document

```bash
# Get by document ID
curl http://localhost:8000/document/abc-123-def
```

### Example 4: Check Processing Statistics

```bash
curl http://localhost:8000/stats
```

**Response:**
```json
{
  "pipeline": {
    "total_sources": 150,
    "processed": 145,
    "failed": 2,
    "skipped": 3,
    "cache_hits": 45,
    "cache_misses": 100,
    "avg_processing_time_ms": 1234.5,
    "languages_detected": {
      "es": 50,
      "fr": 30,
      "de": 25,
      "ja": 20,
      "zh": 15
    },
    "providers_used": {
      "claude": 120,
      "cache": 25
    }
  }
}
```

---

## 📊 Supported Source Types (Pipeline APIs)

| Source Type | Description | Example Use Case |
|-------------|-------------|------------------|
| `web` | Web pages, articles, blogs | Scrape research articles |
| `pdf` | PDF documents | Academic papers |
| `api` | REST API endpoints | Research databases |
| `file` | Text files | Local documents |
| `sql` | SQL databases | Research datasets |
| `nosql` | NoSQL databases | Document collections |

---

## 🎨 Translation Priorities (Pipeline APIs)

Choose the right priority based on your needs:

### **Quality** (Recommended for Research)
- Uses Claude 3.5 Sonnet
- Highest translation quality
- Best for academic/legal content
- Slower, more expensive

### **Balanced**
- Uses mix of providers
- Good quality/speed tradeoff
- Best for general research

### **Volume**
- Uses Google/Azure Translate
- Fastest processing
- Best for large-scale scraping

---

## 🔧 Python Integration (Pipeline APIs)

Use the provided `example_usage.py` script:

```python
import requests

# Process a document
response = requests.post(
    "http://localhost:8000/process/single",
    json={
        "source_type": "web",
        "source_url": "https://example.com/article",
        "priority": "quality"
    }
)

result = response.json()
print(f"Translated: {result['translated_text']}")
```

---

## 📈 Monitoring Your Research

### 1. **Check Health**
```bash
curl http://localhost:8000/health
```

### 2. **View Metrics**
```bash
curl http://localhost:8000/metrics
```

### 3. **Prometheus UI**
Visit: `http://localhost:9091`

### 4. **Grafana Dashboard**
Visit: `http://localhost:3000`
- Username: `admin`
- Password: `admin123`

---

## 💡 Advanced Research Workflows

### Workflow 1: Multi-Language Literature Review

```python
import requests

# Define research topics and sources
sources = [
    {"url": "https://arxiv.org/paper1", "lang": "en"},
    {"url": "https://hal.archives-ouvertes.fr/paper2", "lang": "fr"},
    {"url": "https://example.de/paper3", "lang": "de"},
]

# Process all papers
for source in sources:
    response = requests.post(
        "http://localhost:8000/process/single",
        json={
            "source_type": "web",
            "source_url": source["url"],
            "priority": "quality",
            "metadata": {
                "expected_language": source["lang"],
                "research_phase": "literature_review"
            }
        }
    )

    result = response.json()
    print(f"Processed: {result['id']}")

    # Save to your research database
    # save_to_database(result)
```

### Workflow 2: Competitive Intelligence

```python
# Monitor competitor websites across languages
competitors = [
    "https://competitor1.fr",
    "https://competitor2.de",
    "https://competitor3.jp",
]

batch_response = requests.post(
    "http://localhost:8000/process",
    json={
        "sources": [
            {"source_type": "web", "source_url": url, "priority": "balanced"}
            for url in competitors
        ],
        "async_processing": True
    }
)

batch_id = batch_response.json()["batch_id"]
print(f"Monitoring batch: {batch_id}")
```

---

## 🔍 Language Detection

The system automatically detects languages using:
- fastText language detection model
- 99% accuracy for 176 languages
- Confidence scores included

No need to specify the source language!

---

## 💬 Chat Research APIs (Amor)

In addition to the document-processing endpoints above, Amor exposes chat-oriented research APIs that power the web UI.

### Provider Health & Discovery

- `GET /api` – high-level service status including:
  - `chat_research_available`
  - `local_ai_available`
  - `crawling_available`
  - `translation_available`
- `GET /api/chat/health` – Claude API configuration and connectivity.
- `GET /api/local-ai/health` – Local AI + Ollama readiness.

### Claude-Based Research

```bash
curl -X POST http://localhost:8000/api/chat/research \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Summarize the benefits of vector databases for analytics workloads.",
    "max_tokens": 2048,
    "temperature": 1.0
  }'
```

The response structure matches what the Amor UI expects (message content plus optional sources and metadata).

### Local AI Research

```bash
curl -X POST http://localhost:8000/api/local-ai/research \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "Latest developments in quantum computing",
    "depth": "quick",
    "use_translation": true,
    "target_language": "en",
    "save_to_knowledge": true
  }'
```

Typical `depth` values: `quick`, `standard`, `deep`. See `LOCAL_AI_SETUP.md` and `CHAT_RESEARCH_GUIDE.md` for details.

### Environment Variables for Research Modes

- `ANTHROPIC_API_KEY` – required for Claude-based research APIs.
- `OLLAMA_BASE_URL` – defaults to `http://ollama:11434` (service name `ollama`).
- `OLLAMA_MODEL` – default `qwen2.5:7b`; must be installed in `amor-ollama`.
- `OLLAMA_AUTO_PULL` – optional, whether to auto-download models on first use.

If you change these, restart the `app` container so configuration is reloaded.

---

## 💾 Data Storage

### PostgreSQL (Metadata)
- Document metadata
- Processing statistics
- Translation info

### MongoDB (Full Content)
- Original text
- Translated text
- Complete document history

### Redis (Cache)
- Translation cache
- Deduplication
- Session storage

---

## 🎯 Best Practices for Research

1. **Use Quality Priority for Academic Work**
   - Claude provides best contextual translation
   - Preserves technical terminology

2. **Enable Async Processing for Large Batches**
   - Don't wait for individual requests
   - Process in background

3. **Add Metadata**
   - Tag documents with research topics
   - Add collection dates
   - Include source attribution

4. **Monitor Cache Hit Rate**
   - Avoid reprocessing same content
   - Check `/stats` regularly

5. **Use Batch Processing**
   - More efficient for multiple documents
   - Better resource utilization

---

## 🚨 Limitations & Considerations

1. **API Keys Required**
   - Google Translate API
   - Azure Translator
   - Anthropic API (Claude)

2. **Rate Limits**
   - Built-in rate limiting
   - Configurable per provider

3. **Cost Management**
   - Claude translations cost ~$0.003 per 1K chars
   - Cache aggressively to reduce costs

4. **Max Batch Size**
   - 10,000 documents per batch
   - Use multiple batches for larger datasets

---

## 📞 Getting Help

- **Health Check**: `http://localhost:8000/health`
- **Documentation**: `http://localhost:8000/docs` (Swagger UI)
- **Logs**: `docker compose logs -f app`

---

## 🎓 Example Research Projects

### Project 1: Global AI Policy Analysis
```bash
# Collect AI policy documents from different countries
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{
    "sources": [
      {"source_type": "web", "source_url": "https://gov.fr/ai-policy", "metadata": {"country": "France"}},
      {"source_type": "web", "source_url": "https://gov.de/ki-politik", "metadata": {"country": "Germany"}},
      {"source_type": "web", "source_url": "https://gov.jp/ai-policy", "metadata": {"country": "Japan"}}
    ],
    "async_processing": true
  }'
```

### Project 2: International Patent Search
```bash
# Translate patent documents
curl -X POST http://localhost:8000/process/single \
  -H "Content-Type: application/json" \
  -d '{
    "source_type": "web",
    "source_url": "https://patents.google.com/patent/JP123456",
    "priority": "quality",
    "metadata": {
      "patent_number": "JP123456",
      "technology": "AI/ML"
    }
  }'
```

---

## 🔐 Environment Variables

Configure in `.env` file:

```env
# Translation API Keys
GOOGLE_TRANSLATE_API_KEY=your_key_here
AZURE_TRANSLATOR_KEY=your_key_here
ANTHROPIC_API_KEY=your_key_here

# Service Configuration
SERVICE_NAME=document-processor
ENVIRONMENT=production
LOG_LEVEL=INFO
```

---

## 🎉 Ready to Start!

Your multilingual document processing system is running and ready for research. Start with the examples above and scale up to your specific needs!
