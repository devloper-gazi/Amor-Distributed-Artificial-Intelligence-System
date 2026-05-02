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
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


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
            "g++ -O2 main.cpp -o /tmp/out && /tmp/out 2>&1",
        ],
        "filename": "main.cpp",
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
    ) -> ExecutionResult:
        """
        Execute code in an isolated Docker container.

        Parameters
        ----------
        code               : Source code to run.
        language           : Language key (see LANGUAGE_RUNNERS).
        extra_files        : Map of {filename: content} for additional
                             files mounted alongside `code`.
        install_packages   : Packages to pip/npm install before running.
                             Currently supported for python / js / ts.
        timeout            : Seconds before the container is killed.
        stdin_data         : Optional input piped to stdin.
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

            # Build the command, optionally prefixing with package installs.
            cmd_parts = list(cfg["cmd"])
            install_prefix = ""
            if install_packages:
                if lang == "python":
                    pkgs = " ".join(f'"{p}"' for p in install_packages)
                    install_prefix = f"pip install --quiet {pkgs} && "
                elif lang in ("javascript", "typescript"):
                    pkgs = " ".join(f'"{p}"' for p in install_packages)
                    # Phase 16.5 Commit K — runner already cd-ed via
                    # --workdir; no need for explicit cd here.
                    install_prefix = f"npm install --silent {pkgs} && "
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

            docker_args = [
                "docker",
                "run",
                "--name",
                container_name,
                "--rm",
                "--network",
                "none",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                self._memory_limit,
                "--memory-swap",
                self._memory_limit,
                "--cpu-quota",
                str(self._cpu_quota),
                "--read-only",
                "--tmpfs",
                "/tmp:size=64m,exec",
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

            duration_ms = int((time.monotonic() - t_start) * 1000)

            return ExecutionResult(
                exit_code=124 if timed_out else (proc.returncode or 0),
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
                timed_out=timed_out,
                duration_ms=duration_ms,
                language=lang,
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
