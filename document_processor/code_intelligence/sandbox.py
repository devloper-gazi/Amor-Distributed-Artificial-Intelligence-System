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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Language → image + run command
# ─────────────────────────────────────────────────────────────────────────────


LANGUAGE_RUNNERS: Dict[str, Dict[str, Any]] = {
    "python": {
        "image": "python:3.11-slim",
        "cmd": ["python", "/sandbox/work/main.py"],
        "filename": "main.py",
    },
    "javascript": {
        "image": "node:20-slim",
        "cmd": ["node", "/sandbox/work/main.js"],
        "filename": "main.js",
    },
    "typescript": {
        "image": "node:20-slim",
        "cmd": [
            "sh", "-c",
            "cd /sandbox/work && "
            "npx -y -p typescript -p ts-node ts-node --skipProject main.ts 2>&1",
        ],
        "filename": "main.ts",
    },
    "bash": {
        "image": "bash:5",
        "cmd": ["bash", "/sandbox/work/main.sh"],
        "filename": "main.sh",
    },
    "go": {
        "image": "golang:1.22-alpine",
        "cmd": ["sh", "-c", "cd /sandbox/work && go run main.go 2>&1"],
        "filename": "main.go",
    },
    "rust": {
        "image": "rust:1.78-slim",
        "cmd": [
            "sh", "-c",
            "cd /sandbox/work && rustc main.rs -o /tmp/out && /tmp/out 2>&1",
        ],
        "filename": "main.rs",
    },
    "cpp": {
        "image": "gcc:13",
        "cmd": [
            "sh", "-c",
            "cd /sandbox/work && g++ -O2 main.cpp -o /tmp/out && /tmp/out 2>&1",
        ],
        "filename": "main.cpp",
    },
    "java": {
        "image": "openjdk:21-slim",
        "cmd": [
            "sh", "-c",
            "cd /sandbox/work && cp Main.java /tmp/ && cd /tmp && "
            "javac Main.java && java Main 2>&1",
        ],
        "filename": "Main.java",
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
    error: Optional[str] = None
    duration_ms: int = 0
    language: str = ""

    @property
    def success(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.error
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "language": self.language,
            "success": self.success,
        }

    def to_feedback_str(self) -> str:
        """Compact execution feedback for injecting into next LLM context."""
        if self.success:
            status = "✅ SUCCESS"
        elif self.timed_out:
            status = "⏱ TIMEOUT"
        else:
            status = "❌ FAILED"
        lines = [
            f"Execution: {status} (exit={self.exit_code}, "
            f"{self.duration_ms}ms)"
        ]
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

    # ── Image management ──────────────────────────────────────────────────

    async def docker_available(self) -> bool:
        """Cheap check: is the Docker CLI / daemon reachable?"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "version", "--format", "{{.Server.Version}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return proc.returncode == 0
        except Exception:
            return False

    async def _ensure_image(self, image: str) -> None:
        """Pull the Docker image if it isn't already present locally."""
        proc = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode == 0:
            return

        logger.info("sandbox_pulling_image image=%s", image)
        proc = await asyncio.create_subprocess_exec(
            "docker", "pull", image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to pull Docker image {image}: "
                f"{stderr.decode(errors='replace')}"
            )

    async def image_status(self) -> Dict[str, bool]:
        """Map of language → whether its base image is locally cached."""
        out: Dict[str, bool] = {}
        for lang, cfg in LANGUAGE_RUNNERS.items():
            image = cfg["image"]
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "image", "inspect", image,
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
        extra_files: Optional[Dict[str, str]] = None,
        install_packages: Optional[List[str]] = None,
        timeout: Optional[int] = None,
        stdin_data: Optional[str] = None,
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
        lang = language.lower().strip()
        cfg = LANGUAGE_RUNNERS.get(lang)
        if not cfg:
            return ExecutionResult(
                exit_code=1,
                stdout="",
                stderr="",
                error=(
                    f"Unsupported language: {language!r}. "
                    f"Supported: {sorted(LANGUAGE_RUNNERS)}"
                ),
                language=language,
            )

        timeout = int(timeout or self._default_timeout)
        container_name = f"amor-sandbox-{uuid.uuid4().hex[:12]}"
        workdir = tempfile.mkdtemp(prefix="amor_sandbox_")
        proc: Optional[asyncio.subprocess.Process] = None

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
                    install_prefix = (
                        f"cd /sandbox/work && npm install --silent {pkgs} && "
                    )
            if install_prefix:
                # Wrap whatever cmd we had in a single shell invocation.
                original_cmd = " ".join(cmd_parts)
                cmd_parts = ["sh", "-c", f"{install_prefix}{original_cmd}"]

            await self._ensure_image(cfg["image"])

            docker_args = [
                "docker", "run",
                "--name", container_name,
                "--rm",
                "--network", "none",
                "--security-opt", "no-new-privileges",
                "--memory", self._memory_limit,
                "--memory-swap", self._memory_limit,
                "--cpu-quota", str(self._cpu_quota),
                "--read-only",
                "--tmpfs", "/tmp:size=64m,exec",
                "-v", f"{workdir}:/sandbox/work:ro",
                "--workdir", "/sandbox/work",
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
            except asyncio.TimeoutError:
                timed_out = True
                # Kill the container by name (proc.kill alone is not
                # sufficient because Docker forks the actual workload).
                try:
                    kill_proc = await asyncio.create_subprocess_exec(
                        "docker", "kill", container_name,
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

        except Exception as exc:
            logger.exception("sandbox_execution_failed")
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
                    "docker", "rm", "-f", container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(cleanup.wait(), timeout=5)
            except Exception:
                pass
