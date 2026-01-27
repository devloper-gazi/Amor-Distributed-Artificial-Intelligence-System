"""
Seed URL Manager for initializing the crawl frontier.

Handles:
- Loading seeds from various sources (files, URLs, APIs)
- Domain categorization
- Seed prioritization
- Integration with URL frontier
"""

import asyncio
import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any, Set, AsyncIterator
from urllib.parse import urlparse

import httpx

from .url_frontier import DistributedURLFrontier, PriorityCalculator

logger = logging.getLogger(__name__)


class SeedSource(Enum):
    """Types of seed sources."""
    FILE = "file"           # Local file (CSV, JSON, TXT)
    URL = "url"             # Remote URL
    COMMON_CRAWL = "common_crawl"  # Common Crawl index
    SITEMAP = "sitemap"     # XML sitemap
    MANUAL = "manual"       # Manually added


@dataclass
class SeedEntry:
    """A seed URL entry with metadata."""
    url: str
    source: SeedSource
    priority: float = 0.0
    category: Optional[str] = None
    language: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    added_at: datetime = field(default_factory=datetime.utcnow)


@dataclass 
class SeedManagerStats:
    """Statistics for seed management."""
    total_seeds_loaded: int = 0
    seeds_added_to_frontier: int = 0
    duplicate_seeds: int = 0
    invalid_seeds: int = 0
    sources_processed: int = 0


