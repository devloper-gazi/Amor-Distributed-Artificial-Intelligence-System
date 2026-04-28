#!/usr/bin/env python
"""
AMOR live watch — real-time multi-pane TUI monitor.

What it shows
-------------
* **System Health**     — /health (cache · postgres · mongodb · ollama)
                          + container readiness (gateway, app replicas)
* **Consortium**         — every consortium_session in Redis: status,
                          current phase, heartbeat freshness, gates
* **Other modes**        — research / thinking / code session counts +
                          latest-active per mode
* **Ollama LLM**         — currently loaded model, throughput estimate
                          from the last N HTTP `/api/generate` calls,
                          per-call latency rolling average, total VRAM
* **Live event stream** — Redis pub/sub fan-in across every
                          `amor:consortium:events:*` channel + a tail
                          of the most relevant docker log lines
* **Errors panel**      — last few `error|exception|traceback` lines
                          in the app logs (last 5 minutes)
* **Stats footer**      — uptime, total ticks, avg loop latency

Updated 4× per second, fully async, zero blocking.

Usage
-----
    python scripts/watch_live.py                    # default: localhost:8000
    python scripts/watch_live.py --base http://app.local:8000 \\
                                 --redis localhost:6379 \\
                                 --ollama http://localhost:11434

Press ``q`` or Ctrl-C to exit.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover
    print("redis package missing. pip install redis", file=sys.stderr)
    sys.exit(2)

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("watch_live")


# ─── runtime state ─────────────────────────────────────────────────────────


@dataclass
class WatchState:
    """One in-process snapshot. Updated by the async pollers; read by the
    render loop. All fields default-empty so the UI works during startup
    while real data is still being fetched."""

    # ── system health ──
    health_status: str = "?"
    health_components: dict[str, bool] = field(default_factory=dict)
    container_status: dict[str, str] = field(default_factory=dict)

    # ── consortium / other-mode session lists ──
    consortium_sessions: list[dict[str, Any]] = field(default_factory=list)
    other_session_counts: dict[str, int] = field(default_factory=dict)

    # ── ollama state ──
    ollama_models: list[dict[str, Any]] = field(default_factory=list)
    ollama_version: str = "?"
    ollama_recent_calls: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=20),
    )

    # ── event + error streams ──
    event_log: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=14),
    )
    error_log: deque[str] = field(default_factory=lambda: deque(maxlen=8))

    # ── stats / loop ──
    started_at: float = field(default_factory=time.time)
    tick_count: int = 0
    last_tick_ms: float = 0.0
    last_error: str = ""

    # ── shutdown signal ──
    stop: asyncio.Event = field(default_factory=asyncio.Event)


# ─── helpers ───────────────────────────────────────────────────────────────


_AGE_UNITS = ("s", "m", "h", "d")


def _humanise_age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _humanise_size(bytes_: int) -> str:
    if not bytes_:
        return "?"
    if bytes_ >= 1024 ** 3:
        return f"{bytes_ / 1024**3:.1f} GB"
    if bytes_ >= 1024 ** 2:
        return f"{bytes_ / 1024**2:.1f} MB"
    return f"{bytes_ / 1024:.0f} KB"


def _parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def _short(s: str | None, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _phase_pct(session: dict[str, Any]) -> int:
    """Derive a 0-100 progress percent from the phases list using
    per-phase weights. v8 — research is fast, thinking + implementation
    each cost ~35% of the run, so a flat 25% per phase under-counts
    early progress and over-counts late."""
    phases = session.get("phases") or []
    if not phases:
        return 0
    weights = {
        "scope": 5, "research": 25, "thinking": 35, "implementation": 35,
    }
    total = sum(weights.values())
    accumulated = 0.0
    for p in phases:
        name = p.get("name") or ""
        status = p.get("status") or "pending"
        w = weights.get(name, 100 / max(len(phases), 1))
        if status == "completed":
            accumulated += w
        elif status == "in_progress":
            # 50% credit while in-flight + a small bump for inner-phase
            # tracking via current_task heuristic.
            accumulated += w * _inner_phase_pct(session, name)
    return int(min(100.0, (accumulated / total) * 100.0))


# Engine-internal phase orders we can detect inside `current_task`.
_THINKING_INNER = [
    "understand", "decompose", "explore", "evaluate", "synthesize", "critique",
]
_CODE_INNER = [
    "triage", "model_prep", "plan", "implement",
    "execute", "analyze", "test", "debug", "review",
]


def _inner_phase_pct(session: dict[str, Any], phase_name: str) -> float:
    """0.0–1.0 estimated completion of the active phase.

    Reads `current_task` for keywords from the engine's inner phase
    order. Falls back to 0.5 (half done) if no match — better than 0
    because in-progress phases always represent partial completion.
    """
    task = (session.get("current_task") or "").lower()
    if phase_name == "thinking":
        order = _THINKING_INNER
    elif phase_name == "implementation":
        order = _CODE_INNER
    else:
        return 0.5
    for i, name in enumerate(order):
        if name in task:
            return (i + 0.5) / len(order)
    return 0.4


def _consortium_eta(session: dict[str, Any]) -> tuple[str, float] | None:
    """Linear-extrapolation ETA from elapsed time and progress %."""
    pct = _phase_pct(session)
    if pct <= 0 or pct >= 99:
        return None
    started = _parse_iso(session.get("started_at"))
    if started is None:
        return None
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    if elapsed < 10:
        return None
    remaining = elapsed * (100 - pct) / max(pct, 1)
    return (_humanise_age(remaining), remaining)


def _gate_counts(session: dict[str, Any]) -> tuple[int, int, int]:
    """Return (passed, passed_warn, failed) gate counts."""
    verifs = session.get("verifications") or []
    ok = sum(1 for v in verifs if v.get("status") == "passed")
    warn = sum(1 for v in verifs if v.get("status") == "passed_warn")
    fail = sum(1 for v in verifs if v.get("status") == "failed")
    return ok, warn, fail


def _find_active_session(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the most-active consortium session for the hero panel.

    Prefers `started`/`running` over terminal states, and within each
    bucket prefers the most recent heartbeat."""
    if not sessions:
        return None
    running = [s for s in sessions if s.get("status") in ("started", "running")]
    if running:
        running.sort(
            key=lambda s: s.get("last_heartbeat_at") or s.get("started_at") or "",
            reverse=True,
        )
        return running[0]
    sessions_sorted = sorted(
        sessions,
        key=lambda s: s.get("completed_at") or s.get("last_heartbeat_at")
                       or s.get("started_at") or "",
        reverse=True,
    )
    return sessions_sorted[0]


