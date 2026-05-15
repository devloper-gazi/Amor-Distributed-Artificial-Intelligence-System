"""
Cycle E v18 — cross-platform helpers for tools/setup.

Stdlib-only.  Works on Windows / macOS / Linux without pip-installing
anything.  Anything that needs psutil / colorama / docker-py belongs
elsewhere; this module is the bootstrap floor.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

# ─── Console: color / styling ───────────────────────────────────────

_WIN_VT_OK = False
if sys.platform == "win32":  # pragma: no cover (Windows-only branch)
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7):
            _WIN_VT_OK = True
    except Exception:
        _WIN_VT_OK = False

_NO_COLOR_ENV = os.environ.get("NO_COLOR", "").strip() != ""
_FORCE_COLOR_ENV = os.environ.get("AMOR_FORCE_COLOR", "").strip() != ""

_COLOR_ENABLED = (
    _FORCE_COLOR_ENV
    or (
        not _NO_COLOR_ENV
        and sys.stdout.isatty()
        and (sys.platform != "win32" or _WIN_VT_OK)
    )
)


def _stdout_speaks_unicode() -> bool:
    """True if the current stdout encoding can render our preferred glyphs."""

    enc = (getattr(sys.stdout, "encoding", None) or "ascii").lower()
    if "utf" in enc:
        return True
    try:
        # Probe round-trip; encoders raise UnicodeEncodeError on failure.
        "✓✗▶⠋".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_UNICODE_OK = _stdout_speaks_unicode()

# Glyphs — Unicode-preferred, ASCII fallback when the terminal can't render.
G_OK = "✓" if _UNICODE_OK else "+"
G_FAIL = "✗" if _UNICODE_OK else "x"
G_WARN = "!"
G_INFO = "i"
G_ARROW = "▶" if _UNICODE_OK else ">"
G_BULLET = "•" if _UNICODE_OK else "*"
G_DOT = "·" if _UNICODE_OK else "."
G_DOWN = "↓" if _UNICODE_OK else "v"


def _ansi(code: str) -> str:
    return f"\033[{code}m" if _COLOR_ENABLED else ""


C_RESET = _ansi("0")
C_BOLD = _ansi("1")
C_DIM = _ansi("2")
C_RED = _ansi("31")
C_GREEN = _ansi("32")
C_YELLOW = _ansi("33")
C_BLUE = _ansi("34")
C_MAGENTA = _ansi("35")
C_CYAN = _ansi("36")
C_GRAY = _ansi("90")


# ─── Output helpers ─────────────────────────────────────────────────


def info(msg: str) -> None:
    print(f"{C_BLUE}[{G_INFO}]{C_RESET} {msg}")


def good(msg: str) -> None:
    print(f"{C_GREEN}[{G_OK}]{C_RESET} {msg}")


def warn(msg: str) -> None:
    print(f"{C_YELLOW}[{G_WARN}]{C_RESET} {msg}")


def fail(msg: str) -> None:
    print(f"{C_RED}[{G_FAIL}]{C_RESET} {msg}", file=sys.stderr)


def step(msg: str) -> None:
    print(f"\n{C_BOLD}{G_ARROW} {msg}{C_RESET}")


def dim(msg: str) -> None:
    print(f"{C_DIM}{msg}{C_RESET}")


def banner(title: str, sub: str | None = None) -> None:
    bar_char = "═" if _UNICODE_OK else "="
    bar = bar_char * max(40, min(72, len(title) + 4))
    print(f"{C_CYAN}{bar}{C_RESET}")
    print(f"{C_CYAN}  {C_BOLD}{title}{C_RESET}")
    if sub:
        print(f"{C_DIM}  {sub}{C_RESET}")
    print(f"{C_CYAN}{bar}{C_RESET}\n")


def table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> None:
    """Render a 2-D table.  Cheap manual layout; no extra deps."""

    cols = [list(headers)] + [list(r) for r in rows]
    widths = [max(len(str(row[i])) for row in cols) for i in range(len(headers))]

    def render(row: Sequence[str], is_header: bool = False) -> str:
        cells = []
        for cell, w in zip(row, widths):
            cells.append(str(cell).ljust(w))
        line = "  ".join(cells)
        return f"{C_BOLD}{line}{C_RESET}" if is_header else line

    print(render(headers, is_header=True))
    print(f"{C_DIM}{'-' * (sum(widths) + 2 * (len(widths) - 1))}{C_RESET}")
    for row in rows:
        print(render(row))


# ─── OS detection ───────────────────────────────────────────────────


def detect_os() -> str:
    """Return one of: 'windows', 'macos', 'linux', 'other'."""

    p = sys.platform
    if p == "win32":
        return "windows"
    if p == "darwin":
        return "macos"
    if p.startswith("linux"):
        return "linux"
    return "other"


def os_label() -> str:
    """Human-readable OS string for logs / doctor output."""

    base = detect_os()
    if base == "windows":
        return f"Windows {platform.release()} ({platform.version()})"
    if base == "macos":
        return f"macOS {platform.mac_ver()[0]}"
    if base == "linux":
        # /etc/os-release is the canonical Linux distro descriptor.
        try:
            with open("/etc/os-release", encoding="utf-8") as f:
                kv = {}
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        kv[k] = v.strip().strip('"')
            return kv.get("PRETTY_NAME", f"Linux {platform.release()}")
        except OSError:
            return f"Linux {platform.release()}"
    return f"{sys.platform} {platform.release()}"


def is_wsl() -> bool:
    """True if running inside Windows Subsystem for Linux."""

    if detect_os() != "linux":
        return False
    try:
        with open("/proc/version", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


# ─── Subprocess wrapper ─────────────────────────────────────────────


class CmdResult:
    """Lightweight result wrapper so callers don't need CompletedProcess."""

    __slots__ = ("ok", "code", "stdout", "stderr")

    def __init__(self, code: int, stdout: str, stderr: str) -> None:
        self.code = code
        self.ok = code == 0
        self.stdout = stdout
        self.stderr = stderr

    def __repr__(self) -> str:  # pragma: no cover (debug aid)
        return f"CmdResult(code={self.code}, ok={self.ok})"


