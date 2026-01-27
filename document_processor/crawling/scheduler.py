"""
Crawl Scheduler with Adaptive Politeness.

Implements intelligent scheduling of crawl jobs with:
- Adaptive politeness based on server response times
- Domain round-robin for fair scheduling
- Priority-based URL selection
- Backpressure handling when storage is congested
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable, Awaitable, Set
from enum import Enum
from collections import defaultdict

from pydantic import BaseModel

from .url_frontier import DistributedURLFrontier, PriorityCalculator

logger = logging.getLogger(__name__)


class SchedulerState(Enum):
    """Scheduler states."""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass
class SchedulerConfig:
    """Configuration for the crawl scheduler."""
    # Worker configuration
    max_concurrent_workers: int = 50
    worker_batch_size: int = 10
    
    # Politeness settings
    default_crawl_delay: float = 1.0
    min_crawl_delay: float = 0.5
    max_crawl_delay: float = 30.0
    politeness_factor: float = 10.0  # α in formula: delay = α × response_time
    
    # Rate limiting
    max_requests_per_second: float = 100.0
    max_requests_per_domain_per_minute: int = 60
    
    # Backpressure
    queue_high_watermark: int = 10000
    queue_low_watermark: int = 1000
    backpressure_delay: float = 5.0
    
    # Timeouts
    url_fetch_timeout: float = 30.0
    idle_timeout: float = 60.0
    
    # Statistics
    stats_interval: float = 10.0


@dataclass
class DomainState:
    """State tracking for a domain."""
    domain: str
    last_crawl_time: float = 0.0
    crawl_delay: float = 1.0
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_response_time: float = 0.0
    last_status_code: int = 0
    consecutive_errors: int = 0
    is_blocked: bool = False
    blocked_until: Optional[float] = None
    
    @property
    def average_response_time(self) -> float:
        if self.successful_requests == 0:
            return 0.0
        return self.total_response_time / self.successful_requests
    
    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.successful_requests / self.total_requests


@dataclass
class SchedulerStats:
    """Statistics for the scheduler."""
    state: SchedulerState = SchedulerState.IDLE
    start_time: Optional[float] = None
    urls_scheduled: int = 0
    urls_completed: int = 0
    urls_failed: int = 0
    active_workers: int = 0
    domains_crawled: int = 0
    total_bytes_downloaded: int = 0
    average_response_time: float = 0.0
    requests_per_second: float = 0.0
    backpressure_events: int = 0


class CrawlScheduler:
    """
    Intelligent crawl scheduler with adaptive politeness.
    
    Features:
    - Adaptive delay based on response times: delay = α × response_time
    - Domain round-robin for fair scheduling
    - Circuit breaker for failing domains
    - Backpressure handling when downstream systems are slow
    - Real-time statistics and monitoring
    """
    
    def __init__(
        self,
        frontier: DistributedURLFrontier,
        config: Optional[SchedulerConfig] = None,
        on_url_ready: Optional[Callable[[str], Awaitable[Dict[str, Any]]]] = None,
    ):
        """
        Initialize the scheduler.
        
        Args:
            frontier: URL frontier instance
            config: Scheduler configuration
            on_url_ready: Callback when URL is ready to be crawled
        """
        self.frontier = frontier
        self.config = config or SchedulerConfig()
        self.on_url_ready = on_url_ready
        
        # State
        self.state = SchedulerState.IDLE
        self.stats = SchedulerStats()
        self.domain_states: Dict[str, DomainState] = {}
        
        # Rate limiting
        self._request_times: List[float] = []
        self._domain_request_times: Dict[str, List[float]] = defaultdict(list)
        
        # Worker management
        self._active_workers: Set[asyncio.Task] = set()
        self._worker_semaphore: Optional[asyncio.Semaphore] = None
        
        # Control
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # Not paused initially
        
        # Backpressure
        self._backpressure_active = False
        
        logger.info(f"Scheduler initialized with config: {self.config}")
    
    async def start(self):
        """Start the scheduler."""
        if self.state == SchedulerState.RUNNING:
            logger.warning("Scheduler is already running")
            return
        
        self.state = SchedulerState.RUNNING
        self.stats.state = SchedulerState.RUNNING
        self.stats.start_time = time.time()
        
        self._stop_event.clear()
        self._worker_semaphore = asyncio.Semaphore(self.config.max_concurrent_workers)
        
        logger.info("Scheduler started")
        
        # Start main loop
        await self._main_loop()
    
    async def stop(self):
        """Stop the scheduler gracefully."""
        if self.state not in (SchedulerState.RUNNING, SchedulerState.PAUSED):
            return
        
        self.state = SchedulerState.STOPPING
        self.stats.state = SchedulerState.STOPPING
        
        logger.info("Stopping scheduler...")
        self._stop_event.set()
        
        # Wait for active workers to complete
        if self._active_workers:
            logger.info(f"Waiting for {len(self._active_workers)} workers to complete...")
            await asyncio.gather(*self._active_workers, return_exceptions=True)
        
        self.state = SchedulerState.STOPPED
        self.stats.state = SchedulerState.STOPPED
        logger.info("Scheduler stopped")
    
    async def pause(self):
        """Pause the scheduler."""
        if self.state != SchedulerState.RUNNING:
            return
        
        self._pause_event.clear()
        self.state = SchedulerState.PAUSED
        self.stats.state = SchedulerState.PAUSED
        logger.info("Scheduler paused")
    
    async def resume(self):
        """Resume the scheduler."""
        if self.state != SchedulerState.PAUSED:
            return
        
        self._pause_event.set()
        self.state = SchedulerState.RUNNING
        self.stats.state = SchedulerState.RUNNING
        logger.info("Scheduler resumed")
    
    async def _main_loop(self):
        """Main scheduler loop."""
        last_stats_time = time.time()
        idle_start = None
        
        while not self._stop_event.is_set():
            # Wait if paused
            await self._pause_event.wait()
            
            # Check backpressure
            if await self._check_backpressure():
                logger.warning("Backpressure active, slowing down")
                await asyncio.sleep(self.config.backpressure_delay)
                continue
            
            # Get next URL from frontier
            url = await self.frontier.get_next_url(timeout=1.0)
            
            if url:
                idle_start = None
                
                # Check rate limits
                await self._enforce_rate_limits(url)
                
                # Spawn worker
                await self._spawn_worker(url)
                
            else:
                # Track idle time
                if idle_start is None:
                    idle_start = time.time()
                elif time.time() - idle_start > self.config.idle_timeout:
                    logger.info("Idle timeout reached, no URLs to crawl")
                    break
                
                await asyncio.sleep(0.1)
            
            # Log stats periodically
            if time.time() - last_stats_time > self.config.stats_interval:
                await self._log_stats()
                last_stats_time = time.time()
        
        logger.info("Main loop exited")
    
    async def _spawn_worker(self, url: str):
        """Spawn a worker to process a URL."""
        async with self._worker_semaphore:
            task = asyncio.create_task(self._worker(url))
            self._active_workers.add(task)
            task.add_done_callback(self._active_workers.discard)
    
    async def _worker(self, url: str):
        """Worker coroutine to process a single URL."""
        domain = self._extract_domain(url)
        start_time = time.time()
        
        self.stats.urls_scheduled += 1
        self.stats.active_workers = len(self._active_workers)
        
        try:
            # Get or create domain state
            if domain not in self.domain_states:
                self.domain_states[domain] = DomainState(domain=domain)
            domain_state = self.domain_states[domain]
            
            # Check if domain is blocked
            if domain_state.is_blocked:
                if domain_state.blocked_until and time.time() < domain_state.blocked_until:
                    logger.debug(f"Domain {domain} is blocked, skipping {url}")
                    await self.frontier.add_url(url, priority=-100)  # Re-queue with low priority
                    return
                else:
                    domain_state.is_blocked = False
                    domain_state.blocked_until = None
            
            # Call the URL handler
            if self.on_url_ready:
                result = await asyncio.wait_for(
                    self.on_url_ready(url),
                    timeout=self.config.url_fetch_timeout,
                )
                
                response_time = time.time() - start_time
                status_code = result.get("status_code", 200)
                bytes_downloaded = result.get("bytes", 0)
                
                # Update domain state
                domain_state.total_requests += 1
                domain_state.last_crawl_time = time.time()
                domain_state.last_status_code = status_code
                
                if 200 <= status_code < 400:
                    domain_state.successful_requests += 1
                    domain_state.total_response_time += response_time
                    domain_state.consecutive_errors = 0
                    
                    self.stats.urls_completed += 1
                    self.stats.total_bytes_downloaded += bytes_downloaded
                    
                    # Update adaptive delay
                    await self._update_adaptive_delay(domain, response_time)
                    
                    # Mark as crawled
                    await self.frontier.mark_crawled(url, success=True)
                    
                elif status_code == 429:  # Rate limited
                    domain_state.failed_requests += 1
                    domain_state.consecutive_errors += 1
                    
                    # Increase delay significantly
                    new_delay = min(
                        domain_state.crawl_delay * 2,
                        self.config.max_crawl_delay,
                    )
                    await self.frontier.set_domain_delay(domain, new_delay)
                    domain_state.crawl_delay = new_delay
                    
                    # Re-queue URL
                    await self.frontier.add_url(url, priority=-50, force=True)
                    
                    logger.warning(f"Rate limited on {domain}, increased delay to {new_delay}s")
                    
                elif status_code >= 500:  # Server error
                    domain_state.failed_requests += 1
                    domain_state.consecutive_errors += 1
                    
                    self.stats.urls_failed += 1
                    
                    # Block domain if too many consecutive errors
                    if domain_state.consecutive_errors >= 5:
                        domain_state.is_blocked = True
                        domain_state.blocked_until = time.time() + 300  # 5 minutes
                        logger.warning(f"Domain {domain} blocked due to errors")
                    
                    await self.frontier.mark_crawled(url, success=False)
                    
                else:  # Client error (4xx)
                    domain_state.failed_requests += 1
                    self.stats.urls_failed += 1
                    await self.frontier.mark_crawled(url, success=False)
            
            else:
                logger.warning("No URL handler configured")
                
        except asyncio.TimeoutError:
            logger.error(f"Timeout crawling {url}")
            self.stats.urls_failed += 1
            if domain in self.domain_states:
                self.domain_states[domain].failed_requests += 1
                self.domain_states[domain].consecutive_errors += 1
            await self.frontier.mark_crawled(url, success=False)
            
        except Exception as e:
            logger.error(f"Error crawling {url}: {e}")
            self.stats.urls_failed += 1
            if domain in self.domain_states:
                self.domain_states[domain].failed_requests += 1
            await self.frontier.mark_crawled(url, success=False)
        
        finally:
            self.stats.active_workers = len(self._active_workers) - 1
            
            # Update requests per second
            self._request_times.append(time.time())
            self._request_times = [t for t in self._request_times if time.time() - t < 1.0]
            self.stats.requests_per_second = len(self._request_times)
    
    async def _update_adaptive_delay(self, domain: str, response_time: float):
        """
        Update crawl delay based on response time.
        
        Formula: delay = α × response_time
        """
        new_delay = self.config.politeness_factor * response_time
        new_delay = max(self.config.min_crawl_delay, min(self.config.max_crawl_delay, new_delay))
        
        await self.frontier.update_domain_delay_from_response(domain, response_time)
        
        if domain in self.domain_states:
            self.domain_states[domain].crawl_delay = new_delay
    
    async def _enforce_rate_limits(self, url: str):
        """Enforce global and per-domain rate limits."""
        domain = self._extract_domain(url)
        current_time = time.time()
        
        # Global rate limit
        self._request_times = [t for t in self._request_times if current_time - t < 1.0]
        
        if len(self._request_times) >= self.config.max_requests_per_second:
            sleep_time = 1.0 - (current_time - self._request_times[0])
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
        
        # Per-domain rate limit
        domain_times = self._domain_request_times[domain]
        self._domain_request_times[domain] = [t for t in domain_times if current_time - t < 60.0]
        
        if len(self._domain_request_times[domain]) >= self.config.max_requests_per_domain_per_minute:
            sleep_time = 60.0 - (current_time - self._domain_request_times[domain][0])
            if sleep_time > 0:
                logger.debug(f"Per-domain rate limit hit for {domain}, sleeping {sleep_time:.1f}s")
                await asyncio.sleep(sleep_time)
        
        self._domain_request_times[domain].append(current_time)
    
    async def _check_backpressure(self) -> bool:
        """Check if backpressure should be applied."""
        queue_size = await self.frontier.get_queue_size()
        
        if queue_size > self.config.queue_high_watermark:
            if not self._backpressure_active:
                self._backpressure_active = True
                self.stats.backpressure_events += 1
            return True
        elif queue_size < self.config.queue_low_watermark:
            self._backpressure_active = False
        
        return False
    
    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL."""
        from urllib.parse import urlparse
        return urlparse(url).netloc.lower()
    
    async def _log_stats(self):
        """Log current statistics."""
        duration = time.time() - (self.stats.start_time or time.time())
        
        logger.info(
            f"Scheduler stats: "
            f"scheduled={self.stats.urls_scheduled}, "
            f"completed={self.stats.urls_completed}, "
            f"failed={self.stats.urls_failed}, "
            f"active_workers={self.stats.active_workers}, "
            f"rps={self.stats.requests_per_second:.1f}, "
            f"bytes={self.stats.total_bytes_downloaded:,}, "
            f"duration={duration:.1f}s"
        )
    
    def get_stats(self) -> SchedulerStats:
        """Get current statistics."""
        return self.stats
    
    def get_domain_stats(self) -> Dict[str, DomainState]:
        """Get per-domain statistics."""
        return self.domain_states
