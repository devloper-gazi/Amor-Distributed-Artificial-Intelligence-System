"""
Search/replace diff application — Phase 17 Commit T.

Aider / Cline / OpenHands V1 SDK all use a search/replace block
format for AI-emitted patches because small LLMs generate it
more reliably than unified diffs.  Each block:

    <<<<<<< SEARCH
    [old text — must match the target file EXACTLY]
    =======
    [new text]
    >>>>>>> REPLACE

The applier walks the blocks in order, requiring the SEARCH text
to appear *exactly once* in the current code state for a clean
match.  When a block fails to match (drift, multiple occurrences,
malformed fence) the whole patch is rejected and the caller
falls back to whole-file rewrite.

This is the only safe semantics for small models — partial
application would scramble the file and surface as a different
bug downstream.

License: MIT.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass


logger = logging.getLogger(__name__)


# Markdown fence carrying search/replace blocks.  Either ```diff
# or ```patch label is accepted; bare ``` ``` works too.
_SR_FENCE_RE = re.compile(
    r"```(?:diff|patch|search[-_]?replace)?\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)

_BLOCK_RE = re.compile(
    r"<{3,}\s*SEARCH\s*\n(.*?)\n=+\s*\n(.*?)\n>{3,}\s*REPLACE\s*",
    re.DOTALL,
)


@dataclass
class SearchReplaceBlock:
    search: str
    replace: str


@dataclass
class ApplyResult:
    ok: bool
    patched: str
    error: str = ""
    blocks_applied: int = 0
    blocks_total: int = 0


# ─── extraction ────────────────────────────────────────────────────


def extract_blocks(raw: str) -> list[SearchReplaceBlock]:
    """Pull every ``<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE``
    block out of an LLM reply.  Searches inside Markdown fences
    first; falls back to scanning the whole text if no fence is
    found."""
    if not raw:
        return []
    candidates: list[str] = []
    for fence in _SR_FENCE_RE.finditer(raw):
        candidates.append(fence.group(1))
    if not candidates:
        candidates.append(raw)
    blocks: list[SearchReplaceBlock] = []
    for source in candidates:
        for m in _BLOCK_RE.finditer(source):
            blocks.append(SearchReplaceBlock(
                search=m.group(1), replace=m.group(2),
            ))
    return blocks


# ─── application ───────────────────────────────────────────────────


def apply_blocks(
    code: str, blocks: list[SearchReplaceBlock],
) -> ApplyResult:
    """Apply each block in order; reject the whole patch on the
    first failure (drift / ambiguous match / empty).

    A block's SEARCH must appear *exactly once* in the current
    code state; that's the contract that protects against partial
    application.  Empty SEARCH means "prepend at top" — supported
    so tiny edits don't need a context anchor.
    """
    if not blocks:
        return ApplyResult(
            ok=False, patched=code, error="no search/replace blocks",
            blocks_total=0,
        )

    patched = code
    for i, block in enumerate(blocks, 1):
        search = block.search
        replace = block.replace

        if not search.strip():
            # Pure prepend — supported but rare.
            patched = replace.rstrip("\n") + "\n" + patched
            continue

        count = patched.count(search)
        if count == 0:
            return ApplyResult(
                ok=False, patched=code,
                error=(
                    f"block {i}/{len(blocks)} SEARCH not found in code "
                    f"(drift)"
                ),
                blocks_applied=i - 1,
                blocks_total=len(blocks),
            )
        if count > 1:
            return ApplyResult(
                ok=False, patched=code,
                error=(
                    f"block {i}/{len(blocks)} SEARCH appears {count} "
                    f"times — ambiguous match"
                ),
                blocks_applied=i - 1,
                blocks_total=len(blocks),
            )
        # Exactly one match → safe to replace.
        patched = patched.replace(search, replace, 1)

    return ApplyResult(
        ok=True, patched=patched,
        blocks_applied=len(blocks),
        blocks_total=len(blocks),
    )


def apply_search_replace_diff(
    code: str, raw: str,
) -> ApplyResult:
    """Convenience: extract blocks from ``raw`` and apply them to
    ``code``.  Returns ``ApplyResult.ok=False`` when the diff is
    empty or any block fails to apply cleanly."""
    blocks = extract_blocks(raw)
    if not blocks:
        return ApplyResult(
            ok=False, patched=code,
            error="no SEARCH/REPLACE blocks found in LLM output",
        )
    return apply_blocks(code, blocks)


__all__ = [
    "SearchReplaceBlock",
    "ApplyResult",
    "extract_blocks",
    "apply_blocks",
    "apply_search_replace_diff",
]
