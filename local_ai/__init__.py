"""
Local AI Research System
Autonomous multilingual document processing with local LLMs
Optimized for RTX 4060 8GB VRAM + 16GB RAM
"""

__version__ = "1.0.0"
__author__ = "Amor"

from .ollama_client import OllamaClient
from .translation.nllb_translator import NLLBTranslator
from .scraping.web_scraper import AutonomousScraper
from .vector_store.lancedb_store import LanceDBVectorStore
from .agents.research_crew import ResearchCrew

__all__ = [
    "OllamaClient",
    "NLLBTranslator",
    "AutonomousScraper",
    "LanceDBVectorStore",
    "ResearchCrew",
]