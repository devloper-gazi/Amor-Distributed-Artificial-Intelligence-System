"""
Distributed URL Frontier with Redis Backend.

Implements a scalable URL management system with:
- Priority queue using Redis sorted sets
- Bloom filter deduplication for memory-efficient seen URL tracking
- Per-domain queues for politeness enforcement
- Crawl delay tracking per domain

Key formula for Bloom filter false positive rate:
p ≈ (1 - e^(-kn/m))^k

Where:
- n = number of elements inserted
- m = size of bit array
- k = number of hash functions
"""

import asyncio
import hashlib
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Set
from urllib.parse import urlparse
from datetime import datetime

import redis.asyncio as redis
from pydantic import BaseModel

logger = logging.getLogger(__name__)


@dataclass
class URLFrontierStats:
    """Statistics for the URL frontier."""
    total_urls_added: int = 0
    total_urls_crawled: int = 0
    unique_domains: int = 0
    duplicate_urls_skipped: int = 0
    bloom_filter_size: int = 0
    estimated_false_positive_rate: float = 0.0
    queue_depth: int = 0
    active_domains: int = 0


class BloomFilterConfig(BaseModel):
    """Configuration for Bloom filter."""
    expected_items: int = 10_000_000  # 10 million URLs
    false_positive_rate: float = 0.01  # 1% false positive rate
    
    @property
    def optimal_size(self) -> int:
        """Calculate optimal bit array size."""
        # m = -n * ln(p) / (ln(2)^2)
        m = -self.expected_items * math.log(self.false_positive_rate) / (math.log(2) ** 2)
        return int(m)
    
    @property
    def optimal_hash_count(self) -> int:
        """Calculate optimal number of hash functions."""
        # k = (m/n) * ln(2)
        m = self.optimal_size
        k = (m / self.expected_items) * math.log(2)
        return max(1, int(k))


class RedisBloomFilter:
    """
    Redis-backed Bloom filter for distributed URL deduplication.
    Uses bit operations in Redis for memory efficiency.
    """
    
    def __init__(
        self,
        redis_client: redis.Redis,
        key_prefix: str = "bloom",
        config: Optional[BloomFilterConfig] = None,
    ):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self.config = config or BloomFilterConfig()
        self.size = self.config.optimal_size
        self.hash_count = self.config.optimal_hash_count
        self.key = f"{key_prefix}:filter"
        
        logger.info(
            f"Bloom filter initialized: size={self.size:,} bits, "
            f"hash_count={self.hash_count}, "
            f"expected_fp_rate={self.config.false_positive_rate:.2%}"
        )
    
    def _get_hash_positions(self, item: str) -> List[int]:
        """Generate hash positions for an item using double hashing."""
        # Use SHA256 for the primary hash
        h1 = int(hashlib.sha256(item.encode()).hexdigest(), 16)
        # Use MD5 for the secondary hash
        h2 = int(hashlib.md5(item.encode()).hexdigest(), 16)
        
        positions = []
        for i in range(self.hash_count):
            # Double hashing: h(i) = h1 + i * h2
            pos = (h1 + i * h2) % self.size
            positions.append(pos)
        
        return positions
    
    async def add(self, item: str) -> bool:
        """
        Add item to Bloom filter.
        
        Returns:
            True if item was possibly already present (all bits were set)
            False if item was definitely not present (at least one bit was 0)
        """
        positions = self._get_hash_positions(item)
        
        # Use pipeline for atomic operation
        pipe = self.redis.pipeline()
        
        # Check existing bits
        for pos in positions:
            pipe.getbit(self.key, pos)
        
        results = await pipe.execute()
        was_present = all(results)
        
        # Set all bits
        pipe = self.redis.pipeline()
        for pos in positions:
            pipe.setbit(self.key, pos, 1)
        await pipe.execute()
        
        return was_present
    
    async def contains(self, item: str) -> bool:
        """
        Check if item might be in the Bloom filter.
        
        Returns:
            True if item might be present (all bits are set)
            False if item is definitely not present (at least one bit is 0)
        """
        positions = self._get_hash_positions(item)
        
        pipe = self.redis.pipeline()
        for pos in positions:
            pipe.getbit(self.key, pos)
        
        results = await pipe.execute()
        return all(results)
    
    async def get_count(self) -> int:
        """Estimate number of items in filter."""
        # Count set bits
        set_bits = await self.redis.bitcount(self.key)
        
        if set_bits == 0:
            return 0
        
        # Estimate: n ≈ -(m/k) * ln(1 - X/m)
        # where X is the number of set bits
        ratio = set_bits / self.size
        if ratio >= 1:
            return self.config.expected_items
        
        n = -(self.size / self.hash_count) * math.log(1 - ratio)
        return int(n)
    
    async def clear(self):
        """Clear the Bloom filter."""
        await self.redis.delete(self.key)


