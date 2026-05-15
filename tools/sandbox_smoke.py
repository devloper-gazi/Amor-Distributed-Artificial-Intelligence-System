#!/usr/bin/env python3
"""
Cycle C Sprint 5 Day 4 — sandbox smoke runner.

Drives the live ``ExecutionSandbox`` against 20 cases:

* 10 known-good: ordinary Python the sandbox MUST pass without
  regression (print, math, json, list comprehensions, ...).
* 10 known-bad: privileged operations the hardening MUST deny
  (mount, ptrace, chroot, fork bomb, OOM, raw socket, ...).

Run it inside the app container:

    docker exec amor-app-1 python tools/sandbox_smoke.py

Exits 0 only when 20/20 cases match their expected outcome.

Why this is in tools/ not tests/
--------------------------------
The pytest suite exercises ``security_posture()`` + the in-container
unit-test surface, but it never actually spawns a runner container —
that requires the docker daemon and tens of seconds per case.  This
script is the live counterpart, run on demand against a real daemon.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Tuple

# Make the document_processor package importable when running from /app.
sys.path.insert(0, "/app")

from document_processor.code_intelligence.sandbox import (  # noqa: E402
    ExecutionResult,
    ExecutionSandbox,
)


@dataclass
class Case:
    name: str
    code: str
    language: str = "python"
    timeout: int = 30
    install_packages: List[str] | None = None
    # Predicate evaluated on the ExecutionResult.  Returns
    # (ok: bool, why: str).
    check: Callable[[ExecutionResult], Tuple[bool, str]] = (
        lambda r: (r.exit_code == 0, f"exit={r.exit_code}")
    )


# ─── good cases ────────────────────────────────────────────────────


def _ok_exit_zero(r: ExecutionResult) -> Tuple[bool, str]:
    return r.exit_code == 0, f"exit={r.exit_code} stderr_tail={r.stderr[-120:]!r}"


GOOD_CASES: List[Case] = [
    Case(
        "good_print",
        "print('hello')",
        check=lambda r: (r.exit_code == 0 and "hello" in r.stdout, str(r.exit_code)),
    ),
    Case(
        "good_math",
        "import math; print(round(math.pi, 5))",
        check=lambda r: (r.exit_code == 0 and "3.14159" in r.stdout, str(r.exit_code)),
    ),
    Case(
        "good_json",
        "import json; print(json.dumps({'k':[1,2,3]}))",
        check=lambda r: (r.exit_code == 0 and '"k": [1, 2, 3]' in r.stdout, str(r.exit_code)),
    ),
    Case(
        "good_list_comp",
        "print(sum([x*x for x in range(10)]))",
        check=lambda r: (r.exit_code == 0 and "285" in r.stdout, str(r.exit_code)),
    ),
    Case(
        "good_sorted",
        "print(sorted([3,1,4,1,5,9,2,6,5,3,5]))",
        check=lambda r: (r.exit_code == 0 and "[1, 1, 2, 3, 3, 4, 5, 5, 5, 6, 9]" in r.stdout, str(r.exit_code)),
    ),
    Case(
        "good_dict_comp",
        "print({c: ord(c) for c in 'abc'})",
        check=lambda r: (r.exit_code == 0 and "97" in r.stdout, str(r.exit_code)),
    ),
    Case(
        "good_exception",
        "try:\n    1/0\nexcept ZeroDivisionError as e:\n    print('caught:', e)",
        check=lambda r: (r.exit_code == 0 and "caught" in r.stdout, str(r.exit_code)),
    ),
    Case(
        "good_datetime",
        "from datetime import datetime; print('YEAR' if datetime.now().year >= 2025 else 'OLD')",
        check=lambda r: (r.exit_code == 0 and "YEAR" in r.stdout, str(r.exit_code)),
    ),
    Case(
        "good_subprocess_echo",
        "import subprocess; print(subprocess.run(['echo','hi'], capture_output=True, text=True).stdout.strip())",
        check=lambda r: (r.exit_code == 0 and "hi" in r.stdout, str(r.exit_code)),
    ),
    Case(
        "good_pip_install_requests",
        "import requests; print('requests', requests.__version__)",
        install_packages=["requests"],
        timeout=120,
        check=lambda r: (r.exit_code == 0 and "requests" in r.stdout, str(r.exit_code)),
    ),
]


# ─── bad cases (hardening MUST deny) ──────────────────────────────


def _denied(needles: List[str]) -> Callable[[ExecutionResult], Tuple[bool, str]]:
    def check(r: ExecutionResult) -> Tuple[bool, str]:
        merged = (r.stdout + "\n" + r.stderr).lower()
        hit = next((n for n in needles if n.lower() in merged), None)
        # OK = the operation FAILED (exit non-zero OR a denial keyword
        # appeared somewhere in the output).
        ok = (r.exit_code != 0) or (hit is not None)
        return ok, f"exit={r.exit_code} hit={hit!r}"
    return check


def _oom_or_failure(r: ExecutionResult) -> Tuple[bool, str]:
    # OOM kills the container with exit_code 137 (SIGKILL+128) OR the
    # docker run subprocess hits a memory cap that turns into a
    # non-zero exit.
    if r.exit_code != 0:
        return True, f"oom-killed exit={r.exit_code}"
    if "memory" in r.stderr.lower():
        return True, "stderr mentions memory"
    return False, f"exit={r.exit_code} stderr={r.stderr[:120]!r}"


def _pids_limit(r: ExecutionResult) -> Tuple[bool, str]:
    # We expect one of:
    #   * stdout shows a count well below sys.maxsize (clamped by
    #     pids-limit)
    #   * an OSError "Resource temporarily unavailable" appears as
    #     fork() runs out of PIDs
    merged = (r.stdout + "\n" + r.stderr).lower()
    if "resource temporarily unavailable" in merged or "blockingioerror" in merged:
        return True, "pids-limit fired"
    if "max-pids:" in merged:
        # The script prints the highest count it could reach; verify
        # it didn't exceed the cap.
        try:
            count = int(merged.split("max-pids:")[1].split()[0])
            if count <= 256:
                return True, f"capped at {count}"
        except Exception:
            return False, "couldn't parse count"
    return False, f"exit={r.exit_code}"


def _net_blocked(r: ExecutionResult) -> Tuple[bool, str]:
    merged = (r.stdout + "\n" + r.stderr).lower()
    needles = ["could not resolve", "name or service not known", "network is unreachable", "temporary failure in name resolution"]
    if any(n in merged for n in needles):
        return True, "DNS / connect blocked"
    return False, f"exit={r.exit_code} merged={merged[:120]!r}"


BAD_CASES: List[Case] = [
    Case(
        "bad_mount_proc",
        "import subprocess; r=subprocess.run(['mount','-t','proc','proc','/mnt'], capture_output=True, text=True); print(r.returncode, r.stderr)",
        check=_denied(["permission denied", "operation not permitted"]),
    ),
    Case(
        "bad_chroot",
        "import os; os.chroot('/mnt')",
        check=_denied(["operation not permitted", "permission denied"]),
    ),
    Case(
        # PTRACE_ATTACH (16) on pid 1 needs CAP_SYS_PTRACE.  We dropped
        # all caps, so libc.ptrace returns -1 and errno=EPERM.
        # NB: ``ptrace(0, ...)`` is PTRACE_TRACEME — that's NOT
        # privileged and trivially returns 0; do not regress to it.
        "bad_ptrace",
        (
            "import ctypes, os\n"
            "libc = ctypes.CDLL('libc.so.6', use_errno=True)\n"
            "rc = libc.ptrace(16, 1, 0, 0)\n"
            "print('rc=', rc, 'errno=', os.strerror(ctypes.get_errno()))\n"
        ),
        check=_denied(["rc= -1", "operation not permitted", "permission denied"]),
    ),
    Case(
        # ``setuid(1)`` (not 0!) requires CAP_SETUID, which we drop.
        # Running as root inside the runner means setuid(0) succeeds
        # trivially, so the meaningful test is a *transition*.
        "bad_setuid",
        "import os; os.setuid(1); print('uid=', os.getuid())",
        check=_denied(["operation not permitted", "permission denied"]),
    ),
    Case(
        "bad_raw_socket",
        "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP); print('opened', s)",
        check=_denied(["permission denied", "operation not permitted"]),
    ),
    Case(
        "bad_network_default",
        # Default --network none — DNS + connect MUST fail.
        "import urllib.request; urllib.request.urlopen('https://example.com', timeout=4).read(10)",
        check=_net_blocked,
    ),
    Case(
        "bad_fork_bomb_pidlim",
        # Try to spawn 1000 child processes; --pids-limit=128 MUST
        # cap us long before we get there (or the script aborts on
        # BlockingIOError).
        (
            "import os, sys, time\n"
            "n = 0\n"
            "try:\n"
            "    for _ in range(1000):\n"
            "        pid = os.fork()\n"
            "        if pid == 0:\n"
            "            time.sleep(2); os._exit(0)\n"
            "        n += 1\n"
            "except BlockingIOError as e:\n"
            "    print('blocked:', e)\n"
            "print('max-pids:', n)\n"
        ),
        timeout=15,
        check=_pids_limit,
    ),
    Case(
        "bad_oom",
        # Try to allocate ~512 MB.  --memory=256m + --memory-swap=256m
        # MUST OOM-kill before the print runs.
        "x = bytearray(512 * 1024 * 1024); print('allocated', len(x))",
        timeout=10,
        check=_oom_or_failure,
    ),
    Case(
        "bad_mknod",
        "import os; os.mknod('/tmp/dev_null', 0o666 | 0o020000, os.makedev(1, 3))",
        check=_denied(["operation not permitted", "permission denied"]),
    ),
    Case(
        # finit_module (313 on x86_64) tries to load a kernel module.
        # Denied by both Docker's default seccomp and by CAP_SYS_MODULE
        # being dropped — should return -1 with EPERM.
        "bad_finit_module",
        (
            "import ctypes, os\n"
            "libc = ctypes.CDLL('libc.so.6', use_errno=True)\n"
            "rc = libc.syscall(313, -1, b'', 0)\n"
            "print('rc=', rc, 'errno=', os.strerror(ctypes.get_errno()))\n"
        ),
        check=_denied(["rc= -1", "operation not permitted", "permission denied", "function not implemented"]),
    ),
]


# ─── runner ────────────────────────────────────────────────────────


async def run_case(sb: ExecutionSandbox, case: Case) -> Tuple[bool, str]:
    try:
        res = await sb.execute(
            code=case.code,
            language=case.language,
            install_packages=case.install_packages,
            timeout=case.timeout,
        )
    except Exception as exc:
        return False, f"exec raised {type(exc).__name__}: {exc}"
    ok, why = case.check(res)
    return ok, why


async def main() -> int:
    os.environ.setdefault("AMOR_SANDBOX_WORKDIR", "/sandbox-shared")
    sb = ExecutionSandbox()
    posture = sb.security_posture()
    print(
        f"== sandbox smoke ({posture['level']} / score {posture['score']}/10) ==",
    )
    print(
        "  proxy:",
        "yes" if posture["via_proxy"] else "no",
        " host:",
        repr(posture["docker_host"] or "(direct unix socket)"),
    )

    cases: List[Tuple[str, Case]] = (
        [("good", c) for c in GOOD_CASES] + [("bad", c) for c in BAD_CASES]
    )

    fail = 0
    for kind, case in cases:
        ok, why = await run_case(sb, case)
        marker = "✓" if ok else "✗"
        print(f"  {marker} [{kind}] {case.name:34s} — {why}")
        if not ok:
            fail += 1

    total = len(cases)
    passed = total - fail
    print(f"\nResult: {passed}/{total} passed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
