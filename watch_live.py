#!/usr/bin/env python3
"""
watch_live.py — Amor stack için kompakt TUI tarzı canlı izleme paneli.

Tek bir terminal ekranına sığar — yan yana paneller (multi-column layout).

Görüntüsü (yaklaşık 38 satır × 200+ kolon):
  • Header bar
  • Overall Progress hero (yüzde + faz + ETA + son LLM bir arada)
  • 2-COLUMN: [Stack + Sub-Questions]    |   [Resources + Top Domains + Throughput]
  • Errors / Warnings (son 10 dk)

Salt-okunur. Hiçbir servisi modify etmez.
Bağımlılık: yok — Python 3 stdlib + docker CLI.

Kullanım:
    python watch_live.py            # 2 saniyede yenile (varsayılan)
    python watch_live.py 1          # 1 saniyede (agresif)
    Ctrl+C ile çık.
"""

from __future__ import annotations

import atexit
import json
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# ─── Tema (Tokyo Night esinli, 256-color) ────────────────────────────────────
def _fg(c: int) -> str: return f"\033[38;5;{c}m"

class T:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    BORDER  = _fg(60)
    DIMTEXT = _fg(245)
    MUTED   = _fg(67)
    FG      = _fg(254)
    HEAD    = _fg(117)
    OK      = _fg(150)
    WARN    = _fg(222)
    ERR     = _fg(210)
    INFO    = _fg(111)
    HILITE  = _fg(141)
    ACCENT  = _fg(176)

PHASE_COLOR = {
    "planning":     _fg(117),
    "gathering":    _fg(111),
    "analyzing":    _fg(141),
    "synthesizing": _fg(150),
    "understand":   _fg(117), "decompose": _fg(111),
    "explore":      _fg(141), "evaluate":  _fg(222),
    "synthesize":   _fg(150), "critique":  _fg(141),
}

PHASE_ORDER = ["planning", "gathering", "analyzing", "synthesizing"]
PHASE_SHORT = {"planning": "Plan", "gathering": "Gather",
               "analyzing": "Analyze", "synthesizing": "Synth"}

# ─── Consortium pipeline phases ──────────────────────────────────────────────
CONSORTIUM_PHASE_ORDER = ["scope", "research", "thinking", "implementation"]
CONSORTIUM_PHASE_SHORT = {
    "scope": "Scope", "research": "Research",
    "thinking": "Think", "implementation": "Implement",
}
# Per-phase "weight" so a multi-LLM-call thinking phase counts more than
# a sub-second scope. Used to compute Overall Progress %.
CONSORTIUM_PHASE_WEIGHT = {
    "scope": 5, "research": 25, "thinking": 35, "implementation": 35,
}
# Thinking + Implementation have inner phases too — we surface the
# ones the engines emit ("understand → decompose → explore → evaluate
# → synthesize → critique" for thinking; "triage → plan → implement
# → execute → analyze → test → debug → review" for code intelligence).
THINKING_INNER_ORDER = [
    "understand", "decompose", "explore", "evaluate",
    "synthesize", "critique",
]
CODE_INNER_ORDER = [
    "triage", "model_prep", "plan", "implement",
    "execute", "analyze", "test", "debug", "review",
]

# ─── ANSI control ────────────────────────────────────────────────────────────
HOME, CLEAR_LINE, CLEAR_DOWN = "\033[H", "\033[K", "\033[J"
ALT_ON, ALT_OFF = "\033[?1049h", "\033[?1049l"
HIDE_CURSOR, SHOW_CURSOR = "\033[?25l", "\033[?25h"


# ─── ANSI string helpers ─────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\033\[[\d;?]*[a-zA-Z]")

def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)

def vlen(s: str) -> int:
    return len(strip_ansi(s))

def truncate_v(s: str, width: int) -> str:
    if vlen(s) <= width:
        return s
    out, count, i = [], 0, 0
    while i < len(s) and count < width:
        if s[i] == "\033":
            j = i + 1
            while j < len(s) and not s[j].isalpha():
                j += 1
            if j < len(s):
                out.append(s[i:j + 1]); i = j + 1
            else:
                break
        else:
            out.append(s[i]); count += 1; i += 1
    out.append(T.RESET)
    return "".join(out)

def pad_v(s: str, width: int) -> str:
    diff = width - vlen(s)
    return s + " " * diff if diff > 0 else truncate_v(s, width)


# ─── Box drawing ─────────────────────────────────────────────────────────────
def box_top(title: str, width: int, accent: str = T.HEAD) -> str:
    vt = f" {title} "
    fill = max(0, width - 3 - len(vt))
    return f"{T.BORDER}╭─{accent}{T.BOLD}{vt}{T.RESET}{T.BORDER}{'─' * fill}╮{T.RESET}"

def box_bot(width: int) -> str:
    return f"{T.BORDER}╰{'─' * (width - 2)}╯{T.RESET}"

def box_line(content: str, width: int) -> str:
    inner = width - 4
    content = pad_v(content, inner)
    return f"{T.BORDER}│{T.RESET} {content} {T.BORDER}│{T.RESET}"

def panel(title: str, lines: list[str], width: int, accent: str = T.HEAD) -> list[str]:
    """Wrap content lines in a box of fixed width. Each output line is exactly width chars."""
    return [box_top(title, width, accent)] + [box_line(ln, width) for ln in lines] + [box_bot(width)]


# ─── Bars & sparkline ────────────────────────────────────────────────────────
def big_bar(pct: float, width: int) -> str:
    pct = max(0.0, min(100.0, float(pct or 0)))
    pct_str = f"{pct:5.1f}%"
    inner = max(4, width - len(pct_str) - 1)
    fill = int(inner * pct / 100)
    col = T.OK if pct >= 67 else (T.WARN if pct >= 33 else T.ACCENT)
    return f"{col}{'█' * fill}{T.MUTED}{'░' * (inner - fill)}{T.RESET} {T.BOLD}{col}{pct_str}{T.RESET}"

