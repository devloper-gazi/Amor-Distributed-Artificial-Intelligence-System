# 🎨 Web UI Guide - Amor

## 🎉 Congratulations! Your Modern Web Interface is Live!

You now have a beautiful, professional web interface for conducting multilingual research with Claude!

---

## 🚀 Quick Start

### Access the Web UI

Simply open your browser and visit:

```
http://localhost:8000
```

**That's it!** The Amor chat-first interface (Research / Thinking / Coding) will load automatically.

---

## 📊 Amor Chat UI & Modes

### 1. **Main Chat Interface**

The Amor interface is built around a single chat surface with three modes:

- **Research** – multi-step research workflows.
- **Thinking** – deep reasoning, planning, and analysis.
- **Coding** – code generation, refactoring, and debugging help.

You can switch between these modes from the top bar without leaving the page.

### 2. **Provider Modes: Claude vs Local AI**

In the Settings area you can choose which backend powers the chat:

- **Claude API**:
  - Uses Anthropic’s hosted models via `ANTHROPIC_API_KEY`.
  - Endpoints: `/api/chat/research`, `/api/chat/thinking`, `/api/chat/coding`.
  - Requires internet access and a valid API key.

- **Local AI (Ollama)**:
  - Uses the `ollama` service (container name `amor-ollama`) and a local model such as `qwen2.5:7b`.
  - Endpoints: `/api/local-ai/research`, `/api/local-ai/thinking`, `/api/local-ai/coding`.
  - Can run fully offline once the model is downloaded.

The UI will call the appropriate endpoints based on your chosen provider and mode.

---

### 3. **Batch Processing**

Process multiple documents at once!

#### Steps:
1. Click "Batch Processing" in the sidebar
2. Enter URLs (one per line):
   ```
   https://example.fr/article1
   https://example.de/article2
   https://example.jp/article3
   ```
3. Select Priority
4. Choose **Async** (background) or **Sync** (wait for results)
5. Click "Start Batch Processing"

---

### 4. **Document History**

View all previously processed documents with:
- Document IDs
- Languages detected
- Translation providers
- Quality scores
- Processing times

*(Note: Document listing feature shows aggregated data. Individual document retrieval coming soon!)*

---

### 5. **Analytics**

Detailed metrics for research tracking:

#### Pipeline Metrics:
- Total sources processed
- Processing success/failure rates
- Average processing time
- Total characters translated
- Cost estimates

#### Cache Statistics:
- Cache hits vs misses
- Hit rate percentage
- Memory usage
- Key statistics

---

### 6. **Settings**

Configure your research environment:

- **Auto Refresh** - Enable 30-second automatic updates
- **Reset Metrics** - Clear all statistics
- **View Logs** - Access system logs
- **Export Data** - Download research data (coming soon)

---

## 🎨 UI Features & Design

### Modern Dark Theme
- Professional dark color scheme
- Easy on the eyes for long research sessions
- High contrast for readability

### Responsive Design
- Works on desktop, tablet, and mobile
- Adaptive layout
- Touch-friendly controls

### Real-Time Updates
- Live system health monitoring
- Auto-refresh dashboard (configurable)
- Instant feedback on operations

### Beautiful Visualizations
- Progress bars for language distribution
- Provider usage charts
- Health status indicators
- Color-coded metrics

---

## 📋 Example Research Workflows

### Workflow 1: Academic Literature Review

```
1. Go to "Process Documents"
2. Enter research paper URL
3. Set Priority to "Quality"
4. Add metadata:
   {
     "topic": "Machine Learning",
     "journal": "Nature",
     "year": "2025"
   }
5. Process & review translation
6. Check Analytics to track progress
```

### Workflow 2: Multi-Language News Monitoring

```
1. Go to "Batch Processing"
2. Paste multiple news URLs:
   https://lemonde.fr/tech/article1
   https://spiegel.de/tech/article2
   https://asahi.com/tech/article3
3. Select "Balanced" priority
4. Enable "Async Processing"
5. Monitor progress in Analytics
```

### Workflow 3: Competitive Intelligence

```
1. Use Batch Processing for competitor websites
2. Set to "Volume" for speed
3. Review in Document History
4. Export data for analysis
```

---

## 🔧 Technical Details

### Architecture

