"""
Main application entry point with FastAPI.
Provides REST API for document processing and monitoring.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
import time
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware
from .config.settings import settings
from .config.logging_config import logger
from .core.models import (
    SourceDocument,
    BatchProcessingRequest,
    BatchProcessingResponse,
    HealthStatus,
)
from .processing.pipeline import pipeline
from .infrastructure.monitoring import monitor
from .infrastructure.cache import cache_manager
from .infrastructure.storage import storage_manager
from .infrastructure.chat_store import chat_store

# Import API routers
try:
    from .api.chat_research_routes import router as chat_research_router
    CHAT_RESEARCH_AVAILABLE = True
except ImportError:
    CHAT_RESEARCH_AVAILABLE = False
    logger.warning("Chat research routes not available")

try:
    from .api.local_ai_routes_simple import router as local_ai_router, initialize_local_ai, cleanup_local_ai
    LOCAL_AI_AVAILABLE = True
except ImportError:
    LOCAL_AI_AVAILABLE = False
    logger.warning("Local AI routes not available")

from .api.chat_sessions_routes import router as chat_sessions_router
from .api.query_record_routes import router as query_record_router  # Phase B4
from .api.chat_folders_routes import router as chat_folders_router
from .api.auth_routes import router as auth_router
# Cycle C Sprint 0 Day 3 — admin baselines dashboard.
from .api.admin_baselines_routes import router as admin_baselines_router
# Cycle C Sprint 1 Day 4 — admin LLM dashboard (resident models,
# swap events, cache-reuse hits).
from .api.admin_llm_routes import router as admin_llm_router
# Cycle C Sprint 2 Day 1 — admin Eval harness routes (HumanEval+,
# SWE-bench-Lite, RAGAS, sprint0 corpus replay).
from .api.admin_evals_routes import (
    router as admin_evals_router,
    ensure_eval_runs_schema,
)
# Cycle C Sprint 4 Day 2 — repo symbol discovery for @-mention picker.
from .api.repo_routes import router as repo_router
# Cycle C Sprint 6 Day 1 — preference-pair ingestion for ORPO trainer.
from .api.admin_training_routes import (
    router as admin_training_router,
    ensure_preference_pairs_schema,
)
# Cycle C Sprint 7 Day 2 — Mem0 OSS memory routes.
from .api.admin_memory_routes import router as admin_memory_router
# Cycle C Sprint 8 Day 4 — agentic ReAct loop routes.
from .api.agent_routes import router as agent_router
from .auth.service import auth_service

# Thinking Mode — human-in-the-loop deep reasoning pipeline
try:
    from .api.thinking_routes import router as thinking_router
    THINKING_AVAILABLE = True
except ImportError as _thinking_exc:  # pragma: no cover
    THINKING_AVAILABLE = False
    logger.warning("Thinking routes not available: %s", _thinking_exc)

# Code Intelligence Mode — multi-agent local-only code engine
try:
    from .api.code_intelligence_routes import router as code_intelligence_router
    CODE_INTELLIGENCE_AVAILABLE = True
except ImportError as _code_exc:  # pragma: no cover
    CODE_INTELLIGENCE_AVAILABLE = False
    logger.warning("Code intelligence routes not available: %s", _code_exc)

# Unified model management (More Settings → AI Model picker)
try:
    from .api.model_routes import router as model_router
    from .services.model_manager import ModelManager
    MODEL_ROUTES_AVAILABLE = True
except ImportError as _model_exc:  # pragma: no cover
    MODEL_ROUTES_AVAILABLE = False
    logger.warning("Model routes not available: %s", _model_exc)

# Consortium Mode — meta-orchestrator chaining Code Intelligence + Research + Thinking
try:
    from .api.consortium_routes import router as consortium_router
    CONSORTIUM_AVAILABLE = True
except ImportError as _cons_exc:  # pragma: no cover
    CONSORTIUM_AVAILABLE = False
    logger.warning("Consortium routes not available: %s", _cons_exc)

# QuickCode Mode — 5-phase reasoning-first lite pipeline
try:
    from .api.quick_code_routes import router as quick_code_router
    QUICK_CODE_AVAILABLE = True
except ImportError as _qc_exc:  # pragma: no cover
    QUICK_CODE_AVAILABLE = False
    logger.warning("QuickCode routes not available: %s", _qc_exc)

# Sentinel — multi-agent local security intelligence (V1)
try:
    from .api.sentinel_routes import router as sentinel_router
    SENTINEL_AVAILABLE = True
except ImportError as _sentinel_exc:  # pragma: no cover
    SENTINEL_AVAILABLE = False
    logger.warning("Sentinel routes not available: %s", _sentinel_exc)

# Sentinel Evolution Console — Phase 15 operator surface
try:
    from .api.sentinel_evolution_routes import router as sentinel_evolution_router
    SENTINEL_EVOLUTION_AVAILABLE = True
except ImportError as _sentinel_evo_exc:  # pragma: no cover
    SENTINEL_EVOLUTION_AVAILABLE = False
    logger.warning(
        "Sentinel Evolution routes not available: %s", _sentinel_evo_exc,
    )

# OpenAI-compatible /v1 facade — Phase 16 Commit C
try:
    from .api.openai_compat_routes import router as openai_compat_router
    OPENAI_COMPAT_AVAILABLE = True
except ImportError as _openai_compat_exc:  # pragma: no cover
    OPENAI_COMPAT_AVAILABLE = False
    logger.warning(
        "OpenAI-compatible facade routes not available: %s", _openai_compat_exc,
    )

# MCP server — Phase 16 Commit E
try:
    from .api.mcp_routes import router as mcp_router
    MCP_ROUTES_AVAILABLE = True
except ImportError as _mcp_exc:  # pragma: no cover
    MCP_ROUTES_AVAILABLE = False
    logger.warning("MCP routes not available: %s", _mcp_exc)

# Cycle F Sprint 5 — approval flow bridge
try:
    from .api.approval import approval_router
    APPROVAL_ROUTES_AVAILABLE = True
except ImportError as _appr_exc:  # pragma: no cover
    APPROVAL_ROUTES_AVAILABLE = False
    logger.warning("Approval routes not available: %s", _appr_exc)

# Crawling and Translation API routes
try:
    from .api.crawling_routes import router as crawling_router
    CRAWLING_AVAILABLE = True
except ImportError:
    CRAWLING_AVAILABLE = False
    logger.warning("Crawling routes not available")

try:
    from .api.translation_routes import router as translation_router
    TRANSLATION_AVAILABLE = True
except ImportError:
    TRANSLATION_AVAILABLE = False
    logger.warning("Translation routes not available")


# Phase D4 — periodic SSE event-queue sweeper. Drops queues whose
# backing session is in a terminal state (completed / failed /
# cancelled) so a disconnected client can't leak its asyncio.Queue
# indefinitely. Runs every 5 minutes; the work itself is cheap (one
# `await _load(sid)` per known queue, capped at 512 by the TTL cache).
import asyncio as _asyncio_main


async def _sse_queue_sweeper() -> None:
    """Lifespan task — periodically prune stale event queues."""
    # Lazy imports so a startup failure in one route module doesn't
    # take the whole sweeper offline.
    while True:
        try:
            await _asyncio_main.sleep(300)  # 5 min
        except _asyncio_main.CancelledError:
            return
        try:
            from .api import thinking_routes, local_ai_routes_simple
            t_dropped = await thinking_routes.sweep_stale_event_queues()
            r_dropped = await local_ai_routes_simple.sweep_stale_event_queues()
            c_dropped = 0
            try:
                from .api import code_intelligence_routes
                c_dropped = await code_intelligence_routes.sweep_stale_event_queues()
            except Exception:
                pass
            # v7 — also sweep consortium zombies + queues.
            cons_zombies = 0
            cons_dropped = 0
            try:
                from .api import consortium_routes
                cons_zombies = await consortium_routes.sweep_zombies_periodic()
                cons_dropped = await consortium_routes.sweep_stale_event_queues()
            except Exception:
                pass
            if t_dropped or r_dropped or c_dropped or cons_zombies or cons_dropped:
                logger.info(
                    "sse_queue_sweep",
                    thinking=t_dropped,
                    research=r_dropped,
                    code=c_dropped,
                    consortium_zombies=cons_zombies,
                    consortium_queues=cons_dropped,
                )
        except Exception as exc:
            logger.warning("sse_queue_sweep_failed", error=str(exc))


async def _code_intelligence_warmup() -> None:
    """
    Lifespan startup task — probe Ollama for code models + pre-pull
    the configured sandbox base images so the first user request
    isn't slowed by a 100 MB+ image fetch.

    Best-effort: failures here log a warning but never block startup.
    """
    try:
        from .api.code_intelligence_routes import (
            get_model_registry,
            get_sandbox,
        )
        from .config.settings import settings as _settings_warm

        registry = get_model_registry()
        try:
            await registry.probe()
            logger.info(
                "code_intelligence_probed",
                installed=len(registry.available),
            )
        except Exception as exc:
            logger.warning("code_intelligence_probe_failed: %s", exc)

        if _settings_warm.code_sandbox_enabled:
            sandbox = get_sandbox()
            if sandbox is not None and await sandbox.docker_available():
                images = [
                    s.strip() for s in
                    (_settings_warm.code_sandbox_prewarm_images or "").split(",")
                    if s.strip()
                ]
                for img in images:
                    try:
                        await sandbox._ensure_image(img)  # noqa: SLF001
                        logger.info("code_sandbox_prewarmed image=%s", img)
                    except Exception as exc:
                        logger.warning(
                            "code_sandbox_prewarm_failed image=%s err=%s",
                            img, exc,
                        )
    except Exception as exc:
        logger.warning("code_intelligence_warmup_failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("application_starting", service=settings.service_name)

    sweeper_task: Optional[_asyncio_main.Task] = None
    try:
        # Start pipeline
        await pipeline.start()

        # Ensure chat persistence indexes exist (MongoDB)
        try:
            await chat_store.ensure_indexes()
        except Exception as e:
            logger.warning("chat_store_indexes_failed", error=str(e))

        # Ensure auth tables exist (PostgreSQL)
        try:
            await auth_service.bootstrap()
        except Exception as e:
            logger.warning("auth_bootstrap_failed", error=str(e))

        # Sprint 2 — ensure eval_runs table exists.
        try:
            await ensure_eval_runs_schema()
        except Exception as e:
            logger.warning("eval_runs_schema_failed", error=str(e))

        # Sprint 6 — ensure preference_pairs table exists.
        try:
            await ensure_preference_pairs_schema()
        except Exception as e:
            logger.warning("preference_pairs_schema_failed", error=str(e))

        # Initialize Local AI if available
        if LOCAL_AI_AVAILABLE:
            import os
            ollama_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
            ollama_model = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
            nllb_path = os.getenv("NLLB_MODEL_PATH")
            vector_path = os.getenv("LANCEDB_PATH", "/data/vectors")

            await initialize_local_ai(
                ollama_url=ollama_url,
                ollama_model=ollama_model,
                nllb_model_path=nllb_path,
                vector_db_path=vector_path
            )
            logger.info("local_ai_initialized")

        # Unified ModelManager — singleton on app.state. Created on
        # demand by /api/models/* routes if missing, but pre-instantiating
        # here lets us run the first installed-models probe early so the
        # picker UI doesn't take 8s to render on the first open.
        if MODEL_ROUTES_AVAILABLE:
            try:
                app.state.model_manager = ModelManager()
                # Best-effort warm probe — failure logs but doesn't block.
                _asyncio_main.create_task(
                    app.state.model_manager.list_installed(force_refresh=True),
                )
                logger.info("model_manager_initialized")
            except Exception as e:
                logger.warning("model_manager_init_failed", error=str(e))

        # Phase D4 sweeper task — must outlive every request.
        sweeper_task = _asyncio_main.create_task(_sse_queue_sweeper())
        logger.info("sse_queue_sweeper_started")

        # v7 — at startup, mark any consortium sessions whose bg task
        # died with a previous container as "interrupted". Without
        # this, /status would forever claim the dead session is still
        # running. Fire-and-forget so a Redis hiccup never blocks
        # startup. Multi-replica safe: every replica races to the same
        # work but each session-flip is idempotent (status field check).
        if CONSORTIUM_AVAILABLE:
            async def _consortium_zombie_sweep_at_startup() -> None:
                try:
                    from .api import consortium_routes as _cr
                    flipped = await _cr.mark_zombies_at_startup()
                    if flipped:
                        logger.info(
                            "consortium_zombies_marked_at_startup count=%d",
                            flipped,
                        )
                except Exception as e:
                    logger.warning(
                        "consortium_zombie_sweep_at_startup_failed",
                        error=str(e),
                    )
            _asyncio_main.create_task(_consortium_zombie_sweep_at_startup())

        # Code Intelligence warm-up — fire and forget.
        if CODE_INTELLIGENCE_AVAILABLE:
            _asyncio_main.create_task(_code_intelligence_warmup())

            # v2: long-lived autonomous capability discovery loop.
            if settings.code_capability_discovery_enabled:
                from .api.code_intelligence_routes import (
                    get_capability_discoverer,
                )
                _capability_task = _asyncio_main.create_task(
                    get_capability_discoverer().run_forever()
                )
                logger.info(
                    "capability_discoverer_lifespan_task_started "
                    "interval_s=%d",
                    settings.code_capability_discovery_interval_seconds,
                )
            else:
                _capability_task = None

        logger.info("application_started")
        yield
    finally:
        # Shutdown
        logger.info("application_stopping")

        # Cancel the sweeper so the event loop drains cleanly.
        if sweeper_task is not None:
            sweeper_task.cancel()
            try:
                await sweeper_task
            except (_asyncio_main.CancelledError, Exception):
                pass

        # v2: cancel the capability discoverer if it was started.
        try:
            ct = locals().get("_capability_task")
            if ct is not None:
                ct.cancel()
                try:
                    await ct
                except (_asyncio_main.CancelledError, Exception):
                    pass
        except Exception:
            pass

        # Cleanup Local AI if available
        if LOCAL_AI_AVAILABLE:
            await cleanup_local_ai()
            logger.info("local_ai_cleaned_up")

        await pipeline.stop()
        logger.info("application_stopped")


# Create FastAPI app
app = FastAPI(
    title="Document Processor",
    description="Production-ready multi-lingual document processing system",
    version="1.0.0",
    lifespan=lifespan,
)

# Add CORS middleware
#
# P2.1: `allow_origins=["*"]` combined with `allow_credentials=True` is
# a CSRF foot-gun — any third-party site could make authenticated
# requests using the user's cookies. Replaced the wildcard with an
# environment-driven allowlist (defaults to localhost so dev works out
# of the box). Set CORS_ALLOWED_ORIGINS to a comma-separated list of
# origins in production.
import os as _os
_cors_origins = [
    o.strip()
    for o in _os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files + templates
#
# The v1 monolithic UI was retired in favour of the SolidJS + Vite
# v2 build under ``web_ui/v2/``.  Only the build output + favicons +
# Jinja shell live on disk; everything else is the SPA.
#
# Mount order matters — ``/static/v2`` MUST register before
# ``/static`` because Starlette greedy-matches mount prefixes.  In
# the reverse order ``/static/v2/...`` would route into the public
# ``/static`` mount which doesn't have that subtree, returning 404.
import os
web_ui_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web_ui")

_v2_dist_path = os.path.join(web_ui_path, "v2", "dist")
_v2_available = os.path.isdir(_v2_dist_path) and os.path.isfile(
    os.path.join(_v2_dist_path, ".vite", "manifest.json")
)
if _v2_available:
    app.mount(
        "/static/v2",
        StaticFiles(directory=_v2_dist_path),
        name="static_v2",
    )
    logger.info("v2_ui_mounted dist=%s", _v2_dist_path)

    # Cycle C Sprint 12 Day 1 — PWA root-scoped artifacts.  The
    # service worker MUST be served from ``/sw.js`` (root scope) so
    # it can intercept fetches for every route the SPA owns.  The
    # ``manifest.webmanifest`` + icons are also expected at the root
    # by the browser's installable-app heuristic.  Explicitly route
    # each before the SPA catch-all so Jinja's HTML fallback doesn't
    # shadow them.
    from fastapi.responses import FileResponse  # noqa: PLC0415

    def _make_pwa_route(path: str, filename: str, media_type: str):
        def _serve():
            full = os.path.join(_v2_dist_path, filename)
            if not os.path.isfile(full):
                from fastapi import HTTPException as _HTTPException  # noqa: PLC0415
                raise _HTTPException(status_code=404, detail=f"{filename} not built")
            return FileResponse(full, media_type=media_type)
        _serve.__name__ = f"pwa_{filename.replace('.', '_').replace('-', '_')}"
        app.add_api_route(
            path,
            _serve,
            methods=["GET"],
            include_in_schema=False,
        )

    _make_pwa_route("/manifest.webmanifest", "manifest.webmanifest", "application/manifest+json")
    _make_pwa_route("/sw.js",                "sw.js",                "application/javascript")
    _make_pwa_route("/icon-192.svg",         "icon-192.svg",         "image/svg+xml")
    _make_pwa_route("/icon-512.svg",         "icon-512.svg",         "image/svg+xml")
    logger.info("pwa_artifacts_mounted manifest+sw+icons")
else:
    logger.warning(
        "v2_ui_not_built path=%s — run `cd web_ui/v2 && npm run build`",
        _v2_dist_path,
    )

# ``/static`` only serves favicons + img assets now (web_ui/static/img/).
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(web_ui_path, "static")),
    name="static",
)
templates = Jinja2Templates(directory=os.path.join(web_ui_path, "templates"))


def _read_v2_manifest() -> Optional[Dict[str, Any]]:
    """Read Vite's manifest.json so the Jinja template knows which
    hashed asset files to load.  Returns None if the build is missing
    so the route can fall back to a build-required notice."""
    if not _v2_available:
        return None
    try:
        import json as _json
        manifest_path = os.path.join(
            _v2_dist_path, ".vite", "manifest.json",
        )
        with open(manifest_path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception as exc:  # pragma: no cover - safety net
        logger.warning("v2_manifest_read_failed: %s", exc)
        return None


async def _serve_v2_shell(request: Request) -> Response:
    """Render the v2 SPA shell.  The Jinja template inlines the hashed
    entry script + CSS from the Vite manifest."""
    manifest = _read_v2_manifest()
    if manifest is None:
        # Build hasn't run yet — render a minimal "build required" page
        # so the operator gets a clear next step instead of a blank screen.
        return templates.TemplateResponse(
            "v2_build_required.html",
            {"request": request},
            status_code=503,
        )
    # Vite's manifest keys entries by their source path.  ``index.html``
    # is the canonical entry; its `file` is the hashed JS chunk and
    # `css` is the array of CSS chunks.
    entry = manifest.get("index.html") or {}
    return templates.TemplateResponse(
        "v2_shell.html",
        {
            "request": request,
            "entry_js": "/static/v2/" + entry.get("file", ""),
            "entry_css": [
                "/static/v2/" + p for p in entry.get("css", [])
            ],
        },
    )


# ── SPA paths that must NOT be intercepted by the catch-all below.
# Anything starting with one of these prefixes is either a router
# mount (``/api``, ``/static``, ``/grafana``, ``/prometheus``,
# ``/v1`` for OpenAI-compat, ``/mcp`` for Model Context Protocol)
# or an explicit endpoint registered above (``/health``, ``/metrics``,
# ``/stats``, ``/process``, ``/api`` info).  The catch-all SPA
# fallback only fires for paths NOT matched by any of those.
_RESERVED_PREFIXES: tuple[str, ...] = (
    "api/", "static/", "v1/", "mcp/",
    "grafana/", "prometheus/",
    "health", "metrics", "stats", "process",
    "favicon.ico",
)


@app.get("/")
async def root(request: Request):
    """v17 UI cutover — ``/`` now serves the v2 SolidJS SPA directly.
    No more ``/v2`` prefix in the URL bar; the legacy v1 monolith was
    retired in this turn and removed from the codebase entirely.

    The catch-all SPA fallback for client-side routes (e.g.
    ``/research``, ``/build``) is registered at the BOTTOM of this
    file, AFTER every ``app.include_router(...)`` call, so the API
    routers + mounts always win match-priority over the SPA shell."""
    return await _serve_v2_shell(request)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """
    Browsers auto-request /favicon.ico regardless of <link> tags in
    HTML. Without this route every page load logged a 404 in the
    container — noisy and easy to mistake for a real bug. Serve the
    32×32 PNG (browsers accept PNG even on the .ico path) so the
    request resolves and the noise goes away.
    """
    from fastapi.responses import FileResponse
    return FileResponse(
        os.path.join(web_ui_path, "static", "img", "favicon-32.png"),
        media_type="image/png",
    )


@app.get("/api")
async def api_root():
    """API root endpoint."""
    return {
        "service": settings.service_name,
        "version": "1.0.0",
        "environment": settings.environment,
        "status": "running",
        "chat_research_available": CHAT_RESEARCH_AVAILABLE,
        "local_ai_available": LOCAL_AI_AVAILABLE,
        "crawling_available": CRAWLING_AVAILABLE,
        "translation_available": TRANSLATION_AVAILABLE,
    }


# Include API routers
if CHAT_RESEARCH_AVAILABLE:
    app.include_router(chat_research_router)
    logger.info("Chat research routes included")

if LOCAL_AI_AVAILABLE:
    app.include_router(local_ai_router)
    logger.info("Local AI routes included")

# Authentication (PostgreSQL-backed users + JWT)
app.include_router(auth_router)
logger.info("Auth routes included")

# Thinking Mode (human-in-the-loop deep reasoning)
if THINKING_AVAILABLE:
    app.include_router(thinking_router)
    logger.info("Thinking routes included")

# Code Intelligence Mode (multi-agent local-only code engine)
if CODE_INTELLIGENCE_AVAILABLE:
    app.include_router(code_intelligence_router)
    logger.info("Code intelligence routes included")

# Unified model management — /api/models/* (More Settings → AI Model)
if MODEL_ROUTES_AVAILABLE:
    app.include_router(model_router)
    logger.info("Model routes included")

# Consortium Mode — /api/consortium/* (meta-orchestrator)
if CONSORTIUM_AVAILABLE:
    app.include_router(consortium_router)
    logger.info("Consortium routes included")

if QUICK_CODE_AVAILABLE:
    app.include_router(quick_code_router)
    logger.info("QuickCode routes included")

# Sentinel — /api/sentinel/* (multi-agent security intelligence)
if SENTINEL_AVAILABLE:
    app.include_router(sentinel_router)
    logger.info("Sentinel routes included")

# Sentinel Evolution Console — /api/sentinel/evolution/*
if SENTINEL_EVOLUTION_AVAILABLE:
    app.include_router(sentinel_evolution_router)
    logger.info("Sentinel Evolution routes included")

# OpenAI-compatible /v1 facade — Phase 16 Commit C.
# External SDKs (Letta, OpenHands, Aider, OpenAI SDK) plug in via
# OPENAI_BASE_URL=http://localhost:8000/v1.
if OPENAI_COMPAT_AVAILABLE:
    app.include_router(openai_compat_router)
    logger.info("OpenAI-compatible /v1 facade routes included")

# MCP server — /mcp/v1/* (Phase 16 Commit E).  Endpoints are gated
# by settings.enable_mcp_server (default False); the router is
# included unconditionally so flipping the flag at runtime
# activates discovery + tool calls without a restart.
if MCP_ROUTES_AVAILABLE:
    app.include_router(mcp_router)
    logger.info("MCP /mcp/v1 routes included")

# Cycle F Sprint 5 — approval flow.  The router exposes
# POST /api/approval/{request_id} so the browser can resolve
# `approval_required` SSE events from the code-intelligence stream.
if APPROVAL_ROUTES_AVAILABLE:
    app.include_router(approval_router)
    logger.info("Approval flow routes included")

# Chat sessions persistence (MongoDB)
app.include_router(chat_sessions_router)
logger.info("Chat sessions routes included")

# Query records — durable bridge between ephemeral pipeline state and
# permanent chat history (Phase B4 of fancy-swinging-karp.md).
app.include_router(query_record_router)
logger.info("Query record routes included")

# Chat folders persistence (MongoDB)
app.include_router(chat_folders_router)
logger.info("Chat folders routes included")

# Sprint 0 admin baselines dashboard (Cycle C)
app.include_router(admin_baselines_router)
logger.info("Admin baselines routes included")

# Sprint 1 admin LLM dashboard (Cycle C)
app.include_router(admin_llm_router)
logger.info("Admin LLM routes included")

# Sprint 2 admin Eval routes (Cycle C)
app.include_router(admin_evals_router)
logger.info("Admin Evals routes included")

# Sprint 4 Day 2 repo symbol discovery (Cycle C) — backs @-mention picker.
app.include_router(repo_router)
logger.info("Repo symbol routes included")

# Sprint 6 Day 1 admin Training routes (Cycle C) — preference pairs.
app.include_router(admin_training_router)
logger.info("Admin Training routes included")

# Sprint 7 Day 2 admin Memory routes (Cycle C) — Mem0 OSS adapter.
app.include_router(admin_memory_router)
logger.info("Admin Memory routes included")

# Sprint 8 Day 4 agent routes (Cycle C) — ReAct loop with MCP tool dispatch.
app.include_router(agent_router)
logger.info("Agent routes included")

# Sprint 2 — register concrete eval runners.  Import-only side-effect:
# each module calls ``register_eval(...)`` at import time so the
# manifest sees them.  Failures here are logged but non-fatal —
# the dashboard still works without runners.
def _register_eval_runners() -> None:
    import importlib
    for mod in (
        "tools.eval.humaneval_plus",
        "tools.eval.swebench_lite",
        "tools.eval.ragas_lancedb",
        "tools.eval.aider_polyglot",   # Cycle G G1 — 2026-05-16
    ):
        try:
            importlib.import_module(mod)
            logger.info("registered eval runner: %s", mod)
        except Exception as exc:
            logger.warning("eval runner %s register failed: %s", mod, exc)
_register_eval_runners()

# Crawling API routes
if CRAWLING_AVAILABLE:
    app.include_router(crawling_router)
    logger.info("Crawling routes included")

# Translation API routes
if TRANSLATION_AVAILABLE:
    app.include_router(translation_router)
    logger.info("Translation routes included")


# SPA catch-all is registered at the absolute end of this file so
# explicit routes like ``/health``, ``/metrics``, ``/stats`` (defined
# below) win match-priority.  Earlier placement shadowed those routes
# and they returned 404 via the catch-all's reserved-prefix block.

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    cache_healthy = await cache_manager.health_check()
    storage_health = await storage_manager.health_check()

    all_healthy = cache_healthy and all(storage_health.values())

    return HealthStatus(
        status="healthy" if all_healthy else "degraded",
        components={
            "cache": cache_healthy,
            **storage_health,
        },
    )


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    metrics_data = monitor.get_metrics()
    return Response(
        content=metrics_data,
        media_type=monitor.get_content_type(),
    )


@app.get("/stats")
async def get_stats():
    """Get processing statistics."""
    pipeline_metrics = pipeline.get_metrics()
    cache_stats = await cache_manager.get_stats()
    storage_stats = await storage_manager.get_statistics()

    return {
        "pipeline": pipeline_metrics.model_dump(),
        "cache": cache_stats,
        "storage": storage_stats,
    }


@app.post("/process")
async def process_documents(
    request: BatchProcessingRequest,
    background_tasks: BackgroundTasks = None,
):
    """
    Process batch of documents.

    Args:
        request: Batch processing request
        background_tasks: FastAPI background tasks

    Returns:
        Batch processing response
    """
    try:
        logger.info(
            "batch_request_received",
            count=len(request.sources),
            priority=request.priority,
            async_processing=request.async_processing,
        )

        # Validate sources
        if not request.sources:
            raise HTTPException(status_code=400, detail="No sources provided")

        if len(request.sources) > 10000:
            raise HTTPException(
                status_code=400,
                detail="Maximum batch size is 10000 documents",
            )

        # Process synchronously or asynchronously
        if request.async_processing:
            # Submit to background processing
            background_tasks.add_task(pipeline.process_batch, request.sources)

            response = BatchProcessingResponse(
                submitted=len(request.sources),
                estimated_completion_time_seconds=len(request.sources) * 2.0,  # Rough estimate
            )

            logger.info(
                "batch_submitted_async",
                batch_id=response.batch_id,
                count=len(request.sources),
            )

            return response

        else:
            # Process synchronously
            results = await pipeline.process_batch(request.sources)

            response = BatchProcessingResponse(
                submitted=len(request.sources),
                estimated_completion_time_seconds=0.0,
            )

            logger.info(
                "batch_processed_sync",
                batch_id=response.batch_id,
                count=len(request.sources),
                successful=len(results),
            )

            return response

    except Exception as e:
        logger.error("batch_processing_error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/process/single")
async def process_single_document(source: SourceDocument):
    """
    Process single document.

    Args:
        source: Source document

    Returns:
        Translated document
    """
    try:
        result = await pipeline.process_source(source)
        return result
    except Exception as e:
        logger.error(
            "single_document_processing_error",
            source_id=source.id,
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/document/{document_id}")
async def get_document(document_id: str):
    """
    Get processed document by ID.

    Args:
        document_id: Document ID

    Returns:
        Translated document
    """
    document = await storage_manager.get_document(document_id)

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    return document


@app.post("/reset-metrics")
async def reset_metrics():
    """Reset processing metrics."""
    pipeline.reset_metrics()
    return {"message": "Metrics reset successfully"}


# ── SPA catch-all — must be the LAST registered route ──────────────────────
# FastAPI matches routes in registration order.  An earlier
# ``/{spa_path:path}`` shadowed every explicit route registered after
# it (``/health``, ``/metrics``, ``/stats``, ``/process``, …) — those
# were silently 404'd by the catch-all's reserved-prefix block.
# Putting the catch-all at the bottom of the file guarantees every
# explicit endpoint above wins match-priority.
@app.get("/{spa_path:path}", include_in_schema=False)
async def spa_fallback(request: Request, spa_path: str):
    """Catch-all SPA fallback — every unmatched GET URL renders the
    same shell so SolidJS Router takes over client-side.  Reserved
    prefixes like ``api/``, ``static/``, ``grafana/`` are blocked
    here as a safety net even though their mounts win
    match-priority above us."""
    if any(spa_path.startswith(p) for p in _RESERVED_PREFIXES):
        from fastapi import HTTPException  # noqa: PLC0415
        raise HTTPException(status_code=404, detail="Not Found")
    return await _serve_v2_shell(request)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "document_processor.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
