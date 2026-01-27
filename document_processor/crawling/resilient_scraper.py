"""
Resilient Async Scraper with Advanced Error Handling.

Implements production-grade web scraping with:
- Decorrelated jitter backoff for retries
- Per-domain circuit breakers
- Proxy rotation
- Adaptive timeout configuration
- Concurrent request management
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any, Set, Callable, Awaitable
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientTimeout, ClientError, ClientConnectorError
from bs4 import BeautifulSoup
import trafilatura

from ..reliability.circuit_breaker import CircuitBreaker, CircuitState

logger = logging.getLogger(__name__)


@dataclass
class ScraperConfig:
    """Configuration for the resilient scraper."""
    # Concurrency
    max_concurrent_requests: int = 50
    max_concurrent_per_domain: int = 5
    
    # Timeouts
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    total_timeout: float = 60.0
    
    # Retry configuration with decorrelated jitter
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 60.0
    
    # Circuit breaker
    circuit_failure_threshold: int = 5
    circuit_recovery_timeout: float = 60.0
    
    # Proxy configuration
    proxy_rotation_enabled: bool = False
    proxy_list: List[str] = field(default_factory=list)
    proxy_rotation_on_failure: bool = True
    
    # User agent rotation
    user_agent_rotation: bool = True
    user_agents: List[str] = field(default_factory=lambda: [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
    ])
    
    # Content extraction
    extract_with_trafilatura: bool = True
    fallback_to_beautifulsoup: bool = True
    min_content_length: int = 100


class RequestResult(Enum):
    """Result types for requests."""
    SUCCESS = "success"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    HTTP_ERROR = "http_error"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    CIRCUIT_OPEN = "circuit_open"
    EXTRACTION_ERROR = "extraction_error"
    UNKNOWN_ERROR = "unknown_error"


@dataclass
class ScrapeResult:
    """Result of a scrape operation."""
    url: str
    result: RequestResult
    status_code: Optional[int] = None
    content: Optional[str] = None
    title: Optional[str] = None
    text: Optional[str] = None
    links: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    response_time: float = 0.0
    retry_count: int = 0
    error_message: Optional[str] = None
    bytes_downloaded: int = 0
    
    @property
    def success(self) -> bool:
        return self.result == RequestResult.SUCCESS


@dataclass 
class ScraperStats:
    """Statistics for the scraper."""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    retried_requests: int = 0
    circuit_breaker_trips: int = 0
    proxy_rotations: int = 0
    total_bytes_downloaded: int = 0
    total_response_time: float = 0.0
    requests_by_status: Dict[int, int] = field(default_factory=dict)
    errors_by_type: Dict[str, int] = field(default_factory=dict)


class DecorrelatedJitterBackoff:
    """
    Implements decorrelated jitter backoff algorithm.
    
    Formula: sleep = min(cap, random(base, sleep * 3))
    
    This algorithm prevents synchronized retries (thundering herd)
    by randomizing sleep times while still respecting exponential backoff.
    """
    
    def __init__(
        self,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
    ):
        """
        Initialize backoff calculator.
        
        Args:
            base_delay: Minimum delay in seconds
            max_delay: Maximum delay (cap) in seconds
        """
        self.base = base_delay
        self.cap = max_delay
        self._current_sleep = base_delay
    
    def get_next_delay(self) -> float:
        """
        Calculate next sleep duration using decorrelated jitter.
        
        Returns:
            Sleep duration in seconds
        """
        # Decorrelated jitter formula: sleep = min(cap, random(base, sleep * 3))
        self._current_sleep = min(
            self.cap,
            random.uniform(self.base, self._current_sleep * 3)
        )
        return self._current_sleep
    
    def reset(self):
        """Reset backoff state."""
        self._current_sleep = self.base


class DomainCircuitBreaker:
    """
    Per-domain circuit breaker to prevent hammering failing servers.
    
    States:
    - CLOSED: Normal operation
    - OPEN: All requests fail fast
    - HALF_OPEN: Testing if service recovered
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        
        self._breakers: Dict[str, CircuitBreaker] = {}
    
    def get_breaker(self, domain: str) -> CircuitBreaker:
        """Get or create circuit breaker for domain."""
        if domain not in self._breakers:
            self._breakers[domain] = CircuitBreaker(
                failure_threshold=self.failure_threshold,
                recovery_timeout=int(self.recovery_timeout),
            )
        return self._breakers[domain]
    
    def is_open(self, domain: str) -> bool:
        """Check if circuit is open for domain."""
        breaker = self.get_breaker(domain)
        return breaker.get_state() == CircuitState.OPEN
    
    async def record_success(self, domain: str):
        """Record successful request."""
        breaker = self.get_breaker(domain)
        await breaker._on_success(domain)
    
    async def record_failure(self, domain: str):
        """Record failed request."""
        breaker = self.get_breaker(domain)
        await breaker._on_failure(domain)
    
    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get stats for all breakers."""
        return {
            domain: breaker.get_stats()
            for domain, breaker in self._breakers.items()
        }


class ProxyRotator:
    """
    Manages proxy rotation for distributed scraping.
    
    Features:
    - Round-robin proxy selection
    - Proxy health tracking
    - Automatic removal of dead proxies
    """
    
    def __init__(self, proxies: List[str]):
        """
        Initialize proxy rotator.
        
        Args:
            proxies: List of proxy URLs (e.g., "http://host:port")
        """
        self.proxies = list(proxies)
        self._index = 0
        self._failures: Dict[str, int] = {}
        self._disabled: Set[str] = set()
        self._lock = asyncio.Lock()
    
    async def get_next_proxy(self) -> Optional[str]:
        """Get next available proxy."""
        async with self._lock:
            if not self.proxies:
                return None
            
            available = [p for p in self.proxies if p not in self._disabled]
            if not available:
                # Reset disabled proxies if all are disabled
                self._disabled.clear()
                available = self.proxies
            
            self._index = (self._index + 1) % len(available)
            return available[self._index]
    
    async def report_failure(self, proxy: str, max_failures: int = 3):
        """Report a proxy failure."""
        async with self._lock:
            self._failures[proxy] = self._failures.get(proxy, 0) + 1
            
            if self._failures[proxy] >= max_failures:
                self._disabled.add(proxy)
                logger.warning(f"Proxy disabled due to failures: {proxy}")
    
    async def report_success(self, proxy: str):
        """Report a proxy success."""
        async with self._lock:
            self._failures[proxy] = 0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get proxy stats."""
        return {
            "total_proxies": len(self.proxies),
            "disabled_proxies": len(self._disabled),
            "failures": dict(self._failures),
        }


