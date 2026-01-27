"""
Simplified Local AI Research API Routes
Direct Ollama integration with web scraping capabilities
"""

import asyncio
import logging
import os
from typing import List, Optional, Dict, Any
from datetime import datetime
from uuid import uuid4
import re
from urllib.parse import quote_plus, urlparse

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
import httpx
from bs4 import BeautifulSoup
from ..infrastructure.cache import cache_manager

try:
    import trafilatura
    TRAFILATURA_AVAILABLE = True
except ImportError:
    TRAFILATURA_AVAILABLE = False

logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/api/local-ai", tags=["local-ai"])

# Ollama configuration
def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
# If true, we will attempt to pull the model automatically when missing.
OLLAMA_AUTO_PULL = _env_bool("OLLAMA_AUTO_PULL", True)
try:
    OLLAMA_HTTP_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_HTTP_TIMEOUT_SECONDS", "900"))
except ValueError:
    OLLAMA_HTTP_TIMEOUT_SECONDS = 900.0

_ollama_pull_lock = asyncio.Lock()

# Active research sessions
research_sessions: Dict[str, Dict[str, Any]] = {}

SESSION_CACHE_PREFIX = "local_ai_research_session:"
try:
    SESSION_CACHE_TTL_SECONDS = int(os.getenv("LOCAL_AI_SESSION_TTL_SECONDS", "7200"))  # 2 hours
except ValueError:
    SESSION_CACHE_TTL_SECONDS = 7200


def _session_cache_key(session_id: str) -> str:
    return f"{SESSION_CACHE_PREFIX}{session_id}"


async def _persist_session(session_id: str, session: Dict[str, Any]) -> None:
    """
    Persist session state to Redis so /status works with multiple app replicas.
    Best-effort: failures should not break the research workflow.
    """
    try:
        await cache_manager.set_json(_session_cache_key(session_id), session, ttl=SESSION_CACHE_TTL_SECONDS)
    except Exception as e:
        logger.debug(f"Failed to persist research session to Redis: {e}")


async def _load_session(session_id: str) -> Optional[Dict[str, Any]]:
    """
    Load session state from in-memory cache or Redis.
    """
    session = research_sessions.get(session_id)
    if session:
        return session

    try:
        cached = await cache_manager.get_json(_session_cache_key(session_id))
        if isinstance(cached, dict):
            research_sessions[session_id] = cached
            return cached
    except Exception as e:
        logger.debug(f"Failed to load research session from Redis: {e}")
    return None


# Request/Response Models
class LocalAIResearchRequest(BaseModel):
    topic: str = Field(..., min_length=1, description="Research topic or question")
    depth: str = Field("standard", description="Research depth: quick, standard, or deep")
    use_translation: bool = Field(True, description="Enable automatic translation of non-English sources")
    target_language: str = Field("en", description="Target language for translation (ISO 639-1 code)")
    save_to_knowledge: bool = Field(False, description="Save results to vector store (not implemented)")


class LocalAIResearchResponse(BaseModel):
    success: bool
    session_id: str
    message: str


# Web Scraping Functions
async def search_web(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """Search the web and return URLs. Uses multiple strategies."""
    results = []

    # Strategy 1: Try DuckDuckGo Lite (simpler HTML)
    try:
        search_url = f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }
            response = await client.get(search_url, headers=headers)

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')

                # Parse DuckDuckGo Lite results
                for link in soup.find_all('a', limit=max_results * 3):
                    url = link.get('href', '')
                    title = link.get_text(strip=True)

                    # Filter valid URLs
                    if url and title and url.startswith('http') and len(title) > 10:
                        # Skip DuckDuckGo's own links
                        if 'duckduckgo.com' not in url:
                            results.append({
                                'url': url,
                                'title': title[:100]
                            })
                            if len(results) >= max_results:
                                break

                if results:
                    logger.info(f"Found {len(results)} search results using DuckDuckGo Lite for: {query}")
                    return results
    except Exception as e:
        logger.warning(f"DuckDuckGo Lite search failed: {e}")

    # Strategy 2: Use pre-defined high-quality sources for common topics
    fallback_sources = [
        {"url": "https://en.wikipedia.org/wiki/" + quote_plus(query.replace(" ", "_")), "title": f"Wikipedia: {query}"},
        {"url": "https://arxiv.org/search/?query=" + quote_plus(query), "title": f"arXiv: {query}"},
        {"url": "https://scholar.google.com/scholar?q=" + quote_plus(query), "title": f"Google Scholar: {query}"},
    ]

    # For specific topics, add relevant sources
    query_lower = query.lower()
    if any(term in query_lower for term in ['ai', 'artificial intelligence', 'machine learning', 'transformer']):
        fallback_sources.extend([
            {"url": "https://paperswithcode.com/search?q=" + quote_plus(query), "title": f"Papers with Code: {query}"},
            {"url": "https://huggingface.co/search/full-text?q=" + quote_plus(query), "title": f"Hugging Face: {query}"},
        ])

    logger.info(f"Using {len(fallback_sources[:max_results])} fallback sources for: {query}")
    return fallback_sources[:max_results]


