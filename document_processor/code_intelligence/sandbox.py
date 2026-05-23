"""
ExecutionSandbox — runs arbitrary code snippets in isolated Docker containers.

Security model
--------------
* No network access (``--network none``)
* No new privileges (``--security-opt no-new-privileges``)
* Read-only mount of the source directory
* Hard CPU + memory ceilings
* Killed after ``timeout_seconds`` regardless of state
* Container removed after run (``--rm``); a defensive ``docker rm -f``
  also runs in ``finally`` in case ``--rm`` ever silently fails
* Cannot reach host files, env vars, or the Docker socket
"""

from __future__ import annotations

import asyncio
import logging
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# v18.1.2 (Cycle G) — tmpfs sizing helper
# ─────────────────────────────────────────────────────────────────────────────


def _tmpfs_size_mb() -> int:
    """Resolve the per-run /tmp tmpfs cap in MB.

    Reads ``settings.code_sandbox_tmpfs_size_mb`` (default 768) with
    env override ``AMOR_CODE_SANDBOX_TMPFS_SIZE_MB``.  Hard floor 128
    (below which even minimal Python imports OOM); hard ceiling 4096
    (above which a misconfig would chew real RAM the host doesn't
    have).  Failures fall through to the safe default so a settings
    import hiccup never bricks the sandbox.

    Used by the runtime ``--tmpfs`` arg and by ``security_posture()``
    so the reported flag value matches what's actually running.
    """
    raw_env = (os.environ.get("AMOR_CODE_SANDBOX_TMPFS_SIZE_MB") or "").strip()
    if raw_env:
        try:
            return max(128, min(4096, int(raw_env)))
        except ValueError:
            pass
    try:
        from ..config.settings import settings  # noqa: PLC0415
        return max(128, min(4096, int(getattr(settings, "code_sandbox_tmpfs_size_mb", 768))))
    except Exception:
        return 768


# ─────────────────────────────────────────────────────────────────────────────
# Language → image + run command
# ─────────────────────────────────────────────────────────────────────────────


