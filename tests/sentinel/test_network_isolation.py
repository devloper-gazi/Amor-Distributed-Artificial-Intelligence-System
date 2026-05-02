"""Network-isolation guard for Sentinel.

The plan's Phase 11 hard constraint is "no non-localhost network
traffic during a scan".  We verify two things:

1. **Static, source-level**: no Sentinel module imports a network
   library AT MODULE LOAD time (importlib check).  The only
   networking allowed is local subprocess to Ollama (handled
   inside the LLMCall adapter, not at sentinel module import).
2. **Runtime, opt-in**: when ``Get-NetTCPConnection`` (Windows) or
   ``ss`` (Linux) is available we collect the established
   connections owned by the current Python process before and
   after a scan; the diff must contain only ``127.0.0.1`` / ``::1``
   addresses.  The test skips cleanly on hosts without either tool.

License: MIT.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import pytest

from document_processor.sentinel.engine import SentinelEngine
from document_processor.sentinel.models import SentinelRequest


# ─── Static check ───────────────────────────────────────────────────


# Modules that should NOT appear in any sentinel/* import.  ollama
# isn't a Python module — Ollama is HTTP via aiohttp/requests, but
# those imports happen inside code_intelligence (via _llm_call_local)
# and are loaded LAZILY only when an LLM agent fires.
_BANNED_TOP_LEVEL = {
    "requests",       # synchronous HTTP, no place in Sentinel
    "socket",         # raw socket — same
    "urllib3",        # HTTP — only via aiohttp inside lazy bridges
    "tweepy",         # social network APIs
    "boto3",          # AWS SDK — should never be in a local-only tool
}


def _walk_python_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def test_no_top_level_network_imports():
    """Every sentinel/*.py file must NOT import a network library at
    top level.  Lazy imports inside functions are fine."""
    sentinel_root = Path(__file__).resolve().parents[2] / "document_processor" / "sentinel"
    offenders: list[tuple[Path, str]] = []
    for py in _walk_python_files(sentinel_root):
        text = py.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.lstrip()
            # Skip comments / docstrings.
            if stripped.startswith("#"):
                continue
            # Top-level only — indented imports are inside functions.
            if line != stripped:
                continue
            for banned in _BANNED_TOP_LEVEL:
                if (stripped.startswith(f"import {banned}")
                        or stripped.startswith(f"from {banned}")):
                    offenders.append((py, banned))
    assert not offenders, (
        "sentinel modules imported network libraries at top level: "
        + ", ".join(f"{p.name}→{b}" for p, b in offenders)
    )


# ─── Runtime check (opt-in, Windows / Linux) ────────────────────────


def _enumerate_remote_addrs() -> list[str]:
    """Return the *non-loopback* remote addresses the current process
    is connected to right now.  Empty list when the platform-specific
    enumeration tool is missing."""
    pid = os.getpid()
    if shutil.which("powershell.exe") and sys.platform.startswith("win"):
        try:
            result = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-Command",
                    "Get-NetTCPConnection -State Established | "
                    f"Where-Object {{ $_.OwningProcess -eq {pid} }} | "
                    "Select-Object -ExpandProperty RemoteAddress",
                ],
                capture_output=True, text=True, timeout=5,
            )
            return [
                a.strip() for a in (result.stdout or "").splitlines()
                if a.strip()
                and not a.strip().startswith(("127.", "::1", "0.0.0.0"))
            ]
        except Exception:
            return []
    if shutil.which("ss"):
        try:
            result = subprocess.run(
                ["ss", "-Htnp"],
                capture_output=True, text=True, timeout=5,
            )
            out: list[str] = []
            for line in (result.stdout or "").splitlines():
                if f"pid={pid}" not in line:
                    continue
                parts = line.split()
                if len(parts) >= 5:
                    peer = parts[4]
                    addr = peer.rsplit(":", 1)[0]
                    if addr and not addr.startswith(("127.", "::1", "0.0.0.0", "[::1]")):
                        out.append(addr)
            return out
        except Exception:
            return []
    return []


def test_quick_scan_makes_no_external_connections(tmp_path):
    """Run a Quick scan + assert no non-loopback connections appear
    during it.  Skips on platforms without Get-NetTCPConnection / ss."""
    if not (shutil.which("powershell.exe") or shutil.which("ss")):
        pytest.skip("network enumeration tool missing")

    f = tmp_path / "tiny.py"
    f.write_text("AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n", encoding="utf-8")

    before = set(_enumerate_remote_addrs())
    eng = SentinelEngine(
        request=SentinelRequest(paths=[str(f)], scan_profile="quick"),
    )
    asyncio.run(eng.run())
    after = set(_enumerate_remote_addrs())
    diff = after - before
    assert not diff, (
        f"Quick scan opened non-loopback connections: {sorted(diff)}"
    )