async def scrape_url(url: str) -> Optional[Dict[str, str]]:
    """Scrape content from a URL."""
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            response = await client.get(url, headers=headers)

            if response.status_code != 200:
                logger.warning(f"Failed to scrape {url}: status {response.status_code}")
                return None

            html_content = response.text

            # Try trafilatura first (better content extraction)
            if TRAFILATURA_AVAILABLE:
                extracted_text = trafilatura.extract(html_content, include_comments=False, include_tables=False)
                if extracted_text:
                    return {
                        'url': url,
                        'content': extracted_text[:5000],  # Limit content length
                        'method': 'trafilatura'
                    }

            # Fallback to BeautifulSoup
            soup = BeautifulSoup(html_content, 'html.parser')

            # Remove script and style elements
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.decompose()

            # Get text
            text = soup.get_text(separator='\n', strip=True)

            # Clean up text
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = '\n'.join(chunk for chunk in chunks if chunk)

            return {
                'url': url,
                'content': text[:5000],  # Limit content length
                'method': 'beautifulsoup'
            }

    except Exception as e:
        logger.error(f"Failed to scrape {url}: {e}")
        return None


async def scrape_multiple_urls(urls: List[str], max_concurrent: int = 3) -> List[Dict[str, str]]:
    """Scrape multiple URLs concurrently."""
    results = []

    # Process in batches to avoid overwhelming servers
    for i in range(0, len(urls), max_concurrent):
        batch = urls[i:i + max_concurrent]
        tasks = [scrape_url(url) for url in batch]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in batch_results:
            if isinstance(result, dict) and result:
                results.append(result)

        # Small delay between batches
        if i + max_concurrent < len(urls):
            await asyncio.sleep(2)

    return results


# Language Detection and Translation Functions
async def detect_language(text: str) -> str:
    """Detect the language of text using simple heuristics or API."""
    if not text or len(text) < 20:
        return "en"
    
    # Use a sample of the text for detection
    sample = text[:500]
    
    try:
        # Try to use the translation API's detect endpoint
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "http://localhost:8000/api/translate/detect",
                json={"text": sample}
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("detected_language", "en")
    except Exception as e:
        logger.debug(f"Language detection via API failed: {e}")
    
    # Fallback: Simple heuristic based on character analysis
    # Check for non-ASCII characters that indicate non-English text
    non_ascii_count = sum(1 for c in sample if ord(c) > 127)
    non_ascii_ratio = non_ascii_count / len(sample) if sample else 0
    
    # If more than 20% non-ASCII, likely non-English
    if non_ascii_ratio > 0.2:
        # Try to guess language from common characters
        if any(ord(c) >= 0x4E00 and ord(c) <= 0x9FFF for c in sample):
            return "zh"  # Chinese
        elif any(ord(c) >= 0x3040 and ord(c) <= 0x30FF for c in sample):
            return "ja"  # Japanese
        elif any(ord(c) >= 0xAC00 and ord(c) <= 0xD7AF for c in sample):
            return "ko"  # Korean
        elif any(ord(c) >= 0x0600 and ord(c) <= 0x06FF for c in sample):
            return "ar"  # Arabic
        elif any(ord(c) >= 0x0400 and ord(c) <= 0x04FF for c in sample):
            return "ru"  # Russian
        else:
            return "unknown"
    
    return "en"


async def translate_text(text: str, source_lang: str, target_lang: str) -> Dict[str, Any]:
    """Translate text using the translation API."""
    if source_lang == target_lang or source_lang == "en" and target_lang == "en":
        return {"translated": text, "success": True, "original_language": source_lang}
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "http://localhost:8000/api/translate/",
                json={
                    "text": text,
                    "source_lang": source_lang,
                    "target_lang": target_lang
                }
            )
            if response.status_code == 200:
                data = response.json()
                return {
                    "translated": data.get("translated_text", text),
                    "success": True,
                    "original_language": source_lang,
                    "confidence": data.get("confidence", 0.8)
                }
    except Exception as e:
        logger.warning(f"Translation failed for {source_lang} -> {target_lang}: {e}")
    
    # Return original text if translation fails
    return {"translated": text, "success": False, "original_language": source_lang}