def run(
    cmd: Sequence[str] | str,
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    stream: bool = False,
    check: bool = False,
) -> CmdResult:
    """Run a subprocess with safe defaults.

    * `cmd` can be a list (preferred — no shell parsing) or a string
      (passed via shell — use sparingly).
    * `stream=True` echoes stdout/stderr to the parent terminal as
      they arrive (good for long compose pulls).  When False, output
      is captured and returned in `CmdResult`.
    """

    use_shell = isinstance(cmd, str)
    full_env = None
    if env is not None:
        full_env = os.environ.copy()
        full_env.update(env)

    try:
        if stream:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=full_env,
                shell=use_shell,
                stdout=None,
                stderr=None,
                text=True,
            )
            code = proc.wait(timeout=timeout)
            return CmdResult(code, "", "")

        # Force UTF-8 decoding so Windows cp1252 doesn't mangle
        # docker / docker-compose output (e.g. ellipsis → "â€¦").
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=full_env,
            shell=use_shell,
            timeout=timeout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        result = CmdResult(
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
        )
    except FileNotFoundError as exc:
        result = CmdResult(127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        result = CmdResult(
            124,
            exc.stdout.decode("utf-8", errors="replace") if exc.stdout else "",
            f"timeout after {timeout}s",
        )

    if check and not result.ok:
        raise RuntimeError(
            f"command failed: {cmd} (exit {result.code})\nstderr: {result.stderr}"
        )
    return result


def which(name: str) -> str | None:
    """`shutil.which` wrapper — kept for readability at call sites."""

    return shutil.which(name)


# ─── Memory / disk / port helpers ───────────────────────────────────


def _ram_linux() -> float | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb / 1024 / 1024
    except OSError:
        return None
    return None


def _ram_macos() -> float | None:
    res = run(["sysctl", "-n", "hw.memsize"])
    if not res.ok:
        return None
    try:
        return int(res.stdout.strip()) / (1024 ** 3)
    except ValueError:
        return None


def _ram_windows() -> float | None:
    # PowerShell + CIM avoids the slow WMIC startup.
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
    ]
    res = run(cmd, timeout=10)
    if not res.ok:
        return None
    try:
        return int(res.stdout.strip()) / (1024 ** 3)
    except ValueError:
        return None


