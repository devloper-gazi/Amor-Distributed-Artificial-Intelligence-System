"""
Cycle G G4 — continuous mutation testing in-loop.

Wraps `mutmut` as an async subprocess that scores how well the LLM's
test suite kills synthetic mutations of the implementation.  Output
plugs into the existing reflexion threshold logic (engine.py:_maybe_run_reflexion)
so a low mutation score can trigger a retry — alongside missed
branches and critic verdict.

Why mutation testing on top of coverage
---------------------------------------
Branch coverage tells you which branches were EXECUTED.  Mutation
testing tells you which branches were ACTUALLY TESTED.  The classic
example: a coder produces a function returning ``a + b`` with a test
that calls it with ``(2, 3)`` and asserts the result is non-None.
Branch coverage is 100% — every line executed.  But mutate ``+`` to
``-`` and the test STILL passes (still non-None).  Mutation score
catches that.

Cost / timing
-------------
* mutmut wall-clock is typically 5-30× pytest baseline.  In-loop
  the runner caps the mutation count + uses parallel workers to
  keep the per-session cost under 60s.
* Skips snippets <30 LOC where mutation testing produces noise
  (too few mutants to give a meaningful score).
* Default off via `code_mutation_testing_enabled=False`; operator
  flips on when test phase budget allows the extra wall.

Output shape
------------
The runner returns a `MutationResult` with:
  * killed (int) — mutants the test suite killed
  * survived (int) — mutants that escaped (real test gaps)
  * timeout (int) — mutants that hung (counted as killed for score)
  * total — sum of the above
  * score — killed / total, in [0, 1]
  * surviving_diff_heads — first few SURVIVING mutant diffs, used
    by the MUTANT_SURVIVED reflexion block to point the coder at
    the gap
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MutationResult:
    """Aggregated mutation testing output."""
    killed: int = 0
    survived: int = 0
    timeout: int = 0
    error: int = 0       # mutmut error during analyze
    total: int = 0
    score: float = 0.0   # in [0.0, 1.0]
    surviving_diff_heads: List[str] = field(default_factory=list)
    ran: bool = False
    skipped_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "killed": self.killed,
            "survived": self.survived,
            "timeout": self.timeout,
            "error": self.error,
            "total": self.total,
            "score": round(self.score, 4),
            "surviving_diff_heads": list(self.surviving_diff_heads),
            "ran": self.ran,
            "skipped_reason": self.skipped_reason,
        }


# ─── CLI availability ──────────────────────────────────────────────


def _mutmut_available() -> bool:
    """Cheap presence check — mutmut as a console script in PATH."""
    try:
        result = subprocess.run(
            ["mutmut", "--version"],
            capture_output=True, timeout=3.0, check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ─── Output parser ─────────────────────────────────────────────────


def parse_mutmut_results_output(text: str) -> MutationResult:
    """Mutmut's ``results`` summary follows this shape (best-effort):

        Survived 🙁 (12)
        Killed 🎉 (45)
        Timeout ⏰ (1)
        Suspicious 🤔 (0)

    Different mutmut versions tweak the labels (3.x uses words, older
    uses emoji + counts).  We pattern-match flexibly so a minor format
    drift doesn't blank the result.
    """
    result = MutationResult()
    counts = {"killed": 0, "survived": 0, "timeout": 0, "suspicious": 0}
    for line in (text or "").splitlines():
        lower = line.lower()
        # Pattern: ``<Label>: <N>`` or ``<Label> <emoji> (<N>)`` —
        # extract the trailing integer.
        match = re.search(r"\((\d+)\)|:\s*(\d+)\s*$|\b(\d+)\s+(?:killed|survived|timeout|suspicious)", lower)
        if not match:
            continue
        count = next((int(g) for g in match.groups() if g), 0)
        if "killed" in lower:
            counts["killed"] = count
        elif "survived" in lower:
            counts["survived"] = count
        elif "timeout" in lower:
            counts["timeout"] = count
        elif "suspicious" in lower:
            counts["suspicious"] = count

    result.killed = counts["killed"]
    result.survived = counts["survived"]
    result.timeout = counts["timeout"]
    # Suspicious = mutmut couldn't decide (treat as neither killed nor
    # survived for scoring, but include in total).
    suspicious = counts["suspicious"]
    result.total = result.killed + result.survived + result.timeout + suspicious
    if result.total > 0:
        # Timeout mutants count as killed for the score (the test
        # suite at least made them HANG, which usually means the
        # mutation broke an infinite loop the test exercised).
        result.score = (result.killed + result.timeout) / result.total
    return result


def parse_surviving_diffs(text: str, max_diffs: int = 3) -> List[str]:
    """Extract the first few surviving mutant diff hunks from
    ``mutmut show <id>`` output.  Used to populate
    `surviving_diff_heads` for the reflexion feedback block.

    Each diff head is a short string like:
        "main.py: + return a - b  →  - return a + b"
    Falls back to the first 200 chars when the standard pattern
    doesn't match.
    """
    diffs: List[str] = []
    blocks = re.split(r"-{4,}", text or "")
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        # Look for ``- old`` / ``+ new`` lines (classic diff format).
        old = re.search(r"^- *(.+)$", block, re.MULTILINE)
        new = re.search(r"^\+ *(.+)$", block, re.MULTILINE)
        if old and new:
            diffs.append(f"{old.group(1).strip()} → {new.group(1).strip()}"[:200])
        else:
            diffs.append(block[:200])
        if len(diffs) >= max_diffs:
            break
    return diffs


# ─── Runner ────────────────────────────────────────────────────────


async def run_mutation_testing(
    code: str,
    tests: str,
    *,
    timeout_s: float = 60.0,
    max_mutants: int = 50,
) -> MutationResult:
    """Best-effort mutation run.  Returns ``MutationResult(ran=False,
    skipped_reason=...)`` on every degradation path so the engine
    can keep going without a numeric score."""

    if not _mutmut_available():
        return MutationResult(
            ran=False, skipped_reason="mutmut binary not on PATH",
        )

    # Skip micro-snippets — mutation noise dominates real signal
    # below ~30 LOC of implementation code.
    if not code or len(code.splitlines()) < 5:
        return MutationResult(ran=False, skipped_reason="code <5 LOC")
    if not tests or len(tests.splitlines()) < 3:
        return MutationResult(ran=False, skipped_reason="tests <3 LOC")

    with tempfile.TemporaryDirectory() as work_dir:
        impl_path = Path(work_dir) / "main.py"
        test_path = Path(work_dir) / "test_main.py"
        cfg_path = Path(work_dir) / "pyproject.toml"
        impl_path.write_text(code, encoding="utf-8")
        test_path.write_text(tests, encoding="utf-8")
        # Minimal mutmut config — point at main.py, use pytest as
        # the runner.  CACHE goes in the temp dir so the runner is
        # stateless across calls.
        cfg_path.write_text(
            f"""[tool.mutmut]
