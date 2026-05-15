# Sandbox hardening runbook

> Cycle F Sprint 5 — Wrong #2 fix landed.  This doc consolidates
> the sandbox security posture, the active hardening flags, and the
> rollback paths.

## Active defaults (Cycle F)

| Surface | Default | Rationale |
|---|---|---|
| Docker socket access | **`tecnativa/docker-socket-proxy` allowlist** (`DOCKER_HOST=tcp://amor-docker-proxy:2375`) | Wrong #2 fix.  Whitelisted endpoints: VERSION + PING + INFO + IMAGES + CONTAINERS + POST + EXEC + VOLUMES.  Everything else (NETWORKS, SWARM, SYSTEM, BUILD, CONFIGS, NODES, PLUGINS, SERVICES, TASKS) denied. |
| Sandbox container caps | `--cap-drop=ALL` | Cycle C Sprint 5 Day 3 |
| Sandbox no-new-privs | `--security-opt=no-new-privileges` | Cycle C Sprint 5 Day 3 |
| Sandbox rootfs | `--read-only` + `--tmpfs /tmp:size=384m,exec` | Cycle C Sprint 5 Day 3 |
| Sandbox PIDs | `--pids-limit=128` | Cycle C Sprint 5 Day 3 (fork-bomb guard) |
| Sandbox memory | `--memory=256m` + `--memory-swap=256m` | Cycle C Sprint 5 Day 3 |
| Sandbox CPU | `--cpu-quota=50000` (50% of one core) | Cycle C Sprint 5 Day 3 |
| Network | `--network=none` (or `bridge` only when `pip install` is needed) | Cycle C Sprint 5 Day 3 |
| Workdir | named volume `amor-sandbox-shared` (NOT direct bind-mount of host path) | Cycle C Phase 16.5 Commit K |
| Approval gate on MCP tool dispatch | `code_approval_enabled=False` (off by default; flip to enable) | Cycle F Sprint 5 |
| runc version | bundled with Docker Desktop ≥ 4.30 | Cycle F Sprint 5; CVE-2025-31133 mitigated by Docker Desktop ≥ 4.30 |

## Docker socket proxy default flip (2026-05-15)

`docker-compose.yml` now sets:

```yaml
- DOCKER_HOST=${AMOR_DOCKER_HOST-tcp://amor-docker-proxy:2375}
- AMOR_DOCKER_HOST=${AMOR_DOCKER_HOST-tcp://amor-docker-proxy:2375}
```

The trailing `-` (no colon) means the default applies ONLY when the
env var is UNSET; an explicit empty string `AMOR_DOCKER_HOST=""` in
`.env` reverts to the bind-mounted unix socket.

### How we verified it's safe

`tools/sandbox_proxy_smoke.py` exercises 11 assertions:

* **Allowed** (must succeed under the proxy):
  1. `docker version`            — VERSION
  2. `docker info`               — INFO
  3. `docker image inspect`      — IMAGES
  4. `docker container ls`       — CONTAINERS
  5. `docker volume ls`          — VOLUMES
  6. `docker run --rm busybox echo` — CONTAINERS + POST + IMAGES
  7. `docker exec amor-app-2 echo` — EXEC

* **Denied** (must fail under the proxy):
  1. `docker network create`     — NETWORKS=0
  2. `docker swarm init`         — SWARM=0
  3. `docker system prune`       — SYSTEM=0
  4. `docker buildx ls`          — BUILD=0

Live verification (2026-05-15, against the running proxy):

```
+ docker version (VERSION)                         exp=pass  got=pass  (38 ms)
+ docker info (INFO)                               exp=pass  got=pass  (46 ms)
+ docker image inspect (IMAGES)                    exp=pass  got=pass  (59 ms)
+ docker container ls (CONTAINERS)                 exp=pass  got=pass  (60 ms)
+ docker volume ls (VOLUMES)                       exp=pass  got=pass  (24 ms)
+ docker run --rm busybox echo (CONTAINERS+POST)   exp=pass  got=pass  (711 ms)
+ docker exec amor-app-2 echo (EXEC)               exp=pass  got=pass  (122 ms)
+ docker network create (NETWORKS=0)               exp=fail  got=fail  (22 ms)
+ docker swarm init (SWARM=0)                      exp=fail  got=fail  (21 ms)
+ docker system prune (SYSTEM=0)                   exp=fail  got=fail  (1115 ms)
+ docker build (BUILD=0)                           exp=fail  got=fail  (15 ms)
passed=11/11
```

