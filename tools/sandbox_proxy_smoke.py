#!/usr/bin/env python3
"""
Cycle F Sprint 5 (Wrong #2) — Docker socket proxy allowlist smoke.

Distinct from `tools/sandbox_smoke.py` (Cycle C, exercises the
sandbox execution layer with 20 safe + privileged Python cases).
THIS script verifies the `tecnativa/docker-socket-proxy` allowlist
matches what the AMOR sandbox actually needs, so flipping
`AMOR_DOCKER_HOST` to the proxy by default doesn't break runners.

Proxy allowlist (per docker-compose.yml service block):
    VERSION=1 PING=1 INFO=1 IMAGES=1 CONTAINERS=1
    POST=1 EXEC=1 VOLUMES=1
(all other Docker API endpoints denied)

Suite — 11 assertions:

  Allowed (must succeed):
    1. docker version            — VERSION
    2. docker info               — INFO
    3. docker image inspect      — IMAGES
    4. docker container ls       — CONTAINERS
    5. docker volume ls          — VOLUMES
    6. docker run --rm busybox echo — CONTAINERS + POST + IMAGES
    7. docker exec amor-app-2 echo  — EXEC

  Denied (must fail):
    1. docker network create     — NETWORKS=0
    2. docker swarm init         — SWARM=0
    3. docker system prune       — SYSTEM=0
    4. docker build              — BUILD=0

Exit codes:
   0  11/11 assertions match expectations → safe to flip
      `AMOR_DOCKER_HOST` default to the proxy
   1  one or more assertions broken
   2  proxy unreachable (pre-flight fail)

Usage:
  python tools/sandbox_proxy_smoke.py                  # localhost:2375
  python tools/sandbox_proxy_smoke.py --proxy-host tcp://amor-docker-proxy:2375
  python tools/sandbox_proxy_smoke.py --json
  python tools/sandbox_proxy_smoke.py --out data/security/proxy_smoke_<ts>.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        _r = getattr(_s, "reconfigure", None)
        if _r is not None:
            try:
                _r(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


DEFAULT_PROXY_URL = "tcp://localhost:2375"


@dataclass
class CheckResult:
    name: str
    expected: str            # "pass" | "fail"
    actual: str              # "pass" | "fail"
    exit_code: int
    elapsed_ms: int
    stderr_excerpt: str = ""

    @property
    def matches_expectation(self) -> bool:
        return self.actual == self.expected

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "expected": self.expected,
            "actual": self.actual,
            "exit_code": self.exit_code,
            "elapsed_ms": self.elapsed_ms,
            "ok": self.matches_expectation,
            "stderr_excerpt": self.stderr_excerpt[:200],
        }


def _run(cmd: Sequence[str], *, env: dict, timeout: float = 30.0) -> tuple[int, str]:
    try:
        full_env = os.environ.copy()
        full_env.update(env)
        proc = subprocess.run(
            list(cmd),
            env=full_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        return 127, str(exc)


def _check(
    *, name: str, cmd: Sequence[str], expected: str, env: dict,
) -> CheckResult:
    started = time.monotonic()
    rc, stderr = _run(cmd, env=env)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    actual = "pass" if rc == 0 else "fail"
    return CheckResult(
        name=name,
        expected=expected,
        actual=actual,
        exit_code=rc,
        elapsed_ms=elapsed_ms,
        stderr_excerpt=stderr,
    )


def build_suite(proxy_env: dict) -> list[CheckResult]:
    results: list[CheckResult] = []

    # ── Allowed (7) ─────────────────────────────────────────────────
    results.append(_check(
        name="docker version (VERSION)",
        cmd=["docker", "version", "--format", "{{.Server.Version}}"],
        expected="pass",
        env=proxy_env,
    ))
    results.append(_check(
        name="docker info (INFO)",
        cmd=["docker", "info", "--format", "{{.ServerVersion}}"],
        expected="pass",
        env=proxy_env,
    ))
    results.append(_check(
        name="docker image inspect (IMAGES)",
        cmd=["docker", "image", "inspect", "busybox"],
        expected="pass",
        env=proxy_env,
    ))
    results.append(_check(
        name="docker container ls (CONTAINERS)",
        cmd=["docker", "container", "ls", "-q"],
        expected="pass",
        env=proxy_env,
    ))
    results.append(_check(
        name="docker volume ls (VOLUMES)",
        cmd=["docker", "volume", "ls", "-q"],
        expected="pass",
        env=proxy_env,
    ))
    results.append(_check(
        name="docker run --rm busybox echo (CONTAINERS+POST)",
        cmd=["docker", "run", "--rm", "busybox", "echo", "smoke-ok"],
        expected="pass",
        env=proxy_env,
    ))
    results.append(_check(
        name="docker exec amor-app-2 echo (EXEC)",
        cmd=["docker", "exec", "amor-app-2", "echo", "exec-ok"],
        expected="pass",
        env=proxy_env,
    ))

    # ── Denied (4) ──────────────────────────────────────────────────
    results.append(_check(
        name="docker network create (NETWORKS=0)",
        cmd=["docker", "network", "create",
             f"smoke-net-{int(time.time())}"],
        expected="fail",
        env=proxy_env,
    ))
    results.append(_check(
        name="docker swarm init (SWARM=0)",
        cmd=["docker", "swarm", "init",
             "--advertise-addr", "127.0.0.1"],
        expected="fail",
        env=proxy_env,
    ))
    results.append(_check(
        name="docker system prune (SYSTEM=0)",
        cmd=["docker", "system", "prune", "-f"],
        expected="fail",
        env=proxy_env,
    ))
    results.append(_check(
        name="docker build (BUILD=0)",
        cmd=["docker", "buildx", "ls"],
        expected="fail",
        env=proxy_env,
    ))

    return results


def render(results: list[CheckResult]) -> None:
    print("=" * 76)
    print("Sandbox-through-proxy smoke test")
    print("=" * 76)
    for r in results:
        glyph = "+" if r.matches_expectation else "x"
        print(
            f"  {glyph} {r.name:<48s} "
            f"exp={r.expected:<5s} got={r.actual:<5s} "
            f"({r.elapsed_ms} ms)"
        )
        if not r.matches_expectation and r.stderr_excerpt:
            print(f"      stderr: {r.stderr_excerpt[:120]}")
    print("-" * 76)
    passed = sum(1 for r in results if r.matches_expectation)
    total = len(results)
    print(f"  passed={passed}/{total}")
    print("=" * 76)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--proxy-host",
        default=os.environ.get(
            "AMOR_DOCKER_HOST_SMOKE", DEFAULT_PROXY_URL,
        ),
        help=(
            "Proxy URL to test against.  Default: env "
            "AMOR_DOCKER_HOST_SMOKE or tcp://localhost:2375.  "
            "Use tcp://amor-docker-proxy:2375 when running from "
            "inside the AMOR docker network."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Machine-readable JSON.",
    )
    parser.add_argument(
        "--out", default=None,
        help="Persist scorecard to this path.",
    )
    args = parser.parse_args()

    proxy_env = {"DOCKER_HOST": args.proxy_host}

    pre = _check(
        name="proxy pre-flight (docker version)",
        cmd=["docker", "version", "--format", "{{.Client.Version}}"],
        expected="pass",
        env=proxy_env,
    )
    if not pre.matches_expectation:
        logger.error(
            "proxy unreachable at %s — exit=%d stderr=%s",
            args.proxy_host, pre.exit_code, pre.stderr_excerpt[:200],
        )
        return 2

    logger.info(
        "proxy reachable at %s — running 11 assertions",
        args.proxy_host,
    )
    results = build_suite(proxy_env)

    if args.json:
        print(json.dumps({
            "proxy_host": args.proxy_host,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "passed": sum(1 for r in results if r.matches_expectation),
            "total": len(results),
            "results": [r.to_dict() for r in results],
        }, indent=2))
    else:
        render(results)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({
                "proxy_host": args.proxy_host,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "results": [r.to_dict() for r in results],
            }, indent=2),
            encoding="utf-8",
        )

    return 0 if all(r.matches_expectation for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
