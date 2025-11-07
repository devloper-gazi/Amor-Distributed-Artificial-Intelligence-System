"""
Web scraper with support for static and JavaScript-rendered pages.
Uses aiohttp for static content and Playwright for dynamic content.
"""

import asyncio
import aiohttp
from typing import AsyncIterator, Dict, Any
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser
from .base import BaseSourceProcessor
from ..core.models import SourceDocument, SourceType
from ..core.exceptions import WebScrapingError
from ..config.logging_config import logger
from ..infrastructure.monitoring import monitor


class WebScraper(BaseSourceProcessor):
    """Web scraper for extracting content from web pages."""

    def __init__(self, settings):
        """Initialize web scraper."""
        super().__init__(settings)
        self.session: aiohttp.ClientSession = None
        self.playwright = None
        self.browser: Browser = None

    async def can_process(self, source: SourceDocument) -> bool:
        """Check if this is a web source."""
        return source.source_type == SourceType.WEB

    async def extract_content(self, source: SourceDocument) -> AsyncIterator[str]:
        """Extract content from web page."""
        if not source.source_url:
            raise WebScrapingError("No URL provided for web source")

        try:
            # Try static scraping first
            async for chunk in self._scrape_static(source.source_url):
                yield chunk
        except Exception as e:
            logger.warning(f"Static scraping failed, trying Playwright: {e}")
            # Fallback to Playwright for JS-rendered pages
            async for chunk in self._scrape_playwright(source.source_url):
                yield chunk

    async def _scrape_static(self, url: str) -> AsyncIterator[str]:
        """Scrape static HTML content."""
        if not self.session:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.settings.web_timeout),
                headers={"User-Agent": self.settings.web_user_agent}
            )

        async with monitor.track_extraction_duration("web"):
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    raise WebScrapingError(f"HTTP {resp.status} for {url}")

                html = await resp.text()
                soup = BeautifulSoup(html, "lxml")

                # Remove unwanted elements
                for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                    tag.decompose()

                # Extract text from main content elements
                for element in soup.find_all(["p", "div", "article", "section", "h1", "h2", "h3", "li"]):
                    text = element.get_text(strip=True)
                    if len(text) > 30:  # Skip very short fragments
                        yield text

    async def _scrape_playwright(self, url: str) -> AsyncIterator[str]:
        """Scrape JavaScript-rendered content using Playwright."""
        if not self.playwright:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.settings.playwright_headless
            )

        page = await self.browser.new_page()

        try:
            await page.goto(url, timeout=self.settings.playwright_timeout, wait_until="networkidle")
            content = await page.content()

            soup = BeautifulSoup(content, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()

            for element in soup.find_all(["p", "div", "article", "section", "h1", "h2"]):
                text = element.get_text(strip=True)
                if len(text) > 30:
                    yield text
        finally:
            await page.close()

    async def get_metadata(self, source: SourceDocument) -> Dict[str, Any]:
        """Extract metadata from web page."""
        if not self.session:
            self.session = aiohttp.ClientSession()

        try:
            async with self.session.get(source.source_url) as resp:
                soup = BeautifulSoup(await resp.text(), "lxml")
                return {
                    "title": soup.title.string if soup.title else None,
                    "status_code": resp.status,
                    "content_type": resp.headers.get("Content-Type"),
                }
        except Exception as e:
            logger.error(f"Failed to get metadata: {e}")
            return {}

    async def cleanup(self):
        """Cleanup resources."""
        if self.session:
            await self.session.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