Re-run any time after changing the proxy env block:

```bash
# Quick host-side smoke (requires docker CLI on host + AMOR network
# reachable via Docker Desktop):
docker cp tools/sandbox_proxy_smoke.py amor-app-2:/tmp/
docker exec -e DOCKER_HOST=tcp://amor-docker-proxy:2375 \
  amor-app-2 python /tmp/sandbox_proxy_smoke.py \
  --proxy-host tcp://amor-docker-proxy:2375
```

### Rolling back the default

If the proxy breaks a code-intelligence run that the smoke test
didn't anticipate, revert via:

```bash
# In .env:
AMOR_DOCKER_HOST=
# Then:
docker compose up -d --force-recreate app
```

The escape hatch is intentional: dev hosts that need raw `docker`
API access (e.g. for ad-hoc network inspection from within the
sandbox container) can opt out without re-editing compose.

## runc version (CVE-2025-31133 mitigation)

The November 2025 runc CVE chain (CVE-2025-31133, 52565, 52881, all
disclosed by SUSE's Aleksa Sarai) bypasses AppArmor and SELinux
labels and is exploitable from a malicious image alone.  Mitigation:
**Docker Desktop ≥ 4.30** ships runc ≥ 1.2.x with the patches.

AMOR doesn't bundle its own runc — sandbox containers use whatever
runc the Docker daemon hands them, which on Docker Desktop is the
version bundled with the installer.  We can't pin runc independently.

**Operator action**: keep Docker Desktop on the latest stable
channel.  `tools/setup/preflight.py` warns when Docker server
version is below 24.0.

### Verification

```bash
# Check the active Docker version:
docker version --format '{{.Server.Version}}'
# Anything ≥ 24.0 ships runc ≥ 1.2 (Docker Desktop ≥ 4.30).
```

## Approval flow integration (Cycle F Sprint 5)

The approval-policy gate (`ApprovalPolicy.decide()` wrapped around
`ToolRegistry.dispatch()`) is a SECOND-LAYER defense: even when a
tool call gets through the proxy allowlist, the policy can deny or
require human approval based on tool name / category.  See
`docs/sprint5_runbook.md` for the operator how-to.

Combined defense:

```
LLM-generated tool call
    │
    ▼
ApprovalPolicy.decide()  ──── deny ──→ tool blocked at dispatch
    │
    ▼  (allow or user-approves)
Tool executes via sandbox
    │
    ▼
Docker API call ────────── proxy allowlist ──→ denied destructive ops
    │
    ▼
Runner container with --cap-drop=ALL + --read-only + --pids-limit
```

## Rollback paths (worst to least invasive)

| change | rollback | risk |
|---|---|---|
| Approval gate (Sprint 5) | `code_approval_enabled=False` env | restores Sprint 4 behaviour |
| Proxy default (Wrong #2) | `AMOR_DOCKER_HOST=` in .env + compose recreate | restores direct unix socket |
| Sandbox cap-drop / no-new-priv | Edit `code_intelligence/sandbox.py:901-950` | NOT recommended; reduces hardening |
| Sandbox image | Switch to a different base in `TEST_RUNNERS` | very rare |

## Future work

* **runc-in-sandbox pinning**: when the sandbox image gets a custom
  Dockerfile (today the runners use stock `python:3.11-slim` /
  `node:20-slim`), pin a known-clean runc binary inside.  Until
  then, mitigation is Docker Desktop ≥ 4.30.
* **Cloud Hypervisor microVMs** (deferred): full-isolation runner
  alternative to Docker.  Requires WSL2 nested-virtualisation
  configuration.  Documented in the v18 plan §"Deferred".
* **Per-operator allowlist drift detection**: a CI check that
  warns when `docker-compose.yml` proxy env block drifts from the
  smoke-tested allowlist (e.g. an operator opens NETWORKS=1 by
  mistake).