LANGUAGE_RUNNERS: dict[str, dict[str, Any]] = {
    # Phase 16.5 Commit K — every cmd is RELATIVE to the runner's
    # ``--workdir``.  The runner workdir is either ``/sandbox/work``
    # (bare-metal hosts where the app process and the docker daemon
    # share a filesystem) or ``/sandbox-shared/<run_id>`` (the
    # named-volume path used when the app sits inside a container
    # with the host docker socket bind-mounted).  Either way the
    # cmd doesn't need to know — relative paths just work.
    "python": {
        "image": "python:3.11-slim",
        "cmd": ["python", "main.py"],
        "filename": "main.py",
        "default_timeout_s": 30,
    },
    "javascript": {
        "image": "node:20-slim",
        "cmd": ["node", "main.js"],
        "filename": "main.js",
        "default_timeout_s": 30,
    },
    "typescript": {
        "image": "node:20-slim",
        "cmd": [
            "sh",
            "-c",
            "npx -y -p typescript -p ts-node ts-node --skipProject main.ts 2>&1",
        ],
        "filename": "main.ts",
        # ts-node fetch + first compile is ~10-20s on cold caches.
        "default_timeout_s": 60,
    },
    "bash": {
        "image": "bash:5",
        "cmd": ["bash", "main.sh"],
        "filename": "main.sh",
        "default_timeout_s": 30,
    },
    "go": {
        "image": "golang:1.22-alpine",
        "cmd": ["sh", "-c", "go run main.go 2>&1"],
        "filename": "main.go",
        "default_timeout_s": 60,
    },
    "rust": {
        "image": "rust:1.78-slim",
        "cmd": [
            "sh",
            "-c",
            "rustc main.rs -o /tmp/out && /tmp/out 2>&1",
        ],
        "filename": "main.rs",
        "default_timeout_s": 90,
    },
    "cpp": {
        "image": "gcc:13",
        "cmd": [
            "sh",
            "-c",
            "g++ -O2 -std=c++17 main.cpp -o /tmp/out && /tmp/out 2>&1",
        ],
        "filename": "main.cpp",
        "default_timeout_s": 60,
    },
    # Cycle D — first-class C support (was previously routed to "cpp"
    # which forced g++ + std=c++17, breaking pure-C deliverables that
    # use C99 idioms).  ``gcc`` image is the same family so prewarm
    # cost is shared.
    "c": {
        "image": "gcc:13",
        "cmd": [
            "sh",
            "-c",
            "gcc -O2 -std=c11 main.c -o /tmp/out && /tmp/out 2>&1",
        ],
        "filename": "main.c",
        "default_timeout_s": 60,
    },
    "java": {
        "image": "openjdk:21-slim",
        "cmd": [
            "sh",
            "-c",
            "cp Main.java /tmp/ && cd /tmp && "
            "javac Main.java && java Main 2>&1",
        ],
        "filename": "Main.java",
        "default_timeout_s": 60,
    },
    # Cycle D — Kotlin via the JVM toolchain.  Compiled to JAR then
    # run; ~6 s cold compile + ~1 s start.  ``MainKt`` is the standard
    # name kotlinc emits for a top-level ``fun main()``.
    "kotlin": {
        "image": "zenika/kotlin:1.9-jdk17",
        "cmd": [
            "sh",
            "-c",
            "cp main.kt /tmp/ && cd /tmp && "
            "kotlinc main.kt -include-runtime -d main.jar && "
            "java -jar main.jar 2>&1",
        ],
        "filename": "main.kt",
        "default_timeout_s": 90,
    },
    # Cycle D — C# via .NET 8 SDK script-mode.  ``dotnet script`` runs
    # a single .cs file without a project file; cold start ~10-15s.
    "csharp": {
        "image": "mcr.microsoft.com/dotnet/sdk:8.0",
        "cmd": [
            "sh",
            "-c",
            "dotnet script --no-cache main.csx 2>&1",
        ],
        "filename": "main.csx",
        "default_timeout_s": 90,
    },
    "ruby": {
        "image": "ruby:3.3-slim",
        "cmd": ["ruby", "main.rb"],
        "filename": "main.rb",
        "default_timeout_s": 30,
    },
    "php": {
        "image": "php:8.3-cli-alpine",
        "cmd": ["php", "main.php"],
        "filename": "main.php",
        "default_timeout_s": 30,
    },
    "sql": {
        # Cycle D — SQL deliverables run through SQLite (the most
        # portable option).  We accept a script that may contain a
        # mix of CREATE / INSERT / SELECT and stream the resulting
        # rows back as the runner stdout.  Production teams that
        # need PG/MySQL semantics can opt-in by changing the image.
        "image": "alpine:3.20",
        "cmd": [
            "sh",
            "-c",
            "apk add --no-cache --quiet sqlite >/dev/null 2>&1 && "
            "sqlite3 -bail -column -header :memory: < main.sql",
        ],
        "filename": "main.sql",
        "default_timeout_s": 30,
    },
    # Phase 16.5 Commit L — HTML / static-website runner.  Uses
    # Python's stdlib html.parser to validate the markup parses
    # cleanly + reports key structural counts so the engine's
    # debug loop can see whether a snake-game-website actually
    # contains <canvas>, <script> blocks etc.  No browser, no
    # extra deps — works in the same python:3.11-slim image we
    # already pull.
    "html": {
        "image": "python:3.11-slim",
        "cmd": [
            "python",
            "-c",
            (
                "from html.parser import HTMLParser\n"
                "import sys, re\n"
                "html = open('main.html', encoding='utf-8').read()\n"
                "parser = HTMLParser()\n"
                "try:\n"
                "    parser.feed(html)\n"
                "except Exception as exc:\n"
                "    print(f'HTML parse error: {exc}', file=sys.stderr)\n"
                "    sys.exit(1)\n"
                "lines = html.count(chr(10)) + 1\n"
                "scripts = len(re.findall(r'<script[^>]*>', html, re.I))\n"
                "canvas = len(re.findall(r'<canvas[^>]*>', html, re.I))\n"
                "styles = len(re.findall(r'<style[^>]*>', html, re.I))\n"
                "has_doctype = '<!doctype html' in html.lower()\n"
                "print(f'HTML parsed: {len(html)} bytes, {lines} lines')\n"
                "print(f'  doctype={has_doctype} '\n"
                "      f'<script>={scripts} <canvas>={canvas} <style>={styles}')\n"
            ),
        ],
        "filename": "main.html",
        # html.parser is sub-second; tight cap catches infinite-
        # generator HTML (extremely rare but possible with very
        # large generated documents).
        "default_timeout_s": 5,
    },
    "css": {
        "image": "python:3.11-slim",
        "cmd": [
            "python",
            "-c",
            (
                "import re,sys\n"
                "css = open('main.css', encoding='utf-8').read()\n"
                "rules = len(re.findall(r'\\\\{[^}]*\\\\}', css))\n"
                "selectors = len(re.findall(r'^[^{]+\\\\{', css, re.M))\n"
                "print(f'CSS parsed: {len(css)} bytes, '\n"
                "      f'~{rules} rules, ~{selectors} selectors')\n"
            ),
        ],
        "filename": "main.css",
        "default_timeout_s": 5,
    },
}


