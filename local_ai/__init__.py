"""
Local AI Research System
Autonomous multilingual document processing with local LLMs
Optimized for RTX 4060 8GB VRAM + 16GB RAM

Eager re-exports below are wrapped in try/except so partial-install
environments (e.g. running ``pytest`` without the heavy ML deps like
``ollama``, ``sentence-transformers``, or ``lancedb``) can still
import sibling modules from this package — Phase 1A's ``z3_verifier``,
``logic_engine``, ``episodic_memory``, ``rlef_collector`` only need
the standard scientific stack and must not be blocked by an unrelated
import failure higher up.
"""

import logging as _logging

__version__ = "1.0.0"
__author__ = "Amor"

_log = _logging.getLogger(__name__)
_optional_exports: list[str] = []


def _try_export(name: str, importer):
    try:
        globals()[name] = importer()
        _optional_exports.append(name)
    except Exception as exc:  # pragma: no cover
        _log.debug("local_ai optional export %s unavailable: %s", name, exc)


_try_export("OllamaClient",
             lambda: __import__("local_ai.ollama_client",
                                 fromlist=["OllamaClient"]).OllamaClient)
_try_export("NLLBTranslator",
             lambda: __import__("local_ai.translation.nllb_translator",
                                 fromlist=["NLLBTranslator"]).NLLBTranslator)
_try_export("AutonomousScraper",
             lambda: __import__("local_ai.scraping.web_scraper",
                                 fromlist=["AutonomousScraper"]).AutonomousScraper)
_try_export("LanceDBVectorStore",
             lambda: __import__("local_ai.vector_store.lancedb_store",
                                 fromlist=["LanceDBVectorStore"]).LanceDBVectorStore)
_try_export("ResearchCrew",
             lambda: __import__("local_ai.agents.research_crew",
                                 fromlist=["ResearchCrew"]).ResearchCrew)

__all__ = list(_optional_exports)