class SeedManager:
    """
    Manages seed URLs for initializing the crawl frontier.
    
    Supports loading seeds from:
    - Local files (CSV, JSON, TXT)
    - Remote URLs
    - XML sitemaps
    - Common Crawl index (for domain discovery)
    """
    
    def __init__(
        self,
        frontier: DistributedURLFrontier,
        default_priority: float = 100.0,
        validate_urls: bool = True,
        deduplicate: bool = True,
    ):
        """
        Initialize the seed manager.
        
        Args:
            frontier: URL frontier to add seeds to
            default_priority: Default priority for seeds
            validate_urls: Whether to validate URLs before adding
            deduplicate: Whether to deduplicate seeds locally
        """
        self.frontier = frontier
        self.default_priority = default_priority
        self.validate_urls = validate_urls
        self.deduplicate = deduplicate
        
        self._seen_urls: Set[str] = set()
        self.stats = SeedManagerStats()
        
        # HTTP client for remote sources
        self._client: Optional[httpx.AsyncClient] = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": "SeedManager/1.0"},
            )
        return self._client
    
    async def close(self):
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
    
    def _validate_url(self, url: str) -> bool:
        """Validate a URL."""
        try:
            parsed = urlparse(url)
            
            # Must have scheme and netloc
            if not parsed.scheme or not parsed.netloc:
                return False
            
            # Must be http or https
            if parsed.scheme not in ("http", "https"):
                return False
            
            # Basic domain validation
            if "." not in parsed.netloc:
                return False
            
            return True
            
        except Exception:
            return False
    
    def _normalize_url(self, url: str) -> str:
        """Normalize a URL."""
        url = url.strip()
        
        # Add scheme if missing
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        
        return url
    
    async def add_seed(
        self,
        url: str,
        priority: Optional[float] = None,
        category: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Add a single seed URL.
        
        Args:
            url: Seed URL
            priority: Priority score (higher = more important)
            category: Optional category
            metadata: Optional metadata
            
        Returns:
            True if added successfully
        """
        url = self._normalize_url(url)
        
        # Validate
        if self.validate_urls and not self._validate_url(url):
            self.stats.invalid_seeds += 1
            logger.warning(f"Invalid seed URL: {url}")
            return False
        
        # Deduplicate locally
        if self.deduplicate:
            if url in self._seen_urls:
                self.stats.duplicate_seeds += 1
                return False
            self._seen_urls.add(url)
        
        # Calculate priority
        final_priority = priority if priority is not None else self.default_priority
        
        # Add to frontier
        added = await self.frontier.add_url(
            url,
            priority=final_priority,
            metadata={
                "is_seed": True,
                "category": category,
                **(metadata or {}),
            },
        )
        
        if added:
            self.stats.seeds_added_to_frontier += 1
            self.stats.total_seeds_loaded += 1
            logger.debug(f"Seed added: {url}")
        
        return added
    
    async def add_seeds(
        self,
        urls: List[str],
        priority: Optional[float] = None,
        category: Optional[str] = None,
    ) -> int:
        """
        Add multiple seed URLs.
        
        Returns:
            Number of seeds successfully added
        """
        added = 0
        for url in urls:
            if await self.add_seed(url, priority, category):
                added += 1
        return added
    
    async def load_from_file(
        self,
        file_path: str,
        priority: Optional[float] = None,
        category: Optional[str] = None,
    ) -> int:
        """
        Load seeds from a local file.
        
        Supports:
        - .txt: One URL per line
        - .csv: CSV with 'url' column
        - .json: JSON array of URLs or objects with 'url' field
        
        Returns:
            Number of seeds loaded
        """
        path = Path(file_path)
        
        if not path.exists():
            logger.error(f"Seed file not found: {file_path}")
            return 0
        
        loaded = 0
        suffix = path.suffix.lower()
        
        try:
            if suffix == ".txt":
                loaded = await self._load_txt(path, priority, category)
            elif suffix == ".csv":
                loaded = await self._load_csv(path, priority, category)
            elif suffix == ".json":
                loaded = await self._load_json(path, priority, category)
            else:
                logger.error(f"Unsupported file format: {suffix}")
                return 0
            
            self.stats.sources_processed += 1
            logger.info(f"Loaded {loaded} seeds from {file_path}")
            
        except Exception as e:
            logger.error(f"Error loading seeds from {file_path}: {e}")
        
        return loaded
    
    async def _load_txt(
        self,
        path: Path,
        priority: Optional[float],
        category: Optional[str],
    ) -> int:
        """Load seeds from text file (one URL per line)."""
        loaded = 0
        
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if await self.add_seed(line, priority, category):
                        loaded += 1
        
        return loaded
    
    async def _load_csv(
        self,
        path: Path,
        priority: Optional[float],
        category: Optional[str],
    ) -> int:
        """Load seeds from CSV file."""
        loaded = 0
        
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                url = row.get("url") or row.get("URL") or row.get("link")
                if not url:
                    continue
                
                row_priority = priority
                if "priority" in row:
                    try:
                        row_priority = float(row["priority"])
                    except ValueError:
                        pass
                
                row_category = category or row.get("category")
                
                if await self.add_seed(url, row_priority, row_category):
                    loaded += 1
        
        return loaded
    
    async def _load_json(
        self,
        path: Path,
        priority: Optional[float],
        category: Optional[str],
    ) -> int:
        """Load seeds from JSON file."""
        loaded = 0
        
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Handle array of URLs or objects
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    url = item
                    item_priority = priority
                    item_category = category
                elif isinstance(item, dict):
                    url = item.get("url")
                    item_priority = item.get("priority", priority)
                    item_category = item.get("category", category)
                else:
                    continue
                
                if url and await self.add_seed(url, item_priority, item_category):
                    loaded += 1
        
        return loaded
    
    async def load_from_url(
        self,
        url: str,
        priority: Optional[float] = None,
        category: Optional[str] = None,
    ) -> int:
        """
        Load seeds from a remote URL.
        
        Returns:
            Number of seeds loaded
        """
        try:
            client = await self._get_client()
            response = await client.get(url)
            response.raise_for_status()
            
            content_type = response.headers.get("content-type", "")
            content = response.text
            
            loaded = 0
            
            if "json" in content_type:
                data = json.loads(content)
                if isinstance(data, list):
                    for item in data:
                        seed_url = item if isinstance(item, str) else item.get("url")
                        if seed_url and await self.add_seed(seed_url, priority, category):
                            loaded += 1
            else:
                # Treat as text (one URL per line)
                for line in content.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if await self.add_seed(line, priority, category):
                            loaded += 1
            
            self.stats.sources_processed += 1
            logger.info(f"Loaded {loaded} seeds from {url}")
            return loaded
            
        except Exception as e:
            logger.error(f"Error loading seeds from URL {url}: {e}")
            return 0
    
    async def load_from_sitemap(
        self,
        sitemap_url: str,
        priority: Optional[float] = None,
        max_urls: int = 10000,
    ) -> int:
        """
        Load seeds from an XML sitemap.
        
        Returns:
            Number of seeds loaded
        """
        try:
            from xml.etree import ElementTree
            
            client = await self._get_client()
            response = await client.get(sitemap_url)
            response.raise_for_status()
            
            root = ElementTree.fromstring(response.content)
            
            # Handle sitemap namespace
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            
            loaded = 0
            
            # Check if this is a sitemap index
            sitemap_refs = root.findall(".//sm:sitemap/sm:loc", ns)
            if sitemap_refs:
                # Recursively load referenced sitemaps
                for ref in sitemap_refs[:10]:  # Limit to 10 sub-sitemaps
                    loaded += await self.load_from_sitemap(
                        ref.text,
                        priority,
                        max_urls - loaded,
                    )
                    if loaded >= max_urls:
                        break
            else:
                # Load URLs from sitemap
                url_elements = root.findall(".//sm:url/sm:loc", ns)
                
                for elem in url_elements:
                    if loaded >= max_urls:
                        break
                    
                    url = elem.text
                    if url and await self.add_seed(url, priority):
                        loaded += 1
            
            self.stats.sources_processed += 1
            logger.info(f"Loaded {loaded} seeds from sitemap {sitemap_url}")
            return loaded
            
        except Exception as e:
            logger.error(f"Error loading sitemap {sitemap_url}: {e}")
            return 0
    
    async def load_top_domains(
        self,
        source: str = "tranco",
        limit: int = 1000,
        priority: Optional[float] = None,
    ) -> int:
        """
        Load top domains from a ranking list.
        
        Args:
            source: Source of rankings ("tranco", "majestic", "cisco")
            limit: Number of domains to load
            priority: Priority for these seeds
            
        Returns:
            Number of seeds loaded
        """
        # URLs for top domain lists
        sources = {
            "tranco": "https://tranco-list.eu/top-1m.csv.zip",
            # Add more sources as needed
        }
        
        if source not in sources:
            logger.error(f"Unknown domain source: {source}")
            return 0
        
        # For now, just log that this would load from external source
        logger.info(f"Would load top {limit} domains from {source}")
        
        # Example hardcoded top domains for testing
        test_domains = [
            "google.com",
            "youtube.com",
            "facebook.com",
            "twitter.com",
            "instagram.com",
            "wikipedia.org",
            "reddit.com",
            "amazon.com",
            "linkedin.com",
            "github.com",
        ]
        
        loaded = 0
        for domain in test_domains[:limit]:
            url = f"https://{domain}"
            if await self.add_seed(url, priority or 1000.0, category="top_domain"):
                loaded += 1
        
        return loaded
    
    def get_stats(self) -> SeedManagerStats:
        """Get statistics."""
        return self.stats
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
