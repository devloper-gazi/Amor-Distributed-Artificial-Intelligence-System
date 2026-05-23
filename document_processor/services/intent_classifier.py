"""Cycle UI 2026-05-20 — Auto-mode intent classifier.

Six-class router (build/research/thinking/consortium/sentinel/quickcode)
backed by ``sentence-transformers/all-MiniLM-L6-v2`` + a cosine-similarity
prototype-classifier head trained few-shot on ~10 examples per class.

Why not SetFit?  The full SetFit pipeline drags in pytorch trainer state +
huggingface_hub + datasets; for a 6-class task with ~60 training examples
the gain over a hand-rolled prototype cosine matcher is marginal but the
operational footprint is large.  This module implements the SetFit *idea*
(contrastive few-shot embedding lookup) without the trainer dependencies:

  1. Encode each labeled example with MiniLM-L6 → 384-dim vector.
  2. For each class, average its example embeddings → "prototype" centroid.
  3. Classify a new prompt by cosine similarity against the 6 prototypes.

Latency target: ~5 ms per classify on CPU after warmup (one MiniLM
encode + 6 dot products).  Confidence rule:

  * cosine top-1 score < 0.45 → low confidence
  * top1 - top2 < 0.08      → low confidence (boundary case)

When low confidence, the caller's UI shows a preview pill rather than
silently routing (the ChatGPT GPT-5 router launch lesson — silent
auto-routing surprises users).  Default fallback class: ``build``.

Acceptance gate (sprint0 corpus, 30 prompts × 6 classes): ≥85 % top-1
accuracy + ≤50 ms per classify on the reference RTX 4060 Laptop CPU
fallback path.

Public surface::

    >>> from document_processor.services.intent_classifier import get_classifier
    >>> clf = get_classifier()
    >>> result = clf.classify("fix typo in user.py line 42")
    >>> result.mode
    'quickcode'
    >>> result.confidence
    0.72
    >>> result.low_confidence
    False
    >>> result.alternatives[:2]
    [('quickcode', 0.81), ('build', 0.55)]
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ─── Class set + defaults ──────────────────────────────────────────────

CLASSES: Tuple[str, ...] = (
    "build",
    "research",
    "thinking",
    "consortium",
    "sentinel",
    "quickcode",
)

DEFAULT_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
# Calibrated from sprint0 corpus runs 2026-05-20:
#   * canonical examples score 0.30 - 0.85 (research prototype is
#     looser → "compare crdts vs ot" lands at 0.35 even though the
#     gap to runner-up is 0.15);
#   * encoder garbage / pathological inputs score < 0.20.
# We trust the GAP signal (0.08) primarily; the raw score floor at
# 0.20 only catches truly degenerate prompts where the encoder gave
# up.  Setting it higher creates false low-confidence warnings on
# well-classified prompts whose nearest centroid is loose.
DEFAULT_LOW_CONFIDENCE_SCORE = 0.20
DEFAULT_LOW_CONFIDENCE_GAP = 0.08
DEFAULT_FALLBACK_CLASS = "build"


# ─── Training corpus (10 examples per class = 60 total) ────────────────

# These are hand-curated few-shot prototypes.  Each example is short and
# representative of how a user would PHRASE that mode's request in an
# auto-mode composer.  Order does not matter; only the embeddings'
# centroids do.

TRAINING_DATA: Dict[str, List[str]] = {
    "build": [
        "build a snake game in html and css with arrow controls",
        "create a python script that downloads images from a list of urls",
        "implement a rest api in fastapi for a notes crud with sqlite",
        "write a react todo app with localstorage persistence",
        "scaffold a rust cli tool for managing dotfiles",
        "make a flask web server that serves a markdown blog",
        "build a full-stack chat app with websockets",
        "create a typescript library for parsing iso-8601 durations",
        "implement a tic-tac-toe game in vanilla javascript",
        "write a go service that exposes /metrics for prometheus",
    ],
    "research": [
        "compare crdts and operational transform for collaborative editing",
        "summarize the latest arxiv papers on local llm inference",
        "explain mixture of experts to a senior backend engineer",
        "what are the tradeoffs between lance db qdrant and weaviate",
        "describe the differences between tls 1.2 and tls 1.3",
        "research the state of art in retrieval augmented generation",
        "compare bm25 and dense embedding retrieval for code search",
        "what is direct preference optimization and how does it work",
        "explain how flash attention works at a kernel level",
        "summarize how mamba and state space models differ from transformers",
    ],
    "thinking": [
        "should we replace fastapi with axum, walk me through tradeoffs",
        "plan the migration of a 50k loc python 3.9 codebase to 3.12",
        "two trains leaving opposite cities, when and where do they meet",
        "help me decide between postgres and sqlite for my desktop app",
        "outline a debugging strategy for messages silently lost under load",
        "what would you prioritize for next quarter, weigh the options",
        "should the team adopt monorepo or polyrepo for these three services",
        "evaluate the architecture choice of event sourcing vs crud",
        "reason step by step about why this distributed lock keeps timing out",
        "design a roadmap for migrating from rest to grpc with risks",
    ],
    "consortium": [
        "hold a multi agent architecture review of switching to vllm",
        "convene a panel of experts to debate next quarter priorities",
        "run a consortium code review of the bitnet shadow planner change",
        "assemble three reviewers to vote on the postgres migration",
        "bring backend ops and security agents to evaluate this design",
        "multi expert panel: should we ship the new approval flow",
        "have multiple agents debate the rust rewrite proposal",
        "consortium review of the proposed kafka deprecation",
        "panel of agents argue for and against multi tenant deployment",
        "convene experts to vote on the new authentication scheme",
    ],
    "sentinel": [
        "audit the dependencies for known cves and outdated versions",
        "scan the repo for hardcoded secrets and exposed api keys",
        "monitor recent ci runs for performance regressions",
        "watch the last 100 builds for flaky tests and group by name",
        "audit vram utilization over the last 7 days for budget breaches",
        "guard the staging environment for any unexpected schema changes",
        "monitor production logs for new error signatures this week",
        "sentinel scan of the codebase for sql injection patterns",
        "watch for security advisories on our pinned package versions",
        "audit access logs for anomalous authentication patterns",
    ],
    "quickcode": [
        "fix the typo in user.py line 42 received not recieved",
        "add a getter for email field in models user class",
        "rename foo to bar in routes.ts everywhere in this file",
        "remove the unused import os from utils helpers.py",
        "change max retries from 3 to 5 in config settings.py",
        "fix the off by one error in pagination.py line 87",
        "add type hints to the public functions in api/client.py",
        "rename the variable cnt to count in handlers/foo.py",
        "change the default port from 8000 to 9000 in run.py",
        "remove the deprecated decorator on the legacy_handler function",
    ],
}


# ─── Result dataclass ──────────────────────────────────────────────────


@dataclass
class IntentResult:
    """Output of a single classify call.

    Attributes
    ----------
    mode :
        The chosen class (from ``CLASSES``).  Always non-empty.  Equal
        to ``DEFAULT_FALLBACK_CLASS`` when input is empty / pathological.
    top1_score :
        Cosine similarity score of the winning class, range [-1, 1] but
        in practice [0.2, 0.95] for non-empty prompts.
    top2_score :
        Cosine score of the runner-up class.
    alternatives :
        All 6 ``(class, score)`` tuples sorted descending — useful for
        the UI's confidence-pill hover state.
    confidence :
        A simple [0, 1] mapping ``(top1_score - top2_score)`` clipped
        so the UI doesn't render negative values.  This is a heuristic
        gap-based confidence, NOT a calibrated probability.
    low_confidence :
        True when ``top1_score < low_confidence_score_threshold`` OR
        ``top1_score - top2_score < low_confidence_gap_threshold``.
        UI uses this to show a preview pill instead of silent routing.
    latency_ms :
        Wall-clock duration of the classify call (encoder forward +
        prototype lookup).  Useful for budgeting + telemetry.
    """

    mode: str
    top1_score: float
    top2_score: float
    alternatives: List[Tuple[str, float]] = field(default_factory=list)
    confidence: float = 0.0
    low_confidence: bool = False
    latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "mode": self.mode,
            "top1_score": round(self.top1_score, 4),
            "top2_score": round(self.top2_score, 4),
            "alternatives": [(c, round(s, 4)) for c, s in self.alternatives],
            "confidence": round(self.confidence, 4),
            "low_confidence": self.low_confidence,
            "latency_ms": round(self.latency_ms, 2),
        }


# ─── Classifier ────────────────────────────────────────────────────────


class IntentClassifier:
    """Singleton-style cosine-prototype intent classifier.

    Construction is lazy + thread-safe — first ``classify`` call loads
    the MiniLM model + computes class prototype centroids.  Subsequent
    calls reuse them.

    Parameters
    ----------
    model_id :
        Sentence-transformers model id.  Defaults to
        ``sentence-transformers/all-MiniLM-L6-v2``.
    device :
        Torch device.  ``None`` → ``cpu`` (don't compete with planner
        VRAM).  Operator can pass ``cuda`` if planner is paused.
    training_data :
        ``{class_name: [examples]}``.  Defaults to module-level
        ``TRAINING_DATA``.  Tests can inject custom corpora.
    low_confidence_score_threshold :
        See ``IntentResult.low_confidence``.
    low_confidence_gap_threshold :
        See ``IntentResult.low_confidence``.
    """

    def __init__(
        self,
        *,
        model_id: str = DEFAULT_MODEL_ID,
        device: Optional[str] = None,
        training_data: Optional[Dict[str, List[str]]] = None,
        low_confidence_score_threshold: float = DEFAULT_LOW_CONFIDENCE_SCORE,
        low_confidence_gap_threshold: float = DEFAULT_LOW_CONFIDENCE_GAP,
        fallback_class: str = DEFAULT_FALLBACK_CLASS,
    ) -> None:
        self._model_id = model_id
        self._device = device or "cpu"
        self._training_data = training_data or TRAINING_DATA
        self._low_score = float(low_confidence_score_threshold)
        self._low_gap = float(low_confidence_gap_threshold)
        self._fallback = fallback_class
        self._encoder = None  # SentenceTransformer instance
        self._prototypes: Optional[np.ndarray] = None  # shape (n_classes, dim)
        self._class_order: List[str] = list(CLASSES)
        self._lock = threading.Lock()

    # ── lazy init ────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._encoder is not None and self._prototypes is not None:
            return
        with self._lock:
            if self._encoder is not None and self._prototypes is not None:
                return
            t0 = time.perf_counter()
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415
            self._encoder = SentenceTransformer(
                self._model_id, device=self._device, trust_remote_code=False,
            )
            self._prototypes = self._build_prototypes()
            elapsed = (time.perf_counter() - t0) * 1000.0
            logger.info(
                "intent_classifier_loaded model=%s device=%s classes=%d "
                "examples=%d elapsed_ms=%.1f",
                self._model_id, self._device, len(self._class_order),
                sum(len(v) for v in self._training_data.values()),
                elapsed,
            )

    def _build_prototypes(self) -> np.ndarray:
        """Encode the training examples class-by-class and average each
        class's embedding to form a prototype centroid.  Returns array
        of shape (n_classes, embed_dim) with L2-normalized rows so the
        classify path can use a single dot product as cosine sim."""
        assert self._encoder is not None
        per_class: List[np.ndarray] = []
        for cls in self._class_order:
            examples = self._training_data.get(cls)
            if not examples:
                raise ValueError(
                    f"intent_classifier: training_data missing examples for {cls!r}",
                )
            embeds = self._encoder.encode(
                examples,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            # Average + re-normalize so the centroid sits back on the
            # unit hyper-sphere; cosine sim becomes a single dot product.
            centroid = embeds.mean(axis=0)
            norm = np.linalg.norm(centroid)
            if norm > 0:
                centroid = centroid / norm
            per_class.append(centroid)
        return np.stack(per_class, axis=0)

    # ── core entrypoint ──────────────────────────────────────────────

    def classify(self, prompt: str) -> IntentResult:
        """Classify a single prompt.  Always returns a result (never
        raises) — empty/short prompts fall back to ``fallback_class``
        with ``low_confidence=True`` so the UI shows the preview pill."""
        text = (prompt or "").strip()
        t0 = time.perf_counter()

        if len(text) < 3:
            # Pathological — too short to embed meaningfully.
            return IntentResult(
                mode=self._fallback,
                top1_score=0.0,
                top2_score=0.0,
                alternatives=[(c, 0.0) for c in self._class_order],
                confidence=0.0,
                low_confidence=True,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        self._ensure_loaded()
        assert self._encoder is not None and self._prototypes is not None

        embed = self._encoder.encode(
            [text],
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )[0]  # shape (dim,)

        # Cosine sims via dot product (all rows L2-normalized).
        sims = self._prototypes @ embed  # shape (n_classes,)
        order = np.argsort(sims)[::-1]
        ranked: List[Tuple[str, float]] = [
            (self._class_order[i], float(sims[i])) for i in order
        ]
        top1_cls, top1 = ranked[0]
        top2_cls, top2 = ranked[1] if len(ranked) > 1 else (top1_cls, top1)
        gap = top1 - top2
        confidence = max(0.0, min(1.0, gap * 4.0))  # soft scale [0, 1]
        low_conf = (top1 < self._low_score) or (gap < self._low_gap)

        return IntentResult(
            mode=top1_cls,
            top1_score=top1,
            top2_score=top2,
            alternatives=ranked,
            confidence=confidence,
            low_confidence=low_conf,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def classify_batch(self, prompts: Sequence[str]) -> List[IntentResult]:
        """Bulk classify — used by tests + by future GET /api/chat/
        classify-many endpoints.  Reuses the encoder + prototypes;
        ~5 ms/prompt for batches >50, ~10 ms for batches of 1-5."""
        return [self.classify(p) for p in prompts]


# ─── Singleton accessor ────────────────────────────────────────────────

_INSTANCE: Optional[IntentClassifier] = None
_INSTANCE_LOCK = threading.Lock()


def get_classifier() -> IntentClassifier:
    """Process-wide singleton accessor.  Loads the model lazily on the
    first ``classify`` call, NOT here — so importing this module is
    free and FastAPI startup latency doesn't grow."""
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            # Allow operator override of model_id via env var without
            # touching settings.py — useful for swapping in a fine-tuned
            # variant down the road.
            model_env = os.environ.get("AMOR_INTENT_CLASSIFIER_MODEL")
            device_env = os.environ.get("AMOR_INTENT_CLASSIFIER_DEVICE")
            _INSTANCE = IntentClassifier(
                model_id=model_env or DEFAULT_MODEL_ID,
                device=device_env,
            )
        return _INSTANCE


def reset_classifier_for_tests() -> None:
    """Drop the process-wide singleton so tests can inject custom
    training corpora without polluting the cached instance."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None