def mini_bar(value: float, max_value: float, width: int = 14, color: str = T.HEAD) -> str:
    if max_value <= 0:
        return f"{T.MUTED}{'─' * width}{T.RESET}"
    fill = int(width * value / max_value)
    return f"{color}{'█' * fill}{T.MUTED}{'░' * (width - fill)}{T.RESET}"

_SPARK = "▁▂▃▄▅▆▇█"

def sparkline(values: list[float], color: str = T.HEAD) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return color + _SPARK[3] * len(values) + T.RESET
    out = [_SPARK[int((v - lo) / (hi - lo) * (len(_SPARK) - 1))] for v in values]
    return color + "".join(out) + T.RESET


# ─── Time helpers ────────────────────────────────────────────────────────────
def hms(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h: return f"{h}h{m:02d}m"
    if m: return f"{m}m{s:02d}s"
    return f"{s}s"

def parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ─── Subprocess wrapper ──────────────────────────────────────────────────────
def sh(cmd, timeout: int = 8) -> tuple[int, str]:
    try:
        if isinstance(cmd, str):
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


# ─── Cache ───────────────────────────────────────────────────────────────────
class Cache:
    def __init__(self): self.data = {}
    def get(self, key, ttl, fetcher):
        now = time.time()
        if key in self.data:
            ts, val = self.data[key]
            if now - ts < ttl:
                return val
        val = fetcher()
        self.data[key] = (now, val)
        return val

CACHE = Cache()


# ─── Data sources ────────────────────────────────────────────────────────────
def list_keys(pattern: str) -> list[str]:
    rc, out = sh(["docker", "exec", "amor-redis-1", "redis-cli", "--scan", "--pattern", pattern])
    return [k.strip() for k in out.splitlines() if k.strip()] if rc == 0 else []

def get_session(key: str) -> dict | None:
    rc, out = sh(["docker", "exec", "amor-redis-1", "redis-cli", "GET", key])
    if rc != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except Exception:
        return None

def container_status() -> list[tuple[str, str]]:
    rc, out = sh(["docker", "ps", "--filter", "name=amor", "--format", "{{.Names}}|{{.Status}}"])
    rows = []
    if rc == 0:
        for ln in out.splitlines():
            p = ln.strip().split("|")
            if len(p) == 2:
                rows.append((p[0], p[1]))
    return rows

_NAME_RE = re.compile(r"^(.+?)-(\d+)$")

def _replicated_bases() -> set[str]:
    seen, repl = {}, set()
    for name, _ in container_status():
        s = name.removeprefix("amor-")
        m = _NAME_RE.match(s)
        if m:
            base = m.group(1)
            seen[base] = seen.get(base, 0) + 1
            if seen[base] > 1:
                repl.add(base)
    return repl

def short_name(name: str, replicated: set[str]) -> str:
    s = name.removeprefix("amor-")
    m = _NAME_RE.match(s)
    if m:
        base = m.group(1)
        return s if base in replicated else base
    return s

def _container_stats() -> dict:
    rc, out = sh(["docker", "stats", "--no-stream", "--format",
                  "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}"], timeout=8)
    res = {}
    if rc != 0:
        return res
    for ln in out.splitlines():
        p = ln.split("|")
        if len(p) == 4:
            res[p[0].strip()] = {"cpu": p[1].strip(), "mem": p[2].strip(), "memp": p[3].strip()}
    return res

def container_stats() -> dict:
    return CACHE.get("stats", 6.0, _container_stats)


def _ollama_history() -> list[tuple[str, float]]:
    rc, out = sh(["docker", "logs", "--tail", "300", "amor-ollama"])
    if rc != 0:
        return []
    pat = re.compile(r"(\d{2}:\d{2}:\d{2}).*?\|\s*\d+\s*\|\s*([\d.µms]+)\s*\|.*POST.*generate")
    hits = []
    for ln in out.splitlines():
        m = pat.search(ln)
        if m:
            hits.append((m.group(1), _parse_dur(m.group(2))))
    return hits

def ollama_history() -> list[tuple[str, float]]:
    return CACHE.get("ollama_hist", 2.0, _ollama_history)

def _parse_dur(s: str) -> float:
    s = s.replace("µs", "us")
    total = 0.0
    m = re.search(r"(\d+)m(?!s)", s)
    if m: total += int(m.group(1)) * 60
    m = re.search(r"([\d.]+)s", s)
    if m: total += float(m.group(1))
    elif re.search(r"([\d.]+)ms", s):
        total += float(re.search(r"([\d.]+)ms", s).group(1)) / 1000
    elif re.search(r"([\d.]+)us", s):
        total += float(re.search(r"([\d.]+)us", s).group(1)) / 1_000_000
    return total

def ollama_calls_in(minutes: int) -> int:
    rc, out = sh(["docker", "logs", "--since", f"{minutes}m", "amor-ollama"])
    return sum(1 for ln in out.splitlines() if "POST" in ln and "generate" in ln) if rc == 0 else 0


def _recent_errors() -> list[tuple[str, str]]:
    keywords = re.compile(r"\b(ERROR|Exception|Traceback|FATAL|CRITICAL|UNRECOVERABLE)\b", re.IGNORECASE)
    skip = re.compile(r"GET /metrics|GET /health|GET /api/tags|favicon")
    found = []
    for c in ["amor-app-1", "amor-app-2", "amor-ollama"]:
        rc, out = sh(["docker", "logs", "--since", "10m", c])
        if rc != 0:
            continue
        for ln in out.splitlines():
            if skip.search(ln):
                continue
            if keywords.search(ln):
                found.append((c.replace("amor-", ""), ln.strip()[:200]))
    return found

def recent_errors() -> list[tuple[str, str]]:
    return CACHE.get("errors", 5.0, _recent_errors)


# ─── Live activity (timestamped log tail) ────────────────────────────────────

# Skip these noisy log entries — they don't tell us anything useful.
_NOISE = re.compile(
    r"GET /metrics|GET /health|GET /api/tags|favicon|HEAD /|GET /static/"
)

# Pre-compiled patterns for speed.
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2}))\.[\d]+Z?\s+(.*)")
_OLLAMA_RE = re.compile(
    r"\d{2}:\d{2}:\d{2}\s*\|\s*(\d+)\s*\|\s*([\d.µms]+)\s*\|.*?(POST|GET)\s+\"?/api/(\w+)"
)
_SEARCH_RE = re.compile(
    r"search_web:\s*(\d+\s*results|all engines[^']*)\s*for\s+['\"](.+?)['\"]"
)
_HTTP_RE = re.compile(r'"(\w+)\s+([^\s"]+)\s+HTTP/[\d.]+"\s+(\d+)')
_JSON_EVENT_RE = re.compile(r'"event":\s*"([^"]+)"')
_PHASE_RE = re.compile(r'"(phase_(?:start|complete|failed))".*?"phase":\s*"([^"]+)"')