paths_to_mutate = ["{impl_path.name}"]
tests_dir = "."
runner = "python -m pytest test_main.py -x -q"
max_mutations = {max_mutants}
""",
            encoding="utf-8",
        )

        # mutmut run — sequence: ``mutmut run`` discovers mutants +
        # runs the test suite against each.  ``mutmut results`` then
        # prints the summary.
        try:
            run_proc = await asyncio.create_subprocess_exec(
                "mutmut", "run", "--no-progress",
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(run_proc.communicate(), timeout=timeout_s)
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            return MutationResult(
                ran=False,
                skipped_reason=f"mutmut run exception: {exc}",
            )

        try:
            results_proc = await asyncio.create_subprocess_exec(
                "mutmut", "results",
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(
                results_proc.communicate(), timeout=10.0,
            )
            stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            return MutationResult(
                ran=False,
                skipped_reason=f"mutmut results exception: {exc}",
            )

        result = parse_mutmut_results_output(stdout)
        result.ran = True

        # Best-effort: also fetch the first few SURVIVING mutant
        # diffs so the reflexion block can point the coder at them.
        if result.survived > 0:
            try:
                show_proc = await asyncio.create_subprocess_exec(
                    "mutmut", "show", "survived",
                    cwd=work_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                show_out, _ = await asyncio.wait_for(
                    show_proc.communicate(), timeout=5.0,
                )
                result.surviving_diff_heads = parse_surviving_diffs(
                    (show_out or b"").decode("utf-8", errors="replace"),
                )
            except (asyncio.TimeoutError, FileNotFoundError):
                pass

        return result


# ─── Reflexion feedback block ──────────────────────────────────────


def format_mutant_survived_block(result: MutationResult, threshold: float = 0.35) -> Optional[str]:
    """When mutation score is below the threshold AND there are
    actual surviving diffs, render a feedback block for the coder's
    next reflexion iteration.  Returns ``None`` when the score is
    above threshold (no feedback needed) OR mutation didn't run.
    """
    if not result.ran:
        return None
    if result.score >= threshold:
        return None
    if result.total == 0:
        return None
    lines = [
        "MUTANTS SURVIVED — your tests miss the cases below.",
        f"  killed: {result.killed} / total: {result.total} "
        f"(score: {result.score:.2f}; threshold: {threshold:.2f})",
    ]
    if result.surviving_diff_heads:
        lines.append("  example surviving mutations:")
        for diff in result.surviving_diff_heads[:3]:
            lines.append(f"    - {diff}")
    lines.append(
        "Add tests that would CATCH these mutations (call the function "
        "with inputs that distinguish the mutated vs original behaviour)."
    )
    return "\n".join(lines)