class DistributedURLFrontier:
    """
    Distributed URL Frontier for large-scale web crawling.
    
    Features:
    - Priority-based URL scheduling using Redis sorted sets
    - Bloom filter deduplication for memory efficiency
    - Per-domain politeness with crawl delay tracking
    - Domain-level queues for fair scheduling
    
    Architecture:
    - Priority Queue: Redis ZSET with URL priority scores
    - Domain Queues: Redis LISTs per domain for FIFO within domain
    - Seen URLs: Redis Bloom filter for deduplication
    - Crawl Times: Redis HASH for per-domain last crawl timestamps
    """
    
    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        key_prefix: str = "frontier",
        default_crawl_delay: float = 1.0,
        min_crawl_delay: float = 0.5,
        max_crawl_delay: float = 30.0,
        politeness_factor: float = 10.0,
        bloom_config: Optional[BloomFilterConfig] = None,
    ):
        """
        Initialize the distributed URL frontier.
        
        Args:
            redis_url: Redis connection URL
            key_prefix: Prefix for all Redis keys
            default_crawl_delay: Default delay between requests to same domain
            min_crawl_delay: Minimum crawl delay
            max_crawl_delay: Maximum crawl delay
            politeness_factor: Multiplier for adaptive politeness (α)
            bloom_config: Configuration for Bloom filter
        """
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        self.default_crawl_delay = default_crawl_delay
        self.min_crawl_delay = min_crawl_delay
        self.max_crawl_delay = max_crawl_delay
        self.politeness_factor = politeness_factor
        
        self.redis: Optional[redis.Redis] = None
        self.bloom: Optional[RedisBloomFilter] = None
        self.bloom_config = bloom_config
        
        # Key names
        self.priority_queue_key = f"{key_prefix}:priority_queue"
        self.domain_queues_key = f"{key_prefix}:domain_queues"
        self.crawl_times_key = f"{key_prefix}:crawl_times"
        self.domain_delays_key = f"{key_prefix}:domain_delays"
        self.stats_key = f"{key_prefix}:stats"
        self.active_domains_key = f"{key_prefix}:active_domains"
        
        # Local state
        self._initialized = False
        self._stats = URLFrontierStats()
    
    async def initialize(self):
        """Initialize Redis connection and Bloom filter."""
        if self._initialized:
            return
        
        self.redis = redis.from_url(
            self.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        
        # Test connection
        await self.redis.ping()
        
        # Initialize Bloom filter
        self.bloom = RedisBloomFilter(
            self.redis,
            key_prefix=f"{self.key_prefix}:bloom",
            config=self.bloom_config,
        )
        
        self._initialized = True
        logger.info("URL Frontier initialized successfully")
    
    async def close(self):
        """Close Redis connection."""
        if self.redis:
            await self.redis.close()
            self._initialized = False
    
    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL."""
        parsed = urlparse(url)
        return parsed.netloc.lower()
    
    def _normalize_url(self, url: str) -> str:
        """Normalize URL for deduplication."""
        parsed = urlparse(url)
        
        # Lowercase scheme and netloc
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        
        # Remove default ports
        if netloc.endswith(":80") and scheme == "http":
            netloc = netloc[:-3]
        elif netloc.endswith(":443") and scheme == "https":
            netloc = netloc[:-4]
        
        # Remove trailing slash from path
        path = parsed.path.rstrip("/") or "/"
        
        # Sort query parameters
        query = parsed.query
        if query:
            params = sorted(query.split("&"))
            query = "&".join(params)
        
        # Reconstruct URL
        normalized = f"{scheme}://{netloc}{path}"
        if query:
            normalized += f"?{query}"
        
        return normalized
    
    async def add_url(
        self,
        url: str,
        priority: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> bool:
        """
        Add URL to the frontier.
        
        Args:
            url: URL to add
            priority: Priority score (higher = more important, crawled first)
            metadata: Optional metadata to store with URL
            force: If True, add even if already seen
            
        Returns:
            True if URL was added, False if duplicate
        """
        if not self._initialized:
            await self.initialize()
        
        # Normalize URL
        normalized_url = self._normalize_url(url)
        domain = self._extract_domain(normalized_url)
        
        # Check Bloom filter for duplicates (unless forced)
        if not force:
            is_duplicate = await self.bloom.contains(normalized_url)
            if is_duplicate:
                self._stats.duplicate_urls_skipped += 1
                logger.debug(f"Duplicate URL skipped: {normalized_url}")
                return False
        
        # Add to Bloom filter
        await self.bloom.add(normalized_url)
        
        # Add to priority queue (negative priority so higher priority = lower score)
        # This makes ZRANGEBYSCORE return highest priority first
        await self.redis.zadd(
            self.priority_queue_key,
            {normalized_url: -priority},
        )
        
        # Add to domain queue
        domain_queue_key = f"{self.domain_queues_key}:{domain}"
        await self.redis.rpush(domain_queue_key, normalized_url)
        
        # Track active domain
        await self.redis.sadd(self.active_domains_key, domain)
        
        # Store metadata if provided
        if metadata:
            metadata_key = f"{self.key_prefix}:metadata:{hashlib.md5(normalized_url.encode()).hexdigest()}"
            await self.redis.hset(metadata_key, mapping=metadata)
            await self.redis.expire(metadata_key, 86400 * 7)  # 7 days TTL
        
        # Update stats
        self._stats.total_urls_added += 1
        
        logger.debug(f"URL added: {normalized_url} (priority={priority})")
        return True
    
    async def add_urls(
        self,
        urls: List[str],
        priority: float = 0.0,
    ) -> int:
        """
        Add multiple URLs to the frontier.
        
        Args:
            urls: List of URLs to add
            priority: Priority score for all URLs
            
        Returns:
            Number of URLs successfully added
        """
        added = 0
        for url in urls:
            if await self.add_url(url, priority):
                added += 1
        return added
    
    async def get_next_url(self, timeout: float = 0.0) -> Optional[str]:
        """
        Get next URL to crawl, respecting politeness delays.
        
        This method:
        1. Gets highest priority URL from the queue
        2. Checks if enough time has passed since last crawl to that domain
        3. If not, tries the next URL
        4. Updates crawl time when URL is returned
        
        Args:
            timeout: Maximum time to wait for a URL (0 = no wait)
            
        Returns:
            URL to crawl or None if no URL is ready
        """
        if not self._initialized:
            await self.initialize()
        
        start_time = time.time()
        checked_domains: Set[str] = set()
        
        while True:
            # Check timeout
            if timeout > 0 and time.time() - start_time > timeout:
                return None
            
            # Get URLs sorted by priority
            urls = await self.redis.zrange(
                self.priority_queue_key,
                0, 99,  # Get top 100 candidates
                withscores=False,
            )
            
            if not urls:
                return None
            
            # Find first URL ready to crawl
            for url in urls:
                domain = self._extract_domain(url)
                
                # Skip if we already checked this domain in this iteration
                if domain in checked_domains:
                    continue
                checked_domains.add(domain)
                
                # Check crawl delay
                if await self._can_crawl_domain(domain):
                    # Remove from priority queue
                    await self.redis.zrem(self.priority_queue_key, url)
                    
                    # Remove from domain queue
                    domain_queue_key = f"{self.domain_queues_key}:{domain}"
                    await self.redis.lrem(domain_queue_key, 1, url)
                    
                    # Update last crawl time
                    await self._update_crawl_time(domain)
                    
                    self._stats.total_urls_crawled += 1
                    logger.debug(f"Returning URL for crawling: {url}")
                    return url
            
            # No URL ready, wait a bit if timeout allows
            if timeout > 0:
                await asyncio.sleep(0.1)
                checked_domains.clear()
            else:
                return None
    
    async def _can_crawl_domain(self, domain: str) -> bool:
        """Check if we can crawl a domain based on politeness delay."""
        last_crawl = await self.redis.hget(self.crawl_times_key, domain)
        
        if not last_crawl:
            return True
        
        last_crawl_time = float(last_crawl)
        delay = await self.get_domain_delay(domain)
        
        elapsed = time.time() - last_crawl_time
        return elapsed >= delay
    
    async def _update_crawl_time(self, domain: str):
        """Update last crawl time for a domain."""
        await self.redis.hset(
            self.crawl_times_key,
            domain,
            str(time.time()),
        )
    
    async def get_domain_delay(self, domain: str) -> float:
        """Get crawl delay for a domain."""
        # Check for custom delay
        custom_delay = await self.redis.hget(self.domain_delays_key, domain)
        if custom_delay:
            return float(custom_delay)
        
        return self.default_crawl_delay
    
    async def set_domain_delay(self, domain: str, delay: float):
        """
        Set custom crawl delay for a domain.
        
        Args:
            domain: Domain name
            delay: Delay in seconds
        """
        # Clamp delay to min/max
        delay = max(self.min_crawl_delay, min(self.max_crawl_delay, delay))
        await self.redis.hset(self.domain_delays_key, domain, str(delay))
    
    async def update_domain_delay_from_response(
        self,
        domain: str,
        response_time: float,
    ):
        """
        Update domain delay based on response time (adaptive politeness).
        
        Formula: delay = α × response_time
        where α is the politeness factor (default 10)
        
        Args:
            domain: Domain name
            response_time: Response time in seconds
        """
        # Calculate new delay using politeness formula
        new_delay = self.politeness_factor * response_time
        
        # Clamp to min/max
        new_delay = max(self.min_crawl_delay, min(self.max_crawl_delay, new_delay))
        
        await self.set_domain_delay(domain, new_delay)
        logger.debug(f"Updated delay for {domain}: {new_delay:.2f}s")
    
    async def mark_crawled(self, url: str, success: bool = True):
        """
        Mark URL as crawled (for tracking purposes).
        
        Args:
            url: URL that was crawled
            success: Whether crawl was successful
        """
        # Update stats
        if success:
            await self.redis.hincrby(self.stats_key, "successful_crawls", 1)
        else:
            await self.redis.hincrby(self.stats_key, "failed_crawls", 1)
    
    async def get_queue_size(self) -> int:
        """Get number of URLs in the queue."""
        return await self.redis.zcard(self.priority_queue_key)
    
    async def get_active_domains(self) -> List[str]:
        """Get list of active domains."""
        return list(await self.redis.smembers(self.active_domains_key))
    
    async def get_domain_queue_size(self, domain: str) -> int:
        """Get number of URLs queued for a domain."""
        domain_queue_key = f"{self.domain_queues_key}:{domain}"
        return await self.redis.llen(domain_queue_key)
    
    async def get_stats(self) -> URLFrontierStats:
        """Get frontier statistics."""
        stats = URLFrontierStats()
        
        stats.queue_depth = await self.get_queue_size()
        stats.active_domains = await self.redis.scard(self.active_domains_key)
        stats.unique_domains = stats.active_domains
        
        if self.bloom:
            stats.bloom_filter_size = self.bloom.size
            estimated_items = await self.bloom.get_count()
            stats.total_urls_added = estimated_items
            
            # Calculate estimated false positive rate
            if estimated_items > 0:
                ratio = estimated_items / self.bloom.config.expected_items
                stats.estimated_false_positive_rate = (
                    (1 - math.exp(-self.bloom.hash_count * ratio)) ** self.bloom.hash_count
                )
        
        # Get Redis stats
        redis_stats = await self.redis.hgetall(self.stats_key)
        stats.total_urls_crawled = int(redis_stats.get("successful_crawls", 0))
        stats.duplicate_urls_skipped = self._stats.duplicate_urls_skipped
        
        return stats
    
    async def clear(self):
        """Clear all frontier data."""
        if not self._initialized:
            await self.initialize()
        
        # Get all keys with our prefix
        keys = []
        async for key in self.redis.scan_iter(f"{self.key_prefix}:*"):
            keys.append(key)
        
        if keys:
            await self.redis.delete(*keys)
        
        # Reset local stats
        self._stats = URLFrontierStats()
        
        logger.info("URL Frontier cleared")
    
    async def __aenter__(self):
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


class PriorityCalculator:
    """
    Calculate URL priority scores using various signals.
    
    Higher priority = crawled sooner
    """
    
    @staticmethod
    def calculate_priority(
        url: str,
        depth: int = 0,
        parent_priority: float = 0.0,
        is_seed: bool = False,
        anchor_text_relevance: float = 0.0,
        domain_authority: float = 0.0,
    ) -> float:
        """
        Calculate priority score for a URL.
        
        Args:
            url: URL to score
            depth: Crawl depth from seed
            parent_priority: Priority of parent page
            is_seed: Whether this is a seed URL
            anchor_text_relevance: Relevance score from anchor text
            domain_authority: Domain authority score
            
        Returns:
            Priority score (higher = more important)
        """
        priority = 0.0
        
        # Seed URLs get highest priority
        if is_seed:
            priority += 1000.0
        
        # Decay priority with depth
        depth_decay = 0.8 ** depth
        priority += parent_priority * 0.5 * depth_decay
        
        # Anchor text relevance (0-1 scale)
        priority += anchor_text_relevance * 100.0
        
        # Domain authority (0-1 scale)
        priority += domain_authority * 50.0
        
        # URL structure signals
        parsed = urlparse(url)
        
        # Prefer shorter paths
        path_depth = len([p for p in parsed.path.split("/") if p])
        priority -= path_depth * 2.0
        
        # Penalize URLs with many query parameters
        if parsed.query:
            param_count = len(parsed.query.split("&"))
            priority -= param_count * 5.0
        
        # Prefer HTTPS
        if parsed.scheme == "https":
            priority += 5.0
        
        return priority
