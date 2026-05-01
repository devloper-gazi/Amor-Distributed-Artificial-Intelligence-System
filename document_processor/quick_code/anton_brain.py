"""
QuickCode V2 — Anton-Brain (prompt shaper, no LLM call).

Builds the final system prompt out of four budgeted sections::

    IDENTITY       (always kept verbatim)
    GLOBAL_RULES   (always kept verbatim)
    TASK_CONTEXT   (truncated second, after ERROR_MEMORY)
    ERROR_MEMORY   (truncated first; last-N entries kept)

Total token budget defaults to 3 200 tokens — small enough to leave
headroom for the user's actual prompt + the LLM's reply on a 7 B
local model, large enough to carry several recent error traces.

Tokeniser
---------

* If ``tiktoken`` is installed we use ``cl100k_base`` (OpenAI's
  default); otherwise we fall back to a 4-chars-per-token
  approximation that's accurate enough for budgeting.

No content filters / refusal language anywhere.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


Tokeniser = Callable[[str], int]


# ─────────────────────────────────────────────────────────────────────
# Default tokeniser
# ─────────────────────────────────────────────────────────────────────


def _approx_token_count(text: str) -> int:
    """Cheap 4-chars-per-token approximation (round up).

    Matches OpenAI's published rule of thumb for English; on code
    it slightly under-counts but never over-counts, which is the
    safe direction for budgeting."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _build_default_tokeniser() -> Tokeniser:
    try:  # pragma: no cover - tiktoken not always installed
        import tiktoken  # type: ignore[import-untyped]

        enc = tiktoken.get_encoding("cl100k_base")

        def _tk(text: str) -> int:
            if not text:
                return 0
            return len(enc.encode(text))

        return _tk
    except Exception as exc:  # pragma: no cover
        logger.debug("tiktoken not available, falling back to approx: %s", exc)
        return _approx_token_count


# ─────────────────────────────────────────────────────────────────────
# Public class
# ─────────────────────────────────────────────────────────────────────


