"""
Distributed crawling infrastructure for large-scale web scraping.
Includes URL frontier, scheduler, resilient scraper, and authentication agent.
"""

from .url_frontier import DistributedURLFrontier, URLFrontierStats
from .scheduler import CrawlScheduler, SchedulerConfig
from .resilient_scraper import ResilientScraper, ScraperConfig
from .seed_manager import SeedManager, SeedSource
from .auth_agent import AuthAgent, SessionManager

__all__ = [
    "DistributedURLFrontier",
    "URLFrontierStats",
    "CrawlScheduler",
    "SchedulerConfig",
    "ResilientScraper",
    "ScraperConfig",
    "SeedManager",
    "SeedSource",
    "AuthAgent",
    "SessionManager",
]
