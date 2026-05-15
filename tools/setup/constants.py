"""
Cycle E v18 — single source of truth for the cross-platform installer.

Everything that describes "what AMOR consists of" lives here:
  * compose files per platform
  * services (core vs optional vs judge) with health-check URLs
  * port map
  * resource floors (RAM / disk / GPU)
  * model bootstrap targets

If you change a service in docker-compose.yml, update the entry here so
the doctor / verify / install path stays consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ─── Repo root ──────────────────────────────────────────────────────

REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent

# ─── Compose files ──────────────────────────────────────────────────

COMPOSE_BASE: str = "docker-compose.yml"
COMPOSE_WINDOWS_OVERLAY: str = "docker-compose.windows.yml"
COMPOSE_LOCAL_AI_OVERLAY: str = "docker-compose.local-ai.yml"

# ─── Resource floors ────────────────────────────────────────────────

# Hard floor — install refuses to proceed below.
MIN_DISK_FREE_GB: float = 30.0
MIN_RAM_GB: float = 8.0

# Soft floor — install warns but proceeds.
RECOMMENDED_DISK_FREE_GB: float = 60.0
RECOMMENDED_RAM_GB: float = 16.0
RECOMMENDED_VRAM_GB: float = 8.0  # RTX 4060 reference

# Python version floor (the orchestrator itself).
MIN_PYTHON: tuple[int, int] = (3, 9)

# ─── Service definitions ────────────────────────────────────────────


@dataclass(frozen=True)
class ServiceSpec:
    """Declarative spec for one docker-compose service."""

    # The key under `services:` in docker-compose.yml.
    name: str
    # Human-readable label for tables / errors.
    label: str
    # Container name when known (used by `docker exec`), else None.
    container: str | None
    # URL the doctor polls for liveness, or None if container-only.
    health_url: str | None
    # Host port(s) the service publishes.
    host_ports: tuple[int, ...]
    # Tier: "core" services must be up for AMOR to function;
    # "optional" services are nice-to-have; "judge" runs only during
    # Sprint 0 baseline + on-demand evals.
    tier: str
    # Friendly description shown by `doctor`.
    description: str
    # When health_url is non-HTTP (e.g. tcp probe), use this hint.
    probe_kind: str = "http"


SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        name="gateway",
        label="API Gateway (nginx)",
        container="amor-gateway-1",
        health_url="http://localhost:8000/health",
        host_ports=(8000,),
        tier="core",
        description="Reverse proxy + sticky cookie + SSE pass-through.",
    ),
    ServiceSpec(
        name="app",
        label="AMOR App (FastAPI)",
        container="amor-app-1",
        health_url="http://localhost:8000/health",
        host_ports=(),
        tier="core",
        description="Backend API — code intel, research, sentinel, sessions.",
    ),
    ServiceSpec(
        name="postgres",
        label="Postgres",
        container="amor-postgres-1",
        health_url=None,
        host_ports=(5432,),
        tier="core",
        description="Sessions / preference pairs / eval runs.",
        probe_kind="tcp",
    ),
    ServiceSpec(
        name="redis",
        label="Redis",
        container="amor-redis-1",
        health_url=None,
        host_ports=(6379,),
        tier="core",
        description="Pub/sub fan-out + sliding-window cache.",
        probe_kind="tcp",
    ),
    ServiceSpec(
        name="mongo",
        label="MongoDB",
        container="amor-mongo-1",
        health_url=None,
        host_ports=(27017,),
        tier="core",
        description="Document chunks + RAG metadata.",
        probe_kind="tcp",
    ),
    ServiceSpec(
        name="kafka",
        label="Kafka",
        container="amor-kafka-1",
        health_url=None,
        host_ports=(9092,),
        tier="optional",
        description="Async job stream (used by ingestion pipelines).",
        probe_kind="tcp",
    ),
    ServiceSpec(
        name="zookeeper",
        label="Zookeeper",
        container="amor-zookeeper-1",
        health_url=None,
        host_ports=(),
        tier="optional",
        description="Kafka coordinator.",
        probe_kind="container",
    ),
    ServiceSpec(
        name="ollama",
        label="Ollama",
        container="amor-ollama",
        health_url="http://localhost:11434/api/tags",
        host_ports=(11434,),
        tier="optional",
        description="LLM backend (Cycle B/C legacy; llama-swap is the v18 path).",
    ),
    ServiceSpec(
        name="llama-swap",
        label="llama-swap (llama.cpp)",
        container="amor-llama-swap",
        health_url="http://localhost:9100/health",
        host_ports=(9100,),
        # Cycle F Sprint 1 — promoted to core tier: the v18 default
        # inference path.  Ollama remains as optional rollback target
        # via AMOR_LLM_BACKEND=ollama.
        tier="core",
        description="Default inference layer (architect + editor swap-in).",
    ),
    ServiceSpec(
        name="prometheus",
        label="Prometheus",
        container="amor-prometheus-1",
        # Compose publishes the container's :9090 on host port 9091
        # (port 9090 is reserved on the AMOR app for /metrics).
        # Prometheus is served under a /prometheus/ external-url prefix.
        health_url="http://localhost:9091/prometheus/-/healthy",
        host_ports=(9091,),
        tier="optional",
        description="Metrics scrape + retention.",
    ),
    ServiceSpec(
        name="grafana",
        label="Grafana",
        container="amor-grafana-1",
        health_url="http://localhost:3000/api/health",
        host_ports=(3000,),
        tier="optional",
        description="Dashboards (admin/admin123 on first boot).",
    ),
    ServiceSpec(
        name="docker-socket-proxy",
        label="Docker Socket Proxy",
        container="amor-docker-proxy",
        health_url=None,
        host_ports=(),
        tier="optional",
        description="Sprint 5 sandbox security hardening (opt-in via DOCKER_HOST).",
        probe_kind="container",
    ),
)

# Judge container is launched on-demand by tools/judge/select_and_start.sh
# (Sprint 0 baseline runs).  Not part of the default compose stack.
JUDGE_HEALTH_URL: str = "http://localhost:9101/health"
JUDGE_CONTAINER: str = "amor-judge"

# ─── Port map (for "is this port free?" preflight) ──────────────────

ALL_HOST_PORTS: tuple[int, ...] = tuple(
    sorted({p for svc in SERVICES for p in svc.host_ports})
)

# ─── Reachability probes (network preflight) ────────────────────────

EXTERNAL_HOSTS: tuple[tuple[str, str], ...] = (
    ("registry.docker.io",  "Docker Hub (image pull)"),
    ("registry.ollama.ai",  "Ollama model registry"),
    ("huggingface.co",      "HuggingFace Hub (judge GGUFs)"),
)

# ─── Install profiles ───────────────────────────────────────────────


@dataclass(frozen=True)
class Profile:
    """A named subset of services + optional model bootstrap."""

    name: str
    description: str
    services: tuple[str, ...]
    pull_judge_mistral: bool = False
    pull_judge_phi4: bool = False
    pull_ollama_default: bool = False
    pull_llamaswap_models: bool = False


PROFILES: dict[str, Profile] = {
    "minimal": Profile(
        name="minimal",
        description="Core data plane only (gateway + app + postgres + redis + mongo).",
        services=("gateway", "app", "postgres", "redis", "mongo"),
    ),
    "full": Profile(
        name="full",
        description="Everything in the default compose file (all SERVICES above).",
        services=tuple(svc.name for svc in SERVICES),
    ),
    "dev": Profile(
        name="dev",
        description="Full stack + auto-pull Mistral judge GGUF for Sprint 0.",
        services=tuple(svc.name for svc in SERVICES),
        pull_judge_mistral=True,
    ),
    "baseline": Profile(
        name="baseline",
        description="Full stack + both judge GGUFs (Mistral + Phi-4 fallback).",
        services=tuple(svc.name for svc in SERVICES),
        pull_judge_mistral=True,
        pull_judge_phi4=True,
    ),
}

DEFAULT_PROFILE: str = "full"

# ─── Friendly URLs printed at end of install ────────────────────────

POST_INSTALL_URLS: tuple[tuple[str, str], ...] = (
    ("Web UI",      "http://localhost:8000"),
    ("API docs",    "http://localhost:8000/docs"),
    ("Health",      "http://localhost:8000/health"),
    ("Prometheus",  "http://localhost:9090"),
    ("Grafana",     "http://localhost:3000"),
)