async def translate_scraped_content(
    scraped_content: List[Dict[str, str]],
    target_lang: str = "en",
    session: Optional[Dict[str, Any]] = None
) -> List[Dict[str, str]]:
    """Translate non-target-language content in scraped results."""
    translated_content = []
    translation_count = 0
    
    for i, content in enumerate(scraped_content):
        if session:
            session["current_task"] = f"Translating source {i+1}/{len(scraped_content)}"
        
        text = content.get("content", "")
        if not text:
            translated_content.append(content)
            continue
        
        # Detect language
        detected_lang = await detect_language(text)
        content["original_language"] = detected_lang
        
        # Translate if not in target language
        if detected_lang != target_lang and detected_lang not in ["en", "unknown"]:
            result = await translate_text(text, detected_lang, target_lang)
            if result["success"]:
                content["content"] = result["translated"]
                content["translated"] = True
                content["translation_confidence"] = result.get("confidence", 0.8)
                translation_count += 1
                logger.info(f"Translated content from {detected_lang} to {target_lang}")
            else:
                content["translated"] = False
        else:
            content["translated"] = False
        
        translated_content.append(content)
    
    logger.info(f"Translation complete: {translation_count}/{len(scraped_content)} sources translated")
    return translated_content


# Helper functions
async def _ollama_list_models() -> List[str]:
    """List available Ollama models (by name)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        if response.status_code != 200:
            raise HTTPException(
                status_code=503,
                detail=f"Ollama not available (status {response.status_code})",
            )
        data = response.json()
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


async def _ollama_pull_model(model_name: str) -> None:
    """
    Pull a model into Ollama using the HTTP API.

    This can take minutes and requires network access in the Ollama container.
    """
    # Avoid multiple concurrent pulls for the same model across requests.
    async with _ollama_pull_lock:
        # Check again under the lock to prevent redundant pulls.
        models = await _ollama_list_models()
        if model_name in models:
            return

        if not OLLAMA_AUTO_PULL:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Ollama model '{model_name}' is not installed. "
                    f"Install it with: docker exec amor-ollama ollama pull {model_name}"
                ),
            )

        logger.warning(f"Ollama model '{model_name}' not installed; attempting to pull it now...")

        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post(
                f"{OLLAMA_BASE_URL}/api/pull",
                json={"name": model_name, "stream": False},
            )

        if response.status_code != 200:
            # Ollama returns useful JSON error strings, keep them for debugging.
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Failed to pull Ollama model '{model_name}' "
                    f"(status {response.status_code}): {response.text}"
                ),
            )


async def _ensure_ollama_ready() -> Dict[str, Any]:
    """Ensure Ollama is reachable and the configured model is installed."""
    models = await _ollama_list_models()
    model_installed = OLLAMA_MODEL in models

    if not model_installed:
        await _ollama_pull_model(OLLAMA_MODEL)
        # Re-check after pull attempt
        models = await _ollama_list_models()
        model_installed = OLLAMA_MODEL in models

    return {
        "ollama_available": True,
        "model_installed": model_installed,
        "models": models,
    }


async def call_ollama(prompt: str, system: Optional[str] = None, max_tokens: int = 2048) -> str:
    """Make direct HTTP call to Ollama API."""
    try:
        # Ensure model is available (and optionally auto-pull it).
        await _ensure_ollama_ready()

        # Increased timeout for model loading and complex prompts
        async with httpx.AsyncClient(timeout=OLLAMA_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "system": system or "",
                    "stream": False,
                    "options": {
                        "num_predict": max_tokens,
                        "temperature": 0.7,
                    }
                }
            )

            if response.status_code == 200:
                result = response.json()
                return result.get("response", "")
            else:
                # If the model isn't installed, Ollama responds with 404 and a helpful message.
                if response.status_code == 404 and "model" in response.text.lower():
                    logger.error(f"Ollama model not found: {response.text}")
                    # Try one more time after pulling (if enabled); _ensure_ollama_ready handles messaging.
                    await _ensure_ollama_ready()
                    retry = await client.post(
                        f"{OLLAMA_BASE_URL}/api/generate",
                        json={
                            "model": OLLAMA_MODEL,
                            "prompt": prompt,
                            "system": system or "",
                            "stream": False,
                            "options": {
                                "num_predict": max_tokens,
                                "temperature": 0.7,
                            },
                        },
                    )
                    if retry.status_code == 200:
                        result = retry.json()
                        return result.get("response", "")

                logger.error(f"Ollama API error: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=503,
                    detail=f"Ollama API error: {response.status_code} - {response.text}",
                )

    except httpx.TimeoutException:
        logger.error("Ollama API timeout")
        raise HTTPException(status_code=504, detail="Ollama API timeout")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ollama API call failed: {e}")
        raise HTTPException(status_code=500, detail=f"Ollama API call failed: {str(e)}")


async def check_ollama_health() -> bool:
    """
    Check if Ollama is available and the configured model exists.

    This delegates to _ensure_ollama_ready so that we respect the
    OLLAMA_AUTO_PULL behaviour and keep the health logic in one place.
    """
    try:
        status = await _ensure_ollama_ready()
        return bool(status.get("ollama_available") and status.get("model_installed"))
    except HTTPException as e:
        logger.warning(f"Ollama health check failed: {e.detail}")
        return False
    except Exception as e:
        logger.warning(f"Ollama health check failed: {e}")
        return False


# Research-specific health probe
@router.get("/research/health")
async def research_health():
    """
    Health check for the Local AI research workflow.

    This ensures:
    - Ollama is reachable and the configured model is installed
    - The translation API is reachable (for language detection/translation)
    """
    ollama_ok = False
    translation_ok = False
    details: Dict[str, Any] = {}

    try:
        status = await _ensure_ollama_ready()
        ollama_ok = bool(status.get("ollama_available") and status.get("model_installed"))
        details["ollama"] = status
    except HTTPException as e:
        logger.warning(f"Research health: Ollama not ready: {e.detail}")
        details["ollama_error"] = e.detail
    except Exception as e:
        logger.warning(f"Research health: Ollama check failed: {e}")
        details["ollama_error"] = str(e)

    # Best-effort check of translation API: reuse the detect endpoint with a tiny sample.
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "http://localhost:8000/api/translate/detect",
                json={"text": "Hello world"},
            )
        translation_ok = resp.status_code == 200
        if not translation_ok:
            details["translation_error"] = f"detect endpoint returned {resp.status_code}"
    except Exception as e:
        logger.debug(f"Research health: translation detect failed: {e}")
        details["translation_error"] = str(e)

    overall_ok = ollama_ok and translation_ok
    return {
        "status": "healthy" if overall_ok else "degraded",
        "ollama_ok": ollama_ok,
        "translation_ok": translation_ok,
        "ollama_model": OLLAMA_MODEL,
        "ollama_url": OLLAMA_BASE_URL,
        "details": details,
        "timestamp": datetime.utcnow().isoformat(),
    }


# Health Check
@router.get("/health")
async def health_check():
    """Check health of local AI components with detailed Ollama status."""
    try:
        status = await _ensure_ollama_ready()
        healthy = bool(status.get("ollama_available") and status.get("model_installed"))

        return {
            "status": "healthy" if healthy else "degraded",
            "ollama_status": "healthy" if healthy else "degraded",
            "ollama_url": OLLAMA_BASE_URL,
            "ollama_model": OLLAMA_MODEL,
            "ollama_auto_pull": OLLAMA_AUTO_PULL,
            "ollama_available": status.get("ollama_available", False),
            "model_installed": status.get("model_installed", False),
            "models": status.get("models", []),
            "timestamp": datetime.utcnow().isoformat(),
        }
    except HTTPException as e:
        logger.error(f"Ollama health check failed: {e.detail}")
        return {
            "status": "degraded",
            "ollama_status": "unhealthy",
            "error": e.detail,
            "ollama_url": OLLAMA_BASE_URL,
            "ollama_model": OLLAMA_MODEL,
            "ollama_auto_pull": OLLAMA_AUTO_PULL,
            "timestamp": datetime.utcnow().isoformat(),
        }


@router.get("/models")
async def list_ollama_models():
    """
    Return the list of Ollama models as seen from the application.

    Useful for debugging mismatches between OLLAMA_MODEL and the tags
    actually installed inside the Ollama service.
    """
    try:
        models = await _ollama_list_models()
        return {
            "models": models,
            "ollama_url": OLLAMA_BASE_URL,
        }
    except HTTPException:
        # Bubble up HTTPExceptions from the underlying helper so the client
        # receives the same status code and message.
        raise
    except Exception as e:
        logger.error(f"Failed to list Ollama models: {e}")
        raise HTTPException(status_code=503, detail=f"Failed to list Ollama models: {e}")


# Research Endpoints
@router.post("/research", response_model=LocalAIResearchResponse)
async def start_research(
    request: LocalAIResearchRequest,
    background_tasks: BackgroundTasks,
):
    """Start autonomous research on a topic using Ollama."""
    # Ensure Ollama is reachable and the configured model is available (or can be auto-pulled).
    try:
        status = await _ensure_ollama_ready()
        if not status.get("model_installed"):
            # _ensure_ollama_ready should normally either install the model (when auto-pull is
            # enabled) or raise a detailed HTTPException. If we still end up here with
            # model_installed == False, surface a clear 503 to the client.
            raise HTTPException(
                status_code=503,
                detail=f"Ollama model '{OLLAMA_MODEL}' is not installed or could not be prepared.",
            )
    except HTTPException:
        # Preserve status code and message for caller (likely 503 with detailed info).
        raise
    except Exception as e:
        logger.error(f"Failed to ensure Ollama is ready before starting research: {e}")
        raise HTTPException(status_code=503, detail="Ollama service not available")

    try:
        # Create session
        session_id = str(uuid4())

        # Initialize session tracking
        research_sessions[session_id] = {
            "session_id": session_id,
            "topic": request.topic,
            "depth": request.depth,
            "status": "started",
            "progress": 0,
            "current_agent": None,
            "current_task": "Initializing research",
            "started_at": datetime.utcnow().isoformat(),
        }
        await _persist_session(session_id, research_sessions[session_id])

        # Start research in background
        background_tasks.add_task(
            execute_research,
            session_id,
            request,
        )

        return LocalAIResearchResponse(
            success=True,
            session_id=session_id,
            message="Research started"
        )

    except Exception as e:
        logger.error(f"Failed to start research: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def execute_research(session_id: str, request: LocalAIResearchRequest):
    """Execute research workflow with web scraping."""
    try:
        session = research_sessions[session_id]

        depth = (request.depth or "standard").strip().lower()
        if depth not in {"quick", "standard", "deep"}:
            depth = "standard"

        # Update status
        session["status"] = "in_progress"
        session["progress"] = 5
        session["current_agent"] = "Research Specialist"
        session["current_task"] = "Planning research"
        await _persist_session(session_id, session)

        # QUICK MODE: keep it fast for CPU-only environments by using a minimal flow.
        if depth == "quick":
            session["progress"] = 10
            session["current_task"] = "Finding sources"
            await _persist_session(session_id, session)

            # Use simple, deterministic queries to avoid an extra LLM call.
            search_queries = [
                request.topic,
                f"{request.topic} overview",
            ]

            # Search and gather URLs
            all_search_results: List[Dict[str, str]] = []
            for query in search_queries:
                results = await search_web(query, max_results=2)
                all_search_results.extend(results)
                await asyncio.sleep(1)

            # De-dupe URLs
            seen_urls = set()
            unique_results = []
            for result in all_search_results:
                url = result.get("url")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    unique_results.append(result)

            session["progress"] = 25
            session["current_task"] = f"Scraping {min(len(unique_results), 4)} sources"
            await _persist_session(session_id, session)

            scraped_content = await scrape_multiple_urls(
                [r["url"] for r in unique_results[:4]],
                max_concurrent=2,
            )

            # Translation step (if enabled)
            translated_any = False
            if request.use_translation and scraped_content:
                session["progress"] = 35
                session["current_task"] = "Translating non-English sources"
                await _persist_session(session_id, session)
                scraped_content = await translate_scraped_content(
                    scraped_content,
                    target_lang=request.target_language,
                    session=session
                )
                translated_any = any(c.get("translated", False) for c in scraped_content)

            session["progress"] = 45
            session["current_task"] = "Summarizing sources"
            await _persist_session(session_id, session)

            web_context = ""
            if scraped_content:
                web_context = "\n\n=== WEB SOURCES ===\n"
                for i, content in enumerate(scraped_content[:4], 1):
                    lang_info = ""
                    if content.get("translated"):
                        lang_info = f" [Translated from {content.get('original_language', 'unknown')}]"
                    web_context += f"\nSource {i} ({content['url']}){lang_info}:\n{content['content'][:900]}\n"

            final_prompt = f"""You are a research assistant. Using the web sources below, produce a quick research report.

