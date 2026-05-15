"""
Preflight checks — every assertion needed BEFORE `compose up`.

Each check returns a `CheckResult`.  An overall `PreflightReport`
aggregates them.  `report.fatal` is True if any blocker fails;
`report.warnings` non-empty if any soft check failed.

The doctor reuses this module for read-only diagnostics.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from tools.setup import compose, constants, util


@dataclass
class CheckResult:
    """One preflight assertion."""

    name: str
    ok: bool
    blocker: bool = False           # if True, install must stop
    message: str = ""
    remediation: str = ""

    @property
    def status_glyph(self) -> str:
        if self.ok:
            return f"{util.C_GREEN}{util.G_OK}{util.C_RESET}"
        if self.blocker:
            return f"{util.C_RED}{util.G_FAIL}{util.C_RESET}"
        return f"{util.C_YELLOW}{util.G_WARN}{util.C_RESET}"


@dataclass
class PreflightReport:
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, c: CheckResult) -> None:
        self.checks.append(c)

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok]

    @property
    def blockers(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok and c.blocker]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok and not c.blocker]

    @property
    def fatal(self) -> bool:
        return bool(self.blockers)

    @property
    def all_ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def render(self) -> None:
        for c in self.checks:
            line = f"  {c.status_glyph} {c.name}"
            if c.message:
                line += f" {util.C_DIM}— {c.message}{util.C_RESET}"
            print(line)
            if not c.ok and c.remediation:
                print(f"     {util.C_GRAY}→ {c.remediation}{util.C_RESET}")


# ─── Individual checks ──────────────────────────────────────────────


def check_python_version() -> CheckResult:
    cur = sys.version_info[:2]
    ok = cur >= constants.MIN_PYTHON
    return CheckResult(
        name="Python version",
        ok=ok,
        blocker=not ok,
        message=f"have {cur[0]}.{cur[1]}, need ≥ "
                f"{constants.MIN_PYTHON[0]}.{constants.MIN_PYTHON[1]}",
        remediation="Install Python 3.9 or newer from https://python.org",
    )


def check_os_supported() -> CheckResult:
    osname = util.detect_os()
    ok = osname in {"windows", "macos", "linux"}
    return CheckResult(
        name="Operating system",
        ok=ok,
        blocker=not ok,
        message=util.os_label(),
        remediation=("AMOR supports Windows / macOS / Linux. "
                     "Other platforms are unsupported.")
        if not ok else "",
    )


def check_docker_present() -> CheckResult:
    has = util.which("docker") is not None
    return CheckResult(
        name="Docker CLI",
        ok=has,
        blocker=not has,
        message="found in PATH" if has else "not found",
        remediation=(
            "Install Docker Desktop: https://www.docker.com/products/docker-desktop"
            if util.detect_os() in {"windows", "macos"}
            else "Install Docker Engine: https://docs.docker.com/engine/install/"
        ),
    )


def check_docker_daemon() -> CheckResult:
    # `docker info` exits non-zero if the daemon isn't running.
    res = util.run(["docker", "info"], timeout=10)
    ok = res.ok
    msg = "responsive" if ok else "not running"
    return CheckResult(
        name="Docker daemon",
        ok=ok,
        blocker=not ok,
        message=msg,
        remediation=(
            "Start Docker Desktop (Windows/macOS) or "
            "`sudo systemctl start docker` (Linux)."
        ),
    )


def _parse_docker_version(raw: str) -> tuple[int, int, int] | None:
    """Coerce a Docker version string like ``24.0.7`` or ``25.0.3+rc1``
    into a ``(major, minor, patch)`` tuple.  Returns ``None`` if the
    string is unparseable — caller treats unparseable as "skip the
    check" rather than as a hard failure.
    """

    if not raw:
        return None
    head = raw.strip().split()[0]            # drop any trailing space-separated tokens
    head = head.split("+", 1)[0]             # drop pre-release suffix
    head = head.split("-", 1)[0]             # drop -beta / -rc / -dev tags
    parts = head.split(".")
    if len(parts) < 2:
        return None
    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2]) if len(parts) >= 3 else 0
    except ValueError:
        return None
    return (major, minor, patch)


def check_docker_version() -> CheckResult:
    """v18.1 Step 1 (Cycle G) — warn (do NOT block) when the Docker
    server is below the v24 / Docker Desktop 4.30 floor that ships
    runc ≥ 1.2.x and addresses CVE-2025-31133 / 52565 / 52881.

    Soft-fail by design: older Docker still runs AMOR; we just want
    the operator to see the security pointer.  If the version probe
    itself fails (network down, daemon flaky), this check passes
    silently — `check_docker_daemon()` is the daemon-presence
    blocker, not us.
    """

    res = util.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        timeout=10,
    )
    if not res.ok:
        # Probe failed — daemon already covered by check_docker_daemon().
        return CheckResult(
            name="Docker version",
            ok=True,
            blocker=False,
            message="version probe skipped (daemon check covers this)",
        )

    parsed = _parse_docker_version(res.stdout)
    if parsed is None:
        return CheckResult(
            name="Docker version",
            ok=True,
            blocker=False,
            message=f"unparseable version output: {res.stdout.strip()!r}",
        )

    needed = constants.MIN_DOCKER_SERVER_VERSION
    ok = parsed >= needed
    have_str = ".".join(str(p) for p in parsed)
    need_str = ".".join(str(p) for p in needed)
    return CheckResult(
        name="Docker version",
        ok=ok,
        blocker=False,     # WARN, never block — older Docker still runs AMOR
        message=(
            f"server {have_str} (≥ {need_str} recommended)"
            if ok
            else f"server {have_str} < {need_str} recommended"
        ),
        remediation=(
            f"Upgrade to Docker Desktop ≥ "
            f"{constants.MIN_DOCKER_DESKTOP_VERSION_LABEL} (CVE-2025-31133 / "
            "runc ≥ 1.2.x).  Linux: `sudo apt install docker-ce` / pull from "
            "https://docs.docker.com/engine/install/."
        )
        if not ok
        else "",
    )


def check_compose_present() -> CheckResult:
    engine = compose.detect_engine(include_windows_overlay=False)
    if engine is None:
        return CheckResult(
            name="Docker Compose",
            ok=False,
            blocker=True,
            message="neither `docker compose` nor `docker-compose` works",
            remediation=(
                "Docker Desktop ships compose v2.  On Linux, install "
                "the `docker-compose-plugin` package."
            ),
        )
    return CheckResult(
        name="Docker Compose",
        ok=True,
        message=f"using `{engine.label}`",
    )


def check_disk_free(min_gb: float = constants.MIN_DISK_FREE_GB) -> CheckResult:
    free_gb = util.detect_disk_free_gb(constants.REPO_ROOT)
    ok_blocker = free_gb >= min_gb
    ok_soft = free_gb >= constants.RECOMMENDED_DISK_FREE_GB
    return CheckResult(
        name="Disk space",
        ok=ok_soft,
        blocker=not ok_blocker,
        message=(
            f"{free_gb:.1f} GiB free on repo partition "
            f"(min {min_gb:.0f}, recommended {constants.RECOMMENDED_DISK_FREE_GB:.0f})"
        ),
        remediation="Free up disk or move the repo to a larger partition.",
    )


def check_ram() -> CheckResult:
    ram_gb = util.detect_ram_gb()
    if ram_gb is None:
        return CheckResult(
            name="System RAM",
            ok=True,
            message="couldn't detect (skipped)",
        )
    ok_blocker = ram_gb >= constants.MIN_RAM_GB
    ok_soft = ram_gb >= constants.RECOMMENDED_RAM_GB
    return CheckResult(
        name="System RAM",
        ok=ok_soft,
        blocker=not ok_blocker,
        message=(
            f"{ram_gb:.1f} GiB "
            f"(min {constants.MIN_RAM_GB:.0f}, "
            f"recommended {constants.RECOMMENDED_RAM_GB:.0f})"
        ),
        remediation="Close memory-heavy apps or upgrade host RAM.",
    )


def check_gpu() -> CheckResult:
    info = util.gpu_info()
    if info is None:
        return CheckResult(
            name="GPU (optional)",
            ok=True,
            message="no NVIDIA GPU detected — CPU paths only",
        )
    vram = info.get("vram_gb")
    vram_str = f"{vram:.1f} GiB" if isinstance(vram, (int, float)) else "?"
    has_enough = (vram or 0) >= constants.RECOMMENDED_VRAM_GB
    return CheckResult(
        name="GPU (optional)",
        ok=True,
        message=f"{info['name']} VRAM={vram_str} driver={info['driver']}"
                + ("" if has_enough
                   else f"  (recommended ≥ {constants.RECOMMENDED_VRAM_GB:.0f} GiB)"),
    )


def check_ports(ports: tuple[int, ...] = constants.ALL_HOST_PORTS) -> CheckResult:
    """Warn if any host port AMOR needs is already in use."""

    busy = [p for p in ports if util.port_in_use(p)]
    if not busy:
        return CheckResult(
            name="Host ports",
            ok=True,
            message=f"{len(ports)} ports free",
        )
    return CheckResult(
        name="Host ports",
        ok=False,
        blocker=False,
        message=f"in use: {', '.join(str(p) for p in busy)}",
        remediation=(
            "Stop the process bound to those ports, or AMOR services "
            "bound to the same ports will fail to start. "
            "(`netstat -ano | findstr <port>` on Windows; "
            "`lsof -iTCP:<port>` on macOS/Linux.)"
        ),
    )


def check_network() -> CheckResult:
    """Probe registries needed to pull images / models."""

    reached: list[str] = []
    failed: list[str] = []
    for host, label in constants.EXTERNAL_HOSTS:
        ok = util.tcp_probe(host, 443, timeout=2.0)
        if ok:
            reached.append(label)
        else:
            failed.append(label)
    if not failed:
        return CheckResult(
            name="Network reachability",
            ok=True,
            message=f"{len(reached)}/{len(reached) + len(failed)} registries reachable",
        )
    return CheckResult(
        name="Network reachability",
        ok=False,
        blocker=False,
        message=f"unreachable: {', '.join(failed)}",
        remediation=(
            "Check firewall / proxy / VPN.  Image pulls and model "
            "downloads will fail until these are reachable."
        ),
    )


def check_compose_files(
    *, repo_root: Path | None = None
) -> CheckResult:
    """The base compose file must exist."""

    if repo_root is None:
        repo_root = constants.REPO_ROOT
    base = repo_root / constants.COMPOSE_BASE
    if not base.is_file():
        return CheckResult(
            name="docker-compose.yml",
            ok=False,
            blocker=True,
            message=f"missing at {base}",
            remediation="Re-clone the repo or restore docker-compose.yml.",
        )
    return CheckResult(
        name="docker-compose.yml",
        ok=True,
        message=f"{base.stat().st_size // 1024} KiB",
    )


# ─── Orchestration ──────────────────────────────────────────────────


_STANDARD_CHECKS: tuple[Callable[[], CheckResult], ...] = (
    check_os_supported,
    check_python_version,
    check_docker_present,
    check_docker_daemon,
    check_docker_version,         # v18.1 Step 1 — CVE-2025-31133 soft warn
    check_compose_present,
    check_compose_files,
    check_disk_free,
    check_ram,
    check_gpu,
    check_ports,
    check_network,
)


def run_preflight() -> PreflightReport:
    report = PreflightReport()
    for fn in _STANDARD_CHECKS:
        try:
            report.add(fn())
        except Exception as exc:  # pragma: no cover (defensive)
            report.add(
                CheckResult(
                    name=fn.__name__,
                    ok=False,
                    blocker=False,
                    message=f"check raised: {exc!r}",
                )
            )
    return report