def detect_ram_gb() -> float | None:
    """Return host RAM in GiB, or None if we can't tell."""

    osname = detect_os()
    if osname == "linux":
        return _ram_linux()
    if osname == "macos":
        return _ram_macos()
    if osname == "windows":
        return _ram_windows()
    return None


def detect_disk_free_gb(path: Path | str = ".") -> float:
    """Free disk space in GiB on the partition containing `path`."""

    total, used, free = shutil.disk_usage(str(path))
    return free / (1024 ** 3)


def detect_cpu_count() -> int:
    return os.cpu_count() or 1


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if `port` is currently bound on `host`."""

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.5)
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def tcp_probe(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if a TCP connect to host:port succeeds."""

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def http_probe(url: str, *, timeout: float = 2.0) -> tuple[bool, int | None]:
    """HTTP GET — returns (ok, status_code).  Stdlib-only (urllib)."""

    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    req = Request(url, headers={"User-Agent": "amor-setup/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 (controlled URL)
            return (200 <= resp.status < 300, resp.status)
    except HTTPError as exc:
        return (False, exc.code)
    except (URLError, TimeoutError, ConnectionError, OSError):
        return (False, None)


def gpu_info() -> dict | None:
    """Return {name, vram_gb, driver} from nvidia-smi, or None if absent."""

    if which("nvidia-smi") is None:
        return None
    res = run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ],
        timeout=5,
    )
    if not res.ok or not res.stdout.strip():
        return None
    first = res.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    if len(parts) < 3:
        return None
    try:
        vram_gb = int(parts[1]) / 1024
    except ValueError:
        vram_gb = None
    return {"name": parts[0], "vram_gb": vram_gb, "driver": parts[2]}


# ─── Spinner ────────────────────────────────────────────────────────


class Spinner:
    """Minimal terminal spinner.  No-op when stdout isn't a TTY."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if _UNICODE_OK else "|/-\\"

    def __init__(self, text: str) -> None:
        self.text = text
        self._active = sys.stdout.isatty()
        self._i = 0
        self._last = 0.0

    def tick(self) -> None:
        if not self._active:
            return
        now = time.monotonic()
        if now - self._last < 0.08:
            return
        self._last = now
        frame = self._FRAMES[self._i % len(self._FRAMES)]
        self._i += 1
        sys.stdout.write(f"\r{C_CYAN}{frame}{C_RESET} {self.text}")
        sys.stdout.flush()

    def done(self, msg: str | None = None, ok: bool = True) -> None:
        if self._active:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()
        text = msg if msg is not None else self.text
        (good if ok else fail)(text)


# ─── Logging ────────────────────────────────────────────────────────


def setup_log_file(name: str, *, root: Path | None = None) -> Path:
    """Create a timestamped log file under data/setup_logs/."""

    if root is None:
        root = Path.cwd() / "data" / "setup_logs"
    root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{name}_{ts}.log"
    path.touch()
    return path


def log_to(path: Path, msg: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(msg.rstrip("\n") + "\n")


# ─── JSON helpers ───────────────────────────────────────────────────


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


# ─── Conversion helpers ─────────────────────────────────────────────


def humanize_bytes(n: int | float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PiB"


def humanize_seconds(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{s / 60:.1f}m"
    return f"{s / 3600:.1f}h"


__all__ = [
    "C_BLUE", "C_BOLD", "C_CYAN", "C_DIM", "C_GREEN", "C_MAGENTA",
    "C_RED", "C_RESET", "C_YELLOW", "C_GRAY",
    "CmdResult",
    "Spinner",
    "banner", "dim", "fail", "good", "info", "step", "table", "warn",
    "detect_cpu_count", "detect_disk_free_gb", "detect_os",
    "detect_ram_gb", "gpu_info", "http_probe", "humanize_bytes",
    "humanize_seconds", "is_wsl", "log_to", "os_label",
    "port_in_use", "run", "setup_log_file", "tcp_probe", "which",
    "write_json",
]