def _parse_activity_line(short: str, ln: str) -> tuple[str, str, str, str, str] | None:
    """
    Returns (sortable_iso_ts, hms, source, kind, detail) or None to skip.
    `kind` is one of: llm, web, api, evt, sys.
    """
    if _NOISE.search(ln):
        return None
    m_ts = _TS_RE.match(ln)
    if not m_ts:
        return None
    iso, hms_s, rest = m_ts.group(1), m_ts.group(2), m_ts.group(3)

    # Ollama POST /api/generate (or chat)
    if short == "ollama":
        m = _OLLAMA_RE.search(rest)
        if m:
            status, dur_raw, _meth, path = m.groups()
            secs = _parse_dur(dur_raw)
            return (iso, hms_s, "ollama", "llm",
                    f"{path:9s}  {secs:6.1f}s  → {status}")
        return None

    # Phase events (from JSON-structured logs)
    m_ph = _PHASE_RE.search(rest)
    if m_ph:
        ev_type, phase = m_ph.groups()
        ev_short = ev_type.replace("phase_", "")
        return (iso, hms_s, short, "evt", f"phase.{ev_short:9s}  {phase}")

    # search_web custom log
    m = _SEARCH_RE.search(rest)
    if m:
        cnt_str = m.group(1)
        if "results" in cnt_str:
            cnt = cnt_str.split()[0]
            return (iso, hms_s, short, "web", f"{cnt:>3s} sonuç  {m.group(2)[:60]}")
        # fallback
        return (iso, hms_s, short, "web", f"{'fb':>3s} sonuç  {m.group(2)[:60]}")

    # HTTP request lines (uvicorn access log)
    m = _HTTP_RE.search(rest)
    if m:
        method, path, status = m.groups()
        # Only show /api/* paths (skip static + asset noise we missed).
        if "/api/" not in path:
            return None
        return (iso, hms_s, short, "api", f"{method:5s} {path[:70]}  → {status}")

    # Custom JSON event line — pull out the "event" field if any.
    m = _JSON_EVENT_RE.search(rest)
    if m:
        ev = m.group(1)
        # Ignore very chatty / noisy events
        if ev in {"cache_connected", "postgres_connected", "mongodb_connected"}:
            return None
        return (iso, hms_s, short, "evt", ev)

    return None


def _recent_activity(n_max: int = 30) -> list[tuple[str, str, str, str, str]]:
    """
    Aggregate timestamped log tails from key containers, parse interesting
    events, sort by timestamp, return last n_max.
    """
    events: list[tuple[str, str, str, str, str]] = []
    for c in ["amor-app-1", "amor-app-2", "amor-ollama"]:
        rc, out = sh(["docker", "logs", "--timestamps", "--since", "5m", c])
        if rc != 0:
            continue
        short = c.replace("amor-", "").removesuffix("-1")
        for ln in out.splitlines()[-200:]:  # cap per-container to keep it cheap
            ev = _parse_activity_line(short, ln)
            if ev:
                events.append(ev)
    events.sort(key=lambda e: e[0])
    return events[-n_max:]


def recent_activity(n_max: int = 30):
    return CACHE.get(f"activity_{n_max}", 1.5, lambda: _recent_activity(n_max))


# ─── Sessions ────────────────────────────────────────────────────────────────
def find_active_research() -> dict | None:
    keys = list_keys("local_ai_research_session:*")
    for k in keys:
        d = get_session(k)
        if d and d.get("status") in ("started", "in_progress"):
            return d
    if keys:
        return get_session(keys[-1])
    return None


def find_active_consortium() -> dict | None:
    """Pick the most-active consortium session (preferring `started`)."""
    keys = list_keys("consortium_session:*")
    active = []
    others = []
    for k in keys:
        d = get_session(k)
        if not d:
            continue
        # Tag the type so the renderer can branch.
        d["_kind"] = "consortium"
        if d.get("status") in ("started", "running"):
            active.append(d)
        else:
            others.append(d)
    # Prefer running sessions, sorted by most-recent heartbeat.
    if active:
        active.sort(
            key=lambda d: d.get("last_heartbeat_at") or d.get("started_at") or "",
            reverse=True,
        )
        return active[0]
    if others:
        others.sort(
            key=lambda d: d.get("started_at") or "",
            reverse=True,
        )
        return others[0]
    return None


