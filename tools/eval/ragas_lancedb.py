"""
Sprint 2 Day 4 — RAGAS-on-LanceDB runner (scaffold).

Plan §9 in ``sprint1_results.md``: a content-normalised metric is the
right way to compare backend swaps.  Sprint 1 latency-only A/B was
misleading because Qwen3-thinking output 5-15× more characters than
qwen2.5 non-thinking — a faster/slower wall doesn't tell the quality
story.  RAGAS-on-LanceDB gives us the missing axis.

Design
------
50-query sweep over the existing LanceDB store.  For each query:

1. Hybrid retrieve top-k from LanceDB (existing pipeline).
2. Generate an answer via the active backend (amor-architect or
   amor-editor depending on phase).
3. Score with RAGAS metrics:
     * ``faithfulness``       — does the answer make claims that
                                aren't in the retrieved context?
     * ``answer_relevancy``   — does the answer address the query?
     * ``context_precision``  — what fraction of retrieved chunks are
                                relevant?
4. Aggregate to mean per metric.

Judge LLM: Mistral-Small-3-Q4_K_M on CPU (the Sprint 0 baseline judge,
already running on http://amor-judge:8080).  Distinct family from the
Qwen-derived candidates, no self-correlation.

Status: DAY 4 SCAFFOLD — runner registered with ``implemented=False``.
Day 4 paste-ready prompt in the plan file details the remaining work:
test-query curation, LanceDB query helper, RAGAS metric prompts,
result aggregation.

This stub keeps the manifest stable — when ``run_ragas_50`` is wired,
flip ``runner=...`` and the dashboard immediately surfaces it as
implemented.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from document_processor.api.admin_evals_routes import (
    EvalDescriptor,
    register_eval,
)

logger = logging.getLogger(__name__)


async def run_ragas_50(
    run_id: str,
    progress: Callable[[str], Awaitable[None]],
) -> Dict[str, Any]:
    """SCAFFOLD — full implementation deferred."""
    raise NotImplementedError(
        "RAGAS-on-LanceDB runner is scaffolded, not yet implemented.  "
        "Day 4 paste-ready prompt in plan file (Cycle C section) "
        "details: test-query curation (50 from existing chat sessions), "
        "LanceDB hybrid retrieval helper, RAGAS metric prompts using "
        "Mistral-Small-3 judge, mean aggregation.",
    )


register_eval(
    EvalDescriptor(
        name="ragas_50",
        title="RAGAS 50",
        description=(
            "50-query RAGAS sweep over the LanceDB store.  "
            "faithfulness / answer_relevancy / context_precision via "
            "Mistral-Small-3 judge (CPU).  Day 4 of Cycle C Sprint 2 "
            "— scaffold registered, runner implementation pending."
        ),
        expected_minutes=10,
        summary_keys=(
            "faithfulness",
            "answer_relevancy",
            "context_precision",
            "queries_total",
        ),
        runner=None,
    ),
)
