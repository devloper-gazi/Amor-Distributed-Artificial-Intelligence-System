"""
Autonomous Web Scraper with Playwright and Trafilatura
Ethical scraping with robots.txt compliance and rate limiting
"""

import asyncio
import logging
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, urljoin
from datetime import datetime
import hashlib

import httpx
import trafilatura
from playwright.async_api import async_playwright, Browser, Page
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class AutonomousScraper:
    """
    High-quality web scraping with Trafilatura and Playwright fallback.
    Implements ethical scraping with robots.txt compliance.
    """

    def __init__(
        self,
        user_agent: str = "ClaudeResearchBot/1.0 (+https://github.com/yourorg/research)",
        delay_between_requests: float = 2.0,
        timeout: int = 30,
        headless: bool = True,
    ):
        """
        Initialize autonomous scraper.

        Args:
            user_agent: Descriptive user agent string
            delay_between_requests: Delay in seconds (ethical scraping)
            timeout: Request timeout in seconds
            headless: Run browser in headless mode
        """
        self.user_agent = user_agent
        self.delay = delay_between_requests
        self.timeout = timeout
        self.headless = headless

        self.browser: Optional[Browser] = None
        self.last_request_time: Dict[str, float] = {}

    async def _ensure_browser(self):
        """Ensure Playwright browser is initialized."""
        if not self.browser:
            playwright = await async_playwright().start()
            self.browser = await playwright.chromium.launch(
                headless=self.headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                ]
            )
            logger.info("Playwright browser initialized")

    async def _check_robots_txt(self, url: str) -> bool:
        """
        Check if URL is allowed by robots.txt.

        Args:
            url: URL to check

        Returns:
            True if allowed, False if disallowed
        """
        try:
            parsed = urlparse(url)
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    robots_url,
                    headers={"User-Agent": self.user_agent},
                    timeout=5.0,
                    follow_redirects=True,
                )

                if response.status_code == 200:
                    # Basic robots.txt parsing
                    content = response.text.lower()
                    user_agent_section = False

                    for line in content.split('\n'):
                        line = line.strip()

                        if line.startswith('user-agent:'):
                            agent = line.split(':', 1)[1].strip()
                            user_agent_section = agent == '*' or 'bot' in agent

                        elif user_agent_section and line.startswith('disallow:'):
                            path = line.split(':', 1)[1].strip()
                            if path and parsed.path.startswith(path):
                                logger.warning(f"URL {url} disallowed by robots.txt")
                                return False

            return True

        except Exception as e:
            logger.warning(f"Could not check robots.txt: {e}, proceeding anyway")
            return True

    async def _rate_limit(self, domain: str):
        """Enforce rate limiting per domain."""
        last_request = self.last_request_time.get(domain, 0)
        elapsed = asyncio.get_event_loop().time() - last_request

        if elapsed < self.delay:
            wait_time = self.delay - elapsed
            logger.debug(f"Rate limiting: waiting {wait_time:.2f}s for {domain}")
            await asyncio.sleep(wait_time)

        self.last_request_time[domain] = asyncio.get_event_loop().time()

    async def scrape_url(self, url: str, force_playwright: bool = False) -> Dict[str, Any]:
        """
        Scrape URL content using Trafilatura with Playwright fallback.

        Args:
            url: URL to scrape
            force_playwright: Skip HTTP attempt and use Playwright

        Returns:
            Dict with extracted content and metadata
        """
        try:
            # Check robots.txt
            if not await self._check_robots_txt(url):
                return {
                    "success": False,
                    "url": url,
                    "error": "Disallowed by robots.txt",
                    "content": None,
                }

            # Rate limiting
            domain = urlparse(url).netloc
            await self._rate_limit(domain)

            html_content = None
            method = "unknown"

            # Try lightweight HTTP first (unless forced)
            if not force_playwright:
                try:
                    logger.info(f"Fetching {url} with HTTP...")
                    async with httpx.AsyncClient(follow_redirects=True) as client:
                        response = await client.get(
                            url,
                            headers={"User-Agent": self.user_agent},
                            timeout=self.timeout,
                        )

                        if response.status_code == 200:
                            html_content = response.text
                            method = "httpx"
                            logger.info(f"HTTP fetch successful for {url}")

                except Exception as e:
                    logger.warning(f"HTTP fetch failed: {e}, falling back to Playwright")

            # Fallback to Playwright for JS-heavy sites
            if not html_content:
                logger.info(f"Using Playwright for {url}...")
                await self._ensure_browser()

                page: Page = await self.browser.new_page()

                try:
                    await page.goto(url, wait_until="networkidle", timeout=self.timeout * 1000)

                    # Wait for dynamic content
                    await page.wait_for_timeout(2000)

                    html_content = await page.content()
                    method = "playwright"
                    logger.info(f"Playwright fetch successful for {url}")

                finally:
                    await page.close()

            # Extract content with Trafilatura
            extracted = trafilatura.bare_extraction(
                html_content,
                include_links=True,
                include_images=True,
                include_tables=True,
                output_format="json",
                url=url,
            )

            if not extracted or not extracted.get("text"):
                logger.warning(f"Trafilatura extraction failed for {url}, using fallback")
                extracted = self._fallback_extraction(html_content, url)

            # Calculate content hash for deduplication
            content_hash = hashlib.sha256(
                extracted.get("text", "").encode()
            ).hexdigest()[:16]

            return {
                "success": True,
                "url": url,
                "title": extracted.get("title", ""),
                "author": extracted.get("author"),
                "date": extracted.get("date"),
                "text": extracted.get("text", ""),
                "links": extracted.get("links", []),
                "images": extracted.get("images", []),
                "language": extracted.get("language"),
                "content_hash": content_hash,
                "method": method,
                "fetched_at": datetime.utcnow().isoformat(),
                "word_count": len(extracted.get("text", "").split()),
            }

        except Exception as e:
            logger.error(f"Scraping failed for {url}: {e}")
            return {
                "success": False,
                "url": url,
                "error": str(e),
                "content": None,
            }

    def _fallback_extraction(self, html: str, url: str) -> Dict[str, Any]:
        """Fallback extraction using BeautifulSoup when Trafilatura fails."""
        try:
            soup = BeautifulSoup(html, "lxml")

            # Remove script and style elements
            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.decompose()

            # Extract text
            text = soup.get_text(separator="\n", strip=True)

            # Get title
            title = soup.find("title")
            title = title.get_text() if title else ""

            # Get meta description
            meta_desc = soup.find("meta", attrs={"name": "description"})
            description = meta_desc.get("content", "") if meta_desc else ""

            return {
                "title": title,
                "text": text,
                "description": description,
                "links": [a.get("href") for a in soup.find_all("a", href=True)],
            }

        except Exception as e:
            logger.error(f"Fallback extraction failed: {e}")
            return {"text": "", "title": ""}

    async def scrape_multiple(self, urls: List[str]) -> List[Dict[str, Any]]:
        """
        Scrape multiple URLs with concurrency control.

        Args:
            urls: List of URLs to scrape

        Returns:
            List of scraping results
        """
        semaphore = asyncio.Semaphore(3)  # Max 3 concurrent requests

        async def scrape_with_limit(url: str):
            async with semaphore:
                return await self.scrape_url(url)

        results = await asyncio.gather(
            *[scrape_with_limit(url) for url in urls],
            return_exceptions=True,
        )

        # Filter exceptions
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Scraping failed for {urls[i]}: {result}")
                processed_results.append({
                    "success": False,
                    "url": urls[i],
                    "error": str(result),
                })
            else:
                processed_results.append(result)

        return processed_results

    async def extract_links(self, url: str, same_domain_only: bool = True) -> List[str]:
        """
        Extract all links from a page.

        Args:
            url: URL to extract links from
            same_domain_only: Only return links from same domain

        Returns:
            List of absolute URLs
        """
        result = await self.scrape_url(url)

        if not result["success"]:
            return []

        links = result.get("links", [])
        base_domain = urlparse(url).netloc

        # Convert to absolute URLs and filter
        absolute_links = []
        for link in links:
            absolute_url = urljoin(url, link)
            parsed = urlparse(absolute_url)

            # Filter criteria
            if parsed.scheme not in ["http", "https"]:
                continue

            if same_domain_only and parsed.netloc != base_domain:
                continue

            absolute_links.append(absolute_url)

        return list(set(absolute_links))  # Deduplicate

    async def close(self):
        """Close browser and cleanup resources."""
        if self.browser:
            await self.browser.close()
            self.browser = None
            logger.info("Scraper browser closed")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()