def find_active() -> dict | None:
    """Unified active-session finder. Prefers running consortium > running
    research > most-recent of either. Returned dict has ``_kind`` set to
    ``"consortium"`` or ``"research"`` for the renderer."""
    cons = find_active_consortium()
    res = find_active_research()
    if cons and cons.get("status") in ("started", "running"):
        return cons
    if res and res.get("status") in ("started", "in_progress"):
        if res.get("_kind") is None:
            res["_kind"] = "research"
        return res
    # Both done — return whichever finished more recently.
    cands = [d for d in (cons, res) if d]
    if not cands:
        return None
    if len(cands) == 1:
        d = cands[0]
        if d.get("_kind") is None:
            d["_kind"] = "research"
        return d
    # Pick by most-recent timestamp.
    def _ts(d):
        return (d.get("completed_at") or d.get("last_heartbeat_at")
                or d.get("started_at") or "")
    cands.sort(key=_ts, reverse=True)
    d = cands[0]
    if d.get("_kind") is None:
        d["_kind"] = "research"
    return d


# ─── Consortium-specific helpers ─────────────────────────────────────────────
def consortium_progress_pct(d: dict) -> float:
    """Return 0–100 progress derived from phase weights + inner-phase
    progress for the active phase (if any)."""
    phases = d.get("phases") or []
    total_weight = sum(CONSORTIUM_PHASE_WEIGHT.values())
    accumulated = 0.0
    for p in phases:
        name = p.get("name") or ""
        status = p.get("status") or "pending"
        weight = CONSORTIUM_PHASE_WEIGHT.get(name, 0)
        if status == "completed":
            accumulated += weight
        elif status == "in_progress":
            # Inner phase progress estimate (from the engine's nested events
            # if we tracked them; otherwise 50%).
            inner_pct = _consortium_inner_pct(d, name)
            accumulated += weight * inner_pct
    return min(100.0, (accumulated / total_weight) * 100.0)


def _consortium_inner_pct(d: dict, phase_name: str) -> float:
    """0.0–1.0 estimated inner-phase completion for a phase that's
    `in_progress`. Reads the ``current_task`` heuristically; falls back
    to 50%."""
    task = (d.get("current_task") or "").lower()
    if phase_name == "thinking":
        order = THINKING_INNER_ORDER
    elif phase_name == "implementation":
        order = CODE_INNER_ORDER
    else:
        return 0.5
    # See if any inner phase name appears in the current task string.
    for i, name in enumerate(order):
        if name in task:
            return (i + 0.5) / len(order)
    return 0.4  # fallback — between 0% and 50%


def consortium_phase_chips(d: dict) -> str:
    """Inline phase chips for the 4 consortium phases."""
    phases = d.get("phases") or []
    by_name = {p.get("name"): p for p in phases}
    cur = d.get("current_phase")
    out = []
    for p in CONSORTIUM_PHASE_ORDER:
        col = PHASE_COLOR.get(p, T.HILITE)
        label = CONSORTIUM_PHASE_SHORT.get(p, p)
        meta = by_name.get(p) or {}
        st = meta.get("status") or "pending"
        if st == "completed":
            out.append(f"{T.OK}✓{T.RESET} {col}{label}{T.RESET}")
        elif st == "in_progress" or p == cur:
            out.append(f"{T.WARN}{T.BOLD}◉ {label.upper()}{T.RESET}")
        elif st == "failed":
            out.append(f"{T.ERR}✗ {label}{T.RESET}")
        elif st == "skipped":
            out.append(f"{T.MUTED}⤳ {label}{T.RESET}")
        else:
            out.append(f"{T.MUTED}⋯ {label}{T.RESET}")
    return f"  {T.DIMTEXT}|{T.RESET}  ".join(out)


def consortium_inner_chips(d: dict) -> str:
    """Inner-phase chips for the active consortium phase. Empty when
    the active phase has no known inner steps."""
    cur = d.get("current_phase")
    task = (d.get("current_task") or "").lower()
    if cur == "thinking":
        order = THINKING_INNER_ORDER
    elif cur == "implementation":
        order = CODE_INNER_ORDER
    else:
        return ""
    # Find active inner step.
    active_idx = -1
    for i, name in enumerate(order):
        if name in task:
            active_idx = i
            break
    out = []
    for i, name in enumerate(order):
        col = T.MUTED if active_idx < 0 else (
            T.OK if i < active_idx
            else T.WARN if i == active_idx
            else T.MUTED
        )
        if active_idx == i:
            out.append(f"{col}{T.BOLD}◉ {name}{T.RESET}")
        elif active_idx >= 0 and i < active_idx:
            out.append(f"{T.OK}✓{T.RESET} {T.DIMTEXT}{name}{T.RESET}")
        else:
            out.append(f"{T.MUTED}⋯ {name}{T.RESET}")
    return "  ".join(out)


def consortium_eta(d: dict) -> tuple[str, float] | None:
    """Estimate remaining time using LLM call history."""
    pct = consortium_progress_pct(d)
    if pct <= 0 or pct >= 99:
        return None
    started = parse_iso(d.get("started_at"))
    if not started:
        return None
    elapsed = time.time() - started
    if elapsed < 10:
        return None
    # Crude linear extrapolation: if X% took Y seconds, the remaining
    # (100-X)% takes Y * (100-X) / X seconds.
    remaining = elapsed * (100 - pct) / max(pct, 1)
    return (f"~{hms(remaining)}", remaining)


# ─── Phase helpers ───────────────────────────────────────────────────────────
def phase_chips(d: dict) -> str:
    """Inline phase chips: ✓ Plan  ✓ Gather  ◉ ANALYZE  ⋯ Synth"""
    last = d.get("last_completed_phase")
    cur = d.get("current_phase")
    completed = set()
    if last:
        for p in PHASE_ORDER:
            completed.add(p)
            if p == last:
                break
    out = []
    for p in PHASE_ORDER:
        col = PHASE_COLOR.get(p, T.FG)
        label = PHASE_SHORT.get(p, p)
        if p in completed and p != cur:
            out.append(f"{T.OK}✓{T.RESET} {col}{label}{T.RESET}")
        elif p == cur:
            out.append(f"{T.WARN}{T.BOLD}◉ {label.upper()}{T.RESET}")
        else:
            out.append(f"{T.MUTED}⋯ {label}{T.RESET}")
    return f"  {T.DIMTEXT}|{T.RESET}  ".join(out)