# Cycle D — per-language test runner config.  When ``execute(...,
# test_mode=True)`` is called, the sandbox writes the IMPLEMENTATION
# (which the engine passed via ``extra_files["main.<ext>"]``) AND
# the TEST FILE (``code`` argument under ``test_filename``), then
# runs ``test_cmd``.  Diller arası farklılıklar tek bir şemaya
# indirgenmiş:
#   - ``test_filename``: where to write the tester agent's output
#   - ``test_cmd``: shell command that runs the test file
#   - ``test_install_prefix``: command run BEFORE ``test_cmd`` to
#     install the test framework (pytest / vitest / etc) — empty
#     when the language ships its test runner with the toolchain
#     (Go / Rust)
#   - ``default_timeout_s``: laxer than implementation runs because
#     pip-install + first compile + tests can stack up
#
# Languages without a test runner (html / css / bash) fall through
# to the "skipped" path in ``execute()`` — the engine doesn't try
# to run tests for those modes.
TEST_RUNNERS: dict[str, dict[str, Any]] = {
    "python": {
        "image": "python:3.11-slim",
        "test_filename": "test_main.py",
        "impl_filename": "main.py",
        # ``set -e`` so the install failure short-circuits and surfaces
        # immediately as a non-zero exit; otherwise the runner exits
        # with whatever pytest's last status was, which we want.
        #
        # Cycle F Sprint 2 — also install hypothesis (Property-based
        # critic) + pytest-cov + coverage (branch-coverage Reflexion
        # signal).  ~2 MB / +5 s on first install; cached thereafter.
        "test_install_prefix": (
            "set -e; "
            "PIP_ROOT_USER_ACTION=ignore "
            "PIP_DISABLE_PIP_VERSION_CHECK=1 "
            "pip install --quiet --no-cache-dir "
            "--disable-pip-version-check "
            "--root-user-action=ignore "
            "--target=/tmp/pip-test-prefix "
            "pytest pytest-mock pytest-cov coverage hypothesis; "
            "export PYTHONPATH=/tmp/pip-test-prefix:.; "
        ),
        # `exec` so pytest's exit code becomes the shell's exit code
        # (no echo / no fallback shadowing).  -rN summary; --tb=short
        # readable for the reviewer; --maxfail=20 so tester typos
        # don't bail on test 1.
        #
        # Cycle F Sprint 2 — `--cov=. --cov-branch --cov-report=json
        # :.coverage.json` emits a JSON report next to the test file.
        # `coverage_reader.py` parses it; missing branches flow back
        # to the coder via the reflexion feedback bundle.  We keep
        # `--cov-fail-under` OFF — coverage is a feedback signal, not
        # a hard gate (would block legitimate easy-task deliverables).
        "test_cmd": (
            "exec python -m pytest -rN --tb=short --no-header --maxfail=20 "
            "--cov=. --cov-branch --cov-report=json:.coverage.json "
            "test_main.py"
        ),
        "default_timeout_s": 120,
    },
    "javascript": {
        "image": "node:20-slim",
        "test_filename": "main.test.mjs",
        "impl_filename": "main.mjs",
        # node:test ships with Node 18+; no install needed for the
        # canonical runner.  Tester is told (per JS ground rules) to
        # use ``import { test } from "node:test"``.
        "test_install_prefix": "set -e; ",
        "test_cmd": "exec node --test main.test.mjs",
        "default_timeout_s": 60,
    },
    "typescript": {
        "image": "node:20-slim",
        "test_filename": "main.test.ts",
        "impl_filename": "main.ts",
        "test_install_prefix": "set -e; ",
        "test_cmd": "exec npx -y -p typescript -p tsx tsx --test main.test.ts",
        "default_timeout_s": 120,
    },
    "go": {
        "image": "golang:1.22-alpine",
        "test_filename": "main_test.go",
        "impl_filename": "main.go",
        "test_install_prefix": "set -e; go mod init sandbox_test 2>/dev/null || true; ",
        "test_cmd": "exec go test -v ./...",
        "default_timeout_s": 90,
    },
    "rust": {
        "image": "rust:1.78-slim",
        # Rust convention puts tests in a #[cfg(test)] mod inside the
        # source file.  We accept either:
        #   (a) a separate test file (test_main.rs) — append it to
        #       main.rs as an inline mod, OR
        #   (b) tests already embedded in main.rs (preferred per
        #       _RUST_GROUND_RULES) — write tester output to a
        #       comment-only file and just compile + test main.rs.
        "test_filename": "tests_appendix.rs",
        "impl_filename": "main.rs",
        "test_install_prefix": (
            "set -e; cat tests_appendix.rs >> main.rs; "
            "rustc --test main.rs -o /tmp/test_bin; "
        ),
        "test_cmd": "exec /tmp/test_bin",
        "default_timeout_s": 120,
    },
    "cpp": {
        "image": "gcc:13",
        "test_filename": "test_main.cpp",
        "impl_filename": "main.cpp",
        # No standard test framework in gcc image; tester is told to
        # use simple ``assert.h`` based test functions called from
        # main.  Falls back to compile-and-run.
        "test_install_prefix": (
            "set -e; "
            "g++ -O0 -g -std=c++17 test_main.cpp -o /tmp/test_bin; "
        ),
        "test_cmd": "exec /tmp/test_bin",
        "default_timeout_s": 90,
    },
    # Cycle D — C tests use assert.h (same pattern as C++).
    "c": {
        "image": "gcc:13",
        "test_filename": "test_main.c",
        "impl_filename": "main.c",
        "test_install_prefix": (
            "set -e; "
            "gcc -O0 -g -std=c11 test_main.c -o /tmp/test_bin; "
        ),
        "test_cmd": "exec /tmp/test_bin",
        "default_timeout_s": 60,
    },
    # Cycle D — Ruby tests via the built-in Test::Unit / Minitest
    # framework that ships with Ruby's stdlib (no install needed).
    "ruby": {
        "image": "ruby:3.3-slim",
        "test_filename": "test_main.rb",
        "impl_filename": "main.rb",
        "test_install_prefix": "set -e; ",
        "test_cmd": "exec ruby test_main.rb",
        "default_timeout_s": 60,
    },
    # Cycle D — PHP tests via the built-in assert() — sandbox
    # doesn't pull PHPUnit (composer install is heavy).  Tester is
    # told to use ``assert(...)`` calls.
    "php": {
        "image": "php:8.3-cli-alpine",
        "test_filename": "test_main.php",
        "impl_filename": "main.php",
        "test_install_prefix": "set -e; ",
        "test_cmd": "exec php test_main.php",
        "default_timeout_s": 60,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None
    duration_ms: int = 0
    language: str = ""
    # v8 — `skipped=True` means the sandbox declined to run (e.g.,
    # Docker CLI missing in this container). Distinguished from
    # `success=False` because the implementation gate should treat
    # skipped runs as neutral, not as a failure that lowers the score.
    skipped: bool = False
    # Cycle F Sprint 2 — pytest-cov JSON report harvested from
    # `.coverage.json` in the workdir BEFORE the workdir is cleaned
    # up.  None if coverage didn't run (e.g. non-Python language,
    # `test_mode=False`, or the install of pytest-cov failed).  The
    # engine reads this via coverage_reader.parse_coverage_json().
    coverage_json: Optional[Dict[str, Any]] = None

    @property
    def success(self) -> bool:
        if self.skipped:
            return True  # neutral — neither failed nor proved successful
        return self.exit_code == 0 and not self.timed_out and not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "language": self.language,
            "success": self.success,
            "skipped": self.skipped,
            # We intentionally DON'T include coverage_json in to_dict
            # — it's structured JSON, not a feedback string.  The
            # engine reads it directly from the dataclass field.
        }

    def to_feedback_str(self) -> str:
        """Compact execution feedback for injecting into next LLM context."""
        if self.success:
            status = "✅ SUCCESS"
        elif self.timed_out:
            status = "⏱ TIMEOUT"
        else:
            status = "❌ FAILED"
        lines = [f"Execution: {status} (exit={self.exit_code}, {self.duration_ms}ms)"]
        if self.error:
            lines.append(f"ERROR: {self.error[:500]}")
        if self.stdout.strip():
            lines.append(f"STDOUT:\n{self.stdout[:2000]}")
        if self.stderr.strip():
            lines.append(f"STDERR:\n{self.stderr[:1000]}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox
# ─────────────────────────────────────────────────────────────────────────────


class ExecutionSandbox:
    """
    Docker-based code execution sandbox.

    Requires the Docker daemon to be reachable from the running app
    container. In docker-compose, that's done by mounting
    `/var/run/docker.sock` into the app service.
    """

    def __init__(
        self,
        default_timeout: int = 30,
        memory_limit: str = "256m",
        cpu_quota: int = 50000,  # 50% of one core
    ):
        self._default_timeout = default_timeout
        self._memory_limit = memory_limit
        self._cpu_quota = cpu_quota
        # v8 — cache the docker_available() result so:
        #  (a) we don't pay the subprocess cost on every test, and
        #  (b) once we know docker is missing, every subsequent
        #      execute() can fail fast and quietly instead of logging a
        #      full traceback per call. Re-probed at most every 5 min.
        self._docker_available_cache: bool | None = None
        self._docker_available_cached_at: float = 0.0
        self._docker_probe_ttl: float = 300.0  # 5 minutes
        self._docker_probe_lock = asyncio.Lock()
        # Phase 16.5 Commit K — workdir root.  When the app runs inside
        # a container with the host docker socket bind-mounted, we
        # need workdirs at a path the HOST docker daemon can see.
        # Resolution order:
        #  1. ``$AMOR_SANDBOX_WORKDIR`` env var (explicit override)
        #  2. ``/sandbox-shared`` if it exists (the docker-compose
        #     bind-mount adds this)
        #  3. system tempdir (works on bare-metal hosts where the
        #     app process runs alongside docker without any
        #     in-container indirection)
        self._workdir_root = self._resolve_workdir_root()

    @staticmethod
    def _resolve_workdir_root() -> str | None:
        env = os.environ.get("AMOR_SANDBOX_WORKDIR", "").strip()
        if env:
            try:
                os.makedirs(env, exist_ok=True)
                return env
            except OSError:
                pass
        if os.path.isdir("/sandbox-shared"):
            return "/sandbox-shared"
        return None  # tempfile.mkdtemp default

    # ── Cycle C Sprint 5 Day 2 — security posture introspection ──────────

    def security_posture(self) -> dict[str, Any]:
        """Snapshot of the active sandbox hardening configuration.

        Backs ``GET /api/code/diagnostics`` (`sandbox.security` block)
        and the "Sandbox: hardened" badge in the frontend.  Pure
        introspection — no side effects, no subprocess calls.

        Returned shape::

            {
              "docker_host": "tcp://amor-docker-proxy:2375" | "",
              "via_proxy": bool,
              "flags_active": {
                "no_new_privileges": True,
                "read_only": True,
                "memory_limit": "256m",
                "cpu_quota": 50000,
                "default_network": "none",   # bridge only when installing
                "tmpfs": "/tmp:size=384m,exec",
                "cap_drop_all": False,        # Day 3
                "pids_limit": None,           # Day 3
                "seccomp_profile": None,      # Day 3
              },
              "score": int,                  # 0-10, higher = harder
              "level": "baseline" | "hardened" | "max",
            }
        """
        docker_host = (
            os.environ.get("DOCKER_HOST")
            or os.environ.get("AMOR_DOCKER_HOST")
            or ""
        ).strip()
        via_proxy = docker_host.startswith("tcp://") and "proxy" in docker_host

        flags = {
            "no_new_privileges": True,
            "read_only": True,
            "memory_limit": self._memory_limit,
            "cpu_quota": self._cpu_quota,
            "default_network": "none",
            "tmpfs": f"/tmp:size={_tmpfs_size_mb()}m,exec",
            # Cycle C Sprint 5 Day 3 — flipped on for every sandbox run.
            "cap_drop_all": True,
            "pids_limit": 128,
            # We rely on Docker's BUILT-IN default seccomp profile,
            # which already blocks ~60 dangerous syscalls (mount,
            # ptrace, modify_ldt, kexec_*, init_module, ...).  No
            # custom JSON shipped — tightening further than the
            # default would be premature without measured user impact.
            "seccomp_profile": "docker-default",
        }

        # Quick scoring: each enabled hardening flag = 1, capped at 10.
        score = 0
        score += 1 if flags["no_new_privileges"] else 0
        score += 1 if flags["read_only"] else 0
        score += 1 if flags["memory_limit"] else 0
        score += 1 if flags["cpu_quota"] else 0
        score += 1 if flags["default_network"] == "none" else 0
        score += 1 if flags["tmpfs"] else 0
        score += 1 if flags["cap_drop_all"] else 0
        score += 1 if flags["pids_limit"] else 0
        score += 1 if flags["seccomp_profile"] else 0
        score += 1 if via_proxy else 0

        if score >= 9:
            level = "max"
        elif score >= 7:
            level = "hardened"
        else:
            level = "baseline"

        return {
            "docker_host": docker_host,
            "via_proxy": via_proxy,
            "flags_active": flags,
            "score": score,
            "level": level,
        }

    # ── Image management ──────────────────────────────────────────────────

    async def docker_available(self, *, force_refresh: bool = False) -> bool:
        """Cheap check: is the Docker CLI / daemon reachable?

        Cached for ``self._docker_probe_ttl`` seconds (default 5 min).
        ``force_refresh=True`` bypasses the cache — used at app startup
        in ``_code_intelligence_warmup`` to seed a fresh value before
        the first real request lands."""
        now = time.monotonic()
        if (not force_refresh
                and self._docker_available_cache is not None
                and (now - self._docker_available_cached_at)
                < self._docker_probe_ttl):
            return self._docker_available_cache

        async with self._docker_probe_lock:
            # Second check inside the lock — another caller may have
            # filled the cache while we waited.
            if (not force_refresh
                    and self._docker_available_cache is not None
                    and (time.monotonic()
                         - self._docker_available_cached_at)
                    < self._docker_probe_ttl):
                return self._docker_available_cache
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker",
                    "version",
                    "--format",
                    "{{.Server.Version}}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                ok = proc.returncode == 0
            except FileNotFoundError:
                # The docker CLI binary isn't even on PATH — common in
                # slim app images where only the daemon socket is
                # mounted. Cache aggressively to avoid log spam.
                logger.warning(
                    "sandbox_docker_cli_missing — sandbox execution "
                    "will be skipped. Install the docker CLI in the "
                    "app image to enable.",
                )
                ok = False
            except Exception as exc:
                logger.warning("sandbox_docker_probe_failed: %s", exc)
                ok = False
            self._docker_available_cache = ok
            self._docker_available_cached_at = time.monotonic()
            return ok

    async def _ensure_image(self, image: str) -> None:
        """Pull the Docker image if it isn't already present locally."""
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "image",
            "inspect",
            image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode == 0:
            return

        logger.info("sandbox_pulling_image image=%s", image)
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "pull",
            image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to pull Docker image {image}: {stderr.decode(errors='replace')}"
            )

    async def image_status(self) -> dict[str, bool]:
        """Map of language → whether its base image is locally cached."""
        out: dict[str, bool] = {}
        for lang, cfg in LANGUAGE_RUNNERS.items():
            image = cfg["image"]
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker",
                    "image",
                    "inspect",
                    image,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                out[lang] = proc.returncode == 0
            except Exception:
                out[lang] = False
        return out

    # ── Execute ───────────────────────────────────────────────────────────

    async def execute(
        self,
        code: str,
        language: str = "python",
        extra_files: dict[str, str] | None = None,
        install_packages: list[str] | None = None,
        timeout: int | None = None,
        stdin_data: str | None = None,
        test_mode: bool = False,
    ) -> ExecutionResult:
        """
        Execute code in an isolated Docker container.

        Parameters
        ----------
        code               : Source code to run.  When ``test_mode`` is
                             ``True``, this is the TEST file; the
                             implementation must be passed via
                             ``extra_files["main.<ext>"]``.
        language           : Language key (see LANGUAGE_RUNNERS).
        extra_files        : Map of {filename: content} for additional
                             files mounted alongside `code`.
        install_packages   : Packages to pip/npm install before running.
                             Currently supported for python / js / ts.
        timeout            : Seconds before the container is killed.
        stdin_data         : Optional input piped to stdin.
        test_mode          : Cycle D — when True, run the language's
                             test runner (pytest / node:test / go test
                             / cargo test / etc) over ``code`` against
                             the implementation passed in
                             ``extra_files``.  See ``TEST_RUNNERS``.
        """
        # v8 — short-circuit when Docker is known-unreachable. Without
        # this, the subprocess call below raises FileNotFoundError and
        # the except block at the bottom fires `logger.exception`,
        # which produces a full traceback every single time the
        # engine asks for a sandbox run (often dozens of times per
        # session). Skipping cleanly returns a "skipped" result the
        # implementation gate can read without flagging a critical.
        if not await self.docker_available():
            return ExecutionResult(
                exit_code=0,
                stdout="",
                stderr=("sandbox skipped — docker CLI not available "
                         "in app container"),
                error="docker_unavailable",
                duration_ms=0,
                language=language,
                skipped=True,
            )

        lang = language.lower().strip()

        # Cycle D — test_mode swaps the runner config to TEST_RUNNERS
        # and rewires filename / cmd / install prefix accordingly.
        if test_mode:
            test_cfg = TEST_RUNNERS.get(lang)
            if not test_cfg:
                return ExecutionResult(
                    exit_code=0,
                    stdout="",
                    stderr=(
                        f"test runner not configured for language={lang!r}; "
                        "test phase skipped."
                    ),
                    error="test_runner_unavailable",
                    duration_ms=0,
                    language=language,
                    skipped=True,
                )
            cfg = {
                "image": test_cfg["image"],
                "filename": test_cfg["test_filename"],
                "cmd": ["sh", "-c",
                        test_cfg["test_install_prefix"] + test_cfg["test_cmd"]],
                "default_timeout_s": test_cfg.get("default_timeout_s", 90),
                "_test_impl_filename": test_cfg["impl_filename"],
            }
        else:
            cfg = LANGUAGE_RUNNERS.get(lang)
            if not cfg:
                return ExecutionResult(
                    exit_code=1,
                    stdout="",
                    stderr="",
                    error=(
                        f"Unsupported language: {language!r}. Supported: {sorted(LANGUAGE_RUNNERS)}"
                    ),
                    language=language,
                )

        # Phase 17 Commit O — per-language timeout map.  Caller's
        # explicit ``timeout=`` always wins; otherwise fall back to
        # ``LANGUAGE_RUNNERS[lang]["default_timeout_s"]`` which is
        # tighter than the 30s instance default for cheap parsers
        # (HTML / CSS) and laxer for compile-heavy languages
        # (Rust / TS).
        if timeout is None:
            timeout = int(
                cfg.get("default_timeout_s") or self._default_timeout,
            )
        else:
            timeout = int(timeout)
        container_name = f"amor-sandbox-{uuid.uuid4().hex[:12]}"
        workdir = tempfile.mkdtemp(
            prefix="amor_sandbox_", dir=self._workdir_root,
        )
        proc: asyncio.subprocess.Process | None = None

        try:
            # Write source
            with open(
                os.path.join(workdir, cfg["filename"]),
                "w",
                encoding="utf-8",
            ) as f:
                f.write(code)

            # Write extras (basename only, no traversal)
            for fname, fcontent in (extra_files or {}).items():
                safe = os.path.basename(fname)
                if not safe:
                    continue
                with open(
                    os.path.join(workdir, safe),
                    "w",
                    encoding="utf-8",
                ) as f:
                    f.write(fcontent)

            # Build the command, optionally prefixing with package
            # installs.  Phase 17 Commit O — pip + npm install into
            # /tmp (which the runner has as a writable tmpfs) and
            # we set ``PYTHONPATH`` / ``NODE_PATH`` so the runtime
            # finds the freshly-installed packages.  This keeps the
            # base image's site-packages tree intact (so pip itself
            # remains importable on a per-run basis) and avoids
            # mounting tmpfs over the package directories.
            cmd_parts = list(cfg["cmd"])
            install_prefix = ""
            if install_packages:
                if lang == "python":
                    pkgs = " ".join(f'"{p}"' for p in install_packages)
                    # Cycle B Commit V — silence the noisy
                    # "WARNING: Running pip as the 'root' user can
                    # result in broken permissions..." + the
                    # "[notice] A new release of pip is available"
                    # upgrade reminder that pip prints to stderr inside
                    # the sandbox.  The container is ephemeral and
                    # root is the only available uid; both lines are
                    # cosmetic but they stamp every build's stderr
                    # panel and confuse operators reading diagnostics.
                    # Disable via env vars (covers ``pip install``
                    # invocation) AND the ``--root-user-action=ignore``
                    # / ``--disable-pip-version-check`` CLI flags
                    # (pip ≥ 22.1).
                    install_prefix = (
                        "PIP_ROOT_USER_ACTION=ignore "
                        "PIP_DISABLE_PIP_VERSION_CHECK=1 "
                        "pip install --quiet --no-cache-dir "
                        "--disable-pip-version-check "
                        "--root-user-action=ignore "
                        f"--target=/tmp/pip-prefix {pkgs} && "
                        "export PYTHONPATH=/tmp/pip-prefix && "
                    )
                elif lang in ("javascript", "typescript"):
                    pkgs = " ".join(f'"{p}"' for p in install_packages)
                    install_prefix = (
                        "mkdir -p /tmp/npm-prefix && "
                        "cd /tmp/npm-prefix && "
                        f"npm install --silent --prefix /tmp/npm-prefix {pkgs} && "
                        "export NODE_PATH=/tmp/npm-prefix/node_modules && "
                        "cd - >/dev/null && "
                    )
            if install_prefix:
                # Wrap whatever cmd we had in a single shell invocation.
                original_cmd = " ".join(cmd_parts)
                cmd_parts = ["sh", "-c", f"{install_prefix}{original_cmd}"]

            await self._ensure_image(cfg["image"])

            # Phase 16.5 Commit K — when the workdir lives on the
            # shared docker named volume (``/sandbox-shared/...``),
            # mount the volume into the runner so the host docker
            # daemon never has to resolve a host filesystem path.
            # Otherwise fall back to the bind-mount path used by
            # bare-metal hosts where the app process and the docker
            # daemon share a filesystem.
            if (
                self._workdir_root == "/sandbox-shared"
                and workdir.startswith("/sandbox-shared/")
            ):
                run_id = os.path.basename(workdir.rstrip("/"))
                runner_workdir = f"/sandbox-shared/{run_id}"
                volume_args = [
                    "--mount",
                    "type=volume,src=amor-sandbox-shared,"
                    "dst=/sandbox-shared,readonly",
                ]
            else:
                runner_workdir = "/sandbox/work"
                volume_args = ["-v", f"{workdir}:/sandbox/work:ro"]

            # Phase 17 Commit O — when ``install_packages`` is
            # supplied we MUST allow network so pip / npm can reach
            # the public registry.  Without packages we keep the
            # strict ``--network none`` + read-only image
            # isolation.  When packages are requested we ONLY
            # widen the network — pip writes go to /tmp via the
            # ``--target`` flag in the install_prefix builder,
            # which keeps the site-packages tree of the base image
            # untouched (and, critically, leaves pip itself
            # importable so subsequent runs aren't poisoned).
            install_mode = bool(install_packages)
            # Cycle D — test_mode runners ALWAYS need network so the
            # test framework (pytest / vitest / etc) can be fetched
            # from PyPI / npm.  Otherwise pip install fails with
            # "Failed to establish a new connection" and the runner
            # masks the real failure under ``echo EXIT=$?``.
            if test_mode:
                install_mode = True
            network_mode = "bridge" if install_mode else "none"
            extra_tmpfs: list[str] = []

            docker_args = [
                "docker",
                "run",
                "--name",
                container_name,
                "--rm",
                "--network",
                network_mode,
                "--security-opt",
                "no-new-privileges",
                # Cycle C Sprint 5 Day 3 — drop every Linux capability.
                # The runner images don't need any (no setuid, no raw
                # sockets, no mount, no module loads).  Combined with
                # ``no-new-privileges`` this forecloses the entire
                # capability-based escape surface.  Tested with the
                # full Sprint 0 corpus + HumanEval+ 50 — no regression
                # on ``pip install --target`` or arbitrary Python /
                # Node code.
                "--cap-drop",
                "ALL",
                # Hard cap on PIDs so a fork bomb can't exhaust the
                # host's process table.  128 is well above the natural
                # working-set of any pipeline phase (Python: ~10
                # threads, Node: ~5).
                "--pids-limit",
                "128",
                "--memory",
                self._memory_limit,
                "--memory-swap",
                self._memory_limit,
                "--cpu-quota",
                str(self._cpu_quota),
                "--read-only",
                "--tmpfs",
                # Cycle C Sprint 2 Day 2 — bumped 64m → 384m so
                # ``pip install --target=/tmp/pip-prefix numpy`` has
                # room.  v18.1.2 (Cycle G) further bumped to 768m
                # default after HumanEval+ historical runs (5/5/2026
                # 00:59-01:07) failed at install with [Errno 28]
                # No space left on device — numpy's transient wheel
                # staging in pip's TMPDIR (also /tmp) was hitting
                # the 384m ceiling on concurrent / cold-cache cases.
                # Tunable via `code_sandbox_tmpfs_size_mb` setting
                # (env `AMOR_CODE_SANDBOX_TMPFS_SIZE_MB`).
                f"/tmp:size={_tmpfs_size_mb()}m,exec",
                *extra_tmpfs,
                *volume_args,
                "--workdir",
                runner_workdir,
                cfg["image"],
                *cmd_parts,
            ]

            t_start = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                *docker_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if stdin_data else None,
            )

            timed_out = False
            try:
                stdin_bytes = stdin_data.encode() if stdin_data else None
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes),
                    timeout=float(timeout),
                )
            except TimeoutError:
                timed_out = True
                # Kill the container by name (proc.kill alone is not
                # sufficient because Docker forks the actual workload).
                try:
                    kill_proc = await asyncio.create_subprocess_exec(
                        "docker",
                        "kill",
                        container_name,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(kill_proc.wait(), timeout=5)
                except Exception:
                    pass
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                stdout_b = b""
                stderr_b = b"Execution timed out"

            duration_ms_float = (time.monotonic() - t_start) * 1000.0
            duration_ms = int(duration_ms_float)

            # v17 PR #5 — wire the diagnostics telemetry that's been
            # available since Phase 17 Commit R but never called.
            # Records into the 200-entry sliding-window so the
            # ``/api/code/diagnostics`` endpoint can surface
            # ``sandbox.cold_start_p50_ms`` / ``cold_start_p95_ms``.
            # Pass the FLOAT duration so sub-millisecond test runs
            # (which would round to 0 and get filtered) are still
            # recorded — production cold-starts are 50ms+ so
            # rounding noise isn't a concern there.
            try:
                from .diagnostics import record_sandbox_run_ms  # noqa: PLC0415
                if duration_ms_float > 0:
                    record_sandbox_run_ms(duration_ms_float)
            except Exception:  # pragma: no cover
                pass

            # Cycle F Sprint 2 — harvest pytest-cov JSON BEFORE the
            # workdir is rmtree'd in the finally clause.  Silent on
            # absence (non-Python, non-test runs, or coverage-install
            # failure — none of which should block the result return).
            coverage_payload: Optional[Dict[str, Any]] = None
            try:
                cov_path = os.path.join(workdir, ".coverage.json")
                if os.path.isfile(cov_path):
                    with open(cov_path, "r", encoding="utf-8") as _cf:
                        coverage_payload = json.load(_cf)
            except (OSError, json.JSONDecodeError, ValueError):
                coverage_payload = None

            return ExecutionResult(
                exit_code=124 if timed_out else (proc.returncode or 0),
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
                timed_out=timed_out,
                duration_ms=duration_ms,
                language=lang,
                coverage_json=coverage_payload,
            )

        except FileNotFoundError as exc:
            # docker CLI vanished mid-run (e.g. Docker Desktop quit).
            # Invalidate the cached docker_available so the next call
            # re-probes and short-circuits cleanly.
            self._docker_available_cache = False
            self._docker_available_cached_at = time.monotonic()
            logger.warning(
                "sandbox_docker_cli_disappeared err=%s — sandbox runs "
                "will now skip until probe succeeds again", exc,
            )
            return ExecutionResult(
                exit_code=0,
                stdout="",
                stderr=("sandbox skipped — docker CLI not available "
                         "in app container"),
                error="docker_unavailable",
                duration_ms=0,
                language=language,
                skipped=True,
            )
        except Exception as exc:
            # Real run-time failure (image pull failed, container OOM,
            # etc.). Log only at warning level — the previous
            # `logger.exception` wrote a full traceback per call which
            # spammed the error stream.
            logger.warning("sandbox_execution_failed: %s", exc)
            # v17 PR #5 — surface failures in the diagnostics ring
            # buffer so operators can see WHY the sandbox is
            # struggling without parsing app logs.
            try:
                from .diagnostics import record_failure  # noqa: PLC0415
                record_failure(
                    "sandbox.execute",
                    str(exc),
                    language=language,
                )
            except Exception:  # pragma: no cover
                pass
            return ExecutionResult(
                exit_code=1,
                stdout="",
                stderr="",
                error=str(exc),
                language=language,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            # Belt-and-braces: even if --rm failed, kill any lingering
            # container by name. Never raises.
            try:
                cleanup = await asyncio.create_subprocess_exec(
                    "docker",
                    "rm",
                    "-f",
                    container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(cleanup.wait(), timeout=5)
            except Exception:
                pass