Topic: {request.topic}

{web_context}

Return:
1) Summary (2-4 short paragraphs)
2) Key Findings (5-8 bullets)
3) Analysis (short, practical)
4) Confidence (0-100%)
"""

            final_system = "You are a concise research assistant who summarizes sources and highlights key points."
            report = await call_ollama(final_prompt, final_system, max_tokens=700)

            # Extract key findings (simple parsing)
            findings_list: List[str] = []
            for line in report.split("\n"):
                s = line.strip()
                if not s:
                    continue
                if any(s.startswith(prefix) for prefix in ["- ", "• ", "* ", "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8."]):
                    findings_list.append(s.lstrip("-•* 123456789.").strip())

            # Extract confidence (simple regex)
            confidence = 75
            match = re.search(r"(\d{1,3})\s*%", report)
            if match:
                try:
                    confidence = max(0, min(100, int(match.group(1))))
                except ValueError:
                    confidence = 75

            # Summary = first paragraph-ish
            summary = ""
            for chunk in report.split("\n\n"):
                chunk = chunk.strip()
                if chunk:
                    summary = chunk
                    break

            session["status"] = "completed"
            session["progress"] = 100
            session["current_agent"] = None
            session["current_task"] = None
            session["summary"] = summary
            session["findings"] = findings_list[:10]
            session["analysis"] = report.strip()

            # Build sources list with translation info
            sources_list = []
            for c in scraped_content[:10]:
                source_info = {
                    "url": c["url"],
                    "title": urlparse(c["url"]).netloc
                }
                if c.get("translated"):
                    source_info["translated"] = True
                    source_info["original_language"] = c.get("original_language", "unknown")
                sources_list.append(source_info)
            sources_list.append({"url": OLLAMA_BASE_URL, "title": f"Analysis by {OLLAMA_MODEL}"})
            
            session["sources"] = sources_list
            session["confidence"] = confidence
            session["translated"] = translated_any
            session["depth"] = depth
            session["completed_at"] = datetime.utcnow().isoformat()
            await _persist_session(session_id, session)

            logger.info(f"Quick research completed for session {session_id} with {len(scraped_content)} sources (translated: {translated_any})")
            return

        # Step 1: Generate search queries
        session["current_task"] = "Generating search queries"
        await _persist_session(session_id, session)
        query_prompt = f"""Generate 2-3 specific web search queries to research this topic:

Topic: {request.topic}

Provide only the search queries, one per line, without numbering or explanation."""

        query_system = "You are a search expert who creates effective search queries."
        search_queries_text = await call_ollama(query_prompt, query_system, max_tokens=256)

        # Parse queries
        search_queries = [q.strip() for q in search_queries_text.split('\n') if q.strip()][:4 if depth == "deep" else 3]

        # Update progress
        session["progress"] = 10
        session["current_task"] = "Searching the web"
        await _persist_session(session_id, session)

        # Step 2: Search the web for each query
        all_search_results = []
        per_query_results = 4 if depth == "deep" else 3
        for query in search_queries:
            results = await search_web(query, max_results=per_query_results)
            all_search_results.extend(results)
            await asyncio.sleep(1)  # Rate limiting

        # Remove duplicates
        seen_urls = set()
        unique_results = []
        for result in all_search_results:
            if result['url'] not in seen_urls:
                seen_urls.add(result['url'])
                unique_results.append(result)

        max_urls = 12 if depth == "deep" else 8
        session["progress"] = 25
        session["current_task"] = f"Scraping {min(len(unique_results), max_urls)} web sources"
        await _persist_session(session_id, session)

        # Step 3: Scrape the URLs
        scraped_content = await scrape_multiple_urls([r['url'] for r in unique_results[:max_urls]], max_concurrent=3)

        # Step 3.5: Translation step (if enabled)
        translated_any = False
        if request.use_translation and scraped_content:
            session["progress"] = 32
            session["current_task"] = "Translating non-English sources"
            await _persist_session(session_id, session)
            scraped_content = await translate_scraped_content(
                scraped_content,
                target_lang=request.target_language,
                session=session
            )
            translated_any = any(c.get("translated", False) for c in scraped_content)
            logger.info(f"Translation step complete: {sum(1 for c in scraped_content if c.get('translated'))} sources translated")

        session["progress"] = 40
        session["current_task"] = "Analyzing scraped content"
        await _persist_session(session_id, session)

        # Step 4: Analyze the topic with web content
        web_context = ""
        if scraped_content:
            web_context = "\n\n=== WEB SOURCES ===\n"
            for i, content in enumerate(scraped_content[:5], 1):
                lang_info = ""
                if content.get("translated"):
                    lang_info = f" [Translated from {content.get('original_language', 'unknown')}]"
                web_context += f"\nSource {i} ({content['url']}){lang_info}:\n{content['content'][:1500]}\n"

        analysis_prompt = f"""You are a research specialist. Analyze this research topic using the provided web sources:

Topic: {request.topic}

{web_context}

Based on the web sources above, provide:
1. A brief summary of what this topic encompasses
2. Key points from the sources
3. 3-5 important research questions that should be answered

Format your response clearly with sections."""

        analysis_system = "You are an expert research analyst who analyzes web sources and breaks down complex topics."
        analysis_tokens = 2048 if depth == "deep" else 1200
        analysis = await call_ollama(analysis_prompt, analysis_system, max_tokens=analysis_tokens)

        # Update progress
        session["progress"] = 55
        session["current_task"] = "Synthesizing research findings"
        await _persist_session(session_id, session)

        # Step 5: Synthesize findings from web sources
        research_prompt = f"""Based on this analysis and the web sources provided:

{analysis}

{web_context}

Now provide comprehensive research findings on the topic: {request.topic}

Include:
1. Key findings and discoveries from the sources
2. Current state of knowledge
3. Important facts and statistics mentioned
4. Recent developments or trends
5. Different perspectives from various sources

Be thorough but concise. Cite which source number supports each point."""

        research_system = "You are a thorough researcher who synthesizes information from multiple sources."
        findings_tokens = 2800 if depth == "deep" else 1400
        findings = await call_ollama(research_prompt, research_system, max_tokens=findings_tokens)

        # Update progress
        session["progress"] = 70
        session["current_agent"] = "Data Analyst"
        session["current_task"] = "Analyzing findings"
        await _persist_session(session_id, session)

        # Step 6: Critical analysis
        synthesis_prompt = f"""Based on these research findings:

{findings}