# ─── ETA ─────────────────────────────────────────────────────────────────────
def compute_eta_seconds(d: dict) -> tuple[str, float] | None:
    task = d.get("current_task") or ""
    m = re.search(r"source\s+(\d+)\s*/\s*(\d+)", task, re.IGNORECASE)
    if not m:
        return None
    idx, total = int(m.group(1)), int(m.group(2))
    remaining = max(0, total - idx)
    hist = ollama_history()
    if len(hist) < 3:
        return None
    durations = [d for _, d in hist[-12:]]
    avg = sum(durations) / len(durations)
    sec = remaining * avg
    synth = 240
    return (f"~{hms(sec)} analyze + {hms(synth)} synth = {hms(sec + synth)}", sec + synth)


# ─── Panels (each returns lines of fixed width) ─────────────────────────────
def header_panel(width: int, refresh: int) -> list[str]:
    now = datetime.now().strftime("%H:%M:%S · %Y-%m-%d")
    rows = container_status()
    up = sum(1 for _, s in rows if s.startswith("Up"))
    healthy = sum(1 for _, s in rows if "healthy" in s.lower())
    total = len(rows) or 1
    health = (f"{T.OK}● {up}/{total} up · {healthy} healthy{T.RESET}"
              if up == total else f"{T.ERR}● {up}/{total} up{T.RESET}")
    body = (f"{T.HEAD}{T.BOLD}AMOR · LIVE WATCH{T.RESET}    "
            f"{T.DIMTEXT}{now}{T.RESET}    {health}    "
            f"{T.DIMTEXT}refresh {refresh}s · Ctrl+C ile çık{T.RESET}")
    return panel("watch", [body], width)


def progress_hero(d: dict | None, width: int) -> list[str]:
    if not d:
        return panel(
            "Overall Progress",
            [f"{T.DIMTEXT}Aktif session yok (research veya consortium başlatılmamış).{T.RESET}"],
            width, accent=T.HILITE,
        )
    if d.get("_kind") == "consortium":
        return _progress_hero_consortium(d, width)
    return _progress_hero_research(d, width)


def _progress_hero_research(d: dict, width: int) -> list[str]:
    sid = (d.get("session_id") or "?")[:8]
    topic = d.get("topic") or d.get("prompt") or "?"
    depth = d.get("depth") or "?"
    user_id = (d.get("user_id") or "?")[:8]
    started = parse_iso(d.get("started_at"))
    elapsed = (time.time() - started) if started else 0
    progress = d.get("progress", 0) or 0
    task = d.get("current_task") or "—"

    # last LLM stats
    hist = ollama_history()
    last_llm = ""
    if hist:
        t, dur = hist[-1]
        last5 = ollama_calls_in(5)
        avg20 = sum(d for _, d in hist[-20:]) / min(20, len(hist))
        last_llm = (f"Last LLM: {T.OK}{t}{T.RESET} {T.WARN}{dur:.1f}s{T.RESET}  ·  "
                    f"Avg(20): {T.HEAD}{avg20:.1f}s{T.RESET}  ·  "
                    f"5min: {T.OK}{last5}{T.RESET} ({last5/5:.1f}/dk)")

    eta_str = ""
    eta_data = compute_eta_seconds(d)
    if eta_data:
        eta_str = f"ETA: {T.HEAD}{eta_data[0]}{T.RESET}"

    inner = width - 4

    lines = [
        f"{T.HEAD}{T.BOLD}{sid}{T.RESET}  {T.BOLD}{depth}{T.RESET}  {T.DIMTEXT}·{T.RESET}  "
        f"{T.BOLD}{topic[:inner-30]}{T.RESET}  {T.DIMTEXT}· user {user_id}{T.RESET}",
        f"Elapsed {T.HEAD}{hms(elapsed)}{T.RESET}  {T.DIMTEXT}·{T.RESET}  {phase_chips(d)}",
        f"{T.DIMTEXT}{task}{T.RESET}",
        big_bar(progress, inner),
        eta_str,
        last_llm,
    ]
    return panel("Overall Progress · research", lines, width, accent=T.HILITE)


