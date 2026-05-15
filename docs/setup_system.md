# AMOR setup system — operator reference

> Cross-platform install / start / verify orchestrator.  Replaces the
> ad-hoc `start.sh` / `start.ps1` / `validate_setup.ps1` scripts.
> Cycle E v18 landing.

## Design principles

1. **Stdlib-only Python orchestrator.**  Anything that needs `pip
   install` is in the *target* system (the AMOR container), not in
   the *installer*.  This means the installer boots before the venv
   exists.
2. **Idempotent everything.**  `setup.sh install` is safe to re-run.
   `.env` is never clobbered.  Directories that exist stay as-is.
   `docker compose up -d` short-circuits already-running services.
3. **OS-detection at the seam, not throughout.**  `util.detect_os()`
   is the only place that branches on Windows/macOS/Linux.  Compose
   overlay detection, path handling, encoding, and command-shape
   live behind that one seam.
4. **Friendly errors with remediation.**  Every failed preflight or
   doctor check ships a `remediation` string the user can copy-paste.
5. **Machine + human output.**  Every diagnostic command supports
   `--json` for CI consumption; the default is colour-coded human
   output.
6. **Zero-config defaults.**  `./setup.sh` with no args = "install
   with sensible defaults".  Power users layer on `--profile`,
   `--skip-models`, etc.

## Module map

```
tools/setup/
  __init__.py    — version + module docstrings
  __main__.py    — `python -m tools.setup` entry; forces UTF-8 stdio on Win
  cli.py         — argparse dispatch + subcommand wiring
  constants.py   — service catalogue, profile definitions, resource floors
  util.py        — OS detection, run(), http_probe(), tcp_probe(),
                   port_in_use(), spinner, colour, logging
  preflight.py   — 11 pre-install gates with blocker/warn taxonomy
  envfile.py     — .env materialization + data-dir creation (idempotent)
  compose.py     — docker compose v1/v2 detection + overlay handling
  health.py      — wait_for() with exponential backoff
  models.py      — judge GGUF + ollama tag bootstrap
  services.py    — start / stop / restart / status / logs / destroy
  verify.py      — live HTTP/exec smoke against running stack
  doctor.py      — read-only diagnostic with --json
  install.py     — 8-phase orchestrator (preflight → ... → verify)
```

## Subcommand reference

| command | what it does | exit codes |
|---|---|---|
| `install`   | full bootstrap (preflight + up + verify)                          | 0 / 1 / 2 |
| `start`     | `compose up -d` + wait-for-health                                 | 0 / 1 |
| `stop`      | `compose stop` (keeps volumes)                                    | 0 / 1 |
| `restart`   | restart + re-check health                                         | 0 / 1 |
| `destroy`   | `compose down`; `--volumes` wipes data (requires `--yes`)         | 0 / 1 |
| `status`    | compose ps + per-service health probes                            | 0 / 1 |
| `logs`      | tail container logs (`-f` follow, `-n` lines)                     | 0 / 1 |
| `doctor`    | full read-only diagnostic (`--json` for CI)                       | 0 / 1 |
| `verify`    | live HTTP + container-exec smoke (`--shallow` skips exec)         | 0 / 1 |
| `preflight` | host pre-checks only (read-only)                                  | 0 / 1 / 2 |

Exit codes:
- **0** — success
- **1** — recoverable failure (with remediation hint)
- **2** — fatal preflight failure (Docker missing, disk full, etc.)
- **127** — Python 3.9+ not on PATH (shim layer)
- **130** — interrupted (Ctrl-C)

## Profiles

Defined in `constants.PROFILES`.

| profile | services | judge pull | rationale |
|---|---|---|---|
| `minimal`  | gateway, app, postgres, redis, mongo                        | —                      | Smallest viable AMOR — no Kafka, no Ollama, no Grafana. |
| `full`     | every service in docker-compose.yml                         | —                      | **Default.** Daily-dev profile. |
| `dev`      | every service                                               | Mistral-Small-3        | Single judge ready for Sprint-0-style runs. |
| `baseline` | every service                                               | Mistral + Phi-4        | Pre-pulls both judge GGUFs (~23 GB disk). |

Adding a profile: extend `PROFILES` in `constants.py`.  Tests in
`tests/setup/test_constants.py::test_profiles_reference_real_services`
ensure profiles only name services that exist.

## Preflight checks (11)

