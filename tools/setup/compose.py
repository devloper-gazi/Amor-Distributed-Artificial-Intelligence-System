"""Cross-platform docker-compose wrapper.

Auto-detects:
  * compose v2 (`docker compose ...`) preferred,
  * compose v1 (`docker-compose ...`) fallback,
  * Windows overlay (`-f docker-compose.windows.yml`) on win32.

Idempotent helpers: `ps`, `up`, `down`, `pull`, `build`, `logs`, `exec`.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from tools.setup import constants, util


@dataclass
class ComposeEngine:
    """Resolved docker-compose entry point + applicable -f flags."""

    bin: Sequence[str]                    # e.g. ["docker", "compose"]
    compose_files: tuple[Path, ...]       # absolute paths, in order
    project: str | None = None            # COMPOSE_PROJECT_NAME override

    @property
    def label(self) -> str:
        return " ".join(self.bin)

    def file_flags(self) -> list[str]:
        flags: list[str] = []
        for f in self.compose_files:
            flags.extend(["-f", str(f)])
        if self.project:
            flags.extend(["-p", self.project])
        return flags

    def cmd(self, *args: str) -> list[str]:
        """Build a full command list ready for subprocess."""

        return list(self.bin) + self.file_flags() + list(args)


# ─── Detection ──────────────────────────────────────────────────────


def detect_engine(
    *,
    repo_root: Path | None = None,
    include_windows_overlay: bool | None = None,
) -> ComposeEngine | None:
    """Resolve compose binary + applicable -f flags, or None if absent.

    `include_windows_overlay` defaults to True on win32 if the overlay
    file exists.  Pass False to force-disable (e.g. for tests).
    """

    if repo_root is None:
        repo_root = constants.REPO_ROOT

    bin_candidates: list[Sequence[str]] = []
    # Prefer v2 (`docker compose`); fall back to v1 (`docker-compose`).
    if util.which("docker") is not None:
        # Validate `docker compose` actually works (not just present).
        probe = util.run(["docker", "compose", "version"], timeout=5)
        if probe.ok:
            bin_candidates.append(["docker", "compose"])
    if util.which("docker-compose") is not None:
        probe = util.run(["docker-compose", "version"], timeout=5)
        if probe.ok:
            bin_candidates.append(["docker-compose"])
    if not bin_candidates:
        return None

    bin_ = bin_candidates[0]

    # Compose files.
    files: list[Path] = []
    base = repo_root / constants.COMPOSE_BASE
    if base.is_file():
        files.append(base)

    if include_windows_overlay is None:
        include_windows_overlay = util.detect_os() == "windows"
    overlay = repo_root / constants.COMPOSE_WINDOWS_OVERLAY
    if include_windows_overlay and overlay.is_file():
        files.append(overlay)

    return ComposeEngine(bin=bin_, compose_files=tuple(files))


# ─── High-level helpers ─────────────────────────────────────────────


def ps_json(engine: ComposeEngine) -> list[dict]:
    """Return parsed `compose ps --format json` output (one row per svc).

    Compose v2 emits one JSON object per line; v1 emits a JSON array.
    Handle both shapes.
    """

    res = util.run(engine.cmd("ps", "--format", "json"), timeout=30)
    if not res.ok or not res.stdout.strip():
        return []

    out = res.stdout.strip()
    # Compose v2 ≥ 2.21 emits NDJSON; earlier emits a JSON array.
    if out.startswith("["):
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return []
    rows: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def service_state(engine: ComposeEngine, service: str) -> str:
    """Coarse state: 'running' / 'exited' / 'created' / 'missing' / ..."""

    for row in ps_json(engine):
        # Compose v2 keys: Service, State.  v1 keys differ but contain Name.
        svc = row.get("Service") or row.get("service") or ""
        if svc == service:
            return (row.get("State") or row.get("state") or "unknown").lower()
    return "missing"


def is_running(engine: ComposeEngine, service: str) -> bool:
    return service_state(engine, service) == "running"


def up(
    engine: ComposeEngine,
    services: Sequence[str] | None = None,
    *,
    build: bool = False,
    stream: bool = True,
) -> util.CmdResult:
    """`compose up -d [services...]` with optional --build."""

    args = ["up", "-d"]
    if build:
        args.append("--build")
    if services:
        args.extend(services)
    return util.run(engine.cmd(*args), stream=stream, timeout=1800)


def down(
    engine: ComposeEngine,
    *,
    volumes: bool = False,
    stream: bool = True,
) -> util.CmdResult:
    """`compose down`.  volumes=True nukes named volumes (DESTRUCTIVE)."""

    args = ["down"]
    if volumes:
        args.append("-v")
    return util.run(engine.cmd(*args), stream=stream, timeout=300)


def restart(
    engine: ComposeEngine,
    services: Sequence[str] | None = None,
    *,
    stream: bool = True,
) -> util.CmdResult:
    args = ["restart"]
    if services:
        args.extend(services)
    return util.run(engine.cmd(*args), stream=stream, timeout=300)


def pull(
    engine: ComposeEngine,
    services: Sequence[str] | None = None,
    *,
    stream: bool = True,
) -> util.CmdResult:
    args = ["pull"]
    if services:
        args.extend(services)
    return util.run(engine.cmd(*args), stream=stream, timeout=1800)


def build(
    engine: ComposeEngine,
    services: Sequence[str] | None = None,
    *,
    stream: bool = True,
) -> util.CmdResult:
    args = ["build"]
    if services:
        args.extend(services)
    return util.run(engine.cmd(*args), stream=stream, timeout=1800)


def logs(
    engine: ComposeEngine,
    services: Sequence[str] | None = None,
    *,
    tail: int = 100,
    follow: bool = False,
) -> util.CmdResult:
    args = ["logs", f"--tail={tail}"]
    if follow:
        args.append("-f")
    if services:
        args.extend(services)
    return util.run(engine.cmd(*args), stream=follow, timeout=None if follow else 60)


def exec_(
    engine: ComposeEngine,
    service: str,
    cmd: Sequence[str],
    *,
    tty: bool = False,
    timeout: float | None = 120,
) -> util.CmdResult:
    args = ["exec", "-T" if not tty else "-it", service, *cmd]
    return util.run(engine.cmd(*args), timeout=timeout)


# ─── Static analysis helpers (no docker call needed) ────────────────


def parse_services(compose_file: Path) -> list[str]:
    """Extract top-level service names from a YAML file.

    Stdlib-only — minimal indent-aware parser, NOT a full YAML reader.
    Good enough for the compose files in this repo (no anchors, no
    flow style).  We use this for offline tests of constants.py.
    """

    services: list[str] = []
    in_services = False
    base_indent: int | None = None
    for raw in compose_file.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0:
            in_services = stripped.startswith("services:")
            base_indent = None
            continue
        if not in_services:
            continue
        if base_indent is None:
            base_indent = indent
        if indent != base_indent:
            continue
        # `name:` at base_indent
        if stripped.endswith(":") and not stripped.startswith("-"):
            services.append(stripped[:-1])
    return services