def _progress_hero_consortium(d: dict, width: int) -> list[str]:
    """Render a consortium-aware Overall Progress panel.

    Layout (4 lines + bar + ETA + LLM stats):
      <sid> <status>  ·  <goal>
      Elapsed XXm  ·  Phase chips: ✓ Scope | ✓ Research | ◉ THINKING | ⋯ Implement
      Inner: ✓ understand  ✓ decompose  ◉ explore  ⋯ evaluate  ⋯ synthesize  ⋯ critique
      [████████░░░░] 62%
      ETA: ~5m
      Last LLM: 1m12s · Avg(20): 1m08s · 5min: 4 (0.8/dk) · Heartbeat 2.5s
    """
    sid = (d.get("session_id") or "?")[:8]
    scope = d.get("scope") or {}
    goal = scope.get("goal") or scope.get("title") or "?"
    depth = scope.get("depth") or "?"
    status = d.get("status") or "?"

    started = parse_iso(d.get("started_at"))
    elapsed = (time.time() - started) if started else 0
    pct = consortium_progress_pct(d)

    # Status colour.
    status_col = {
        "started": T.HEAD, "running": T.HEAD,
        "ok": T.OK, "cancelled": T.WARN,
        "interrupted": T.HILITE, "error": T.ERR,
    }.get(status, T.FG)

    # Heartbeat freshness — important signal that the bg task is alive.
    hb_str = ""
    hb = parse_iso(d.get("last_heartbeat_at"))
    if hb:
        age = time.time() - hb
        hb_col = T.OK if age < 30 else (T.WARN if age < 90 else T.ERR)
        hb_str = f"{hb_col}heartbeat {age:.0f}s{T.RESET}"

    # last LLM stats — same as the research path.
    hist = ollama_history()
    last_llm = ""
    if hist:
        t, dur = hist[-1]
        last5 = ollama_calls_in(5)
        avg20 = sum(d for _, d in hist[-20:]) / min(20, len(hist))
        last_llm = (f"Last LLM: {T.OK}{t}{T.RESET} {T.WARN}{dur:.1f}s{T.RESET}  ·  "
                    f"Avg(20): {T.HEAD}{avg20:.1f}s{T.RESET}  ·  "
                    f"5min: {T.OK}{last5}{T.RESET} ({last5/5:.1f}/dk)")

    eta_str = ""
    eta = consortium_eta(d)
    if eta:
        eta_str = f"ETA: {T.HEAD}{eta[0]}{T.RESET} (kalan ~{eta[1]/60:.0f} dk)"

    inner = width - 4

    # Verifications summary (passed / passed_warn / failed counts).
    verifs = d.get("verifications") or []
    if verifs:
        ok = sum(1 for v in verifs if v.get("status") == "passed")
        warn = sum(1 for v in verifs if v.get("status") == "passed_warn")
        fail = sum(1 for v in verifs if v.get("status") == "failed")
        verif_str = (f"  {T.DIMTEXT}·{T.RESET}  Gates: "
                     f"{T.OK}✓ {ok}{T.RESET} "
                     f"{T.WARN}⚠ {warn}{T.RESET} "
                     f"{T.ERR}✗ {fail}{T.RESET}")
    else:
        verif_str = ""

    inner_chips = consortium_inner_chips(d)

    head = (f"{T.HILITE}{T.BOLD}🏛 {sid}{T.RESET}  "
            f"{status_col}{T.BOLD}{status.upper()}{T.RESET}  "
            f"{T.DIMTEXT}·{T.RESET}  "
            f"{T.BOLD}{goal[:inner-40]}{T.RESET}  "
            f"{T.DIMTEXT}({depth}){T.RESET}")

    line2 = (f"Elapsed {T.HEAD}{hms(elapsed)}{T.RESET}  "
             f"{T.DIMTEXT}·{T.RESET}  {consortium_phase_chips(d)}{verif_str}")

    lines = [head, line2]
    if inner_chips:
        lines.append(f"  {T.DIMTEXT}Inner:{T.RESET} {inner_chips}")
    if d.get("current_task"):
        lines.append(f"{T.DIMTEXT}{(d.get('current_task') or '')[:inner]}{T.RESET}")
    lines.append(big_bar(pct, inner))
    if eta_str:
        lines.append(eta_str)
    bottom = last_llm
    if hb_str:
        bottom = (f"{bottom}  {T.DIMTEXT}·{T.RESET}  {hb_str}"
                  if bottom else hb_str)
    if bottom:
        lines.append(bottom)
    return panel("Overall Progress · consortium", lines, width, accent=T.HILITE)


def stack_compact_panel(width: int) -> list[str]:
    rows = container_status()
    if not rows:
        return panel("Stack", [f"{T.ERR}docker daemon yanıt vermiyor{T.RESET}"], width)
    repl = _replicated_bases()
    items = []
    for name, status in sorted(rows):
        short = short_name(name, repl)
        is_up = status.startswith("Up")
        is_h = "healthy" in status.lower()
        if is_h:
            items.append(f"{T.OK}●{T.RESET} {T.FG}{short:11s}{T.RESET}{T.OK}healthy{T.RESET}")
        elif is_up:
            items.append(f"{T.OK}●{T.RESET} {T.FG}{short:11s}{T.RESET}{T.DIMTEXT}up     {T.RESET}")
        else:
            items.append(f"{T.ERR}●{T.RESET} {T.FG}{short:11s}{T.RESET}{T.ERR}down   {T.RESET}")
    # 2 columns within the panel
    inner = width - 4
    each_w = inner // 2 - 1
    lines = []
    rowsN = (len(items) + 1) // 2
    for i in range(rowsN):
        a = items[i] if i < len(items) else ""
        b = items[i + rowsN] if (i + rowsN) < len(items) else ""
        lines.append(f"{pad_v(a, each_w)}  {pad_v(b, each_w)}")
    return panel("Stack", lines, width)


def resources_compact_panel(width: int) -> list[str]:
    stats = container_stats()
    if not stats:
        return panel("Resources", [f"{T.DIMTEXT}istatistik alınıyor…{T.RESET}"], width)
    repl = _replicated_bases()
    order = ["amor-ollama", "amor-app-1", "amor-app-2", "amor-redis-1",
             "amor-postgres-1", "amor-mongo-1", "amor-kafka-1",
             "amor-zookeeper-1", "amor-grafana-1", "amor-prometheus-1",
             "amor-gateway-1"]
    rows = []
    for name in order:
        s = stats.get(name)
        if not s:
            continue
        short = short_name(name, repl)
        cpu = s["cpu"]; memp = s["memp"]
        m = re.search(r"([\d.]+)", cpu)
        cpu_n = float(m.group(1)) if m else 0
        cpu_col = T.ERR if cpu_n > 80 else (T.WARN if cpu_n > 30 else T.OK)
        rows.append(
            f"{T.FG}{short:10s}{T.RESET} {cpu_col}CPU{cpu:>8s}{T.RESET} "
            f"{T.INFO}MEM{memp:>7s}{T.RESET}"
        )
    # 2 columns within the panel
    inner = width - 4
    each_w = inner // 2 - 1
    lines = []
    rowsN = (len(rows) + 1) // 2
    for i in range(rowsN):
        a = rows[i] if i < len(rows) else ""
        b = rows[i + rowsN] if (i + rowsN) < len(rows) else ""
        lines.append(f"{pad_v(a, each_w)}  {pad_v(b, each_w)}")
    return panel("Resources", lines, width)