class AntonBrain:
    """Pure prompt shaper.  Stateless — call ``shape()`` per task."""

    SECTION_HEADERS = {
        "identity": "## Identity",
        "global_rules": "## Rules",
        "task_context": "## Task Context",
        "error_memory": "## Error Memory",
    }

    DEFAULT_IDENTITY = (
        "You are an expert software engineer. Produce the best technical "
        "answer. Direct, specific, no hedging."
    )

    DEFAULT_GLOBAL_RULES = (
        "- Reason from first principles before writing code.\n"
        "- Prefer the simplest correct solution.\n"
        "- Cite assumptions when the input is ambiguous.\n"
        "- Match the existing code style of any provided context.\n"
        "- Tests must reflect the spec, not the implementation."
    )

    def __init__(
        self,
        *,
        budget_tokens: int = 3200,
        tokenizer: Tokeniser | None = None,
        min_error_memory_keep: int = 1,
    ) -> None:
        self._budget = max(256, int(budget_tokens))
        self._tokenizer: Tokeniser = tokenizer or _build_default_tokeniser()
        self._min_error_memory_keep = max(0, int(min_error_memory_keep))

    # ─── Public API ─────────────────────────────────────────────────

    def shape(
        self,
        *,
        identity: str | None = None,
        global_rules: str | None = None,
        task_context: str | None = None,
        error_memory: Iterable[str] | str | None = None,
    ) -> str:
        """Compose the final system prompt under the token budget."""
        identity = (identity or self.DEFAULT_IDENTITY).strip()
        global_rules = (global_rules or self.DEFAULT_GLOBAL_RULES).strip()
        task_context = (task_context or "").strip()
        errors = self._normalise_errors(error_memory)

        # Identity + global rules are non-negotiable.
        non_negotiable = [
            self._block("identity", identity),
            self._block("global_rules", global_rules),
        ]
        non_negotiable_tokens = sum(self._tokenizer(b) for b in non_negotiable)

        if non_negotiable_tokens >= self._budget:
            # The static sections alone are over budget — return them
            # truncated rather than risk an empty prompt.
            return self._truncate_to_budget(
                "\n\n".join(non_negotiable), self._budget
            )

        remaining = self._budget - non_negotiable_tokens

        # ERROR_MEMORY is truncated first.  Keep the most recent
        # entries; drop oldest until they fit (but always keep at
        # least ``min_error_memory_keep`` if any are provided).
        kept_errors, errors_block = self._fit_errors(errors, budget=remaining)
        remaining -= self._tokenizer(errors_block) if errors_block else 0
        remaining = max(0, remaining)

        # TASK_CONTEXT is truncated second.  Cut from the end so the
        # opening of the context (typically the user's request) is
        # preserved.
        ctx_block = self._fit_task_context(task_context, budget=remaining)

        sections = list(non_negotiable)
        if ctx_block:
            sections.append(ctx_block)
        if errors_block:
            sections.append(errors_block)

        return "\n\n".join(sections)

    def count_tokens(self, text: str) -> int:
        return self._tokenizer(text)

    @property
    def budget_tokens(self) -> int:
        return self._budget

    # ─── Internals ──────────────────────────────────────────────────

    def _block(self, key: str, body: str) -> str:
        return f"{self.SECTION_HEADERS[key]}\n{body}"

    def _normalise_errors(
        self, value: Iterable[str] | str | None
    ) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        out: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                out.append(text)
        return out

    def _fit_errors(
        self, errors: list[str], *, budget: int
    ) -> tuple[list[str], str]:
        """Keep the most-recent errors that fit in ``budget``.  Always
        keep at least ``min_error_memory_keep`` entries when ``errors``
        is non-empty (truncating the body of those entries if needed)."""
        if not errors or budget <= 0:
            return [], ""

        # Try fitting from newest backwards.
        newest_first = list(reversed(errors))
        kept_newest_first: list[str] = []
        running = self._tokenizer(self.SECTION_HEADERS["error_memory"]) + 1
        for err in newest_first:
            est = self._tokenizer(err) + 1  # +1 for separator
            if running + est > budget:
                break
            kept_newest_first.append(err)
            running += est

        if not kept_newest_first and self._min_error_memory_keep > 0:
            # Force-keep the most recent N errors, truncating body to
            # fit.  Avoids the "we promised to remember errors but
            # silently dropped them all" footgun.
            forced = newest_first[: self._min_error_memory_keep]
            body = "\n---\n".join(forced)
            block = self._block("error_memory", body)
            block = self._truncate_to_budget(block, max(64, budget))
            return forced, block

        if not kept_newest_first:
            return [], ""

        kept = list(reversed(kept_newest_first))
        body = "\n---\n".join(kept)
        return kept, self._block("error_memory", body)

    def _fit_task_context(self, ctx: str, *, budget: int) -> str:
        if not ctx or budget <= 0:
            return ""
        block = self._block("task_context", ctx)
        if self._tokenizer(block) <= budget:
            return block
        return self._truncate_to_budget(block, budget)

    def _truncate_to_budget(self, text: str, budget: int) -> str:
        """Drop characters from the end until ``text`` fits the
        token budget.  Approximate but monotonic — always converges."""
        if budget <= 0:
            return ""
        if self._tokenizer(text) <= budget:
            return text
        # Convert budget back to a character estimate using the
        # average ratio observed for this text.  Iterates at most
        # ~5 times in practice.
        est_chars = max(64, budget * max(1, len(text) // max(1, self._tokenizer(text))))
        truncated = text[:est_chars]
        # Tighten: shrink by 5 % until it fits.
        while truncated and self._tokenizer(truncated) > budget:
            truncated = truncated[: max(64, int(len(truncated) * 0.95))]
            if len(truncated) < 64:
                break
        # Append a clear marker so the LLM knows it was clipped.
        return (truncated + "\n[...TRUNCATED]").strip()


__all__ = ["AntonBrain", "Tokeniser"]