class ResilientScraper:
    """
    Production-grade async scraper with advanced resilience patterns.
    
    Features:
    - Decorrelated jitter backoff for intelligent retries
    - Per-domain circuit breakers
    - Proxy rotation
    - Concurrent request management
    - Adaptive timeouts
    - Content extraction with Trafilatura
    """
    
    def __init__(self, config: Optional[ScraperConfig] = None):
        """
        Initialize the resilient scraper.
        
        Args:
            config: Scraper configuration
        """
        self.config = config or ScraperConfig()
        
        # Rate limiting
        self._global_semaphore = asyncio.Semaphore(self.config.max_concurrent_requests)
        self._domain_semaphores: Dict[str, asyncio.Semaphore] = {}
        
        # Circuit breakers
        self._circuit_breakers = DomainCircuitBreaker(
            failure_threshold=self.config.circuit_failure_threshold,
            recovery_timeout=self.config.circuit_recovery_timeout,
        )
        
        # Proxy rotation
        self._proxy_rotator: Optional[ProxyRotator] = None
        if self.config.proxy_rotation_enabled and self.config.proxy_list:
            self._proxy_rotator = ProxyRotator(self.config.proxy_list)
        
        # Session
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Statistics
        self.stats = ScraperStats()
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            timeout = ClientTimeout(
                connect=self.config.connect_timeout,
                sock_read=self.config.read_timeout,
                total=self.config.total_timeout,
            )
            
            connector = aiohttp.TCPConnector(
                limit=self.config.max_concurrent_requests,
                limit_per_host=self.config.max_concurrent_per_domain,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )
            
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
            )
        
        return self._session
    
    async def close(self):
        """Close the scraper and release resources."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    def _get_domain(self, url: str) -> str:
        """Extract domain from URL."""
        return urlparse(url).netloc.lower()
    
    def _get_user_agent(self) -> str:
        """Get a user agent string."""
        if self.config.user_agent_rotation:
            return random.choice(self.config.user_agents)
        return self.config.user_agents[0]
    
    def _get_domain_semaphore(self, domain: str) -> asyncio.Semaphore:
        """Get semaphore for a domain."""
        if domain not in self._domain_semaphores:
            self._domain_semaphores[domain] = asyncio.Semaphore(
                self.config.max_concurrent_per_domain
            )
        return self._domain_semaphores[domain]
    
    async def _fetch_with_retry(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> ScrapeResult:
        """
        Fetch URL with retry logic and decorrelated jitter backoff.
        
        Args:
            url: URL to fetch
            headers: Optional headers
            
        Returns:
            Scrape result
        """
        domain = self._get_domain(url)
        backoff = DecorrelatedJitterBackoff(
            base_delay=self.config.retry_base_delay,
            max_delay=self.config.retry_max_delay,
        )
        
        result = ScrapeResult(url=url, result=RequestResult.UNKNOWN_ERROR)
        
        for attempt in range(self.config.max_retries + 1):
            if attempt > 0:
                result.retry_count = attempt
                self.stats.retried_requests += 1
                
                # Calculate delay with decorrelated jitter
                delay = backoff.get_next_delay()
                logger.debug(f"Retry {attempt} for {url}, waiting {delay:.2f}s")
                await asyncio.sleep(delay)
            
            # Check circuit breaker
            if self._circuit_breakers.is_open(domain):
                result.result = RequestResult.CIRCUIT_OPEN
                result.error_message = f"Circuit breaker open for {domain}"
                self.stats.circuit_breaker_trips += 1
                return result
            
            try:
                result = await self._do_fetch(url, headers, domain)
                
                if result.success:
                    await self._circuit_breakers.record_success(domain)
                    return result
                
                # Record failure for circuit breaker (except rate limits)
                if result.result not in (RequestResult.RATE_LIMITED,):
                    await self._circuit_breakers.record_failure(domain)
                
                # Don't retry on certain errors
                if result.result in (RequestResult.BLOCKED, RequestResult.HTTP_ERROR):
                    if result.status_code and result.status_code < 500:
                        return result
                
            except Exception as e:
                result.result = RequestResult.UNKNOWN_ERROR
                result.error_message = str(e)
                await self._circuit_breakers.record_failure(domain)
                logger.error(f"Unexpected error fetching {url}: {e}")
        
        return result
    
    async def _do_fetch(
        self,
        url: str,
        headers: Optional[Dict[str, str]],
        domain: str,
    ) -> ScrapeResult:
        """
        Perform actual HTTP fetch.
        
        Args:
            url: URL to fetch
            headers: Optional headers
            domain: Domain for the URL
            
        Returns:
            Scrape result
        """
        result = ScrapeResult(url=url, result=RequestResult.UNKNOWN_ERROR)
        start_time = time.time()
        
        session = await self._get_session()
        
        # Build headers
        request_headers = {
            "User-Agent": self._get_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if headers:
            request_headers.update(headers)
        
        # Get proxy if enabled
        proxy = None
        if self._proxy_rotator:
            proxy = await self._proxy_rotator.get_next_proxy()
            if proxy:
                self.stats.proxy_rotations += 1
        
        try:
            async with self._global_semaphore:
                async with self._get_domain_semaphore(domain):
                    async with session.get(
                        url,
                        headers=request_headers,
                        proxy=proxy,
                        allow_redirects=True,
                        ssl=False,  # For broader compatibility
                    ) as response:
                        result.status_code = response.status
                        result.response_time = time.time() - start_time
                        
                        # Update stats
                        self.stats.total_requests += 1
                        self.stats.total_response_time += result.response_time
                        self.stats.requests_by_status[response.status] = \
                            self.stats.requests_by_status.get(response.status, 0) + 1
                        
                        # Handle different status codes
                        if response.status == 429:
                            result.result = RequestResult.RATE_LIMITED
                            result.error_message = "Rate limited"
                            return result
                        
                        if response.status == 403:
                            result.result = RequestResult.BLOCKED
                            result.error_message = "Access forbidden"
                            return result
                        
                        if response.status >= 400:
                            result.result = RequestResult.HTTP_ERROR
                            result.error_message = f"HTTP {response.status}"
                            self.stats.failed_requests += 1
                            return result
                        
                        # Read content
                        content = await response.text()
                        result.content = content
                        result.bytes_downloaded = len(content.encode('utf-8'))
                        self.stats.total_bytes_downloaded += result.bytes_downloaded
                        
                        # Report proxy success
                        if proxy and self._proxy_rotator:
                            await self._proxy_rotator.report_success(proxy)
                        
                        # Extract text content
                        try:
                            extracted = await self._extract_content(content, url)
                            result.title = extracted.get("title")
                            result.text = extracted.get("text")
                            result.links = extracted.get("links", [])
                            result.metadata = extracted.get("metadata", {})
                            
                            # Check minimum content length
                            if result.text and len(result.text) >= self.config.min_content_length:
                                result.result = RequestResult.SUCCESS
                                self.stats.successful_requests += 1
                            else:
                                result.result = RequestResult.EXTRACTION_ERROR
                                result.error_message = "Content too short"
                                self.stats.failed_requests += 1
                                
                        except Exception as e:
                            result.result = RequestResult.EXTRACTION_ERROR
                            result.error_message = f"Extraction failed: {e}"
                            self.stats.failed_requests += 1
                        
                        return result
                        
        except asyncio.TimeoutError:
            result.result = RequestResult.TIMEOUT
            result.error_message = "Request timed out"
            result.response_time = time.time() - start_time
            self.stats.failed_requests += 1
            self.stats.errors_by_type["timeout"] = self.stats.errors_by_type.get("timeout", 0) + 1
            
            # Report proxy failure on timeout
            if proxy and self._proxy_rotator:
                await self._proxy_rotator.report_failure(proxy)
            
        except ClientConnectorError as e:
            result.result = RequestResult.CONNECTION_ERROR
            result.error_message = f"Connection error: {e}"
            result.response_time = time.time() - start_time
            self.stats.failed_requests += 1
            self.stats.errors_by_type["connection"] = self.stats.errors_by_type.get("connection", 0) + 1
            
            if proxy and self._proxy_rotator:
                await self._proxy_rotator.report_failure(proxy)
            
        except ClientError as e:
            result.result = RequestResult.UNKNOWN_ERROR
            result.error_message = f"Client error: {e}"
            result.response_time = time.time() - start_time
            self.stats.failed_requests += 1
            self.stats.errors_by_type["client"] = self.stats.errors_by_type.get("client", 0) + 1
        
        return result
    
    async def _extract_content(
        self,
        html: str,
        url: str,
    ) -> Dict[str, Any]:
        """
        Extract content from HTML using Trafilatura with BeautifulSoup fallback.
        
        Args:
            html: HTML content
            url: Source URL
            
        Returns:
            Extracted content dictionary
        """
        result = {
            "title": None,
            "text": None,
            "links": [],
            "metadata": {},
        }
        
        # Try Trafilatura first
        if self.config.extract_with_trafilatura:
            try:
                extracted = trafilatura.bare_extraction(
                    html,
                    url=url,
                    include_links=True,
                    include_images=False,
                    include_tables=True,
                )
                
                if extracted:
                    result["title"] = extracted.get("title")
                    result["text"] = extracted.get("text")
                    result["links"] = extracted.get("links", []) or []
                    result["metadata"] = {
                        "author": extracted.get("author"),
                        "date": extracted.get("date"),
                        "sitename": extracted.get("sitename"),
                        "language": extracted.get("language"),
                    }
                    
                    if result["text"]:
                        return result
                        
            except Exception as e:
                logger.debug(f"Trafilatura extraction failed: {e}")
        
        # Fallback to BeautifulSoup
        if self.config.fallback_to_beautifulsoup:
            try:
                soup = BeautifulSoup(html, "lxml")
                
                # Remove unwanted elements
                for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                    tag.decompose()
                
                # Extract title
                title_tag = soup.find("title")
                result["title"] = title_tag.get_text(strip=True) if title_tag else None
                
                # Extract main text
                main_content = soup.find("main") or soup.find("article") or soup.find("body")
                if main_content:
                    result["text"] = main_content.get_text(separator="\n", strip=True)
                
                # Extract links
                result["links"] = [
                    a.get("href") for a in soup.find_all("a", href=True)
                    if a.get("href", "").startswith(("http", "/"))
                ]
                
            except Exception as e:
                logger.debug(f"BeautifulSoup extraction failed: {e}")
        
        return result
    
    async def scrape(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> ScrapeResult:
        """
        Scrape a single URL.
        
        Args:
            url: URL to scrape
            headers: Optional custom headers
            
        Returns:
            Scrape result
        """
        return await self._fetch_with_retry(url, headers)
    
    async def scrape_batch(
        self,
        urls: List[str],
        max_concurrent: Optional[int] = None,
    ) -> List[ScrapeResult]:
        """
        Scrape multiple URLs concurrently.
        
        Args:
            urls: List of URLs to scrape
            max_concurrent: Override max concurrent requests
            
        Returns:
            List of scrape results
        """
        if max_concurrent:
            semaphore = asyncio.Semaphore(max_concurrent)
            
            async def limited_scrape(url: str) -> ScrapeResult:
                async with semaphore:
                    return await self.scrape(url)
            
            tasks = [limited_scrape(url) for url in urls]
        else:
            tasks = [self.scrape(url) for url in urls]
        
        return await asyncio.gather(*tasks, return_exceptions=False)
    
    def get_stats(self) -> ScraperStats:
        """Get scraper statistics."""
        return self.stats
    
    def get_circuit_breaker_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get circuit breaker stats."""
        return self._circuit_breakers.get_stats()
    
    def get_proxy_stats(self) -> Optional[Dict[str, Any]]:
        """Get proxy rotation stats."""
        if self._proxy_rotator:
            return self._proxy_rotator.get_stats()
        return None
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
