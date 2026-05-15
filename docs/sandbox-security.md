# Sandbox security hardening — operator runbook

> Cycle C Sprint 5.  Status: scaffolding live, opt-in by env flip.

## Threat model

The Code Intelligence sandbox runs LLM-generated code in a fresh
ephemeral Docker container per request.  Containers are spawned by the
app process via `docker run`, which currently requires a path to the
Docker daemon.  Two ways to grant that access:

| Path | What it can do | Escape risk |
|------|----------------|-------------|
| **Direct bind-mount** `/var/run/docker.sock:ro` | Full Docker Engine API (read-only file descriptor doesn't help — the app can still call `POST /containers/create` over the socket and get root via privileged-flag tricks) | Container escape via `--privileged`, host filesystem mount, or kernel module load |
| **`docker-socket-proxy`** + whitelisted endpoints | `VERSION` / `PING` / `INFO` / `IMAGES` / `CONTAINERS` / `POST` / `EXEC` / `VOLUMES` only | App can still spin runners, but **cannot** mount host paths, run `--privileged`, or hit the Swarm/Network/Build APIs |

Defense in depth then layers per-runner-container hardening (Sprint 5
Day 3): `--read-only`, `--tmpfs /tmp`, `--cap-drop=ALL`,
`--security-opt=no-new-privileges`, seccomp profile.

## Activating the proxy

The compose file already defines `docker-socket-proxy` (the
`tecnativa/docker-socket-proxy:latest` image) on the
`docprocessor-network`.  Each app replica depends on it but uses the
*direct* socket by default — flipping the switch is a single env var:

```bash
# .env
AMOR_DOCKER_HOST=tcp://amor-docker-proxy:2375

# then
docker compose up -d
```

When `AMOR_DOCKER_HOST` is non-empty, compose passes it to the app as
`DOCKER_HOST`, which the docker CLI honours for **every** subprocess
the app spawns (including the sandbox's `docker run`/`exec`/`rm`).

## Smoke probe

```bash
# Reachable through the proxy (whitelisted)
docker exec amor-app-1 sh -c \
  'DOCKER_HOST=tcp://amor-docker-proxy:2375 docker version --format "{{.Server.Version}}"'

# Forbidden (denied by proxy whitelist) — should return 403
docker exec amor-app-1 sh -c \
  'DOCKER_HOST=tcp://amor-docker-proxy:2375 docker swarm init 2>&1 | head -1'
```

## Rolling back

If the proxy whitelist is too tight for a new sandbox flow:

```bash
# .env
AMOR_DOCKER_HOST=

# or just delete the line; compose's :- default expands to empty
docker compose up -d app
```

Compose hot-restarts the app replicas; sandbox immediately falls back
to the direct socket.

## Whitelist rationale (compose env on `docker-socket-proxy`)

| Toggle | Why | Source |
|--------|-----|--------|
| `VERSION=1` | `docker version` probes (health + smoke) | sandbox.py:`docker_available` |
| `PING=1` | Container health check | docker daemon |
| `INFO=1` | Capability discovery | optional |
| `IMAGES=1` | `image inspect`, `pull` for runtime images | sandbox.py:`_pull_image_if_needed` |
| `CONTAINERS=1` | List / inspect containers | sandbox state introspection |
| `POST=1` | Allow container *create + start* writes | required for `docker run` |
| `EXEC=1` | Future warm-pool path | Sprint 5 Day 3+ |
| `VOLUMES=1` | Named volume `amor-sandbox-shared` | sandbox.py:`_resolve_workdir_root` |

Forbidden by absence: `BUILD`, `COMMIT`, `CONFIGS`, `NETWORKS`,
`NODES`, `PLUGINS`, `SERVICES`, `SWARM`, `TASKS`, `SECRETS`, plus
`ALLOW_RESTARTS` left at `0`.

## Sprint 5 day-by-day status

| Day | Deliverable | Status |
|-----|-------------|--------|
| 1 | docker-socket-proxy scaffolding (env opt-in) | **live** |
| 2 | sandbox.py `AMOR_DOCKER_HOST` plumbing + tests | TBD |
| 3 | seccomp profiles + `--read-only` / `--cap-drop` flags | TBD |
| 4 | `tools/sandbox_smoke.py` (20 known-good/bad cases) | TBD |
| 5 | docker-bench-security wrapper + status-badge UI | TBD |