Provide a critical analysis:
1. What are the most significant insights?
2. What patterns or connections emerge?
3. What are the implications?
4. What questions remain unanswered?
5. Overall confidence level in the findings (0-100%)

Be analytical and objective."""

        synthesis_system = "You are a data analyst who synthesizes research findings into actionable insights."
        synthesis_tokens = 2048 if depth == "deep" else 1000
        synthesis = await call_ollama(synthesis_prompt, synthesis_system, max_tokens=synthesis_tokens)

        # Update progress
        session["progress"] = 85
        session["current_agent"] = "Technical Writer"
        session["current_task"] = "Generating final report"
        await _persist_session(session_id, session)

        # Step 7: Generate executive summary
        summary_prompt = f"""Create a concise executive summary (2-3 paragraphs) of this research on: {request.topic}

Research findings: {findings[:500]}...

Analysis: {synthesis[:500]}...

The summary should be clear, informative, and suitable for a general audience."""

        summary_system = "You are a technical writer who creates clear, concise summaries."
        summary = await call_ollama(summary_prompt, summary_system, max_tokens=512)

        # Extract key findings (simple parsing)
        findings_list = []
        for line in findings.split('\n'):
            line = line.strip()
            if line and any(line.startswith(prefix) for prefix in ['- ', '• ', '* ', '1.', '2.', '3.', '4.', '5.']):
                findings_list.append(line.lstrip('-•* 123456789.').strip())

        # Extract confidence (simple regex/parsing)
        confidence = 75  # Default confidence
        for line in synthesis.split('\n'):
            if 'confidence' in line.lower() and '%' in line:
                try:
                    match = re.search(r'(\d+)%', line)
                    if match:
                        confidence = int(match.group(1))
                        break
                except:
                    pass

        # Mark complete
        session["status"] = "completed"
        session["progress"] = 100
        session["current_agent"] = None
        session["current_task"] = None
        session["summary"] = summary.strip()
        session["findings"] = findings_list[:10]  # Top 10 findings
        session["analysis"] = synthesis.strip()

        # Include actual web sources with translation info
        sources_list = []
        for content in scraped_content[:10]:
            source_info = {
                "url": content['url'],
                "title": urlparse(content['url']).netloc
            }
            if content.get("translated"):
                source_info["translated"] = True
                source_info["original_language"] = content.get("original_language", "unknown")
            sources_list.append(source_info)

        # Add LLM as a source too
        sources_list.append({
            "url": OLLAMA_BASE_URL,
            "title": f"Analysis by {OLLAMA_MODEL}"
        })

        session["sources"] = sources_list
        session["confidence"] = confidence
        session["translated"] = translated_any
        session["depth"] = depth
        session["completed_at"] = datetime.utcnow().isoformat()
        await _persist_session(session_id, session)

        logger.info(f"Research completed for session {session_id} with {len(scraped_content)} sources (translated: {translated_any})")

    except Exception as e:
        logger.error(f"Research failed for session {session_id}: {e}")
        session["status"] = "failed"
        session["error"] = str(e)
        session["progress"] = 0
        await _persist_session(session_id, session)


@router.get("/research/{session_id}/status")
async def get_research_status(session_id: str):
    """Get research session status."""
    session = await _load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    response = {
        "session_id": session_id,
        "status": session["status"],
        "progress": session["progress"],
        "current_agent": session.get("current_agent"),
        "current_task": session.get("current_task"),
    }

    # Include result if completed
    if session["status"] == "completed":
        response.update({
            "summary": session.get("summary", ""),
            "findings": session.get("findings", []),
            "analysis": session.get("analysis", ""),
            "sources": session.get("sources", []),
            "confidence": session.get("confidence", 0),
            "translated": session.get("translated", False),
            "depth": session.get("depth", "standard"),
        })
    elif session["status"] == "failed":
        response["error"] = session.get("error", "Unknown error")

    return response


@router.get("/research/{session_id}")
async def get_research_result(session_id: str):
    """Get completed research result."""
    session = await _load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Research not completed yet (status: {session['status']})")

    return {
        "summary": session.get("summary", ""),
        "findings": session.get("findings", []),
        "analysis": session.get("analysis", ""),
        "sources": session.get("sources", []),
        "confidence": session.get("confidence", 0),
        "translated": session.get("translated", False),
        "depth": session.get("depth", "standard"),
    }


# Ollama Direct Endpoints
@router.post("/ollama/generate")
async def generate_text(
    prompt: str,
    system: Optional[str] = None,
    max_tokens: int = 2048,
):
    """Generate text using Ollama directly."""
    response_text = await call_ollama(prompt, system, max_tokens)
    return {"response": response_text}


@router.get("/ollama/models")
async def list_models():
    """List available Ollama models."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            if response.status_code == 200:
                data = response.json()
                return {"models": data.get("models", [])}
            else:
                raise HTTPException(status_code=500, detail="Failed to list models")
    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Thinking Mode Endpoint
class ThinkingRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="The problem or question to analyze")
    mode: Optional[str] = Field("thinking", description="Mode identifier")
    history: Optional[List[dict]] = Field(None, description="Conversation history")
    max_tokens: int = Field(2048, description="Maximum tokens in response")


@router.post("/thinking")
async def local_thinking(request: ThinkingRequest):
    """
    Deep analytical thinking mode using local Ollama model.

    This endpoint provides:
    - Complex problem-solving
    - Analytical reasoning
    - Step-by-step thinking
    - Critical analysis
    """
    if not await check_ollama_health():
        raise HTTPException(status_code=503, detail="Ollama service not available")

    try:
        system_prompt = """You are an expert analytical thinker and problem solver. Your role is to:

1. Break down complex problems into manageable components
2. Analyze situations from multiple angles
3. Apply logical reasoning and critical thinking
4. Generate insights and novel perspectives
5. Challenge assumptions and identify biases

When responding:
- Think step-by-step through the problem
- Show your reasoning process clearly
- Consider alternative viewpoints
- Identify key insights and implications
- Provide actionable conclusions

Be thorough, logical, and insightful in your analysis."""

        # Build context from history if provided
        context = ""
        if request.history:
            for msg in request.history[-5:]:  # Last 5 messages for context
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                context += f"{role.capitalize()}: {content}\n\n"

        full_prompt = f"{context}User: {request.prompt}\n\nAssistant:"

        response_text = await call_ollama(full_prompt, system_prompt, request.max_tokens)

        return {
            "response": response_text,
            "content": response_text,
            "text": response_text,
            "metadata": {
                "model": OLLAMA_MODEL,
                "mode": "thinking",
                "ollama_url": OLLAMA_BASE_URL
            }
        }

    except Exception as e:
        logger.error(f"Thinking mode error: {e}")
        raise HTTPException(status_code=500, detail=f"Thinking mode failed: {str(e)}")


# Coding Mode Endpoint
class CodingRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="The coding task or question")
    mode: Optional[str] = Field("coding", description="Mode identifier")
    history: Optional[List[dict]] = Field(None, description="Conversation history")
    max_tokens: int = Field(2048, description="Maximum tokens in response")


@router.post("/coding")
async def local_coding(request: CodingRequest):
    """
    Code generation and technical assistance using local Ollama model.

    This endpoint provides:
    - Code generation
    - Debugging assistance
    - Code review and optimization
    - Technical explanations
    """
    if not await check_ollama_health():
        raise HTTPException(status_code=503, detail="Ollama service not available")

    try:
        system_prompt = """You are an expert software engineer and coding assistant. Your role is to:

1. Write clean, efficient, and well-documented code
2. Debug issues and provide clear solutions
3. Review code for best practices and improvements
4. Explain technical concepts clearly
5. Suggest optimizations and alternative approaches

When responding:
- Provide working code examples with proper formatting
- Include helpful comments in the code
- Follow language-specific best practices
- Explain your reasoning and approach
- Consider edge cases and error handling
- Suggest testing approaches when relevant

Be practical, precise, and educational in your responses."""

        # Build context from history if provided
        context = ""
        if request.history:
            for msg in request.history[-5:]:  # Last 5 messages for context
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                context += f"{role.capitalize()}: {content}\n\n"

        full_prompt = f"{context}User: {request.prompt}\n\nAssistant:"

        response_text = await call_ollama(full_prompt, system_prompt, request.max_tokens)

        return {
            "response": response_text,
            "content": response_text,
            "text": response_text,
            "metadata": {
                "model": OLLAMA_MODEL,
                "mode": "coding",
                "ollama_url": OLLAMA_BASE_URL
            }
        }

    except Exception as e:
        logger.error(f"Coding mode error: {e}")
        raise HTTPException(status_code=500, detail=f"Coding mode failed: {str(e)}")


# Initialization functions for main.py
async def initialize_local_ai(**kwargs):
    """Initialize local AI - simplified version just checks Ollama."""
    logger.info("Initializing simplified local AI system...")

    try:
        models = await _ollama_list_models()
        if OLLAMA_MODEL in models:
            logger.info("Ollama is available and model is installed")
        else:
            logger.warning(
                f"Ollama is available but model '{OLLAMA_MODEL}' is not installed. "
                f"{'Will attempt auto-pull on first request.' if OLLAMA_AUTO_PULL else 'Auto-pull disabled.'}"
            )
    except Exception as e:
        logger.warning(f"Ollama is not available - local AI endpoints will not work: {e}")

    logger.info("Simplified local AI system ready")


async def cleanup_local_ai():
    """Cleanup local AI resources - nothing to clean up in simplified version."""
    logger.info("Local AI system cleaned up")