| check | blocker if fails? | seam (for tests) |
|---|---|---|
| OS supported (Win/macOS/Linux)        | yes | `util.detect_os` |
| Python ≥ 3.9                          | yes | `sys.version_info` |
| `docker` in PATH                      | yes | `util.which` |
| Docker daemon responsive              | yes | `util.run(['docker','info'])` |
| Compose engine detectable             | yes | `compose.detect_engine` |
| `docker-compose.yml` exists           | yes | `Path.is_file` |
| Disk free ≥ 30 GiB (warn < 60)        | block at 30, warn at 60 | `util.detect_disk_free_gb` |
| RAM ≥ 8 GiB (warn < 16)               | block at 8, warn at 16  | `util.detect_ram_gb` |
| GPU info (optional, warn-only)        | never | `util.gpu_info` |
| Host ports free (warn if busy)        | never | `util.port_in_use` |
| Registry reachability (warn-only)     | never | `util.tcp_probe` |

## Service catalogue

`constants.SERVICES` lists 12 services across 2 tiers:

* **Core (5):** gateway, app, postgres, redis, mongo
* **Optional (7):** kafka, zookeeper, ollama, llama-swap,
  prometheus, grafana, docker-socket-proxy

For each service:
- `name` matches the compose service key
- `container` is the docker container name for `docker exec` probes
- `health_url` is the HTTP probe URL (or `None` for tcp-only)
- `host_ports` lists published ports
- `tier` is `"core"` or `"optional"`
- `probe_kind` is `"http"` / `"tcp"` / `"container"`

`tests/setup/test_constants.py::test_constants_services_match_compose_yaml`
verifies every service listed here actually exists in
`docker-compose.yml`.

## Health wait

`health.wait_for(services, timeout_s=240)` polls each service with:
- initial 1 s interval, doubling up to 5 s cap
- one round per "tick" — healthy services drop out
- `on_attempt(remaining, elapsed)` callback used by the install
  orchestrator for the spinner

Used by `install` (240 s budget), `start` (180 s budget), and
`restart` (120 s budget).

## Tests

73 unit tests in `tests/setup/`:

```bash
python -m pytest tests/setup -v          # all tests
python -m pytest tests/setup -k preflight  # subset
python -m pytest tests/setup -q          # quick summary
```

Test layout:

| file | covers | tests |
|---|---|---|
| test_cli.py        | argparse dispatch + flag combinations          |  9 |
| test_compose.py    | engine detection + yaml parser + cmd shape     |  8 |
| test_constants.py  | catalogue invariants + compose sync            |  9 |
| test_envfile.py    | idempotency + placeholder reset                |  7 |
| test_health.py     | probe shape + backoff + completion tracking    |  9 |
| test_preflight.py  | every check, including monkey-patched seams    | 17 |
| test_util.py       | OS detection + port probe + run() + spinner    | 14 |

Every test stubs the outside world via `monkeypatch` so the suite
runs offline in <2 s.

## Live verification matrix (Cycle E launch)

Ran against the running Windows 11 stack on 2026-05-11:

```
$ python -m tools.setup preflight
  ✓ Operating system — Windows 11 (10.0.26200)
  ✓ Python version — have 3.13, need ≥ 3.9
  ✓ Docker CLI — found in PATH
  ✓ Docker daemon — responsive
  ✓ Docker Compose — using `docker compose`
  ✓ docker-compose.yml — 18 KiB
  ✓ Disk space — 352.4 GiB free
  ✓ System RAM — 31.7 GiB
  ✓ GPU (optional) — NVIDIA GeForce RTX 4060 Laptop GPU VRAM=8.0 GiB
  ✓ Host ports — 9 ports free
  ✓ Network reachability — 3/3 registries reachable

$ python -m tools.setup status
  All 12 services running; 11/12 health probes green
  (one Prometheus probe url corrected post-test from /-/healthy
   to /prometheus/-/healthy; see constants.py).

$ python -m tools.setup verify
  ✓ API /health — all deps OK
  ✓ Auth router — HTTP 401
  ✓ OpenAPI /docs — HTTP 200
  ✓ Prometheus /metrics — 1 amor_* series
  ✓ Postgres pg_isready — accepting connections
  ✓ Redis PING
  ✓ MongoDB ping — 1
  All 7/7 verification checks passed.
```

## Rollback

If `tools/setup` regresses, fall back to direct compose:

```bash
docker compose up -d
docker compose ps
docker compose logs -f app
docker compose down
```

The legacy `start.sh` / `start.ps1` files still exist (now as 3-line
shims pointing at `setup.sh`).  Re-instate the Cycle D versions from
git history if needed:

```bash
git show HEAD~1:start.sh > start.sh
git show HEAD~1:start.ps1 > start.ps1
```

## Future work (deferred)

* Auto-detect WSL2 distro and recommend Docker Desktop integration.
* Speak `docker compose up --wait` on Compose v2.23+ (skips our
  manual health poll on supported hosts).
* Pull from `tools/pull_models.py` for the llama-swap models when
  Sprint 1 v18 lands.
* `setup.sh upgrade` — `git pull` + image pull + recreate, with
  pre-flight check that the configured profile is still valid after
  the upgrade.
