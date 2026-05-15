"""
Code Intelligence pipeline engine.

Mirrors the structure of ``thinking/engine.py`` but with a 9-phase
pipeline tuned for software-engineering tasks:

    triage → model_prep → plan → implement → execute →
    analyze → test → debug (loop) → review

The engine is **LLM-agnostic**: the caller injects ``llm_call``, exactly
as ThinkingEngine does, so the same pipeline works against any local
Ollama tag, a mock, or (in principle) a remote model — but in practice
the routes only ever wire it to local Ollama, per the zero-API
constraint.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Optional

from .agents import (
    AgentContext,
    CoderAgent,
    CriticAgent,
    DebuggerAgent,
    LLMCall,
    PlannerAgent,
    TesterAgent,
    run_triage,
)
from .hooks import NoopHooks, PhaseHooks
from .sandbox import ExecutionResult, ExecutionSandbox
from .static_analysis import StaticAnalysisHarness, StaticAnalysisResult

logger = logging.getLogger(__name__)


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
PhaseStatus = Literal[
    "pending",
    "in_progress",
    "completed",
    "failed",
    "skipped",
]


# ─────────────────────────────────────────────────────────────────────────────
# Phase definitions + budgets
# ─────────────────────────────────────────────────────────────────────────────


CODE_PHASES: list[tuple] = [
    ("triage", "Triaging request"),
    ("model_prep", "Preparing models"),
    ("plan", "Planning approach"),
    ("implement", "Implementing"),
    ("execute", "Executing code"),
    ("analyze", "Analyzing quality"),
    ("test", "Running tests"),
    ("debug", "Debugging & fixing"),
    ("review", "Final review"),
]


# Mirrors ThinkingEngine.PHASE_PROGRESS — per-phase end progress %.
PHASE_PROGRESS: dict[str, int] = {
    "triage": 10,
    "model_prep": 15,
    "plan": 25,
    "implement": 50,
    "execute": 60,
    "analyze": 68,
    "test": 78,
    "debug": 88,
    "review": 98,
}


# Effort tier → per-phase token budget. Mirrors thinking/engine.py.
_CODE_EFFORT_BUDGETS: dict[str, dict[str, int]] = {
    "basic": {"plan": 800, "implement": 2000, "test": 600, "debug": 1000, "review": 600},
    "medium": {"plan": 1200, "implement": 3500, "test": 1000, "debug": 1800, "review": 1000},
    "deep": {"plan": 1500, "implement": 5000, "test": 1500, "debug": 2500, "review": 1500},
    "expert": {"plan": 2000, "implement": 7000, "test": 2000, "debug": 3500, "review": 2000},
    "ultra": {"plan": 2500, "implement": 9000, "test": 3000, "debug": 5000, "review": 3000},
}


# Effort tier → max debug→fix→reexecute iterations.
_DEFAULT_DEBUG_ITERATIONS: dict[str, int] = {
    "basic": 1,
    "medium": 3,
    "deep": 3,
    "expert": 5,
    "ultra": 5,
}


# Legacy aliases — same set as ThinkingEngine.
_EFFORT_ALIAS: dict[str, str] = {
    "quick": "basic",
    "fast": "basic",
    "standard": "medium",
    "balanced": "medium",
    "thorough": "deep",
    "comprehensive": "expert",
    "exhaustive": "ultra",
}


def _canonical_effort(effort: str) -> str:
    if not effort:
        return "medium"
    key = str(effort).strip().lower()
    key = _EFFORT_ALIAS.get(key, key)
    return key if key in _CODE_EFFORT_BUDGETS else "medium"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class CodePhase:
    name: str
    label: str
    status: PhaseStatus = "pending"
    detail: dict[str, Any] = field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────


class CodeIntelligenceEngine:
    """Orchestrates a single Code Intelligence session end-to-end."""

    PHASE_PROGRESS = PHASE_PROGRESS  # exposed for routes

    def __init__(
        self,
        *,
        prompt: str,
        code_context: str | None,
        language: str | None,
        effort: str,
        provider: str,
        llm_call: LLMCall,
        sandbox: ExecutionSandbox | None = None,
        static_harness: StaticAnalysisHarness | None = None,
        enable_execution: bool = True,
        enable_static_analysis: bool = True,
        enable_testing: bool = True,
        max_debug_iterations: int | None = None,
        on_event: EventCallback | None = None,
        # Optional pre-flight helpers (the routes layer plugs these in
        # when it wants to surface model-download progress to the UI).
        prepare_models: Callable[[], Awaitable[dict[str, str]]] | None = None,
        # Charter §6 Mandate 3 — phase-boundary hooks. Defaults to
        # NoopHooks; routes wire ChainedHooks(TelemetryHooks(), ...)
        # when they want span emission per phase.
        hooks: PhaseHooks | None = None,
        # v4 — per-phase agent-role hint. Routes wire this to the
        # local-AI ContextVar `set_active_role` so multi-model strategy
        # (per_role / ensemble) can pick a different tag for each
        # phase without changing the engine's LLM-agnostic shape.
        # Called once at the START of every agent phase with the role
        # name (e.g. "planner", "coder", "tester", "debugger", "critic").
        role_setter: Callable[[str | None], Any] | None = None,
        # Phase 17 Commit S — routing_setter inverts the previous
        # engine→routes layer violation.  Routes inject a callable
        # that takes the auto-derived ``{role: tag}`` dict from
        # ``_phase_model_prep`` and pushes it into the active
        # routing ContextVar (so ``call_ollama_with`` can resolve
        # per-role tags).  Default ``None`` is a no-op so the engine
        # stays unit-testable in isolation.
        routing_setter: Callable[[dict], None] | None = None,
    ) -> None:
        self.prompt = prompt
        self.code_context = code_context or None
        self.language_hint = (language or "").lower() or None
        self.effort = _canonical_effort(effort)
        self.provider = provider
        self.llm_call = llm_call
        self._budgets = _CODE_EFFORT_BUDGETS[self.effort]
        self.max_debug_iterations = (
            max_debug_iterations
            if max_debug_iterations is not None
            else _DEFAULT_DEBUG_ITERATIONS[self.effort]
        )
        self.enable_execution = enable_execution
        self.enable_static_analysis = enable_static_analysis
        self.enable_testing = enable_testing

        self.sandbox = sandbox
        self.static_harness = static_harness or StaticAnalysisHarness()
        self._prepare_models = prepare_models
        self._on_event = on_event or _noop_event
        # PhaseHooks default = NoopHooks (zero overhead, never raises).
        self._hooks: PhaseHooks = hooks or NoopHooks()
        # v4 — role_setter is a no-op fallback so engine code stays
        # branch-free at every phase boundary.
        self._role_setter = role_setter or (lambda _r: None)
        # Phase 17 Commit S — routing_setter same shape as role_setter.
        self._routing_setter: Callable[[dict], None] = (
            routing_setter or (lambda _doc: None)
        )

        self.phases: list[CodePhase] = [
            CodePhase(name=name, label=label) for name, label in CODE_PHASES
        ]
        self._phase_index = {p.name: p for p in self.phases}

        # Accumulated state — surfaced in snapshot()
        self.triage: dict[str, Any] = {}
        self.models_used: dict[str, str] = {}
        self.plan: dict[str, Any] = {}
        # Phase 17 Commit M — coder metadata stored separately so
        # ``_phase_execute`` can forward ``dependencies`` to the
        # sandbox.  Without this, snake-game-website kept crashing
        # with ``ModuleNotFoundError: No module named 'flask'``.
        self.coder_metadata: dict[str, Any] = {}
        # Cycle B Commit V — cache install_packages + extra_files at
        # session level so the debug-retry loop can re-run the sandbox
        # with the SAME deps and the SAME companion files.  Previously
        # ``_phase_debug`` called ``sandbox.execute(code=..., language=)``
        # with no deps, so a missing-Flask error stayed missing for
        # every iteration.
        self.install_packages: list[str] = []
        self.extra_files: dict[str, str] = {}
        self.code: str | None = None
        self.tests: str | None = None
        self.test_metadata: dict[str, Any] = {}
        self.execution_results: list[dict[str, Any]] = []
        self.static_analysis: StaticAnalysisResult | None = None
        # Cycle F Sprint 2 — coverage_report from coverage_reader.
        # Always None outside `_phase_test`; populated when pytest-cov
        # produced a parseable .coverage.json.
        self.coverage_report: Any = None
        self.review: dict[str, Any] = {}
        self.deliverable_markdown: str = ""
        self.debug_iterations_used: int = 0
        self.detected_language: str = self.language_hint or "python"
        self.title: str = "Code task"
        # v18.1 Step 4 (Cycle G) — async critic decoupling.  When
        # `code_critic_async=True` (default), the critic LLM call is
        # kicked off as a background task right after the parallel
        # (execute, analyze, test) block completes, in parallel with
        # the debug retry loop.  `_phase_review` then awaits the task
        # via `_resolve_critic_task()` with a verdict-freshness
        # timeout fallback.  The fallback default is
        # `approved_with_minor` + score 70, matching the existing
        # critic-unavailable error path so the score function stays
        # well-defined.
        self._critic_task: asyncio.Task | None = None
        # Hash of `self.code` at the moment the critic was kicked off.
        # Used by `_phase_review` to detect when debug retries modified
        # the code while the critic was in flight; on mismatch we
        # cancel + re-launch the critic on the post-debug code.
        self._critic_code_hash: str | None = None

    # ── helpers ───────────────────────────────────────────────────────────

    async def _emit(self, event: dict[str, Any]) -> None:
        try:
            await self._on_event(event)
        except Exception:
            logger.exception("code.on_event callback raised")

    async def _run_phase(
        self,
        name: str,
        runner: Callable[[], Awaitable[dict[str, Any] | None]],
    ) -> dict[str, Any] | None:
        phase = self._phase_index[name]
        phase.status = "in_progress"
        phase.started_at = _now()
        await self._emit({"type": "phase_start", "phase": name, "label": phase.label})
        # Charter §6 Mandate 3 — fire phase-boundary hook.
        await self._hooks.before_phase(name, self.snapshot())
        try:
            result = await runner()
            phase.status = "completed"
            phase.completed_at = _now()
            phase.detail = result or {}
            await self._emit(
                {
                    "type": "phase_complete",
                    "phase": name,
                    "label": phase.label,
                    "detail": phase.detail,
                }
            )
            await self._hooks.after_phase(name, self.snapshot(), result)
            return result
        except Exception as exc:
            phase.status = "failed"
            phase.completed_at = _now()
            phase.detail = {"error": str(exc)}
            logger.exception("code.phase_failed phase=%s", name)
            # Hook still fires on failure so telemetry captures the timing.
            try:
                await self._hooks.after_phase(name, self.snapshot(), None)
            except Exception:
                logger.debug("after_phase hook raised on failure path")
            await self._emit(
                {
                    "type": "phase_failed",
                    "phase": name,
                    "label": phase.label,
                    "error": str(exc),
                }
            )
            return None

    def _skip(self, name: str, reason: str) -> None:
        self._phase_index[name].status = "skipped"
        self._phase_index[name].detail = {"reason": reason}

    # ── phases ────────────────────────────────────────────────────────────

    async def _phase_triage(self) -> dict[str, Any]:
        # Sprint 3 Day 4 — repomap context injection (opt-in via env).
        # Prepend a token-budgeted markdown block to ``code_context`` so
        # triage sees the repository's symbol surface before deciding
        # language / complexity / phases.  No-op when AMOR_REPOMAP_ENABLED
        # is unset / 0 / false.  No-op if rendering raises (import error,
        # cache miss, etc.) — triage must never fail on repomap.
        effective_context = self.code_context
        repomap_meta: Optional[dict[str, Any]] = None
        if os.environ.get("AMOR_REPOMAP_ENABLED", "").lower() in {"1", "true", "yes"}:
            try:
                from ..services.repo_map import render_repomap  # noqa: PLC0415
                from pathlib import Path  # noqa: PLC0415
                t0 = time.perf_counter()
                budget = int(os.environ.get("AMOR_REPOMAP_BUDGET_TOKENS", "2048"))
                repomap_md = render_repomap(
                    repo_root=Path(os.environ.get("AMOR_REPOMAP_ROOT", "/app")),
                    budget_tokens=budget,
                    rescan=False,    # rely on cache; first call seeds it
                )
                ms = (time.perf_counter() - t0) * 1000.0
                if repomap_md:
                    effective_context = (
                        repomap_md + "\n\n---\n\n" + (self.code_context or "")
                    )
                    repomap_meta = {
                        "tokens_estimate": len(repomap_md) // 4,
                        "render_ms": int(ms),
                        "budget_tokens": budget,
                    }
            except Exception as exc:
                # Non-fatal — log and continue without repomap.
                logger.warning("repomap render failed: %s", exc)

        triage = await run_triage(
            self.llm_call,
            self.prompt,
            effective_context,
            max_tokens=600,
        )
        # Honour the explicit language hint over what the model guessed.
        if self.language_hint:
            triage["language"] = self.language_hint
        if repomap_meta is not None:
            triage["_repomap"] = repomap_meta
            await self._emit({"type": "repomap_attached", **repomap_meta})

        # Sprint 7 Day 4 — Mem0 recall hook.  Pulls up to 5 memories
        # for the prompt's user, prepends them to the triage system
        # context, and emits a ``memory_recalled`` SSE event so the
        # frontend can render the "Remembered N" pill on the assistant
        # turn.  Always fault-tolerant — recall_for_prompt swallows
        # every error and returns count=0.
        try:
            from ..services.memory_recall import (  # noqa: PLC0415
                memory_recall_enabled_in_engine,
                recall_for_prompt,
                format_recall_block,
            )
            if memory_recall_enabled_in_engine():
                user_id = (
                    getattr(self, "user_id", None)
                    or getattr(self, "_user_id", None)
                    or "local"
                )
                limit = int(os.environ.get("AMOR_MEMORY_RECALL_LIMIT", "5"))
                recall = await recall_for_prompt(
                    self.prompt or "",
                    user_id=user_id,
                    limit=limit,
                )
                if recall.count > 0:
                    block = format_recall_block(recall)
                    if block:
                        # Memories live ABOVE the repomap block so the
                        # planner sees the user's preferences first.
                        effective_context = (
                            block + "\n---\n\n" + (effective_context or "")
                        )
                    await self._emit({
                        "type": "memory_recalled",
                        "count": recall.count,
                        "snippets": recall.snippets[:3],
                        "backend": recall.backend,
                    })
        except Exception as exc:
            logger.warning("memory recall hook failed: %s", exc)

        self.triage = triage
        self.detected_language = triage.get("language") or self.detected_language
        return triage

    async def _phase_model_prep(self) -> dict[str, Any]:
        if self._prepare_models is None:
            return {"models_used": {}, "skipped": True}
        models_used = await self._prepare_models()
        self.models_used = dict(models_used or {})

        # Phase 17 Commit S — promote the per-role tag map into the
        # active routing via the injected ``routing_setter`` callback
        # so the engine doesn't have to import from the routes layer
        # (the previous engine→routes layer violation).  Routes wire
        # the callback to ``set_active_routing`` when constructing
        # the engine.
        if self.models_used and len(set(self.models_used.values())) > 1:
            try:
                self._routing_setter({
                    "strategy": "per_role",
                    "role_routes": dict(self.models_used),
                })
            except Exception as exc:  # pragma: no cover
                logger.debug(
                    "auto-routing setup failed (non-fatal): %s", exc,
                )

        return {"models_used": self.models_used}

    async def _phase_plan(self) -> dict[str, Any]:
        # v17 PR #1 — emit ``architect`` as the active routing role
        # so the per-role tag map (built by ``_phase_model_prep`` from
        # ``select_models_for_session``) routes this phase to the
        # reasoning-tuned model (DeepSeek-R1-Distill when installed,
        # else qwen2.5:7b).  ``planner`` is preserved as a registry
        # alias for back-compat with quick_code + older callers.
        self._role_setter("architect")
        agent = PlannerAgent(self.llm_call, max_tokens=self._budgets["plan"])
        out = await agent.run(
            AgentContext(
                user_prompt=self.prompt,
                code_context=self.code_context,
                triage=self.triage,
            )
        )
        if out.error or not out.data:
            raise RuntimeError(out.error or "Planner returned empty plan")
        self.plan = out.data
        # Cycle D — propagate triage's domain detection into the
        # plan dict so the coder prompt's domain directive renderer
        # can read it from a single source.
        if isinstance(self.triage, dict) and self.triage.get("domain"):
            self.plan["domain"] = self.triage["domain"]
        # Pin language + title from the plan if present.
        if out.data.get("language"):
            self.detected_language = out.data["language"]
        if out.data.get("title"):
            self.title = out.data["title"]
        # Cycle D Fix #6 — surface the resilience-fallback flag so the
        # UI can show a subtle banner and operators can spot the
        # degradation in event logs.  The plan is otherwise treated
        # exactly like a normal plan (the minimal fallback is shaped
        # to satisfy every downstream consumer).
        if out.data.get("_resilience_fallback"):
            await self._emit({
                "type": "planner_fallback",
                "reason": "planner_llm_unparseable_or_empty",
                "language": self.detected_language,
                "title": self.title,
            })
            logger.info("planner_fallback_used language=%s", self.detected_language)
        return out.data

    async def _phase_implement(self) -> dict[str, Any]:
        # v17 PR #1 — fire ``editor`` so the implement phase routes
        # to the code-specialist tag (qwen2.5-coder:7b/14b).  ``coder``
        # remains in the registry as an alias.
        self._role_setter("editor")

        # Cycle D Fix #5 — extract a focused spec from the plan.
        # The planner often emits abstract step lists ("use Doxygen /
        # Sphinx"); the coder needs concrete signatures + headers.
        # Build a smaller, language-aware ``focused_spec`` block and
        # attach it to the plan dict — the coder prompt renders it as
        # a HIGHER-priority section than the free-form plan.
        plan_for_coder = self._extract_focused_spec(
            self.plan or {}, self.detected_language,
        )

        agent = CoderAgent(self.llm_call, max_tokens=self._budgets["implement"])
        out = await agent.run(
            AgentContext(
                user_prompt=self.prompt,
                code_context=self.code_context,
                triage=self.triage,
                plan=plan_for_coder,
                language=self.detected_language,
            )
        )
        if out.error or not out.code:
            raise RuntimeError(out.error or "Coder produced no code")
        self.code = out.code
        prev_language = self.detected_language
        if out.data.get("language"):
            self.detected_language = out.data["language"]
        # Phase 17 Commit M — capture coder metadata (incl. the
        # ``dependencies`` list) so ``_phase_execute`` can forward
        # them as ``install_packages`` to the sandbox.
        self.coder_metadata = dict(out.data or {})

        # Cycle B Commit V — if the post-coder sniff flipped the
        # language away from python (e.g. the sniffer caught a
        # ``<!DOCTYPE html>`` body that the planner had labelled as
        # python+flask), drop the now-stale dependency list.  Otherwise
        # ``_phase_execute`` will try to ``pip install flask`` against
        # an HTML runner that has no pip in the first place, producing
        # the cascade of failures the user reported as the snake-game
        # screenshot.
        if (
            prev_language == "python"
            and self.detected_language in {"html", "css"}
        ):
            spec = (self.plan or {}).get("spec") or {}
            if isinstance(spec, dict) and spec.get("dependencies"):
                logger.info(
                    "language_corrected python→%s — clearing %d stale deps",
                    self.detected_language,
                    len(spec.get("dependencies") or []),
                )
                spec["dependencies"] = []
            if self.coder_metadata.get("dependencies"):
                self.coder_metadata["dependencies"] = []
            await self._emit(
                {
                    "type": "language_corrected",
                    "from": prev_language,
                    "to": self.detected_language,
                    "reason": "coder_output_sniff",
                }
            )

        await self._emit(
            {
                "type": "code_ready",
                "language": self.detected_language,
                "code": self.code,
                "metadata": out.data,
            }
        )
        return {
            "language": self.detected_language,
            "loc": len(self.code.splitlines()),
            "metadata": out.data,
        }

    # Phase 17 Commit M — dependency sanitiser.  Allow-list-only
    # to keep arbitrary shell metacharacters out of the
    # ``pip install ...`` command-line the sandbox builds.
    _DEP_RE = re.compile(
        r"^[a-zA-Z][\w\-\.]*(?:\[[\w\-\.,]+\])?(?:[<>=!~]=?[\w\-\.\+]+)?$"
    )

    @classmethod
    def _sanitise_dependencies(
        cls, raw: list, *, max_packages: int = 12,
    ) -> list[str]:
        """Filter a candidate dependency list against an allow-list
        regex + cap at ``max_packages``.  Empty / non-string entries
        are dropped; entries failing the regex are dropped.  Returns
        the sanitised list.  Never raises."""
        out: list[str] = []
        seen: set[str] = set()
        for entry in raw or []:
            if not isinstance(entry, str):
                continue
            cleaned = entry.strip()
            if not cleaned or cleaned in seen:
                continue
            if len(cleaned) > 80:
                continue
            if not cls._DEP_RE.match(cleaned):
                continue
            out.append(cleaned)
            seen.add(cleaned)
            if len(out) >= max_packages:
                break
        return out

    # Cycle D Fix #3 — language-aware install_packages cross-check.
    # The user's "a c++ system for user guide" Build run installed
    # `doxygen` + `latex` even though the generated C++ never invoked
    # either.  Filter declared dependencies against actual code use
    # so the sandbox doesn't waste time on unused packages.
    @staticmethod
    def _filter_unused_packages(
        packages: list[str], code: str, language: str,
    ) -> tuple[list[str], list[str]]:
        """Return ``(kept, dropped)`` after cross-checking ``packages``
        against the imports/includes actually present in ``code``.
        Conservative: when in doubt, KEEP the package.

          * python: keep packages whose canonical module name appears in
            an ``import X`` / ``from X(.Y)? import`` / ``import X as Y``
            statement.  Build-tool packages (e.g. ``setuptools``) always
            keep.
          * javascript / typescript: keep packages whose name appears in
            ``require("X")`` or ``from "X"`` / ``import "X"`` lines.
          * cpp / c: drop unconditionally unless the code shells out to
            them via ``system(...)`` / ``popen(...)`` / ``exec*(...)``.
            Self-contained C++ (the common case) does not need ANY
            sandbox-installed packages — gcc + the STL are enough.
          * Anything else: pass-through unchanged.
        """
        if not packages:
            return [], []
        if not code:
            return list(packages), []

        kept: list[str] = []
        dropped: list[str] = []

        if language in {"cpp", "c"}:
            # Drop everything unless the code clearly invokes a build
            # tool via shell-out.  Single conservative check.
            shell_hint = bool(re.search(
                r"\b(system|popen|execl|execv|execvp|fork)\s*\(",
                code,
            ))
            if not shell_hint:
                dropped = list(packages)
                return [], dropped
            # Code DOES shell out — keep packages whose name appears in
            # a shell-out string literal.
            for pkg in packages:
                pkg_root = pkg.split("[")[0].split("==")[0].split(">")[0].split("<")[0].split("~")[0]
                if pkg_root and re.search(
                    r'["\'].*\b' + re.escape(pkg_root) + r'\b.*["\']',
                    code,
                ):
                    kept.append(pkg)
                else:
                    dropped.append(pkg)
            return kept, dropped

        if language == "python":
            # Map a few well-known PyPI names to their import roots.
            pypi_to_module = {
                "beautifulsoup4": "bs4",
                "pillow": "PIL",
                "pyyaml": "yaml",
                "scikit-learn": "sklearn",
                "opencv-python": "cv2",
                "python-dateutil": "dateutil",
                "msgpack-python": "msgpack",
            }
            # Always-keep build helpers
            always_keep = {"setuptools", "wheel", "pip"}
            for pkg in packages:
                pkg_root = pkg.split("[")[0].split("==")[0].split(">")[0].split("<")[0].split("~")[0].lower()
                if pkg_root in always_keep:
                    kept.append(pkg)
                    continue
                module = pypi_to_module.get(pkg_root, pkg_root.replace("-", "_"))
                # Match `import module`, `from module ...`, `import module as ...`
                if re.search(
                    r"^\s*(?:import\s+" + re.escape(module)
                    + r"|from\s+" + re.escape(module) + r"(?:\.\w+)*\s+import)",
                    code,
                    re.MULTILINE,
                ):
                    kept.append(pkg)
                else:
                    dropped.append(pkg)
            return kept, dropped

        if language in {"javascript", "typescript", "tsx", "jsx"}:
            for pkg in packages:
                pkg_root = pkg.split("[")[0].split("@")[0] if not pkg.startswith("@") else pkg.split("/")[0]
                if not pkg_root:
                    dropped.append(pkg)
                    continue
                escaped = re.escape(pkg_root)
                if re.search(
                    r'(?:require\s*\(\s*["\']' + escaped + r'(?:/|["\']))'
                    r'|(?:from\s+["\']' + escaped + r'(?:/|["\']))'
                    r'|(?:import\s+["\']' + escaped + r'(?:/|["\']))',
                    code,
                ):
                    kept.append(pkg)
                else:
                    dropped.append(pkg)
            return kept, dropped

        # Unknown language → pass-through (safer than dropping)
        return list(packages), []

    # Cycle D Fix #5 — plan-to-spec extractor.  The planner output is
    # often abstract ("use Doxygen / Sphinx"); the coder needs concrete
    # signatures + dependencies.  Compress the plan's authoritative
    # ``spec`` block into a focused dict + (for C++) a list of likely-
    # needed STL headers so the coder grounds its implementation in
    # measurable constraints.  Pure data-shape transform; never raises.
    @staticmethod
    def _extract_focused_spec(
        plan: dict[str, Any], language: str,
    ) -> dict[str, Any]:
        """Return a NEW plan dict with a ``focused_spec`` field added.

        The original plan dict is left untouched (shallow copy).  The
        focused_spec contains only the spec sub-fields the coder
        actually needs (signatures, invariants, dependencies, files)
        plus, for C++ specifically, a ``suggested_includes`` list
        derived from the spec's signatures + dependencies.
        """
        if not isinstance(plan, dict):
            return plan
        spec = plan.get("spec") or {}
        if not isinstance(spec, dict):
            spec = {}

        focused: dict[str, Any] = {}
        for key in ("signatures", "invariants", "preconditions",
                    "postconditions", "error_cases", "dependencies",
                    "files"):
            val = spec.get(key)
            if val:
                focused[key] = val

        # For C++: scan the signatures + dependencies for std::*
        # references and pre-list the likely headers the coder needs.
        if language == "cpp":
            try:
                from . import prompts as _P  # noqa: PLC0415
                table = _P.CPP_STD_SYMBOL_TO_HEADER
            except Exception:
                table = {}
            if table:
                blob_parts: list[str] = []
                for v in focused.values():
                    if isinstance(v, str):
                        blob_parts.append(v)
                    elif isinstance(v, list):
                        for item in v:
                            blob_parts.append(str(item))
                blob = "\n".join(blob_parts)
                seen: set[str] = set()
                suggested: list[str] = []
                for sym, header in table.items():
                    if header in seen:
                        continue
                    if re.search(r"\bstd::" + re.escape(sym) + r"\b", blob):
                        suggested.append(header)
                        seen.add(header)
                if suggested:
                    focused["suggested_includes"] = suggested

        if not focused:
            return plan  # nothing to add — return original

        # Shallow copy the plan + attach focused_spec.  Only mutate the
        # copy so callers' dicts stay untouched.
        plan_copy = dict(plan)
        plan_copy["focused_spec"] = focused
        return plan_copy

    async def _phase_execute(self) -> dict[str, Any]:
        if not self.enable_execution or not self.sandbox or not self.code:
            self._skip("execute", "execution disabled or no code")
            return {"skipped": True}
        await self._emit(
            {
                "type": "execution_start",
                "language": self.detected_language,
            }
        )
        # Phase 17 Commit M — forward `dependencies` from the plan
        # spec block + coder metadata to the sandbox so the user's
        # earlier ``ModuleNotFoundError: No module named 'flask'``
        # scenario actually pip-installs flask before running.
        deps_raw = []
        spec = (self.plan or {}).get("spec") or {}
        if isinstance(spec, dict):
            deps_raw.extend(spec.get("dependencies") or [])
        deps_raw.extend(self.coder_metadata.get("dependencies") or [])
        install_packages = self._sanitise_dependencies(deps_raw)

        # Cycle D Fix #3 — cross-check declared deps against actual
        # code use.  Drops e.g. `doxygen` + `latex` from a self-
        # contained C++ formatter that never shells out.
        if install_packages and self.code:
            kept, dropped = self._filter_unused_packages(
                install_packages, self.code, self.detected_language,
            )
            if dropped:
                logger.info(
                    "install_packages_filtered language=%s kept=%d dropped=%s",
                    self.detected_language, len(kept), dropped,
                )
                await self._emit({
                    "type": "install_packages_filtered",
                    "language": self.detected_language,
                    "kept": kept,
                    "dropped": dropped,
                })
            install_packages = kept

        try:
            from ..config.settings import settings  # noqa: PLC0415
            if not getattr(settings, "code_sandbox_pip_install_enabled", True):
                install_packages = []
        except Exception:
            pass

        # Cycle B Commit V — extra companion files (e.g. Flask
        # ``templates/snake_game.html``, a CSS sidecar for an HTML
        # entry).  Mirrored from spec.files + coder additional_files;
        # cached on ``self`` so the debug-retry loop can re-mount the
        # same files alongside patched code.
        extra_files = self._collect_extra_files()

        # Cache for debug retries.
        self.install_packages = list(install_packages)
        self.extra_files = dict(extra_files)

        if install_packages:
            await self._emit({
                "type": "execution_install_packages",
                "packages": list(install_packages),
                "language": self.detected_language,
            })
        if extra_files:
            await self._emit({
                "type": "execution_extra_files",
                "files": sorted(extra_files.keys()),
                "language": self.detected_language,
            })

        result = await self.sandbox.execute(
            code=self.code,
            language=self.detected_language,
            install_packages=install_packages or None,
            extra_files=extra_files or None,
        )
        self.execution_results.append(result.to_dict())
        await self._emit(
            {
                "type": "execution_result",
                "result": result.to_dict(),
                "iteration": 0,
            }
        )
        return result.to_dict()

    # Cycle B Commit V — sidecar-file collector.  Pulls "additional_files"
    # / "files" from coder metadata or planner spec and normalises into a
    # ``{path: content}`` map the sandbox accepts directly.  Bounded:
    # at most 16 files, 64 KB each, paths kept relative + safe.
    _MAX_EXTRA_FILES = 16
    _MAX_EXTRA_FILE_BYTES = 64 * 1024

    def _collect_extra_files(self) -> dict[str, str]:
        candidates: list[Any] = []
        coder_files = self.coder_metadata.get("additional_files")
        if coder_files is not None:
            candidates.append(coder_files)
        spec = (self.plan or {}).get("spec") or {}
        if isinstance(spec, dict):
            spec_files = spec.get("files") or spec.get("additional_files")
            if spec_files is not None:
                candidates.append(spec_files)

        merged: dict[str, str] = {}
        for source in candidates:
            iterable: list[Any] = []
            if isinstance(source, dict):
                iterable = list(source.items())
            elif isinstance(source, list):
                for item in source:
                    if isinstance(item, dict):
                        path = item.get("path") or item.get("name")
                        body = item.get("content") or item.get("body")
                        if path and body is not None:
                            iterable.append((path, body))
            for path_raw, content_raw in iterable:
                if not isinstance(path_raw, str) or not isinstance(content_raw, str):
                    continue
                # Sanitise path: must be relative, no traversal, no abs.
                p = path_raw.strip().lstrip("./").lstrip("/")
                if not p or ".." in p.split("/") or p.startswith("\\"):
                    continue
                if len(p) > 200:
                    continue
                if len(content_raw) > self._MAX_EXTRA_FILE_BYTES:
                    continue
                if p in merged:
                    continue
                merged[p] = content_raw
                if len(merged) >= self._MAX_EXTRA_FILES:
                    return merged
        return merged

    async def _phase_analyze(self) -> dict[str, Any]:
        if not self.enable_static_analysis or not self.code:
            self._skip("analyze", "static analysis disabled or no code")
            return {"skipped": True}
        sa = await self.static_harness.analyze(self.code, self.detected_language)
        self.static_analysis = sa
        payload = sa.to_dict()
        await self._emit(
            {
                "type": "static_analysis_result",
                "result": payload,
            }
        )
        return payload

    async def _phase_test(self) -> dict[str, Any]:
        if not self.enable_testing or not self.code:
            self._skip("test", "testing disabled or no code")
            return {"skipped": True}
        # Skip tests for explanation/architecture-only deliverables.
        if self.plan.get("deliverable_type") in {
            "explanation",
            "architecture_doc",
        }:
            self._skip("test", "non-code deliverable")
            return {"skipped": True}
        self._role_setter("tester")
        # Cycle F Sprint 2 — property_mode is gated by
        # settings.code_property_tests_enabled (default True).  The
        # prompt itself short-circuits to a no-op for non-Python so
        # we can pass True unconditionally without harming JS/Go/etc.
        try:
            from ..config.settings import settings  # noqa: PLC0415
            _property_on = bool(getattr(settings, "code_property_tests_enabled", True))
        except Exception:
            _property_on = True
        agent = TesterAgent(
            self.llm_call,
            max_tokens=self._budgets["test"],
            property_mode=_property_on,
        )
        out = await agent.run(
            AgentContext(
                user_prompt=self.prompt,
                plan=self.plan,
                code=self.code,
                language=self.detected_language,
            )
        )
        if out.error or not out.code:
            # Tester failure shouldn't kill the pipeline.
            return {
                "skipped": True,
                "reason": out.error or "tester returned empty",
            }
        self.tests = out.code
        # Cycle F Sprint 2 — stash tester data so _score_candidate
        # can surface property_tests_present in the breakdown dict.
        self.test_metadata = out.data or {}
        await self._emit(
            {
                "type": "test_ready",
                "code": self.tests,
                "metadata": out.data,
            }
        )

        # Cycle D — actually RUN the tests so the reviewer can grade
        # the implementation against test outcomes (and so a test
        # written against a non-existent API surfaces immediately
        # instead of staying hidden until a human reads it).
        if (
            self.enable_execution
            and self.sandbox
            and self.detected_language
        ):
            try:
                from .sandbox import TEST_RUNNERS  # noqa: PLC0415

                if self.detected_language in TEST_RUNNERS:
                    test_cfg = TEST_RUNNERS[self.detected_language]
                    impl_filename = test_cfg["impl_filename"]
                    test_install = self._sandbox_test_install_packages()

                    test_result = await self.sandbox.execute(
                        code=self.tests,
                        language=self.detected_language,
                        test_mode=True,
                        install_packages=test_install or None,
                        # Mount the implementation so the test file
                        # can ``import main`` / ``import "./main"`` /
                        # etc.  Other extra_files (sidecar templates)
                        # also flow in unchanged.
                        extra_files={
                            **(self.extra_files or {}),
                            impl_filename: self.code,
                        },
                    )
                    self.test_execution_result = test_result.to_dict()
                    # Cycle F Sprint 2 — parse pytest-cov JSON
                    # harvested by the sandbox just before workdir
                    # cleanup.  Stored on the engine so:
                    #   * `_score_candidate` can include it in breakdown
                    #   * `_maybe_run_reflexion` can format a
                    #     MISSED_BRANCHES block for the coder retry.
                    coverage_payload = getattr(test_result, "coverage_json", None)
                    if coverage_payload is not None:
                        try:
                            from .coverage_reader import (  # noqa: PLC0415
                                parse_coverage_json,
                            )
                            self.coverage_report = parse_coverage_json(
                                coverage_payload,
                            )
                            await self._emit(
                                {
                                    "type": "coverage_report",
                                    "branch_coverage": round(
                                        self.coverage_report.branch_coverage_ratio,
                                        3,
                                    ),
                                    "line_coverage": round(
                                        self.coverage_report.line_coverage_ratio,
                                        3,
                                    ),
                                    "missed_branches": len(
                                        self.coverage_report.missed_branches,
                                    ),
                                }
                            )
                        except Exception as exc:
                            logger.info("coverage_parse_failed: %s", exc)
                            self.coverage_report = None
                    else:
                        self.coverage_report = None
                    await self._emit(
                        {
                            "type": "test_execution_result",
                            "result": test_result.to_dict(),
                        }
                    )
                else:
                    await self._emit(
                        {
                            "type": "test_execution_skipped",
                            "language": self.detected_language,
                            "reason": "no test runner configured",
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                logger.info("test_execution_failed: %s", exc)
                await self._emit(
                    {
                        "type": "test_execution_skipped",
                        "language": self.detected_language,
                        "reason": f"runner error: {exc}",
                    }
                )

        return out.data

    def _sandbox_test_install_packages(self) -> list[str]:
        """Cycle D — return the implementation's install_packages so
        the test runner inherits the same dependency surface
        (otherwise pytest's ``import main`` would fail when ``main``
        depends on ``flask`` but pytest's prefix doesn't have it)."""
        return list(getattr(self, "install_packages", []) or [])

    async def _phase_debug(self) -> dict[str, Any]:
        """
        Debug → re-execute loop. Only runs when:
          • Execution was enabled AND
          • The most recent execution result failed (or timed out).
        Up to ``self.max_debug_iterations`` loop iterations.
        """
        if not self.enable_execution or not self.sandbox or not self.code:
            self._skip("debug", "execution disabled or no code")
            return {"skipped": True}
        if self.max_debug_iterations <= 0:
            self._skip("debug", "max_debug_iterations=0")
            return {"skipped": True}
        # If the most recent execution succeeded, nothing to debug.
        if self.execution_results and self.execution_results[-1].get(
            "success",
            False,
        ):
            self._skip("debug", "execution already passing")
            return {"skipped": True, "reason": "no failure"}

        self._role_setter("debugger")
        debugger = DebuggerAgent(self.llm_call, max_tokens=self._budgets["debug"])

        last_result: dict[str, Any] = {}
        iteration = 0
        while iteration < self.max_debug_iterations:
            iteration += 1
            self.debug_iterations_used = iteration

            await self._emit(
                {
                    "type": "debug_iteration_start",
                    "iteration": iteration,
                    "max": self.max_debug_iterations,
                }
            )

            exec_feedback = (
                ExecutionResult(
                    **{
                        k: v
                        for k, v in self.execution_results[-1].items()
                        if k
                        in {
                            "exit_code",
                            "stdout",
                            "stderr",
                            "timed_out",
                            "error",
                            "duration_ms",
                            "language",
                        }
                    }
                ).to_feedback_str()
                if self.execution_results
                else "(no execution data)"
            )
            static_feedback = (
                self.static_analysis.to_feedback_str()
                if self.static_analysis
                else "(no static analysis)"
            )

            out = await debugger.run(
                AgentContext(
                    user_prompt=self.prompt,
                    code_context=self.code_context,
                    plan=self.plan,
                    code=self.code,
                    language=self.detected_language,
                    execution_feedback=exec_feedback,
                    static_feedback=static_feedback,
                    debug_iteration=iteration,
                )
            )
            if out.error or not out.code:
                logger.warning(
                    "debug_iteration_no_fix iteration=%d error=%s",
                    iteration,
                    out.error,
                )
                break

            self.code = out.code
            await self._emit(
                {
                    "type": "code_ready",
                    "language": self.detected_language,
                    "code": self.code,
                    "metadata": out.data,
                    "iteration": iteration,
                }
            )

            # Cycle B Commit V — merge any new dependencies the
            # debugger discovered (e.g. it added an ``import requests``
            # while patching the bug) into the cached install_packages
            # so the sandbox actually has them on the retry.  Also
            # accept new sidecar files via ``additional_files`` in the
            # debugger metadata.
            debugger_deps = (out.data or {}).get("dependencies") or []
            if debugger_deps:
                merged = list(self.install_packages) + list(debugger_deps)
                self.install_packages = self._sanitise_dependencies(merged)
            debugger_files = (out.data or {}).get("additional_files")
            if debugger_files is not None:
                # Splice into coder_metadata so _collect_extra_files
                # picks them up + caches.
                self.coder_metadata["additional_files"] = debugger_files
                self.extra_files = self._collect_extra_files()

            # Re-run the sandbox with the patched code.
            await self._emit(
                {
                    "type": "execution_start",
                    "iteration": iteration,
                }
            )
            new_result = await self.sandbox.execute(
                code=self.code,
                language=self.detected_language,
                install_packages=self.install_packages or None,
                extra_files=self.extra_files or None,
            )
            new_dict = new_result.to_dict()
            self.execution_results.append(new_dict)
            await self._emit(
                {
                    "type": "execution_result",
                    "result": new_dict,
                    "iteration": iteration,
                }
            )
            last_result = new_dict
            if new_result.success:
                break

        return {
            "iterations": iteration,
            "max_iterations": self.max_debug_iterations,
            "last_result": last_result,
            "final_success": bool(
                self.execution_results and self.execution_results[-1].get("success", False),
            ),
        }

    def _critic_fallback_verdict(self, reason: str) -> dict[str, Any]:
        """Default verdict when the critic call is unavailable / stalled.
        v18.1 Step 4 — extracted from the inline review block so both
        the legacy synchronous path and the new async-task path can
        share it.  Identical shape to a successful Critic AgentResult."""
        return {
            "verdict": "approved_with_minor",
            "score": 70,
            "strengths": [],
            "issues": [],
            "security_concerns": [],
            "performance_concerns": [],
            "final_comment": (
                f"Critic unavailable — defaulting to approved_with_minor. ({reason})"
            )[:400],
        }

    def _build_critic_context(self) -> AgentContext:
        """Construct the AgentContext fed to CriticAgent.run().  Shared
        by the legacy synchronous path and the v18.1 async kickoff
        helper.  Reads:
          * self.execution_results (last entry; populated by _phase_execute)
          * self.static_analysis    (populated by _phase_analyze)
          * self.test_execution_result (populated by _phase_test)
        All three are optional — the critic copes when feedback is None."""
        exec_feedback = None
        if self.execution_results:
            last = self.execution_results[-1]
            exec_feedback = (
                f"exit={last.get('exit_code')} "
                f"timed_out={last.get('timed_out')} "
                f"stdout(len)={len(last.get('stdout', ''))} "
                f"stderr={last.get('stderr', '')[:600]}"
            )
        static_feedback = (
            self.static_analysis.to_feedback_str()
            if self.static_analysis
            else None
        )
        test_exec_feedback = None
        ter = getattr(self, "test_execution_result", None)
        if ter:
            test_exec_feedback = (
                f"exit={ter.get('exit_code')} "
                f"timed_out={ter.get('timed_out')} "
                f"skipped={ter.get('skipped', False)}\n"
                f"stdout: {(ter.get('stdout') or '')[:1200]}\n"
                f"stderr: {(ter.get('stderr') or '')[:600]}"
            )
        return AgentContext(
            user_prompt=self.prompt,
            plan=self.plan,
            code=self.code,
            language=self.detected_language,
            execution_feedback=exec_feedback,
            static_feedback=static_feedback,
            test_execution_feedback=test_exec_feedback,
        )

    def _code_hash(self) -> str | None:
        """sha256 of the current `self.code` for staleness detection."""
        if not self.code:
            return None
        import hashlib  # noqa: PLC0415
        return hashlib.sha256(self.code.encode("utf-8", errors="replace")).hexdigest()

    async def _kickoff_critic_task(self) -> None:
        """v18.1 Step 4 — launch the critic LLM call as a background
        task right after debug completes (or, when debug skips, right
        after the parallel block).  The task runs in parallel with
        the deliverable assembly + reflexion threshold check, removing
        the critic LLM latency from the critical path.

        Cancels any previously-running critic task before launching
        (debug retries that mutate `self.code` invalidate the in-flight
        verdict; we re-launch on the post-debug code so the verdict
        actually reflects what shipped).
        """

        if not self.code:
            return

        # If a previous task is still running on stale code, cancel.
        prev = self._critic_task
        if prev is not None and not prev.done():
            prev.cancel()
            try:
                await prev
            except (asyncio.CancelledError, Exception):
                pass

        self._role_setter("critic")
        agent = CriticAgent(self.llm_call, max_tokens=self._budgets["review"])
        ctx = self._build_critic_context()
        self._critic_task = asyncio.create_task(
            agent.run(ctx), name="amor_critic_async",
        )
        self._critic_code_hash = self._code_hash()
        logger.debug("critic_task_kicked_off code_hash=%s", self._critic_code_hash)

    async def _resolve_critic_task(
        self, *, timeout_s: float = 8.0,
    ) -> Any | None:
        """Await the kicked-off critic task with a freshness timeout.

        Returns the AgentResult on success, None when the task is
        absent or missed the timeout (caller falls back to the
        approved_with_minor default).  Uses `asyncio.shield` so a
        timeout doesn't cancel the underlying task — it might still
        finish before the pipeline's next critic invocation.
        """
        task = self._critic_task
        if task is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "critic_async_stalled timeout_s=%s — falling back to approved_with_minor",
                timeout_s,
            )
            return None
        except asyncio.CancelledError:
            return None
        except Exception as exc:  # pragma: no cover (defensive)
            logger.warning("critic_async_raised err=%s", exc)
            return None

    async def _phase_review(self) -> dict[str, Any]:
        if not self.code:
            self._skip("review", "no code to review")
            return {"skipped": True}

        # v18.1 Step 4 — two paths:
        #   1. ASYNC-DECOUPLE (orchestrator kicked off `_critic_task`
        #      after the parallel block): await the in-flight task
        #      with a freshness timeout.  On timeout fall back to the
        #      `approved_with_minor` default (so the score function
        #      stays well-defined and Reflexion still has a number).
        #   2. INLINE (no kickoff — unit tests / CLI single-mode /
        #      `code_critic_async=False`): run the critic agent
        #      synchronously, exact v18 behaviour.
        out: Any = None
        if self._critic_task is not None:
            # Staleness check: if debug modified the code AFTER the
            # critic was kicked off, the in-flight verdict is on stale
            # code.  Cancel + re-launch on the current code.
            current_hash = self._code_hash()
            if (
                self._critic_code_hash is not None
                and current_hash is not None
                and current_hash != self._critic_code_hash
            ):
                logger.info(
                    "critic_async_re-launch reason=code_changed_during_debug",
                )
                await self._kickoff_critic_task()
            # Verdict-freshness timeout (Plan-agent locked at 8s).
            try:
                from ..config.settings import settings  # noqa: PLC0415
                _timeout = float(
                    getattr(settings, "code_critic_async_timeout_s", 8.0),
                )
            except Exception:
                _timeout = 8.0
            out = await self._resolve_critic_task(timeout_s=_timeout)
            if out is None:
                # Freshness fallback — use the approved_with_minor default
                # so `_score_candidate` keeps producing well-defined
                # numbers and Reflexion's threshold logic still fires.
                self.review = self._critic_fallback_verdict(
                    "critic_async_stalled",
                )
                await self._emit(
                    {"type": "review_ready", "review": self.review},
                )
                return self.review
        else:
            # Inline (legacy) path — no kickoff was performed.
            self._role_setter("critic")
            agent = CriticAgent(
                self.llm_call, max_tokens=self._budgets["review"],
            )
            ctx = self._build_critic_context()
            out = await agent.run(ctx)

        if not out or getattr(out, "error", None) or not getattr(out, "data", None):
            # Don't fail the whole pipeline if review breaks.
            err = getattr(out, "error", "no_result") if out else "no_result"
            self.review = self._critic_fallback_verdict(err)
        else:
            self.review = out.data
        await self._emit(
            {
                "type": "review_ready",
                "review": self.review,
            }
        )
        return self.review

    # ── reflexion (quality-improvement loop) ─────────────────────────────

    def _score_candidate(self) -> dict[str, Any]:
        """Cycle D — combine signals from execute / test / static / critic
        into a single 0-100 quality score.  Components:

          * Execution success    (35 pts) — initial run exit==0
          * Test execution pass  (25 pts) — pytest / jest / etc. exit==0
          * Static analysis      (15 pts) — full marks if 0 errors
          * Critic verdict score (25 pts) — critic's own 0-100 scaled

        Returns ``{score: int, breakdown: dict}`` so the
        ``_maybe_run_reflexion`` call site can both threshold-test and
        log/emit the diagnostic for the operator.
        """
        breakdown: dict[str, int] = {
            "execution": 0,
            "test_execution": 0,
            "static": 0,
            "critic": 0,
        }
        # Execution success
        if self.execution_results:
            last = self.execution_results[-1]
            if last.get("exit_code") == 0 and not last.get("timed_out"):
                breakdown["execution"] = 35
            elif last.get("skipped"):
                # No execution → don't penalise (e.g. explanation deliverable)
                breakdown["execution"] = 25
        else:
            breakdown["execution"] = 25  # neutral when execution wasn't required

        # Test execution
        ter = getattr(self, "test_execution_result", None)
        if ter:
            if ter.get("exit_code") == 0 and not ter.get("skipped"):
                breakdown["test_execution"] = 25
            elif ter.get("skipped"):
                breakdown["test_execution"] = 15
        else:
            # Tester didn't run (deliverable_type=explanation, etc.)
            breakdown["test_execution"] = 15

        # Static analysis: full marks when 0 errors
        if self.static_analysis is not None:
            errs = getattr(self.static_analysis, "errors", None) or []
            warns = getattr(self.static_analysis, "warnings", None) or []
            if len(errs) == 0:
                breakdown["static"] = 15
            else:
                breakdown["static"] = max(0, 15 - 3 * len(errs))
            # Light warning penalty (1 pt per 5 warnings)
            breakdown["static"] = max(0, breakdown["static"] - len(warns) // 5)
        else:
            breakdown["static"] = 12

        # Critic verdict — scale critic's 0-100 to 25
        if isinstance(self.review, dict):
            critic_score = int(self.review.get("score") or 0)
            breakdown["critic"] = round(critic_score * 25 / 100)

        # Cycle F Sprint 2 — surface property_tests + branch_coverage
        # as informational fields on breakdown (do NOT alter the
        # existing 4-slot 100-point scoring math: keeping the Cycle D
        # weights preserves the reflexion threshold's calibration).
        # The coverage_report attribute is set by _phase_test() when
        # pytest-cov ran successfully; None otherwise.
        breakdown["property_tests"] = bool(
            (getattr(self, "test_metadata", {}) or {}).get(
                "property_tests_present", False
            )
        )
        cov = getattr(self, "coverage_report", None)
        if cov is not None and getattr(cov, "available", False):
            breakdown["branch_coverage"] = round(cov.branch_coverage_ratio, 3)
            breakdown["line_coverage"] = round(cov.line_coverage_ratio, 3)
            breakdown["missed_branches"] = len(cov.missed_branches)

        # Sum only the four numeric scoring slots (the informational
        # property/coverage fields are ignored in the total).
        numeric_slots = ("execution", "test_execution", "static", "critic")
        total = sum(breakdown[k] for k in numeric_slots if isinstance(breakdown.get(k), (int, float)))
        return {"score": min(100, int(total)), "breakdown": breakdown}

    async def _warmup_critic_prefix(self) -> None:
        """Cycle F Sprint 6 — fire a tiny critic call to load the
        system+plan prefix into the model's KV cache.  Fire-and-
        forget; failures swallowed.

        Mechanism: llama-swap's `--cache-reuse 256` keeps identical
        prefixes in cache across requests.  A 2-token warmup primes
        that cache.  When `_phase_review` later issues the real
        critic call with the same prefix, it lands on a hot slot
        (prefill ≈ 0.13× cold per Sprint 1 A/B probe results).

        Skipped silently when:
          * code isn't ready yet (called too early)
          * critic budget config missing
          * any LLM-call exception (cold path remains functional)
        """

        if not self.code:
            return
        try:
            self._role_setter("critic")
            # Use the same system+plan prompt shape the real
            # review will use, but cap completion at 2 tokens so
            # the call ends fast.  No metadata extraction.
            from . import prompts as _P  # noqa: PLC0415
            try:
                prompt = _P.critic_prompt(
                    user_prompt=self.prompt,
                    code=self.code or "",
                    plan=self.plan or {},
                )
            except (AttributeError, Exception):
                # Fallback: a short generic warmup string.
                prompt = "Review the implementation."
            # Tiny max_tokens so the warmup runs in ~half-second
            # while still completing the prefill phase that pre-warms
            # the cache slot.
            await self.llm_call(prompt, _P.CRITIC_SYSTEM_PROMPT, 2)
            logger.debug("critic_prefix_warmup_complete")
        except Exception as exc:  # pragma: no cover (defensive)
            logger.debug("critic_prefix_warmup_failed err=%s", exc)

    async def _maybe_run_reflexion(self) -> None:
        """Cycle D — Reflexion quality-improvement loop.  If the
        pipeline produced a low-scoring deliverable, regenerate the
        implementation with sandbox + critic feedback and keep the
        higher-scored version.

        Distinct from ``_phase_debug`` (which only fires on FAILED
        execution).  Reflexion fires on LOW QUALITY — passing tests
        + clean static + no critic concerns means no reflexion is
        needed.
        """
        # Settings gate
        try:
            from ..config.settings import settings  # noqa: PLC0415
            max_iter = int(getattr(settings, "code_max_reflexion_iterations", 0))
            threshold = int(
                getattr(settings, "code_reflexion_quality_threshold", 80),
            )
        except Exception:
            max_iter, threshold = 0, 80
        if max_iter <= 0:
            return
        if not self.code:
            return

        baseline = self._score_candidate()
        # Cycle D — domain feature coverage check.  Even if the
        # critic's score is high, we re-trigger reflexion when the
        # detected-domain's must-have features are missing from the
        # generated code.  The user explicitly asked for "real
        # production quality" — this is the gate that enforces it
        # independently of the critic's subjective opinion.
        coverage = {"covered": 0, "total": 0, "missing": [], "ratio": 1.0}
        domain = (self.triage or {}).get("domain") or (self.plan or {}).get("domain")
        if domain and self.code:
            try:
                from .domain_templates import feature_coverage  # noqa: PLC0415
                coverage = feature_coverage(self.code, domain)
            except Exception as exc:  # noqa: BLE001
                logger.info("feature_coverage_failed: %s", exc)
        baseline["coverage"] = coverage
        await self._emit({
            "type": "reflexion_score",
            "phase": "baseline",
            **baseline,
        })

        # Reflexion fires when EITHER score is low OR feature
        # coverage is incomplete (≥2 must-haves missing).
        score_ok = baseline["score"] >= threshold
        coverage_ok = coverage["total"] == 0 or len(coverage["missing"]) <= 1
        if score_ok and coverage_ok:
            return  # already good enough

        # Stash the baseline so we can compare and (if needed) restore
        baseline_code = self.code
        baseline_review = dict(self.review) if isinstance(self.review, dict) else None
        baseline_static = self.static_analysis
        baseline_test_exec = getattr(self, "test_execution_result", None)
        baseline_exec_results = list(self.execution_results)

        for iteration in range(1, max_iter + 1):
            await self._emit({
                "type": "reflexion_iteration_start",
                "iteration": iteration,
                "max": max_iter,
                "baseline_score": baseline["score"],
                "threshold": threshold,
            })
            try:
                improved = await self._run_reflexion_iteration(
                    iteration=iteration,
                    baseline_code=baseline_code,
                    baseline_review=baseline_review,
                    baseline_test_exec=baseline_test_exec,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("reflexion_iteration_failed iter=%d err=%s",
                               iteration, exc)
                await self._emit({
                    "type": "reflexion_iteration_complete",
                    "iteration": iteration,
                    "outcome": "error",
                    "error": str(exc),
                })
                continue
            if not improved:
                continue
            new_score = self._score_candidate()
            await self._emit({
                "type": "reflexion_iteration_complete",
                "iteration": iteration,
                "outcome": "improved" if new_score["score"] > baseline["score"] else "no_gain",
                "baseline_score": baseline["score"],
                "new_score": new_score["score"],
                "breakdown": new_score["breakdown"],
            })
            if new_score["score"] > baseline["score"]:
                # Keep the new version — already the active state.
                baseline = new_score
                baseline_code = self.code
                baseline_review = dict(self.review) if isinstance(self.review, dict) else None
                baseline_test_exec = getattr(self, "test_execution_result", None)
                if baseline["score"] >= threshold:
                    return
            else:
                # Restore the better baseline state.
                self.code = baseline_code
                if baseline_review is not None:
                    self.review = baseline_review
                self.static_analysis = baseline_static
                if baseline_test_exec is not None:
                    self.test_execution_result = baseline_test_exec
                self.execution_results = baseline_exec_results

    async def _run_reflexion_iteration(
        self,
        *,
        iteration: int,
        baseline_code: str,
        baseline_review: dict[str, Any] | None,
        baseline_test_exec: dict[str, Any] | None,
    ) -> bool:
        """Run ONE reflexion iteration: generate an improved version,
        re-execute it in the sandbox, re-run tests, re-score with
        the critic.  Returns True if a new candidate was generated
        (regardless of whether it scored higher)."""
        from .agents import CoderAgent, AgentContext  # noqa: PLC0415

        # Build a feedback bundle from the baseline run
        feedback_parts: list[str] = []
        if self.execution_results:
            last = self.execution_results[-1]
            if last.get("exit_code") not in (0, None):
                feedback_parts.append(
                    f"Execution: exit={last.get('exit_code')} "
                    f"stderr={(last.get('stderr') or '')[:600]}"
                )
        if baseline_test_exec and baseline_test_exec.get("exit_code") not in (0, None):
            feedback_parts.append(
                f"Test execution failed: exit={baseline_test_exec.get('exit_code')}\n"
                f"stdout: {(baseline_test_exec.get('stdout') or '')[:1200]}\n"
                f"stderr: {(baseline_test_exec.get('stderr') or '')[:600]}"
            )
        if isinstance(baseline_review, dict):
            issues = baseline_review.get("issues") or []
            for issue in issues[:6]:
                if not isinstance(issue, dict):
                    continue
                sev = issue.get("severity", "minor")
                desc = issue.get("description", "")
                sug = issue.get("suggestion", "")
                feedback_parts.append(
                    f"Critic issue [{sev}]: {desc} — suggestion: {sug}"
                )
        # Cycle D — surface missing domain features so the coder
        # knows EXACTLY what to add (this is the user's "real
        # quality" feedback channel — independent of critic opinion).
        domain = (self.triage or {}).get("domain") or (self.plan or {}).get("domain")
        if domain and self.code:
            try:
                from .domain_templates import feature_coverage  # noqa: PLC0415
                cov = feature_coverage(self.code, domain)
                if cov["missing"]:
                    feedback_parts.append(
                        "MISSING REQUIRED FEATURES (these are non-negotiable for "
                        f"the {domain.get('domain')}/{domain.get('subdomain')} domain — "
                        "your improved version MUST add them):\n"
                        + "\n".join(f"  • {m}" for m in cov["missing"])
                    )
            except Exception:
                pass
        # Cycle F Sprint 2 — surface missed branches from pytest-cov
        # so the coder retry can add cases (or refactor) to drive
        # un-tested branches.  format_missed_branches_block returns
        # empty string when coverage is above threshold, so this is
        # a no-op for already-well-covered code.
        cov_report = getattr(self, "coverage_report", None)
        if cov_report is not None and getattr(cov_report, "available", False):
            try:
                from .coverage_reader import (  # noqa: PLC0415
                    format_missed_branches_block,
                )
                from ..config.settings import settings  # noqa: PLC0415
                threshold = float(getattr(
                    settings, "code_branch_coverage_threshold", 0.80,
                ))
                block = format_missed_branches_block(
                    cov_report, threshold=threshold,
                )
                if block:
                    feedback_parts.append(block)
            except Exception:  # pragma: no cover (defensive)
                pass
        feedback_blob = "\n\n".join(feedback_parts) or "(no actionable feedback)"

        # Prompt the coder with reflexion-style "improve this" framing
        from . import prompts as _P  # noqa: PLC0415
        plan_for_coder = self._extract_focused_spec(
            self.plan or {}, self.detected_language,
        )
        improve_feedback = (
            f"REFLEXION IMPROVEMENT REQUEST (iteration {iteration}).\n\n"
            "You previously produced this implementation:\n\n"
            f"```{self.detected_language}\n{baseline_code[:6000]}\n```\n\n"
            "But it scored below the production-quality threshold.  "
            "Sandbox + critic feedback:\n\n"
            f"{feedback_blob}\n\n"
            "Generate an IMPROVED version that:\n"
            "  • Resolves every concrete issue listed above\n"
            "  • Keeps the same public API surface unless an issue requires changing it\n"
            "  • Uses idiomatic patterns for the language\n"
            "  • Stays within the same file unless multi-file is explicitly required\n"
            "Output exactly one code fence + one JSON metadata fence.\n"
        )
        agent = CoderAgent(
            self.llm_call, max_tokens=self._budgets.get("implement", 2000),
        )
        out = await agent.run(AgentContext(
            user_prompt=self.prompt,
            code_context=self.code_context,
            triage=self.triage,
            plan=plan_for_coder,
            language=self.detected_language,
            execution_feedback=improve_feedback,
        ))
        if out.error or not out.code:
            logger.info("reflexion_coder_empty: %s", out.error)
            return False
        # Adopt the improved candidate
        self.code = out.code
        self.coder_metadata = dict(out.data or {})
        await self._emit({
            "type": "code_ready",
            "language": self.detected_language,
            "code": self.code,
            "metadata": out.data,
            "reflexion_iteration": iteration,
        })

        # Re-execute the new candidate
        await self._phase_execute()
        # Re-run static analysis
        await self._phase_analyze()
        # Re-run tester (which itself re-runs pytest in test_mode)
        await self._phase_test()
        # Re-grade with the critic (test feedback now reflects the
        # improved candidate)
        await self._phase_review()
        return True

    # ── deliverable assembly ──────────────────────────────────────────────

    def _build_deliverable_markdown(self) -> str:
        """Final markdown that lands in the chat history."""
        task_type = (
            (self.plan.get("task_type") or self.triage.get("task_type") or "Code task")
            .replace("_", " ")
            .title()
        )
        title = self.title or "Code task"
        lang = self.detected_language or "text"

        lines: list[str] = []
        lines.append(f"## {task_type} — {title}")
        lines.append("")

        # Plan summary
        if self.plan:
            lines.append("### Plan")
            steps = self.plan.get("plan", []) or []
            if steps:
                for step in steps[:8]:
                    lines.append(
                        f"{step['step']}. **{step['action']}** "
                        f"({step['agent']}) — {step['description']}"
                    )
            else:
                lines.append(self.plan.get("title") or "(no detailed plan)")
            lines.append("")

        # Implementation
        if self.code:
            lines.append("### Implementation")
            lines.append(f"```{lang}")
            lines.append(self.code.rstrip())
            lines.append("```")
            lines.append("")

        # Static analysis (only if interesting)
        if self.static_analysis and self.static_analysis.issues:
            counts = self.static_analysis.severity_counts()
            if any(counts.values()):
                lines.append("### Static Analysis")
                lines.append(
                    f"- {counts['error']} errors, "
                    f"{counts['warning']} warnings, "
                    f"{counts['security']} security issues"
                )
                if self.static_analysis.complexity_score is not None:
                    lines.append(
                        f"- Avg cyclomatic complexity: {self.static_analysis.complexity_score:.1f}"
                    )
                lines.append("")

        # Tests
        if self.tests:
            lines.append("### Tests")
            lines.append(f"```{lang}")
            lines.append(self.tests.rstrip())
            lines.append("```")
            lines.append("")

        # Execution results
        if self.execution_results:
            lines.append("### Execution Results")
            for i, res in enumerate(self.execution_results):
                tag = (
                    "✅ Pass"
                    if res.get("success")
                    else ("⏱ Timeout" if res.get("timed_out") else "❌ Fail")
                )
                attempt = "Initial run" if i == 0 else f"Debug iteration {i}"
                lines.append(
                    f"- **{attempt}** — {tag} "
                    f"(exit={res.get('exit_code')}, "
                    f"{res.get('duration_ms', 0)}ms)"
                )
                stderr = (res.get("stderr") or "").strip()
                if stderr and not res.get("success"):
                    snippet = stderr.splitlines()[0][:200]
                    lines.append(f"  - `{snippet}`")
            lines.append("")

        # Review
        if self.review:
            verdict_label = {
                "approved": "✅ Approved",
                "approved_with_minor": "🟢 Approved with minor comments",
                "needs_revision": "🟠 Needs revision",
                "rejected": "🔴 Rejected",
            }.get(self.review.get("verdict", "approved_with_minor"), "🟢 Approved")
            score = self.review.get("score", 70)
            lines.append("### Code Review")
            lines.append(f"**Score: {score}/100** — {verdict_label}")
            lines.append("")
            comment = self.review.get("final_comment") or ""
            if comment:
                lines.append(comment)
                lines.append("")
            major_issues = [
                i
                for i in self.review.get("issues", [])
                if i.get("severity") in ("critical", "major")
            ]
            if major_issues:
                lines.append("**Notable issues:**")
                for issue in major_issues[:6]:
                    lines.append(f"- *{issue['severity']}* — {issue['description']}")
                lines.append("")

        return "\n".join(lines).strip() + "\n"

    # ── run ───────────────────────────────────────────────────────────────

    async def run(self) -> dict[str, Any]:
        await self._run_phase("triage", self._phase_triage)
        await self._run_phase("model_prep", self._phase_model_prep)
        await self._run_phase("plan", self._phase_plan)

        # If planning failed, we cannot meaningfully continue.
        if self._phase_index["plan"].status != "completed":
            for n in ("implement", "execute", "analyze", "test", "debug", "review"):
                self._skip(n, "plan failed")
            self.deliverable_markdown = self._build_deliverable_markdown()
            return self.snapshot()

        await self._run_phase("implement", self._phase_implement)
        if self._phase_index["implement"].status != "completed":
            for n in ("execute", "analyze", "test", "debug", "review"):
                self._skip(n, "implementation failed")
            self.deliverable_markdown = self._build_deliverable_markdown()
            return self.snapshot()

        # Cycle F Sprint 6 — async pipeline parallelism.  When
        # `code_pipeline_parallel=True` (default), execute + analyze
        # + test all run concurrently after implement.  Test phase
        # only depends on `self.code` (from implement) — NOT on
        # execute or analyze — so it's safe to run in parallel.
        # Saves ~30s on the Sprint-0 corpus median (the tester's
        # LLM call dominates).  Falls back to the Cycle D
        # sequential test phase when the flag is off.
        try:
            from ..config.settings import settings  # noqa: PLC0415
            _parallel = bool(getattr(settings, "code_pipeline_parallel", True))
            _warmup = bool(getattr(settings, "code_critic_prefix_warmup", True))
        except Exception:
            _parallel, _warmup = True, True

        # Fire-and-forget critic prefix-cache warmup as soon as
        # code + plan are available.  The warmup result is ignored;
        # the value is the cached KV slot waiting for the real
        # review call to land on a hot prefix.  Best-effort — any
        # error inside is swallowed (the review call works fine
        # cold, just slower).
        warmup_task = None
        if _warmup:
            try:
                warmup_task = asyncio.create_task(
                    self._warmup_critic_prefix(),
                    name="amor_critic_warmup",
                )
            except Exception:  # pragma: no cover (defensive)
                warmup_task = None

        if _parallel:
            await asyncio.gather(
                self._run_phase("execute", self._phase_execute),
                self._run_phase("analyze", self._phase_analyze),
                self._run_phase("test", self._phase_test),
                return_exceptions=True,
            )
        else:
            await asyncio.gather(
                self._run_phase("execute", self._phase_execute),
                self._run_phase("analyze", self._phase_analyze),
                return_exceptions=True,
            )
            await self._run_phase("test", self._phase_test)

        # v18.1 Step 4 — async critic decouple.  Kick off the REAL
        # critic LLM call as a background task right after the
        # parallel block completes, BEFORE entering the debug retry
        # loop.  The critic then runs in parallel with debug — for
        # Build prompts (where debug retries inflate wall-clock by
        # 100-300s) this lifts the critic LLM latency (30-60s on
        # Phi-4 Q4_K_M CPU) entirely off the critical path.
        #
        # If debug modifies `self.code`, `_phase_review` detects the
        # staleness via `_critic_code_hash` and re-launches the
        # critic on the post-debug code.  Settings flag
        # `code_critic_async=False` reverts to the legacy inline
        # critic call at `_phase_review` entry; `code_critic_async_timeout_s`
        # (default 8s) bounds how long review() will block on a
        # still-running task before falling back to approved_with_minor.
        try:
            _async_critic = bool(getattr(settings, "code_critic_async", True))
        except Exception:
            _async_critic = True
        if _async_critic and self.code:
            try:
                await self._kickoff_critic_task()
            except Exception as exc:  # pragma: no cover (defensive)
                logger.warning(
                    "critic_async_kickoff_failed err=%s — review will run inline",
                    exc,
                )

        await self._run_phase("debug", self._phase_debug)
        await self._run_phase("review", self._phase_review)

        # If the warmup is still running by the time review finishes
        # (shouldn't be, but defensive), cancel it so we don't leak
        # the task.
        if warmup_task is not None and not warmup_task.done():
            warmup_task.cancel()

        # Cycle D — Reflexion quality-improvement loop.  After the
        # full pipeline produces a deliverable, score it against the
        # sandbox's ground truth; if it's below the threshold and we
        # have iterations available, re-invoke the coder with a
        # feedback-rich prompt and keep whichever version scores
        # higher.  This is the user's "give me the BEST code" path.
        await self._maybe_run_reflexion()

        self.deliverable_markdown = self._build_deliverable_markdown()
        await self._emit(
            {
                "type": "deliverable_ready",
                "markdown": self.deliverable_markdown,
            }
        )
        return self.snapshot()

    # ── snapshot ──────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "phases": [p.to_dict() for p in self.phases],
            "triage": self.triage,
            "models_used": self.models_used,
            "plan": self.plan,
            "code": self.code,
            "tests": self.tests,
            "language": self.detected_language,
            "title": self.title,
            "execution_results": self.execution_results,
            "static_analysis": (self.static_analysis.to_dict() if self.static_analysis else None),
            "review": self.review,
            "deliverable_markdown": self.deliverable_markdown,
            "debug_iterations": self.debug_iterations_used,
            "task_type": self.plan.get("task_type") or self.triage.get("task_type") or "generation",
        }


# ─────────────────────────────────────────────────────────────────────────────
# noop helper
# ─────────────────────────────────────────────────────────────────────────────


async def _noop_event(_event: dict[str, Any]) -> None:
    return None