def subq_panel(d: dict, width: int, limit: int | None = None) -> list[str]:
    sub_qs = d.get("sub_questions") or []
    sources = d.get("live_sources") or []
    if not sub_qs:
        return panel("Sub-Questions", [f"{T.DIMTEXT}—{T.RESET}"], width)
    sq_counts = Counter()
    for s in sources:
        if isinstance(s, dict):
            sq_counts[s.get("sub_question_index", -1)] += 1
    max_sq = max(sq_counts.values()) if sq_counts else 1
    inner = width - 4
    lines = []
    show = sub_qs if limit is None else sub_qs[:limit]
    for i, sq in enumerate(show):
        cnt = sq_counts.get(i, 0)
        label = sq if not isinstance(sq, dict) else sq.get("question", "?")
        bar = mini_bar(cnt, max_sq, 14, T.HEAD)
        prefix = f"{T.HEAD}{i+1:2d}{T.RESET} {bar} {T.OK}{cnt:3d}{T.RESET}  "
        avail = inner - vlen(prefix)
        text = label[:max(0, avail)] if avail > 0 else ""
        lines.append(prefix + f"{T.DIMTEXT}{text}{T.RESET}")
    if limit is not None and len(sub_qs) > limit:
        rest = len(sub_qs) - limit
        rest_total = sum(sq_counts.get(i, 0) for i in range(limit, len(sub_qs)))
        lines.append(f"{T.DIMTEXT}    + {rest} daha · toplam {rest_total} kaynak{T.RESET}")
    title = f"Sub-Questions ({len(sub_qs)} · {len(sources)} sources)"
    return panel(title, lines, width)


def domains_panel(d: dict, width: int, limit: int = 5) -> list[str]:
    sources = d.get("live_sources") or []
    if not sources:
        return panel("Top Domains", [f"{T.DIMTEXT}—{T.RESET}"], width)
    domains = Counter()
    for s in sources:
        if isinstance(s, dict):
            domains[s.get("domain", "?")] += 1
    top = domains.most_common(limit)
    if not top:
        return panel("Top Domains", [f"{T.DIMTEXT}—{T.RESET}"], width)
    mx = top[0][1]
    inner = width - 4
    lines = []
    for dom, cnt in top:
        bar = mini_bar(cnt, mx, 12, T.ACCENT)
        prefix = f"{bar} {T.OK}{cnt:3d}{T.RESET}  "
        avail = inner - vlen(prefix)
        text = dom[:max(0, avail)]
        lines.append(prefix + f"{T.FG}{text}{T.RESET}")
    return panel("Top Domains", lines, width)


def throughput_panel(width: int) -> list[str]:
    hist = ollama_history()
    if not hist:
        return panel("Throughput", [f"{T.DIMTEXT}henüz LLM çağrısı yok{T.RESET}"], width)
    last1 = ollama_calls_in(1)
    last5 = ollama_calls_in(5)
    durs = [d for _, d in hist]
    avg_all = sum(durs) / len(durs)
    last20 = durs[-20:]
    avg20 = sum(last20) / len(last20)
    spark = sparkline(last20, T.HEAD)
    lines = [
        f"{T.DIMTEXT}Toplam:{T.RESET} {T.BOLD}{T.FG}{len(hist):>3}{T.RESET}   "
        f"{T.DIMTEXT}1dk:{T.RESET} {T.BOLD}{last1:>2}{T.RESET}   "
        f"{T.DIMTEXT}5dk:{T.RESET} {T.BOLD}{last5:>2}{T.RESET} "
        f"{T.DIMTEXT}({last5/5:.1f}/dk){T.RESET}",
        f"{T.DIMTEXT}Avg(tüm):{T.RESET} {T.BOLD}{T.HEAD}{avg_all:.1f}s{T.RESET}   "
        f"{T.DIMTEXT}Avg(20):{T.RESET} {T.BOLD}{T.HEAD}{avg20:.1f}s{T.RESET}",
        f"{T.DIMTEXT}Spark:{T.RESET} {spark}",
        f"{T.DIMTEXT}Son LLM çağrıları:{T.RESET}",
    ]
    for t, dur in hist[-3:]:
        col = T.OK if dur < 20 else (T.WARN if dur < 45 else T.ERR)
        lines.append(f"  {T.DIMTEXT}{t}{T.RESET}  {T.MUTED}llm.generate{T.RESET}  {col}{dur:>6.1f}s{T.RESET}")
    return panel("Throughput", lines, width)


def live_activity_panel(width: int, n_events: int = 10) -> list[str]:
    """Real-time tail of interesting backend events with color-coded kinds."""
    events = recent_activity(n_max=max(n_events, 30))
    title = f"Live Activity (son 5 dk · n={n_events})"
    if not events:
        return panel(title, [f"{T.DIMTEXT}— aktivite yok —{T.RESET}"], width)

    # Take only the freshest n_events to fill the panel exactly.
    events = events[-n_events:]
    inner = width - 4
    lines: list[str] = []
    for _iso, hms_s, src, kind, detail in events:
        # Color-coded kind label (fixed width for alignment).
        if kind == "llm":
            tag = f"{T.HILITE}{T.BOLD}LLM{T.RESET}"
        elif kind == "web":
            tag = f"{T.INFO}{T.BOLD}WEB{T.RESET}"
        elif kind == "api":
            # color by status if present in detail tail "→ NNN"
            mst = re.search(r"→\s+(\d{3})", detail)
            if mst:
                code = int(mst.group(1))
                if code >= 500:
                    tag = f"{T.ERR}{T.BOLD}API{T.RESET}"
                elif code >= 400:
                    tag = f"{T.WARN}{T.BOLD}API{T.RESET}"
                else:
                    tag = f"{T.OK}{T.BOLD}API{T.RESET}"
            else:
                tag = f"{T.OK}{T.BOLD}API{T.RESET}"
        elif kind == "evt":
            tag = f"{T.ACCENT}{T.BOLD}EVT{T.RESET}"
        else:
            tag = f"{T.MUTED}{T.BOLD}---{T.RESET}"
        prefix = (
            f"{T.DIMTEXT}{hms_s}{T.RESET}  "
            f"{tag}  "
            f"{T.DIMTEXT}[{src:8s}]{T.RESET}  "
        )
        avail = inner - vlen(prefix)
        body = detail[:max(0, avail)] if avail > 0 else ""
        lines.append(prefix + f"{T.FG}{body}{T.RESET}")
    return panel(title, lines, width)


