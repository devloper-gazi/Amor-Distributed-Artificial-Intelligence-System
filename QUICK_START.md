# Quick Start Guide - Monochrome Chat UI

## ✅ Integration Complete!

The new monochrome chat-first UI is now fully integrated with the backend. All old UI files have been removed and replaced with the unified interface.

---

## 🚀 How to Start

### Step 1: Start Docker Desktop
Make sure Docker Desktop is running on Windows.

### Step 2: Run the Startup Script
```powershell
.\start.ps1
```

### Step 3: Wait for Services (About 30 seconds)
The script will:
- Check Docker availability
- Create `.env` file if needed
- Pull Docker images
- Build and start all services
- Wait for initialization

### Step 4: Open the Web UI
Navigate to: **`http://localhost:8000`**

### Optional: Monitoring (also available via port 8000)
- **Metrics**: http://localhost:8000/metrics
- **Grafana**: http://localhost:8000/grafana/ (admin/admin123)
- **Prometheus**: http://localhost:8000/prometheus/

---

## 🎨 What You'll See

### The New Monochrome Interface
- **Clean Design**: Pure blacks, whites, and grays
- **Three Modes**: Research 🔍, Thinking 🧠, Coding 💻
- **Chat-First**: Single interface for all interactions
- **Dark Mode**: Toggle with moon/sun icon

### Key Features
- **Mode Selector**: Dropdown in top bar to switch modes
- **Sidebar**: Collapsible chat history (click hamburger menu)
- **Settings**: Toggle between Local AI and Claude API
- **Keyboard Shortcuts**:
  - `⌘K` / `Ctrl+K` - Toggle sidebar
  - `⌘N` / `Ctrl+N` - New chat
  - `⌘1-3` / `Ctrl+1-3` - Switch modes
  - `ESC` - Close modals

---

## 💬 Try It Out

### Research Mode (Default)
**Example**: "What are the latest developments in AI?"
- **With Local AI**: Multi-agent research with web scraping
- **With Claude API**: Direct Claude research query

### Thinking Mode
**Example**: "How should I design a scalable database architecture?"
- Deep analytical thinking
- Step-by-step reasoning
- Multiple perspectives

### Coding Mode
**Example**: "Write a Python function to parse JSON files"
- Code generation
- Debugging help
- Best practices

---

## 🔧 Configuration

### Using Claude API
1. Add your API key to `.env`:
   ```
   ANTHROPIC_API_KEY=your_key_here
   ```
2. Restart services: `docker-compose restart app`
3. In UI: Settings → Toggle "Use Claude API"

### Using Local AI (Default)
- No external API key needed.
- Uses the **`ollama`** service (container name **`amor-ollama`**) inside Docker.
- The app connects to Ollama at `OLLAMA_BASE_URL=http://ollama:11434` and uses the model from `OLLAMA_MODEL` (default `qwen2.5:7b`).
- Runs completely offline once the model is pulled into `amor-ollama`.

To verify Local AI is ready:

```powershell
# Check container status
docker-compose ps ollama

# Inspect Ollama logs
docker-compose logs ollama

# List installed models inside the container
docker exec amor-ollama ollama list
```

If `qwen2.5:7b` (or your configured `OLLAMA_MODEL`) is missing, pull it:

```powershell
docker exec amor-ollama ollama pull qwen2.5:7b
```

---

## 📚 Documentation

- **[INTEGRATION_COMPLETE.md](INTEGRATION_COMPLETE.md)** - Full integration details
- **[TEST_REPORT.md](TEST_REPORT.md)** - Frontend testing results
- **[RESEARCH_GUIDE.md](RESEARCH_GUIDE.md)** - Research mode guide

---

## 🐛 Troubleshooting

### Docker Not Running
```powershell
# Start Docker Desktop manually, then:
.\start.ps1
```

### Services Not Starting
```powershell
# Check Docker logs:
docker-compose logs app

# Restart services:
docker-compose restart
```

### UI Not Loading
```powershell
# Hard refresh browser:
Ctrl + Shift + R

# Or restart app container:
docker-compose restart app
```

### Local AI / Ollama Issues

#### "Local AI is unavailable" or research stuck in Local mode

- Confirm the service is healthy:

```powershell
curl http://localhost:8000/api/local-ai/health
```

- Check that the container is running and has a model installed:

```powershell
docker-compose ps ollama
docker exec amor-ollama ollama list
```

- If no suitable model appears, pull the default:

```powershell
docker exec amor-ollama ollama pull qwen2.5:7b
```

#### Want to bypass Local AI and only use Claude?

1. Make sure `ANTHROPIC_API_KEY` is set in `.env`.
2. Restart the app: `docker-compose restart app`.
3. In the UI Settings, toggle to **Claude API** provider.

---

## 📊 Service URLs

| Service | URL | Purpose |
|---------|-----|---------|
| **Web UI** | http://localhost:8000 | Main interface |
| **API Docs** | http://localhost:8000/docs | API documentation |
| **Health Check** | http://localhost:8000/health | System status |
| **Grafana** | http://localhost:3000 | Monitoring (admin/admin123) |

---

## ✨ What Changed

### Removed (Old UI)
- ❌ `chat_research.html` - Old research page
- ❌ `local_research.html` - Old local AI page
- ❌ `local-ai.css` - Old styling
- ❌ `local-ai.js` - Old scripts

### Added (New UI)
- ✅ Unified `index.html` - Single chat interface
- ✅ `tokens.css` - Monochrome design system
- ✅ Refactored `app.js` - Mode-based navigation
- ✅ `ChatController` class - Unified chat logic
- ✅ **2 new Claude API endpoints** (/thinking, /coding)
- ✅ **2 new Local AI endpoints** (/thinking, /coding)

---

## 🎯 Next Steps

1. **Test All Three Modes**: Try Research, Thinking, and Coding
2. **Create Some Sessions**: Build up chat history
3. **Try Dark Mode**: Toggle theme with moon icon
4. **Use Keyboard Shortcuts**: Speed up your workflow
5. **Explore Settings**: Configure API preferences

---

## ✅ Validation

Run the validation script anytime:
```powershell
.\validate_setup.ps1
```

This checks:
- Old files removed ✅
- New files present ✅
- Backend endpoints working ✅
- All 6 modes available ✅

---

## 🆘 Need Help?

1. Check [INTEGRATION_COMPLETE.md](INTEGRATION_COMPLETE.md) for detailed info
2. Review Docker logs: `docker-compose logs app`
3. Verify `.env` configuration
4. Ensure Docker Desktop is running

---

**Status**: ✅ Ready to Use!

Start with: `.\start.ps1`

**Enjoy your new monochrome chat interface!** 🚀
