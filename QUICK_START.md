# AMOR — Quick Start

One-page bootstrap for Windows / macOS / Linux.  Backed by the
cross-platform installer at `tools/setup/`.

---

## Prerequisites

| | minimum | recommended |
|---|---|---|
| OS | Windows 10/11, macOS 12+, Linux (any modern distro) | — |
| RAM | 8 GiB | 16 GiB |
| Disk | 30 GiB free | 60 GiB free |
| Docker | Docker Desktop ≥4.30 (Win/Mac) or Engine ≥24 (Linux) | with Compose v2 |
| Python | 3.9+ (only for the installer itself) | 3.11 or 3.12 |
| GPU (optional) | none — CPU paths work | NVIDIA ≥8 GiB VRAM |

The installer checks all of the above for you before doing anything.

---

## Install

Pick the entry point that matches your shell:

```bash
# Linux / macOS / Windows Git-Bash / WSL
./setup.sh install
```

```powershell
# Windows PowerShell
.\setup.ps1 install
```

```bash
# Unix users who prefer make
make install
```

What `install` does (each step is idempotent — safe to re-run):

1. **Preflight** — Docker, disk, RAM, GPU, ports, network registries.
2. **Repo state** — creates `.env` from `.env.example` (with placeholder
   API-keys safely emptied) + creates `data/`, `models/`, `sandbox-shared/`.
3. **Pull images** — `docker compose pull` (Windows overlay auto-detected).
4. **Build local images** — `docker compose build`.
5. **Start containers** — `docker compose up -d`.
6. **Wait-for-health** — polls each core service with exponential backoff
   until `/health` returns OK (or 4-min timeout).
7. **Model bootstrap** — pulls judge GGUFs / ollama tags per profile.
8. **Verify** — live HTTP smoke against /health, /docs, /metrics, plus
   redis-cli / pg_isready / mongo ping inside the containers.

Total fresh-install time: ~10–15 min on a warm host, ~25–40 min cold.

---

## Profiles

```bash
./setup.sh install --profile minimal      # core data plane only
./setup.sh install --profile full         # default: every service
./setup.sh install --profile dev          # full + auto-pull Mistral judge
./setup.sh install --profile baseline     # full + Mistral + Phi-4 judges
```

| profile | services | judge models |
|---|---|---|
| `minimal`  | gateway + app + postgres + redis + mongo | — |
| `full`     | everything in `docker-compose.yml`       | — |
| `dev`      | everything                               | Mistral-Small-3 |
| `baseline` | everything                               | Mistral-Small-3 + Phi-4 |

---

## Daily commands

```bash
./setup.sh start          # bring stack up + wait-for-health
./setup.sh stop           # stop containers (volumes preserved)
./setup.sh restart        # restart + re-check health
./setup.sh status         # compose ps + per-service health probes
./setup.sh logs app -f    # follow app logs
./setup.sh doctor         # full read-only diagnostic
./setup.sh verify         # live smoke against running stack
```

Or via `make`:

```bash
make start    make stop    make status    make doctor    make verify
make logs SVC=app
```

---

## URLs (after install)

- **Web UI**     http://localhost:8000
- **API docs**   http://localhost:8000/docs
- **Health**     http://localhost:8000/health
- **Metrics**    http://localhost:8000/metrics
- **Prometheus** http://localhost:9091/prometheus/
- **Grafana**    http://localhost:3000  (admin / admin123 on first boot)

---

## Troubleshooting

```bash
./setup.sh doctor
```

prints a colour-coded report covering:

* Host (OS / RAM / disk / GPU)
* Repo state (compose files / .env / data dirs)
* Docker engine + compose detection
* Per-service health (probe URL + state)
* Judge GGUF inventory (which profiles are pre-downloaded)
* Ollama model inventory
* **Suggested fixes** — every check that failed includes a remediation hint

For machine-readable output:

```bash
./setup.sh doctor --json
./setup.sh verify --json
```

---

## Teardown

```bash
./setup.sh stop                              # graceful stop, keeps data
./setup.sh destroy                           # remove containers, keep volumes
./setup.sh destroy --volumes --yes           # DELETE ALL DATA (irreversible)
```

---

## Sprint 0 v18 baseline run

After `install --profile baseline` you can kick off the overnight
baseline:

```bash
export AMOR_BASELINE_USERNAME=amor-baseline-runner
export AMOR_BASELINE_PASSWORD='<vault-secret>'
nohup tools/run_sprint0_v18.sh > /tmp/sprint0_v18.log 2>&1 &
```

See `docs/sprint0_v18_runbook.md` for the full walkthrough.