# ─── pollers (one task per data source) ────────────────────────────────────


async def poll_health(state: WatchState, client: httpx.AsyncClient,
                      base_url: str, interval: float = 2.0) -> None:
    """Hit /health every ``interval`` seconds. Failure-quiet."""
    while not state.stop.is_set():
        try:
            resp = await client.get(f"{base_url}/health", timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                state.health_status = str(data.get("status") or "?")
                state.health_components = dict(data.get("components") or {})
            else:
                state.health_status = f"HTTP {resp.status_code}"
        except Exception as exc:
            state.health_status = "unreachable"
            state.last_error = f"health: {exc}"
        try:
            await asyncio.wait_for(state.stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def poll_containers(state: WatchState, interval: float = 5.0) -> None:
    """Run `docker compose ps` and parse status. Slow (~150ms per poll)
    so we tick less frequently than the LLM/health pollers."""
    while not state.stop.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "ps", "--format", "{{.Name}}|{{.Status}}",
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            new_status: dict[str, str] = {}
            for line in out.decode("utf-8", errors="replace").splitlines():
                if "|" not in line:
                    continue
                name, status = line.split("|", 1)
                new_status[name.strip()] = status.strip()
            state.container_status = new_status
        except Exception as exc:
            state.last_error = f"containers: {exc}"
        try:
            await asyncio.wait_for(state.stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def poll_consortium_sessions(
    state: WatchState, redis_client: aioredis.Redis,
    base_url: str, client: httpx.AsyncClient, interval: float = 1.5,
) -> None:
    """Enumerate consortium_session:* keys + load each. Fast: pure Redis."""
    while not state.stop.is_set():
        try:
            sessions: list[dict[str, Any]] = []
            cursor = 0
            while True:
                cursor, batch = await redis_client.scan(
                    cursor=cursor, match="consortium_session:*", count=100,
                )
                for raw_key in batch:
                    key = (raw_key.decode("utf-8")
                           if isinstance(raw_key, bytes) else str(raw_key))
                    raw = await redis_client.get(key)
                    if not raw:
                        continue
                    try:
                        s = json.loads(
                            raw.decode("utf-8") if isinstance(raw, bytes) else raw,
                        )
                        if isinstance(s, dict):
                            sessions.append(s)
                    except Exception:
                        continue
                if cursor == 0:
                    break
            sessions.sort(
                key=lambda s: (
                    {"started": 0, "running": 0, "interrupted": 1,
                     "cancelled": 2, "ok": 3, "error": 4}.get(
                        str(s.get("status") or "started"), 5,
                    ),
                    s.get("started_at") or "",
                ),
                reverse=False,
            )
            state.consortium_sessions = sessions[:8]

            # Other-mode counts.
            other = {}
            for prefix, label in (
                ("research_session:", "research"),
                ("thinking_session:", "thinking"),
                ("code_session:", "code"),
            ):
                cursor = 0
                count = 0
                while True:
                    cursor, batch = await redis_client.scan(
                        cursor=cursor, match=f"{prefix}*", count=100,
                    )
                    count += len(batch)
                    if cursor == 0:
                        break
                other[label] = count
            state.other_session_counts = other
        except Exception as exc:
            state.last_error = f"consortium: {exc}"
        try:
            await asyncio.wait_for(state.stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def poll_ollama(
    state: WatchState, client: httpx.AsyncClient,
    ollama_url: str, interval: float = 3.0,
) -> None:
    while not state.stop.is_set():
        try:
            ver = await client.get(f"{ollama_url}/api/version", timeout=3.0)
            if ver.status_code == 200:
                state.ollama_version = str(ver.json().get("version") or "?")
            ps = await client.get(f"{ollama_url}/api/ps", timeout=3.0)
            if ps.status_code == 200:
                state.ollama_models = list(ps.json().get("models") or [])
        except Exception as exc:
            state.last_error = f"ollama: {exc}"
        try:
            await asyncio.wait_for(state.stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


_OLLAMA_LATENCY_RE = re.compile(
    r"\[GIN\].*\| (?P<status>\d+) \| +(?P<lat>\S+) \|.+\| POST +\"(?P<path>[^\"]+)\""
)


async def tail_ollama_logs(state: WatchState, interval: float = 2.0) -> None:
    """Tail docker logs ollama and parse GIN access lines for latency."""
    while not state.stop.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "logs", "--since", f"{int(interval+1)}s",
                "ollama",
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            for raw in out.decode("utf-8", errors="replace").splitlines():
                m = _OLLAMA_LATENCY_RE.search(raw)
                if not m:
                    continue
                if "/api/generate" not in m.group("path"):
                    continue
                state.ollama_recent_calls.append({
                    "ts": time.time(),
                    "status": int(m.group("status")),
                    "latency": m.group("lat"),
                    "path": m.group("path"),
                })
        except Exception as exc:
            state.last_error = f"ollama_logs: {exc}"
        try:
            await asyncio.wait_for(state.stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def subscribe_consortium_events(
    state: WatchState, redis_client: aioredis.Redis,
) -> None:
    """Pattern-subscribe to every consortium event channel and pump
    received events into ``state.event_log``. This is the headline
    feature — sub-second visibility into pipeline progress."""
    while not state.stop.is_set():
        pubsub = None
        try:
            pubsub = redis_client.pubsub()
            await pubsub.psubscribe("amor:consortium:events:*")
            async for msg in pubsub.listen():
                if state.stop.is_set():
                    break
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") not in {"pmessage", "message"}:
                    continue
                raw = msg.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    event = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(event, dict):
                    continue
                # Extract session id from channel name for display.
                channel = msg.get("channel")
                if isinstance(channel, bytes):
                    channel = channel.decode("utf-8", errors="replace")
                event["_channel"] = str(channel or "")
                event["_received_at"] = time.time()
                state.event_log.append(event)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            state.last_error = f"events: {exc}"
            try:
                await asyncio.wait_for(state.stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        finally:
            if pubsub is not None:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()


_ERROR_RE = re.compile(
    r"(error|exception|traceback|fail)", re.IGNORECASE,
)
_EXCLUDE_RE = re.compile(
    r"deprecat|info|warning_signal|user_role|GIN|capability_discoverer_cancelled",
    re.IGNORECASE,
)


async def tail_app_errors(state: WatchState, interval: float = 5.0) -> None:
    """Tail recent app logs filtering for errors / tracebacks."""
    while not state.stop.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "logs", "--since", "30s", "app",
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            new_errors: list[str] = []
            for line in out.decode("utf-8", errors="replace").splitlines():
                if not _ERROR_RE.search(line):
                    continue
                if _EXCLUDE_RE.search(line):
                    continue
                # Trim noisy JSON envelope down to the message.
                if '"event"' in line and '"level"' in line:
                    try:
                        # Last JSON object on the line.
                        idx = line.rfind("{")
                        if idx >= 0:
                            obj = json.loads(line[idx:].split('"timestamp"')[0]
                                              .rstrip(", ") + "}")
                            level = obj.get("level", "?")
                            event = obj.get("event", "?")
                            new_errors.append(f"{level} · {event}")
                            continue
                    except Exception:
                        pass
                new_errors.append(line[-180:])
            # Replace (not append) so we don't accumulate forever.
            state.error_log.clear()
            for err in new_errors[-state.error_log.maxlen:]:
                state.error_log.append(err)
        except Exception as exc:
            state.last_error = f"app_errors: {exc}"
        try:
            await asyncio.wait_for(state.stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# ─── render functions ──────────────────────────────────────────────────────


def render_health_panel(state: WatchState) -> Panel:
    parts: list[Text] = []
    base_color = (
        "green" if state.health_status == "healthy"
        else "yellow" if state.health_status in {"degraded", "?"}
        else "red"
    )
    header = Text(f"{state.health_status.upper()}", style=f"bold {base_color}")
    parts.append(header)

    comps = Text()
    for name, ok in state.health_components.items():
        comps.append(f" {name}", style="green" if ok else "red")
        comps.append(" ✓ " if ok else " ✗ ", style="green" if ok else "red")
    parts.append(comps)

    # Container row.
    cont_text = Text("\n", end="")
    healthy = unhealthy = 0
    for name, status in state.container_status.items():
        s = status.lower()
        ok = "up" in s and ("(healthy)" in s or "(health" not in s)
        if "exited" in s or "restarting" in s:
            unhealthy += 1
        else:
            healthy += 1
        short = name.replace("amor-", "")
        col = "green" if ok else "red"
        cont_text.append(f"{short} ", style=col)
    if state.container_status:
        cont_text.append(
            f"\n{healthy} up · {unhealthy} bad",
            style="dim",
        )
    parts.append(cont_text)
    return Panel(Group(*parts), title="System Health", border_style=base_color)


def render_active_pipeline_hero(state: WatchState) -> Panel:
    """Full-width hero showing the most-active consortium session with
    a prominent progress bar, phase chips, inner-phase chips, ETA and
    gate counts. The headline panel — what the user looks at first."""
    active = _find_active_session(state.consortium_sessions)
    if not active:
        return Panel(
            Align.center(Text(
                "No active consortium pipeline\n"
                "(start one from the Consortium card in More settings)",
                style="dim",
            )),
            title="Active Pipeline",
            border_style="purple",
            padding=(1, 2),
        )

    sid = (active.get("session_id") or "?")[:8]
    scope = active.get("scope") or {}
    goal = scope.get("goal") or scope.get("title") or "?"
    depth = scope.get("depth") or "?"
    status = str(active.get("status") or "?")
    cur_phase = active.get("current_phase") or "—"
    pct = _phase_pct(active)

    started = _parse_iso(active.get("started_at"))
    elapsed = ((datetime.now(timezone.utc) - started).total_seconds()
               if started else 0)
    hb = _parse_iso(active.get("last_heartbeat_at"))
    hb_age = ((datetime.now(timezone.utc) - hb).total_seconds()
              if hb else None)
    ok, warn, fail = _gate_counts(active)
    eta = _consortium_eta(active)

    status_color = {
        "started": "cyan", "running": "cyan",
        "ok": "green", "cancelled": "yellow",
        "interrupted": "magenta", "error": "red",
    }.get(status, "white")
    border_color = {
        "ok": "green", "error": "red", "cancelled": "yellow",
        "interrupted": "magenta",
    }.get(status, "purple")

    # ── Title row ──
    title_row = Text.assemble(
        ("🏛 ", "purple bold"),
        (sid, "bold cyan"),
        ("  "),
        (status.upper(), f"bold {status_color}"),
        ("  ·  ", "dim"),
        (_short(goal, 80), "bold white"),
        ("  ", ""),
        (f"({depth})", "dim"),
    )

    # ── Phase chips row ──
    phases = active.get("phases") or []
    phase_chips = Text()
    for i, p in enumerate(phases):
        name = p.get("name") or "?"
        st = p.get("status") or "pending"
        label = name.title() if len(name) <= 14 else name[:14]
        glyph, col = {
            "completed": ("✓ ", "green"),
            "in_progress": ("◉ ", "yellow"),
            "failed": ("✗ ", "red"),
            "skipped": ("⤳ ", "dim yellow"),
        }.get(st, ("⋯ ", "dim"))
        phase_chips.append(glyph, style=col)
        phase_chips.append(label, style=f"bold {col}" if st == "in_progress" else col)
        if i < len(phases) - 1:
            phase_chips.append("  │  ", style="dim")

    # ── Inner phase chips for the active phase ──
    inner_text = Text()
    if cur_phase == "thinking":
        order = _THINKING_INNER
    elif cur_phase == "implementation":
        order = _CODE_INNER
    else:
        order = []
    if order:
        task = (active.get("current_task") or "").lower()
        active_idx = -1
        for i, name in enumerate(order):
            if name in task:
                active_idx = i
                break
        inner_text.append("Inner: ", style="dim")
        for i, name in enumerate(order):
            if active_idx >= 0 and i < active_idx:
                inner_text.append(f"✓ {name}  ", style="green")
            elif active_idx == i:
                inner_text.append(f"◉ {name}  ", style="bold yellow")
            else:
                inner_text.append(f"⋯ {name}  ", style="dim")

    # ── Progress bar ──
    bar_width = 60
    filled = int(bar_width * pct / 100)
    bar = Text()
    bar_color = (
        "green" if pct >= 67 else "yellow" if pct >= 33 else "magenta"
    )
    bar.append("█" * filled, style=bar_color)
    bar.append("░" * (bar_width - filled), style="dim")
    bar.append(f"  {pct:>3}%", style=f"bold {bar_color}")

    # ── Stats row ──
    stats = Text()
    stats.append(f"Elapsed {_humanise_age(elapsed)}", style="cyan")
    stats.append("  ·  ", style="dim")
    if eta:
        stats.append(f"ETA ~{eta[0]}", style="cyan")
        stats.append("  ·  ", style="dim")
    stats.append("Gates: ", style="dim")
    stats.append(f"✓{ok} ", style="green")
    stats.append(f"⚠{warn} ", style="yellow")
    stats.append(f"✗{fail}", style="red")
    if hb_age is not None:
        stats.append("  ·  ", style="dim")
        hb_color = "green" if hb_age < 30 else "yellow" if hb_age < 90 else "red"
        stats.append(f"♥ {hb_age:.0f}s", style=hb_color)

    return Panel(
        Group(
            title_row,
            Text(""),
            phase_chips,
            inner_text if inner_text.plain else Text(""),
            Text(""),
            bar,
            Text(""),
            stats,
        ),
        title="Active Pipeline",
        border_style=border_color,
        padding=(0, 2),
    )


def render_consortium_panel(state: WatchState) -> Panel:
    if not state.consortium_sessions:
        return Panel(
            Align.center(Text("(no consortium sessions)", style="dim")),
            title="Consortium Sessions",
            border_style="purple",
        )

    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(width=10, no_wrap=True)
    table.add_column(width=12, no_wrap=True)
    table.add_column(ratio=2)
    table.add_column(width=14, no_wrap=True)
    table.add_column(width=10, no_wrap=True)

    now = datetime.now(timezone.utc)
    for s in state.consortium_sessions:
        sid = (s.get("session_id") or "?")[:8]
        status = str(s.get("status") or "?")
        goal = (s.get("scope") or {}).get("goal") or "?"
        cur = s.get("current_phase") or "—"
        hb = _parse_iso(s.get("last_heartbeat_at") or s.get("started_at"))
        age = _humanise_age((now - hb).total_seconds()) if hb else "?"
        pct = _phase_pct(s)

        status_color = {
            "started": "cyan", "running": "cyan",
            "ok": "green", "cancelled": "yellow",
            "interrupted": "magenta", "error": "red",
        }.get(status, "white")
        bar_filled = "█" * (pct // 10)
        bar_empty = "░" * (10 - pct // 10)
        bar = Text(bar_filled, style=status_color) + Text(bar_empty, style="dim")

        # Phase chip row — compact 4 dots
        phases_text = Text()
        for p in s.get("phases") or []:
            sym = {
                "completed": "●", "in_progress": "◐",
                "failed": "✗", "skipped": "·", "pending": "○",
            }.get(p.get("status"), "?")
            col = {
                "completed": "green", "in_progress": "cyan",
                "failed": "red", "skipped": "yellow",
            }.get(p.get("status"), "dim")
            phases_text.append(sym + " ", style=col)

        table.add_row(
            Text(sid, style="bold"),
            Text(status, style=status_color),
            Text(_short(goal, 60)),
            phases_text,
            Text(f"{pct:>3}% · {age}", style="dim"),
        )
    return Panel(table, title="Consortium Sessions", border_style="purple")


def render_other_modes_panel(state: WatchState) -> Panel:
    counts = state.other_session_counts
    table = Table.grid(padding=(0, 2), expand=True)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True, justify="right")
    table.add_row(Text("research", style="cyan"), Text(str(counts.get("research", 0))))
    table.add_row(Text("thinking", style="magenta"), Text(str(counts.get("thinking", 0))))
    table.add_row(Text("code", style="yellow"), Text(str(counts.get("code", 0))))
    return Panel(table, title="Other Mode Sessions", border_style="dim")


def render_ollama_panel(state: WatchState) -> Panel:
    parts: list[Any] = []
    parts.append(Text(f"Ollama {state.ollama_version}", style="bold"))
    if not state.ollama_models:
        parts.append(Text("\nno models currently loaded", style="dim"))
    else:
        t = Table.grid(padding=(0, 2), expand=True)
        t.add_column(no_wrap=True, ratio=2)
        t.add_column(no_wrap=True, justify="right")
        t.add_column(no_wrap=True, justify="right")
        for m in state.ollama_models:
            tag = m.get("name") or "?"
            size = m.get("size") or 0
            vram = m.get("size_vram") or 0
            on_gpu = vram > 0
            t.add_row(
                Text(tag, style="green" if on_gpu else "yellow"),
                Text(_humanise_size(size), style="dim"),
                Text("GPU" if on_gpu else "CPU",
                     style="green" if on_gpu else "yellow"),
            )
        parts.append(t)

    # Recent /api/generate calls
    if state.ollama_recent_calls:
        recent = list(state.ollama_recent_calls)[-5:]
        parts.append(Text("\nRecent /api/generate calls:", style="dim"))
        for call in recent:
            age = time.time() - call["ts"]
            parts.append(Text(
                f"  {call['latency']:>8s}  status={call['status']}  "
                f"({_humanise_age(age)} ago)",
                style="dim",
            ))
    return Panel(Group(*parts), title="Ollama LLM Activity", border_style="cyan")


_PHASE_EMOJI = {
    "scope": "🎯", "research": "🔍", "thinking": "🧠", "implementation": "⚙️",
    "consortium_phase_start": "▶", "consortium_phase_complete": "✓",
    "consortium_gate": "⚖", "consortium_completed": "●",
    "consortium_cancelled": "○", "consortium_error": "✗",
    "consortium_started": "🚀",
}


def render_event_log_panel(state: WatchState) -> Panel:
    if not state.event_log:
        return Panel(
            Align.center(Text("(waiting for events)", style="dim")),
            title="Live Event Stream",
            border_style="blue",
        )

    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(no_wrap=True, width=8)
    table.add_column(no_wrap=True, width=10)
    table.add_column(no_wrap=True, width=25)
    table.add_column(ratio=1)

    for event in list(state.event_log)[-12:]:
        ts = datetime.fromtimestamp(
            event.get("_received_at") or time.time(),
        ).strftime("%H:%M:%S")
        sid = (event.get("session_id") or "?")[:8]
        etype = str(event.get("type") or "?")

        # Pretty colour by type.
        col = "white"
        if etype == "consortium_completed":
            col = "green" if event.get("status") == "ok" else "yellow"
        elif etype == "consortium_started":
            col = "cyan"
        elif etype == "consortium_cancelled":
            col = "yellow"
        elif etype == "consortium_error":
            col = "red"
        elif etype == "consortium_gate":
            gate = event.get("gate") or {}
            col = {"passed": "green", "passed_warn": "yellow",
                   "failed": "red"}.get(gate.get("status"), "white")
        elif etype.startswith("consortium:"):
            col = "magenta"

        # Build the human-readable message column.
        if etype == "consortium_gate":
            gate = event.get("gate") or {}
            msg = (f"{gate.get('phase', '?')} gate · "
                   f"{gate.get('status', '?')} · score={gate.get('score', '?')}")
        elif etype == "consortium_phase_start":
            msg = f"phase start: {event.get('phase', '?')}"
        elif etype == "consortium_phase_complete":
            msg = f"phase complete: {event.get('phase', '?')}"
        elif etype.startswith("consortium:"):
            inner = etype.split(":", 2)
            phase = inner[1] if len(inner) > 1 else "?"
            sub = inner[2] if len(inner) > 2 else "?"
            msg = f"{phase} · {sub}"
        else:
            msg = json.dumps(
                {k: v for k, v in event.items()
                 if k not in {"_channel", "_received_at", "session_id",
                              "event_id", "ts", "type"}},
                ensure_ascii=False,
            )[:80]
        table.add_row(
            Text(ts, style="dim"),
            Text(sid, style="bold"),
            Text(etype, style=col),
            Text(msg, style="dim"),
        )
    return Panel(table, title="Live Event Stream (Redis pub/sub)",
                 border_style="blue")


def render_errors_panel(state: WatchState) -> Panel:
    if not state.error_log:
        return Panel(
            Align.center(Text("(no errors in last 30s)", style="green")),
            title="Recent Errors / Warnings",
            border_style="green",
        )
    body = Text()
    for line in list(state.error_log)[-6:]:
        body.append(line + "\n", style="red")
    return Panel(body, title="Recent Errors / Warnings",
                 border_style="red")


def render_footer(state: WatchState) -> Panel:
    uptime = _humanise_age(time.time() - state.started_at)
    last_tick_color = (
        "green" if state.last_tick_ms < 50
        else "yellow" if state.last_tick_ms < 200 else "red"
    )
    text = Text()
    text.append(" AMOR live watch ", style="bold reverse")
    text.append(f" · uptime {uptime}", style="dim")
    text.append(f" · ticks {state.tick_count}", style="dim")
    text.append(f" · loop ", style="dim")
    text.append(f"{state.last_tick_ms:.0f}ms", style=last_tick_color)
    if state.last_error:
        text.append(f"  ⚠ {state.last_error[:80]}", style="yellow")
    text.append("    q quit", style="dim")
    return Panel(text, border_style="dim")


# ─── render loop ───────────────────────────────────────────────────────────


def build_layout(state: WatchState) -> Layout:
    """Compose the full screen layout.

    v8 — adds a full-width Active Pipeline hero panel at the top so the
    user sees overall progress %, phase chips and ETA at a glance
    without hunting through the multi-column session table."""
    layout = Layout()
    layout.split_column(
        Layout(name="hero", size=12),
        Layout(name="top", size=8),
        Layout(name="middle", ratio=2),
        Layout(name="bottom", ratio=1),
        Layout(name="footer", size=3),
    )
    layout["top"].split_row(
        Layout(name="health", ratio=2),
        Layout(name="other", ratio=1),
        Layout(name="ollama", ratio=2),
    )
    layout["middle"].split_row(
        Layout(name="consortium", ratio=2),
        Layout(name="events", ratio=2),
    )

    layout["hero"].update(render_active_pipeline_hero(state))
    layout["health"].update(render_health_panel(state))
    layout["other"].update(render_other_modes_panel(state))
    layout["ollama"].update(render_ollama_panel(state))
    layout["consortium"].update(render_consortium_panel(state))
    layout["events"].update(render_event_log_panel(state))
    layout["bottom"].update(render_errors_panel(state))
    layout["footer"].update(render_footer(state))
    return layout


async def render_loop(state: WatchState, console: Console,
                      refresh_per_sec: float = 4.0) -> None:
    """The Rich Live render coroutine. Re-renders ``refresh_per_sec``
    times per second from the shared ``state``."""
    period = 1.0 / max(refresh_per_sec, 1.0)
    with Live(
        build_layout(state),
        console=console,
        refresh_per_second=refresh_per_sec,
        screen=True,
    ) as live:
        while not state.stop.is_set():
            t0 = time.time()
            try:
                live.update(build_layout(state))
            except Exception as exc:
                state.last_error = f"render: {exc}"
            state.tick_count += 1
            state.last_tick_ms = (time.time() - t0) * 1000
            try:
                await asyncio.wait_for(state.stop.wait(), timeout=period)
            except asyncio.TimeoutError:
                pass


# ─── entry ─────────────────────────────────────────────────────────────────


async def amain(args: argparse.Namespace) -> int:
    console = Console()
    state = WatchState()

    # Wire SIGINT / SIGTERM to a single shutdown event.
    def _shutdown(*_a):  # noqa: ANN001
        state.stop.set()

    with contextlib.suppress(NotImplementedError):
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, _shutdown)
        with contextlib.suppress(AttributeError):
            loop.add_signal_handler(signal.SIGTERM, _shutdown)

    # Connect Redis (with retry).
    redis_client: aioredis.Redis | None = None
    for attempt in range(5):
        try:
            redis_client = aioredis.from_url(
                f"redis://{args.redis}",
                decode_responses=False,
                socket_connect_timeout=3.0,
            )
            await redis_client.ping()
            break
        except Exception as exc:
            state.last_error = f"redis connect: {exc}"
            await asyncio.sleep(2 ** attempt)
    if redis_client is None:
        console.print("[red]Could not connect to Redis. Aborting.[/red]")
        return 2

    async with httpx.AsyncClient(timeout=10.0) as client:
        tasks = [
            asyncio.create_task(poll_health(state, client, args.base)),
            asyncio.create_task(poll_containers(state)),
            asyncio.create_task(
                poll_consortium_sessions(state, redis_client, args.base, client),
            ),
            asyncio.create_task(poll_ollama(state, client, args.ollama)),
            asyncio.create_task(tail_ollama_logs(state)),
            asyncio.create_task(
                subscribe_consortium_events(state, redis_client),
            ),
            asyncio.create_task(tail_app_errors(state)),
            asyncio.create_task(
                render_loop(state, console, args.refresh),
            ),
        ]
        try:
            await state.stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await redis_client.aclose()
    return 0


def main() -> int:
    # Windows cp1252 stdout can't encode the ✓ ◐ █ etc. glyphs the UI
    # uses everywhere. Force UTF-8 on stdout/stderr where supported
    # (Python 3.7+); harmless on Linux/macOS (already UTF-8).
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(
        prog="watch_live",
        description="AMOR real-time multi-pane live monitor (Rich TUI)",
    )
    p.add_argument(
        "--base", default=os.getenv("AMOR_BASE", "http://localhost:8000"),
        help="AMOR API base URL (default: http://localhost:8000)",
    )
    p.add_argument(
        "--redis", default=os.getenv("AMOR_REDIS", "localhost:6379"),
        help="Redis host:port (default: localhost:6379)",
    )
    p.add_argument(
        "--ollama", default=os.getenv("OLLAMA_BASE_URL",
                                       "http://localhost:11434"),
        help="Ollama base URL",
    )
    p.add_argument(
        "--refresh", type=float, default=4.0,
        help="UI refresh rate (Hz, default: 4)",
    )
    args = p.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