def errors_panel(width: int) -> list[str]:
    errs = recent_errors()
    if not errs:
        return panel("Errors / Warnings (son 10 dk)",
                     [f"{T.OK}✓ temiz · son 10 dakikada hata yok{T.RESET}"], width)
    lines = []
    for src, ln in errs[-3:]:
        inner = width - 4
        avail = inner - 6 - len(src)
        lines.append(f"{T.ERR}!{T.RESET} {T.DIMTEXT}[{src}]{T.RESET} {T.FG}{ln[:max(0, avail)]}{T.RESET}")
    return panel("Errors / Warnings (son 10 dk)", lines, width)


# ─── Multi-column layout ─────────────────────────────────────────────────────
def join_columns(cols: list[list[str]], col_widths: list[int], gap: int = 2) -> list[str]:
    """Render multiple panels side-by-side. Each column must have lines of fixed width."""
    max_h = max((len(c) for c in cols), default=0)
    out = []
    sep = " " * gap
    for r in range(max_h):
        parts = []
        for i, col in enumerate(cols):
            if r < len(col):
                line = col[r]
            else:
                line = ""
            parts.append(pad_v(line, col_widths[i]))
        out.append(sep.join(parts))
    return out


def stack_vertical(panels: list[list[str]], spacer: bool = True) -> list[str]:
    """Stack multiple panels vertically with optional blank-line spacer."""
    out = []
    for i, p in enumerate(panels):
        if i > 0 and spacer:
            out.append("")
        out += p
    return out


# ─── Compose full frame ──────────────────────────────────────────────────────
def build_frame(width: int, height: int, refresh: int) -> list[str]:
    out: list[str] = []
    # v8 — unified active-session finder. Picks consortium when one is
    # running, falls back to research, falls back to whichever finished
    # most recently. The renderer branches on `_kind` ("consortium" vs
    # "research") so both pipeline types light up the Overall Progress
    # hero with their own metrics.
    active = find_active()
    research = active if (active and active.get("_kind") == "research") else None
    is_compact = height < 42  # squeeze more if user has shorter terminal

    # 1. Header (full width)
    out += header_panel(width, refresh)

    # 2. Overall Progress hero (full width)
    out += progress_hero(active, width)

    # 3. Two-column main section
    if width >= 160 and research:
        gap = 2
        col_w_left = (width - gap) // 2
        col_w_right = width - col_w_left - gap

        # Trim sub-Q if compact terminal
        sq_panel_l = subq_panel(research, col_w_left, limit=10 if is_compact else None)

        left = stack_compact_panel(col_w_left) + sq_panel_l
        right = (resources_compact_panel(col_w_right)
                 + domains_panel(research, col_w_right, limit=5 if is_compact else 6)
                 + throughput_panel(col_w_right))
        out += join_columns([left, right], [col_w_left, col_w_right], gap)
    else:
        # Narrow / no session — stacked fallback
        out += stack_compact_panel(width)
        out += resources_compact_panel(width)
        if research:
            out += subq_panel(research, width)
            out += domains_panel(research, width, limit=6)
        out += throughput_panel(width)

    # 4. Errors (full width)
    out += errors_panel(width)

    # 5. Live Activity — adaptive: fill remaining vertical space.
    used = len(out)
    remaining = height - used - 1  # 1-row bottom margin so cursor doesn't bump
    if remaining >= 4:
        # 2 lines for borders + N event lines
        n_events = max(2, min(15, remaining - 2))
        out += live_activity_panel(width, n_events=n_events)

    return out


# ─── Renderer (no-flicker) ───────────────────────────────────────────────────
def render(refresh: int) -> None:
    size = shutil.get_terminal_size((100, 30))
    width = max(70, size.columns)
    height = max(20, size.lines)
    lines = build_frame(width, height, refresh)
    buf = [HOME]
    for ln in lines:
        ln = pad_v(ln, width)
        buf.append(ln + CLEAR_LINE + "\n")
    buf.append(CLEAR_DOWN)
    sys.stdout.write("".join(buf))
    sys.stdout.flush()


# ─── Lifecycle ───────────────────────────────────────────────────────────────
_alt_active = False

def _enter_alt():
    global _alt_active
    if _alt_active:
        return
    sys.stdout.write(ALT_ON + HIDE_CURSOR + "\033[2J" + HOME)
    sys.stdout.flush()
    _alt_active = True

def _leave_alt():
    global _alt_active
    if not _alt_active:
        return
    sys.stdout.write(SHOW_CURSOR + ALT_OFF + T.RESET)
    sys.stdout.flush()
    _alt_active = False

def _on_signal(*_):
    _leave_alt()
    sys.exit(0)


def main() -> None:
    refresh = 2
    if len(sys.argv) > 1:
        try:
            refresh = max(1, int(sys.argv[1]))
        except ValueError:
            pass
    atexit.register(_leave_alt)
    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except Exception:
        pass
    _enter_alt()
    try:
        while True:
            t0 = time.time()
            try:
                render(refresh)
            except Exception as exc:
                sys.stdout.write(HOME + f"{T.ERR}render error: {exc}{T.RESET}{CLEAR_LINE}\n")
                sys.stdout.flush()
            elapsed = time.time() - t0
            time.sleep(max(0.2, refresh - elapsed))
    except KeyboardInterrupt:
        pass
    finally:
        _leave_alt()


if __name__ == "__main__":
    main()
