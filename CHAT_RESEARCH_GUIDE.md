# Chat-Based Research Interface Guide

## 🎯 Overview

Your system now has a modern **chat-based research interface** that allows you to conduct research using either:
- **Claude API** (cloud-based, high-quality responses)
- **Local AI** (offline, using Ollama + CrewAI)

Simply toggle between modes and type your research questions naturally!

---

## 🚀 Quick Start

### 1. Access the Chat Interface

Open your browser to:
```
http://localhost:8000
```

### 2. Configure Claude API (Optional)

To use Claude API, you need to set your API key.

**Option A: Using .env file** (Recommended)
```bash
# Create .env file in project root
echo "ANTHROPIC_API_KEY=your-api-key-here" > .env
```

**Option B: Using environment variable**
```bash
# Linux/Mac
export ANTHROPIC_API_KEY=your-api-key-here

# Windows PowerShell
$env:ANTHROPIC_API_KEY="your-api-key-here"

# Windows CMD
set ANTHROPIC_API_KEY=your-api-key-here
```

Then restart the container:
```bash
docker compose -f docker-compose.yml -f docker-compose.windows.yml restart app
```

### 3. Start Researching!

1. **Choose your AI mode**: Toggle the switch at the top
   - **OFF (Green)**: Local AI using Ollama
   - **ON (Orange)**: Claude API

2. **Type your research question**: Just type naturally in the input box

3. **Send**: Click send or press Ctrl+Enter

---

## 🎨 Interface Features

### Toggle Switch
- **Local AI** (Green): Uses your local Ollama + CrewAI setup
  - Fully offline
  - Multi-agent orchestration
  - Real-time progress monitoring
  - Shows agent activities

- **Claude API** (Orange): Uses Anthropic's Claude Sonnet 4.5
  - Cloud-based
  - High-quality responses
  - Fast generation
  - Requires API key

### Chat Messages
- **Your messages**: Blue bubbles on the right
- **Assistant (Local AI)**: Green avatar with "AI" badge
- **Assistant (Claude)**: Orange avatar with Claude logo
- **System messages**: Gray background for errors/info

### Progress Tracking (Local AI only)
When using Local AI, you'll see:
- Progress modal showing research stages
- Current agent working (Researcher, Analyst, Writer)
- Progress percentage
- Real-time updates every 2 seconds

---

## 💬 Example Research Queries

### General Research
```
What are the latest developments in quantum computing?
```

### Specific Topics
```
Explain the differences between RAG and fine-tuning for LLMs
```

### Technical Questions
```
How does LanceDB compare to other vector databases?
```

### Code-Related
```
Best practices for async Python with FastAPI
```

### Multi-part Questions
```
What is CrewAI and how does it compare to LangChain?
Give me specific examples of when to use each.
```

---

## 🔧 Advanced Configuration

### Claude API Settings

Claude-based research is implemented in `document_processor/api/chat_research_routes.py` and uses `ANTHROPIC_API_KEY` from your `.env`. Typical knobs include:

- **Model** (e.g. Claude Sonnet).
- **Maximum tokens** per response.
- **Temperature** for creativity vs determinism.

See the route implementation for up-to-date defaults.

### Local AI Settings

Local research uses the `ollama` service (container `amor-ollama`) plus the local AI modules under `local_ai/`. The main configuration points are:

- In `.env` (consumed by the `app` service):
  - `OLLAMA_BASE_URL` – defaults to `http://ollama:11434`.
  - `OLLAMA_MODEL` – default `qwen2.5:7b`.
  - `OLLAMA_AUTO_PULL` – whether to auto-pull missing models.
- In `docker-compose.yml`:
  - `services.ollama` – resource limits, health checks, and ports.

Adjust these to change which model is used or how aggressively Local AI is started.

### UI Customization

The chat experience is implemented in the modern Amor UI:

- Template: `web_ui/templates/index.html`
- Styles: `web_ui/static/css/chat-research.css`, `web_ui/static/css/tokens.css`
- Logic: `web_ui/static/js/chat-research.js`, `web_ui/static/js/app.js`

You can adjust themes and layout by editing these files and rebuilding the `app` image.

---

## 📊 API Endpoints

### Check System Status
```bash
curl http://localhost:8000/api

# Response:
# {
#   "service": "document-processor",
#   "version": "1.0.0",
#   "chat_research_available": true,
#   "local_ai_available": false
# }
```

### Check Claude API Health
```bash
curl http://localhost:8000/api/chat/health

# Response (not configured):
# {
#   "claude_api_configured": false,
#   "api_key_set": false,
#   "status": "not_configured"
# }

# Response (configured):
# {
#   "claude_api_configured": true,
#   "api_key_set": true,
#   "status": "healthy"
# }
```

### Research via API (Claude)
```bash
curl -X POST http://localhost:8000/api/chat/research \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "What are the benefits of vector databases?",
    "max_tokens": 2048,
    "temperature": 1.0
  }'
```

### Research via API (Local AI)
```bash
curl -X POST http://localhost:8000/api/local-ai/research \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "What are the benefits of vector databases?",
    "depth": "standard",
    "use_translation": true,
    "target_language": "en",
    "save_to_knowledge": false
  }'
```

