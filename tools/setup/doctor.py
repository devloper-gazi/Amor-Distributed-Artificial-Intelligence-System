"""
Diagnostic reporter.

Walks every aspect of an AMOR install and prints a colour-coded
report.  Read-only — never modifies state.  Returns a non-zero exit
code if any blocker check fails (so it can gate CI).

Subreports:
  1. Host environment    (OS, Python, Docker, RAM, disk, GPU)
  2. Repository state    (compose files, .env, data dirs)
  3. Compose engine      (which binary, which overlay files)
  4. Service health      (core + optional + judge)
  5. Models inventory    (judge GGUFs in volume + ollama tags)
  6. Suggested fixes     (deduplicated remediation list)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from tools.setup import (
    compose,
    constants,
    envfile,
    health,
    models as models_mod,
    preflight,
    util,
)


def _section(title: str) -> None:
    print(f"\n{util.C_BOLD}{title}{util.C_RESET}")
    sep = ("─" if util._UNICODE_OK else "-") * len(title)
    print(util.C_DIM + sep + util.C_RESET)


def _kv(key: str, val: str, ok: bool | None = None) -> None:
    glyph = ""
    if ok is True:
        glyph = f"{util.C_GREEN}{util.G_OK} {util.C_RESET}"
    elif ok is False:
        glyph = f"{util.C_YELLOW}{util.G_WARN} {util.C_RESET}"
    print(f"  {glyph}{key:<24}{util.C_DIM}{val}{util.C_RESET}")


# ─── Sections ───────────────────────────────────────────────────────


def _report_host() -> list[str]:
    """Returns list of remediation hints."""

    _section("Host")
    rem: list[str] = []
    _kv("OS", util.os_label())
    if util.is_wsl():
        _kv("WSL", "detected")
    _kv("CPU cores", str(util.detect_cpu_count()))

    ram = util.detect_ram_gb()
    if ram is not None:
        _kv(
            "RAM",
            f"{ram:.1f} GiB",
            ok=ram >= constants.RECOMMENDED_RAM_GB,
        )
        if ram < constants.MIN_RAM_GB:
            rem.append(
                f"Upgrade RAM (have {ram:.1f} GiB, "
                f"minimum {constants.MIN_RAM_GB:.0f})."
            )

    free = util.detect_disk_free_gb(constants.REPO_ROOT)
    _kv(
        "Disk free",
        f"{free:.1f} GiB on repo partition",
        ok=free >= constants.RECOMMENDED_DISK_FREE_GB,
    )
    if free < constants.MIN_DISK_FREE_GB:
        rem.append(
            f"Free disk space (have {free:.1f} GiB, "
            f"minimum {constants.MIN_DISK_FREE_GB:.0f})."
        )

    gpu = util.gpu_info()
    if gpu is None:
        _kv("GPU", "none / nvidia-smi missing")
    else:
        vram = gpu.get("vram_gb")
        vstr = f"{vram:.1f} GiB" if isinstance(vram, (int, float)) else "?"
        _kv("GPU", f"{gpu['name']} VRAM={vstr} driver={gpu['driver']}")
    return rem


def _report_repo() -> list[str]:
    _section("Repository")
    rem: list[str] = []
    root = constants.REPO_ROOT
    base = root / constants.COMPOSE_BASE
    overlay_win = root / constants.COMPOSE_WINDOWS_OVERLAY

    _kv("Repo root", str(root))
    _kv("compose base", str(base.name), ok=base.is_file())
    if util.detect_os() == "windows":
        _kv(
            "compose overlay (win)",
            str(overlay_win.name),
            ok=overlay_win.is_file(),
        )

    env_path = root / ".env"
    env_example = root / ".env.example"
    _kv(".env", "present" if env_path.exists() else "missing — will be seeded",
        ok=env_path.exists())
    _kv(".env.example", "present" if env_example.exists() else "missing",
        ok=env_example.exists())

    if not base.is_file():
        rem.append("docker-compose.yml is missing — re-clone the repo.")
    return rem


def _report_compose() -> tuple[compose.ComposeEngine | None, list[str]]:
    _section("Docker / Compose")
    rem: list[str] = []
    if util.which("docker") is None:
        _kv("docker", "not in PATH", ok=False)
        rem.append("Install Docker Desktop or Docker Engine.")
        return None, rem

    info_res = util.run(["docker", "info"], timeout=10)
    _kv("docker daemon", "responsive" if info_res.ok else "not responding",
        ok=info_res.ok)
    if not info_res.ok:
        rem.append("Start Docker Desktop (Win/macOS) or `systemctl start docker` (Linux).")
        return None, rem

    engine = compose.detect_engine()
    if engine is None:
        _kv("compose", "not detected", ok=False)
        rem.append("Install docker compose v2 (`docker compose` subcommand).")
        return None, rem

    _kv("compose binary", engine.label)
    for f in engine.compose_files:
        _kv("compose file", f.name, ok=True)
    return engine, rem


def _report_services(engine: compose.ComposeEngine | None) -> list[str]:
    _section("Service health")
    rem: list[str] = []
    rows: list[list[str]] = []
    headers = ["service", "tier", "state", "probe"]

    if engine is None:
        print(f"  {util.C_DIM}(compose unavailable — skipping){util.C_RESET}")
        return rem

    state_by_name = {}
    for row in compose.ps_json(engine):
        name = row.get("Service") or row.get("service") or ""
        st = (row.get("State") or row.get("state") or "?").lower()
        state_by_name[name] = st

    for svc in constants.SERVICES:
        st = state_by_name.get(svc.name, "missing")
        probe = "—"
        if st == "running":
            r = health.probe_service(svc, engine=engine, timeout_s=2.0)
            probe = (f"{util.C_GREEN}OK{util.C_RESET} {r.detail}"
                     if r.ok
                     else f"{util.C_YELLOW}{r.detail}{util.C_RESET}")
        rows.append([
            svc.name,
            svc.tier,
            (f"{util.C_GREEN}{st}{util.C_RESET}" if st == "running"
             else f"{util.C_YELLOW}{st}{util.C_RESET}" if st != "missing"
             else f"{util.C_RED}{st}{util.C_RESET}"),
            probe,
        ])
    util.table(rows, headers)

    # Missing core services are blockers.
    for svc in constants.SERVICES:
        if svc.tier == "core" and state_by_name.get(svc.name, "missing") != "running":
            rem.append(
                f"Core service `{svc.name}` is not running.  "
                f"Try `python -m tools.setup start`."
            )
            break

    return rem


def _report_judge() -> list[str]:
    """Judge container + GGUF inventory."""

    _section("Judge (Sprint 0 baseline)")
    rem: list[str] = []
    profiles = models_mod.load_judge_profiles()
    if not profiles:
        _kv("judge_profiles.json", "missing or invalid", ok=False)
        rem.append("Restore tools/judge/judge_profiles.json.")
        return rem
    _kv("default profile", profiles.get("default", "?"))
    for name, prof in profiles.get("profiles", {}).items():
        gguf = prof.get("gguf_filename", "?")
        present = models_mod._judge_gguf_present(gguf)
        _kv(
            f"  {name}",
            f"{gguf} {'in volume' if present else 'NOT in volume'}",
            ok=present,
        )
    if profiles.get("profiles") and not any(
        models_mod._judge_gguf_present(p.get("gguf_filename", ""))
        for p in profiles["profiles"].values()
    ):
        rem.append(
            "No judge GGUFs downloaded.  Run "
            "`python -m tools.setup install --profile baseline` "
            "to pre-pull Mistral + Phi-4."
        )
    return rem


def _report_models() -> list[str]:
    _section("Models (Ollama)")
    rem: list[str] = []
    tags = models_mod.list_ollama_tags()
    if not tags:
        _kv("ollama tags", "none / container not running")
        return rem
    for t in tags:
        _kv("  tag", t, ok=True)
    return rem


# ─── Public entry ───────────────────────────────────────────────────


def run_doctor(*, json_out: bool = False) -> int:
    if json_out:
        return _run_doctor_json()

    util.banner(
        "AMOR doctor",
        f"Cycle E v18  •  {util.os_label()}  •  {constants.REPO_ROOT}",
    )

    rem: list[str] = []
    rem += _report_host()
    rem += _report_repo()
    engine, e_rem = _report_compose()
    rem += e_rem
    rem += _report_services(engine)
    rem += _report_judge()
    rem += _report_models()

    print()
    if not rem:
        util.good("All checks passed — your AMOR install looks healthy.")
        return 0

    _section("Suggested fixes")
    # Deduplicate while preserving order.
    seen: set[str] = set()
    for line in rem:
        if line not in seen:
            seen.add(line)
            print(f"  • {line}")
    print()
    util.warn(f"{len(seen)} issue(s) detected — see above.")
    # Soft warnings only — return 1 (non-fatal) so the caller can
    # decide whether to gate.  Fatal preflight failures during install
    # already exit 2.
    return 1


def _run_doctor_json() -> int:
    """JSON variant — machine-readable, no color."""

    payload: dict = {
        "os": util.os_label(),
        "is_wsl": util.is_wsl(),
        "cpu_count": util.detect_cpu_count(),
        "ram_gb": util.detect_ram_gb(),
        "disk_free_gb": util.detect_disk_free_gb(constants.REPO_ROOT),
        "gpu": util.gpu_info(),
        "compose": None,
        "services": [],
        "judge_profiles": list(models_mod.load_judge_profiles().get("profiles", {})),
        "ollama_tags": [],
    }
    engine = compose.detect_engine()
    if engine is not None:
        payload["compose"] = {
            "binary": engine.label,
            "files": [str(f) for f in engine.compose_files],
        }
        rep = health.probe_all(constants.SERVICES, engine=engine)
        payload["services"] = [
            {
                "name": svc.name,
                "tier": svc.tier,
                "ok": r.ok,
                "detail": r.detail,
            }
            for svc, r in zip(constants.SERVICES, rep.results)
        ]
        payload["ollama_tags"] = models_mod.list_ollama_tags()
    print(json.dumps(payload, indent=2, default=str))
    return 0