```
Browser (You)
     ↓
FastAPI Server (Port 8000)
     ↓
Processing Pipeline
     ├── Language Detection
     ├── Translation (Claude/Google/Azure)
     ├── Quality Checking
     └── Storage
     ↓
Databases
     ├── PostgreSQL (Metadata)
     ├── MongoDB (Full Documents)
     └── Redis (Cache)
```

### API Endpoints

The UI uses these backend APIs:

- `GET /` - Web UI (you're using this!)
- `GET /health` - System health
- `GET /stats` - Processing statistics
- `GET /metrics` - Prometheus metrics
- `POST /process/single` - Process one document
- `POST /process` - Batch processing
- `GET /document/{id}` - Get document by ID
- `POST /reset-metrics` - Reset statistics

---

## 🎯 Tips & Best Practices

### For Best Results:

1. **Use Quality Priority** for:
   - Academic research
   - Legal documents
   - Technical content
   - Important translations

2. **Use Balanced Priority** for:
   - General web content
   - News articles
   - Blog posts

3. **Use Volume Priority** for:
   - Large-scale scraping
   - Bulk processing
   - Non-critical content

### Cost Optimization:

- **Enable caching** (automatic) - Saves money on duplicate content
- **Check cache hit rate** in Dashboard
- **Use batch processing** for efficiency

### Accuracy Tips:

- Review **confidence scores** for language detection
- Check **quality scores** for translations
- Use **Quality priority** for important work

---

## 🚨 Troubleshooting

### UI Not Loading?
```bash
# Check if containers are running
docker compose ps

# Restart if needed
docker compose -f docker-compose.yml -f docker-compose.windows.yml restart app
```

### Chat / Research Failing?

- Check system health:
  - `curl http://localhost:8000/health`
  - `curl http://localhost:8000/api`
- Check provider-specific health:
  - Claude: `curl http://localhost:8000/api/chat/health`
  - Local AI: `curl http://localhost:8000/api/local-ai/health`
- Inspect logs:
  - `docker compose logs app`
  - `docker compose logs ollama`

### Local AI Issues (Ollama)

- Verify the service:

```bash
docker compose ps ollama
docker compose logs ollama
docker exec amor-ollama ollama list
```

- If the configured `OLLAMA_MODEL` (default `qwen2.5:7b`) is missing, pull it:

```bash
docker exec amor-ollama ollama pull qwen2.5:7b
```

### Claude API Issues

- Ensure `ANTHROPIC_API_KEY` is set in `.env`.
- Restart the `app` service after changing it.
- Re-check `GET /api/chat/health` for configuration status.

---

## 📱 Mobile Access

The UI is fully responsive! Access from:
- **Phone** - Full touch support
- **Tablet** - Optimized layout
- **Desktop** - Complete features

---

## 🎨 Customization

### Want to change colors?
Edit: `web_ui/static/css/styles.css`

### Want to add features?
Edit: `web_ui/static/js/app.js`

### Want to modify layout?
Edit: `web_ui/templates/index.html`

Then rebuild:
```bash
docker compose -f docker-compose.yml -f docker-compose.windows.yml up -d --build app
```

---

## 🔐 Security Notes

- The UI is currently configured for **local use only** (localhost:8000)
- For production deployment, add authentication
- Configure CORS settings in `main.py`
- Use HTTPS in production

---

## 📚 Additional Resources

- **API Documentation**: http://localhost:8000/docs (Swagger UI)
- **Prometheus Metrics**: http://localhost:9091
- **Grafana Dashboards**: http://localhost:3000 (admin/admin123)

---

## 🎉 You're All Set!

Your modern research interface is ready. Start by:

1. Opening http://localhost:8000 in your browser
2. Clicking "Process Documents"
3. Entering a URL to translate
4. Watching the magic happen!

**Happy Researching!** 🚀

---

## 📞 Need Help?

- Check the **System Health** panel
- View **Analytics** for detailed metrics
- Check Docker logs: `docker compose logs -f app`
- Review `RESEARCH_GUIDE.md` for API details

---

## 🎨 Screenshots

### Dashboard
- Clean, modern dark theme
- Real-time statistics
- Visual charts and graphs

### Process Documents
- Simple, intuitive form
- Instant results
- Side-by-side original/translated text

### Batch Processing
- Multi-document handling
- Progress tracking
- Async processing support

---

**Enjoy your professional research tool!** 🎊