---

## 🛠️ Troubleshooting

### Issue: "Claude API not configured"

**Cause**: `ANTHROPIC_API_KEY` not set or invalid.

**Solution**:
1. Get your API key from `https://console.anthropic.com/`.
2. Set `ANTHROPIC_API_KEY` in `.env` (or as an environment variable).
3. Restart the `app` container (`docker compose restart app`).
4. Verify with: `curl http://localhost:8000/api/chat/health`.

### Issue: Local AI toggle fails or returns 503

**Common causes**:
- `amor-ollama` container not running or failing health checks.
- The configured `OLLAMA_MODEL` is not installed yet.

**Checks**:

```bash
docker compose ps ollama
docker compose logs ollama
docker exec amor-ollama ollama list
curl http://localhost:8000/api/local-ai/health
```

If the expected model is missing, pull it:

```bash
docker exec amor-ollama ollama pull qwen2.5:7b
```

### Issue: Chat messages not appearing

**Cause**: JavaScript error or network issue.

**Solution**:
1. Check browser console (F12).
2. Verify server logs: `docker compose logs app`.
3. Test API endpoint: `curl http://localhost:8000/api/chat/health`.

### Issue: Progress modal stuck

**Cause**: Local AI research taking longer than expected

**Solution**:
1. Wait for completion (can take 2-5 minutes)
2. Check server logs for errors
3. Refresh page if stuck for >10 minutes

---

## 📁 File Structure

```
Claude-Multi-Research/
├── web_ui/
│   ├── templates/
│   │   └── index.html                    # Unified Amor chat interface
│   └── static/
│       ├── css/
│       │   ├── chat-research.css         # Chat-specific styles
│       │   └── tokens.css                # Design system tokens
│       └── js/
│           ├── chat-research.js          # Research/thinking/coding logic
│           └── app.js                    # App bootstrap and routing
├── document_processor/
│   ├── api/
│   │   ├── chat_research_routes.py       # Claude API backend
│   │   ├── local_ai_routes.py            # Local AI (advanced) routes
│   │   └── local_ai_routes_simple.py     # Simple Local AI research endpoints
│   └── main.py                           # FastAPI app with route includes
├── docker-compose.yml                    # Canonical Amor stack (includes Ollama)
└── LOCAL_AI_SETUP.md                     # Detailed local AI setup & tuning
```

---

## 🎯 Best Practices

### For Claude API
1. **Be specific**: More detailed prompts get better responses
2. **Use context**: Reference previous messages for follow-ups
3. **Adjust temperature**: Lower (0.3) for factual, higher (1.0) for creative
4. **Monitor tokens**: Large responses cost more API credits

### For Local AI
1. **Be patient**: Local research takes 2-5 minutes
2. **Choose depth wisely**:
   - Quick: 3 sources (~1 min)
   - Standard: 5 sources (~3 min)
   - Deep: 10 sources (~5 min)
3. **Monitor VRAM**: Use `nvidia-smi` to check GPU usage
4. **Optimize model**: Smaller models = faster but lower quality

### General Tips
1. **Start simple**: Test with basic queries first
2. **Check health**: Use `/api/chat/health` before research
3. **Review logs**: Check `docker logs` if something fails
4. **Save results**: Copy important responses (auto-scroll to bottom)

---

## 🚀 Next Steps

### Enable Local AI Research
To implement full local AI functionality:

1. **Create local AI module**:
   ```bash
   mkdir -p document_processor/local_ai/{agents,scraping,vector_store}
   ```

2. **Implement research agents** using CrewAI (see [LOCAL_AI_SETUP.md](LOCAL_AI_SETUP.md))

3. **Add web scraping** with Playwright + Trafilatura

4. **Setup vector store** with LanceDB for knowledge base

5. **Pull Ollama model**:
   ```bash
   docker exec amor-ollama ollama pull qwen2.5:7b
   ```

### Enhance Chat Interface
- Add conversation history persistence
- Implement message search/filter
- Add export functionality (PDF, Markdown)
- Create saved research sessions
- Add source citations display

### Monitor Performance
- Access Grafana dashboards: http://localhost:3000
- View Prometheus metrics: http://localhost:9091
- Check API docs: http://localhost:8000/docs

---

## 🔗 Related Documentation

- [LOCAL_AI_SETUP.md](LOCAL_AI_SETUP.md) - Complete local AI setup guide
- [RESEARCH_GUIDE.md](RESEARCH_GUIDE.md) - Original research system guide
- [WEB_UI_GUIDE.md](WEB_UI_GUIDE.md) - Web UI documentation

---

## 📝 Changelog

### v1.0.0 (Current)
- ✅ Modern chat-based UI with dark theme
- ✅ Claude API integration (Sonnet 4.5)
- ✅ Toggle switch for AI mode selection
- ✅ Real-time progress monitoring for local AI
- ✅ Responsive design for mobile/desktop
- ✅ Health check endpoints
- ⏳ Local AI implementation (pending)

---

**Built with Claude Code** 🤖

For questions or issues, check the troubleshooting section or review server logs.