"""Cycle I.2 — predictive test-time memory layer.

Currently exports ``TitansPredictiveMemory`` (the Sapienza MAC reimpl
adapted to AMOR's session traces).  See ``titans_predictive.py`` for
the full design rationale.
"""

from .titans_predictive import (
    TitansPredictiveMemory,
    TitansEntry,
    EmbedderFn,
    cosine_similarity,
)

__all__ = [
    "TitansPredictiveMemory",
    "TitansEntry",
    "EmbedderFn",
    "cosine_similarity",
]
