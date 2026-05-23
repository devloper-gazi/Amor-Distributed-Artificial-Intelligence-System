"""
Sprint 0 canonical baseline runner.

Loads ``tests/baselines/sprint0_prompts.json``, dispatches each prompt
to the matching AMOR pipeline endpoint, captures per-prompt metrics
from the SSE stream, and persists the result to
``data/baselines/sprint0_<utc-iso>.jsonl`` plus
``data/baselines/sprint0_latest.json``.

Day 1 (this commit): mode dispatch + SSE capture + metrics + JSON dump.
Day 2 (next): Mistral-Small-3 judge with position-swap + 2-rubric.
Day 3 (after): ``/api/admin/baselines/latest`` endpoint + SolidJS table.

Acceptance (per Cycle C plan):
* 10/10 prompts complete without manual intervention
* Each row carries wall_clock_ms, first_token_ms, prompt_tokens,
  completion_tokens, peak_vram_mb, tool_calls, retries, mode, output
* JSON dump validates against ``tests/baselines/sprint0_schema.json``

Reuses the existing AMOR pipeline endpoints — does NOT touch any LLM
backend code (locked: stays on Ollama for Sprint 0; Sprint 1 swaps
behind ``AMOR_LLM_BACKEND`` flag).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional

import httpx

logger_name = __name__


# ─── mode dispatch ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ModeRoute:
    """Per-mode endpoint quartet.

    All four paths are appended to ``base_url`` (no leading-slash
    difference between Build/Research/Thinking).  ``body_builder``
    receives the prompt-record dict and returns the POST body for
    ``start_path``.
    """

    mode: str
    start_path: str
    events_path: Callable[[str], str]
    cancel_path: Callable[[str], str]
    body_builder: Callable[[Dict[str, Any]], Dict[str, Any]]
    # Keys whose presence in an SSE event signals that the assistant has
    # started emitting content.  AMOR's Build pipeline uses `code` (and
    # later `markdown`/`report`/`final` for the deliverable); Research
    # and Thinking emit `text`/`content`/`delta` chunks throughout.  We
    # match any of these to flip first_token_ms off the default None.
    text_event_keys: tuple[str, ...] = (
        "text", "content", "delta",
        "code",      # Build code_ready event
        "patch",     # Build search/replace debugger event
        "review",    # Build review_ready event
    )
    done_event_types: tuple[str, ...] = (
        "done",
        "complete",
        "deliverable_ready",
        "research_complete",
        "thinking_complete",
        "consortium_completed",
        "sentinel_completed",
    )
    error_event_types: tuple[str, ...] = ("error",)
    tool_event_substrings: tuple[str, ...] = ("tool_", "tool-")
    final_text_keys: tuple[str, ...] = (
        "markdown",
        "report",
        "content",
        "summary",
        "final",
    )


def _build_request_body(record: Dict[str, Any]) -> Dict[str, Any]:
    """Default body-builder — modes that mirror this can reuse it."""
    return {"prompt": record["prompt"]}


def _build_code_body(record: Dict[str, Any]) -> Dict[str, Any]:
    return {"prompt": record["prompt"], "effort": "medium"}


def _build_research_body(record: Dict[str, Any]) -> Dict[str, Any]:
    return {"topic": record["prompt"], "depth": "medium"}


def _build_thinking_body(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "prompt": record["prompt"],
        "effort": "medium",
        "clarifications": {},
    }


MODE_ROUTES: Dict[str, ModeRoute] = {
    "Build": ModeRoute(
        mode="Build",
        start_path="/api/code/start",
        events_path=lambda sid: f"/api/code/{sid}/events",
        cancel_path=lambda sid: f"/api/code/{sid}/cancel",
        body_builder=_build_code_body,
    ),
    "Research": ModeRoute(
        mode="Research",
        start_path="/api/local-ai/research",
        events_path=lambda sid: f"/api/local-ai/research/{sid}/events",
        cancel_path=lambda sid: f"/api/local-ai/research/{sid}/cancel",
        body_builder=_build_research_body,
    ),
    "Thinking": ModeRoute(
        mode="Thinking",
        start_path="/api/thinking/think",
        events_path=lambda sid: f"/api/thinking/{sid}/events",
        cancel_path=lambda sid: f"/api/thinking/{sid}/cancel",
        body_builder=_build_thinking_body,
    ),
}


# ─── result row dataclass (matches sprint0_schema.json #/$defs/row) ─


@dataclass
class BaselineMetrics:
    wall_clock_ms: int = 0
    first_token_ms: Optional[int] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    peak_vram_mb: Optional[int] = None
    tool_calls: int = 0
    retries: int = 0
    phase_timings_ms: Dict[str, int] = field(default_factory=dict)


@dataclass
class BaselineRow:
    prompt_id: str
    mode: str
    prompt: str
    started_utc: str
    finished_utc: str
    session_id: Optional[str]
    status: str  # completed | failed | cancelled | timeout
    error: Optional[str]
    metrics: BaselineMetrics
    output: str
    judge_score: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "mode": self.mode,
            "prompt": self.prompt,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "session_id": self.session_id,
            "status": self.status,
            "error": self.error,
            "metrics": asdict(self.metrics),
            "output": self.output,
            "judge_score": self.judge_score,
        }


# ─── nvidia-smi VRAM polling ────────────────────────────────────────


_NVIDIA_SMI_CMD = (
    "nvidia-smi",
    "--query-gpu=memory.used",
    "--format=csv,noheader,nounits",
    "--id=0",
)


async def poll_peak_vram_mb(stop: asyncio.Event, interval: float = 2.0) -> int:
    """Background task: poll nvidia-smi every ``interval`` seconds and
    return the peak ``memory.used`` (MB) observed.  Returns 0 on poll
    failure (e.g. no NVIDIA driver inside this container).
    """
    peak = 0
    while not stop.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                *_NVIDIA_SMI_CMD,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            if proc.returncode == 0:
                txt = (out or b"").decode("utf-8", "replace").strip()
                # First line; could be a number with surrounding noise.
                first = txt.splitlines()[0] if txt else ""
                m = re.search(r"\d+", first)
                if m:
                    peak = max(peak, int(m.group(0)))
        except Exception:
            # Polling is best-effort; never raise into the runner.
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    return peak


# ─── SSE parsing ────────────────────────────────────────────────────


async def _iter_sse_events(
    response: httpx.Response,
) -> Iterable[Dict[str, Any]]:
    """Yield decoded SSE event objects from an httpx streaming
    response.  Each event is the parsed ``data:`` payload (assumed
    JSON).  Lines that aren't ``data:`` are ignored.

    NOTE: this is a synchronous generator over an async iterator —
    callers should wrap it in an ``async for`` via the
    ``response.aiter_lines()`` helper directly.
    """
    raise NotImplementedError("use _stream_events directly")


async def _stream_events(
    client: httpx.AsyncClient, url: str, *, timeout: float,
):
    """Async-iter over SSE events from ``url``.  Yields parsed JSON
    objects.  Raises ``asyncio.TimeoutError`` if no event arrives
    within ``timeout`` seconds (per-iteration deadline)."""
    async with client.stream("GET", url, timeout=None) as response:
        response.raise_for_status()
        buf: list[str] = []
        async for line in response.aiter_lines():
            if line == "":
                if buf:
                    yield _decode_sse_block(buf)
                    buf = []
                continue
            buf.append(line)
        if buf:
            yield _decode_sse_block(buf)


def _decode_sse_block(lines: list[str]) -> Dict[str, Any]:
    """Decode an SSE event block (list of raw lines) into a payload
    dict.  Tolerates non-JSON ``data:`` payloads (returned as
    ``{"_raw": ..., "_event_type": ...}``)."""
    event_type = ""
    data_chunks: list[str] = []
    for line in lines:
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_chunks.append(line[5:].lstrip())
        # ignore id:, retry:, comments
    raw_data = "\n".join(data_chunks)
    if not raw_data:
        return {"type": event_type or "_empty"}
    try:
        parsed = json.loads(raw_data)
        if isinstance(parsed, dict):
            if event_type and "type" not in parsed:
                parsed["type"] = event_type
            return parsed
        return {"type": event_type or "_payload", "_data": parsed}
    except json.JSONDecodeError:
        return {
            "type": event_type or "_raw",
            "_raw": raw_data,
        }


# ─── per-prompt runner ──────────────────────────────────────────────


@dataclass
class RunnerConfig:
    base_url: str = "http://localhost:8000"
    auth_token: Optional[str] = None
    auth_username: Optional[str] = None  # for re-login on token expiry
    auth_password: Optional[str] = None
    client_id: Optional[str] = None
    per_prompt_timeout_s: float = 600.0  # 10 min hard cap per prompt
    poll_vram: bool = True
    extra_headers: Dict[str, str] = field(default_factory=dict)


async def _login(cfg: "RunnerConfig", client: httpx.AsyncClient) -> Optional[str]:
    """POST /api/auth/login with the configured creds; return new
    access_token, or None on failure.  The runner refreshes per-prompt
    so a 60-min full sweep doesn't trip the default 15-min access TTL."""
    if not cfg.auth_username or not cfg.auth_password:
        return None
    try:
        r = await client.post(
            cfg.base_url + "/api/auth/login",
            json={
                "identifier": cfg.auth_username,
                "password": cfg.auth_password,
            },
            timeout=30.0,
        )
        if r.status_code == 200:
            tok = r.json().get("access_token")
            if isinstance(tok, str) and tok:
                return tok
    except Exception:
        return None
    return None


async def _ensure_fresh_token(
    cfg: "RunnerConfig", client: httpx.AsyncClient,
) -> None:
    """Refresh ``cfg.auth_token`` in place by re-logging-in.  No-op when
    creds aren't configured.  Mutates the client's headers as well so
    every subsequent request sees the new token."""
    new_token = await _login(cfg, client)
    if new_token:
        cfg.auth_token = new_token  # type: ignore[misc]
        client.headers["Authorization"] = f"Bearer {new_token}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_filename_token() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace(":", "-")
        .replace("+00-00", "Z")
    )


async def run_one_prompt(
    record: Dict[str, Any],
    cfg: RunnerConfig,
    client: httpx.AsyncClient,
) -> BaselineRow:
    """Drive one prompt end-to-end: POST start → consume SSE → row."""

    mode = record.get("mode") or ""
    route = MODE_ROUTES.get(mode)
    if route is None:
        return BaselineRow(
            prompt_id=record.get("id", "<no-id>"),
            mode=mode or "unknown",
            prompt=record.get("prompt", ""),
            started_utc=_now(),
            finished_utc=_now(),
            session_id=None,
            status="failed",
            error=f"unknown mode: {mode!r}",
            metrics=BaselineMetrics(),
            output="",
        )

    metrics = BaselineMetrics()
    started_at = _now()
    started_perf = time.perf_counter()
    text_buf: list[str] = []
    final_text: str = ""
    session_id: Optional[str] = None
    # Bug fix: status starts as "pending" so a successful POST /start
    # actually proceeds to the SSE stream.  An unset done-event leaves
    # it at "pending" → caller treats that as timeout/no-terminal.
    status = "pending"
    error: Optional[str] = None

    vram_stop = asyncio.Event()
    vram_task: Optional[asyncio.Task] = None
    if cfg.poll_vram:
        vram_task = asyncio.create_task(poll_peak_vram_mb(vram_stop))

    try:
        # 1) POST /start — re-login if access TTL expired since last prompt.
        if cfg.auth_username and cfg.auth_password:
            await _ensure_fresh_token(cfg, client)
        start_url = cfg.base_url + route.start_path
        body = route.body_builder(record)
        post_resp = await client.post(start_url, json=body, timeout=60.0)
        if post_resp.status_code == 401 and cfg.auth_username and cfg.auth_password:
            # One retry after explicit re-login (covers the gap between
            # _ensure_fresh_token above and a token already invalidated).
            await _ensure_fresh_token(cfg, client)
            post_resp = await client.post(start_url, json=body, timeout=60.0)
        if post_resp.status_code >= 400:
            status = "failed"
            error = f"start {post_resp.status_code}: {post_resp.text[:300]}"
        else:
            payload = post_resp.json()
            session_id = (
                payload.get("session_id")
                or payload.get("id")
                or payload.get("sid")
            )
            if not session_id:
                status = "failed"
                error = f"no session_id in start response: {payload!r}"

        # 2) GET /events (SSE) — only if start succeeded
        if status == "pending" and session_id:
            events_url = cfg.base_url + route.events_path(session_id)
            try:
                async with asyncio.timeout(cfg.per_prompt_timeout_s):
                    async for ev in _stream_events(
                        client, events_url, timeout=cfg.per_prompt_timeout_s,
                    ):
                        ev_type = str(ev.get("type") or "")
                        # First-token timestamp — first event with text-ish payload.
                        if metrics.first_token_ms is None and any(
                            isinstance(ev.get(k), str) and ev.get(k)
                            for k in route.text_event_keys + route.final_text_keys
                        ):
                            metrics.first_token_ms = int(
                                (time.perf_counter() - started_perf) * 1000
                            )
                        # Tool-call accounting.
                        if any(
                            sub in ev_type for sub in route.tool_event_substrings
                        ):
                            metrics.tool_calls += 1
                        # Debug-retry accounting (Build).
                        if ev_type == "debug_iteration_start":
                            metrics.retries = max(
                                metrics.retries,
                                int(ev.get("iteration") or metrics.retries + 1),
                            )
                        # Phase timing — best-effort capture from
                        # phase_complete events.
                        if ev_type == "phase_complete":
                            phase = ev.get("phase")
                            duration = ev.get("duration_ms") or ev.get("ms")
                            if isinstance(phase, str) and isinstance(duration, int):
                                metrics.phase_timings_ms[phase] = duration
                        # Token usage — Build emits at deliverable / done.
                        usage = ev.get("usage") if isinstance(ev, dict) else None
                        if isinstance(usage, dict):
                            metrics.prompt_tokens = max(
                                metrics.prompt_tokens,
                                int(usage.get("prompt_tokens", 0) or 0),
                            )
                            metrics.completion_tokens = max(
                                metrics.completion_tokens,
                                int(usage.get(
                                    "completion_tokens", 0,
                                ) or 0),
                            )
                        # Streaming text.
                        for k in route.text_event_keys:
                            chunk = ev.get(k)
                            if isinstance(chunk, str) and chunk:
                                text_buf.append(chunk)
                        # Final-text replacement.
                        for k in route.final_text_keys:
                            final = ev.get(k)
                            if isinstance(final, str) and final:
                                final_text = final
                        # Done / error.
                        if ev_type in route.done_event_types or ev_type == "done":
                            status = "completed"
                            break
                        if ev_type in route.error_event_types:
                            status = "failed"
                            error = str(
                                ev.get("message") or ev.get("error") or "stream error"
                            )
                            break
            except asyncio.TimeoutError:
                status = "timeout"
                error = (
                    f"no terminal event within {cfg.per_prompt_timeout_s:.0f}s"
                )
                # Best-effort cancel.
                try:
                    cancel_url = cfg.base_url + route.cancel_path(session_id)
                    await client.post(cancel_url, timeout=10.0)
                except Exception:
                    pass
            except httpx.HTTPError as exc:
                status = "failed"
                error = f"sse error: {exc}"

    finally:
        if vram_task is not None:
            vram_stop.set()
            try:
                metrics.peak_vram_mb = await asyncio.wait_for(vram_task, timeout=5.0)
            except Exception:
                metrics.peak_vram_mb = None

    # Safety net: if the SSE stream exited without emitting a terminal
    # event AND no exception fired, mark it failed instead of leaking
    # the "pending" sentinel into the schema.  Common cause: stream
    # closed prematurely (server disconnect, empty event sequence).
    if status == "pending":
        status = "failed"
        error = error or "stream ended without terminal event"

    finished_at = _now()
    metrics.wall_clock_ms = int((time.perf_counter() - started_perf) * 1000)
    output = final_text or "".join(text_buf)

    return BaselineRow(
        prompt_id=record.get("id", "<no-id>"),
        mode=mode,
        prompt=record.get("prompt", ""),
        started_utc=started_at,
        finished_utc=finished_at,
        session_id=session_id,
        status=status,
        error=error,
        metrics=metrics,
        output=output,
    )


# ─── public entry — run all prompts + persist ───────────────────────


@dataclass
class RunResult:
    run_id: str
    started_utc: str
    finished_utc: str
    rows: list[BaselineRow]
    jsonl_path: Path
    latest_path: Path
    backend_name: str
    models_used: Dict[str, str]
    git_sha: Optional[str]
    judge_meta: Optional[Dict[str, Any]] = None


def _git_sha(repo_root: Path) -> Optional[str]:
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return None
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", "replace").strip()
    except Exception:
        return None


async def run_baseline(
    *,
    prompts_path: Path,
    output_dir: Path,
    cfg: RunnerConfig,
    backend_name: str = "ollama",
    models_used: Optional[Dict[str, str]] = None,
    judge_meta: Optional[Dict[str, Any]] = None,
    sequential: bool = True,
) -> RunResult:
    """Run every prompt in the corpus and persist the result.

    ``sequential=True`` is the default — Sprint 0 must NOT run prompts
    in parallel because that contaminates per-prompt VRAM and latency
    measurements (one prompt's swap-in penalty becomes another's
    first-token delay).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    corpus = json.loads(prompts_path.read_text(encoding="utf-8"))
    records = corpus.get("prompts") or []
    if not records:
        raise ValueError(f"no prompts in {prompts_path}")

    run_id = uuid.uuid4().hex
    started = _now()
    file_token = _utc_filename_token()
    jsonl_path = output_dir / f"sprint0_{file_token}.jsonl"
    latest_path = output_dir / "sprint0_latest.json"

    # Compose headers once.
    headers = {"Content-Type": "application/json"}
    if cfg.auth_token:
        headers["Authorization"] = f"Bearer {cfg.auth_token}"
    if cfg.client_id:
        headers["X-Client-Id"] = cfg.client_id
    headers.update(cfg.extra_headers)

    rows: list[BaselineRow] = []
    async with httpx.AsyncClient(headers=headers, base_url="") as client:
        with jsonl_path.open("w", encoding="utf-8") as jsonl_fp:
            for record in records:
                if sequential:
                    row = await run_one_prompt(record, cfg, client)
                else:
                    row = await run_one_prompt(record, cfg, client)
                rows.append(row)
                jsonl_fp.write(
                    json.dumps(row.to_dict(), ensure_ascii=False) + "\n",
                )
                jsonl_fp.flush()

    finished = _now()
    repo_root = Path(__file__).resolve().parent.parent.parent
    meta: Dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "started_utc": started,
        "finished_utc": finished,
        "backend": backend_name,
        "models_used": models_used or {},
        "git_sha": _git_sha(repo_root),
        "host": socket.gethostname(),
        "judge": judge_meta,
    }
    latest_payload = {
        "meta": meta,
        "rows": [row.to_dict() for row in rows],
    }
    latest_path.write_text(
        json.dumps(latest_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return RunResult(
        run_id=run_id,
        started_utc=started,
        finished_utc=finished,
        rows=rows,
        jsonl_path=jsonl_path,
        latest_path=latest_path,
        backend_name=backend_name,
        models_used=models_used or {},
        git_sha=meta["git_sha"],
        judge_meta=judge_meta,
    )


# ─── post-process: judge the rows + rewrite latest.json ─────────────


async def rejudge_existing(
    *,
    latest_path: Path,
    judge_cfg,  # baseline_judge.JudgeConfig
    output_dir: Optional[Path] = None,
) -> RunResult:
    """Load an existing ``sprint0_latest.json`` and re-run the judge
    over its rows.  No pipeline calls — pure re-grading.  Used when the
    judge errored on the first pass (e.g. CPU-24B 5-min timeout) and we
    want to retry with a longer timeout / different judge model
    without paying the 20-min pipeline cost again."""
    if not latest_path.is_file():
        raise FileNotFoundError(f"latest baseline not found: {latest_path}")

    payload = json.loads(latest_path.read_text(encoding="utf-8"))
    meta = payload.get("meta") or {}

    rows: list[BaselineRow] = []
    for raw in payload.get("rows", []):
        m = raw.get("metrics") or {}
        rows.append(
            BaselineRow(
                prompt_id=raw["prompt_id"],
                mode=raw["mode"],
                prompt=raw["prompt"],
                started_utc=raw["started_utc"],
                finished_utc=raw["finished_utc"],
                session_id=raw.get("session_id"),
                status=raw["status"],
                error=raw.get("error"),
                metrics=BaselineMetrics(
                    wall_clock_ms=int(m.get("wall_clock_ms") or 0),
                    first_token_ms=m.get("first_token_ms"),
                    prompt_tokens=int(m.get("prompt_tokens") or 0),
                    completion_tokens=int(m.get("completion_tokens") or 0),
                    peak_vram_mb=m.get("peak_vram_mb"),
                    tool_calls=int(m.get("tool_calls") or 0),
                    retries=int(m.get("retries") or 0),
                    phase_timings_ms=dict(m.get("phase_timings_ms") or {}),
                ),
                output=raw.get("output", ""),
                judge_score=None,  # cleared — to be re-set
            )
        )

    # Reconstruct a RunResult so apply_judge_to_result can target it.
    result = RunResult(
        run_id=meta.get("run_id", "rejudge-" + uuid.uuid4().hex),
        started_utc=meta.get("started_utc", _now()),
        finished_utc=meta.get("finished_utc", _now()),
        rows=rows,
        jsonl_path=latest_path.with_suffix(".rejudge.jsonl"),
        latest_path=latest_path,
        backend_name=meta.get("backend", "ollama"),
        models_used=dict(meta.get("models_used") or {}),
        git_sha=meta.get("git_sha"),
        judge_meta=dict(meta.get("judge") or {}),
    )
    return await apply_judge_to_result(
        result, judge_cfg, output_dir=output_dir,
    )


async def apply_judge_to_result(
    result: RunResult,
    judge_cfg,  # baseline_judge.JudgeConfig (avoid hard import for --no-judge users)
    *,
    output_dir: Optional[Path] = None,
) -> RunResult:
    """Run the Mistral-Small-3 critic over every row in ``result``,
    populate each row's ``judge_score``, then rewrite the JSONL +
    latest.json on disk.  Skips rows that didn't complete (status !=
    "completed") — they get ``judge_score=None``.

    Returns the same ``result`` object (mutated in-place) for chaining.
    """
    # Lazy import so callers passing --no-judge never pull baseline_judge.
    from .baseline_judge import (  # noqa: PLC0415
        JudgeBatchInput, JudgeConfig, judge_batch, is_judge_healthy,
    )
    if not isinstance(judge_cfg, JudgeConfig):
        raise TypeError("judge_cfg must be baseline_judge.JudgeConfig")

    # Health gate.
    healthy = await is_judge_healthy(judge_cfg)
    if not healthy:
        # Stamp every row's judge_score with an error and bail.
        for row in result.rows:
            if row.status == "completed":
                row.judge_score = {
                    "uncertain": True,
                    "rationale": "",
                    "error": (
                        f"judge unreachable at {judge_cfg.base_url}"
                        " — start it via tools/judge/start_judge.sh"
                    ),
                }
        _rewrite_outputs(result, output_dir)
        return result

    # Build the batch — only judge rows that actually produced output.
    batch: list[JudgeBatchInput] = [
        JudgeBatchInput(
            prompt_id=row.prompt_id,
            prompt=row.prompt,
            candidate=row.output,
            reference="",  # absolute scoring against empty baseline
        )
        for row in result.rows
        if row.status == "completed" and row.output
    ]
    judge_results = await judge_batch(batch, cfg=judge_cfg, concurrency=1)

    # Merge judge results back onto rows.
    for row in result.rows:
        if row.status != "completed" or not row.output:
            row.judge_score = None
            continue
        jr = judge_results.get(row.prompt_id)
        if jr is None:
            row.judge_score = {
                "uncertain": True,
                "rationale": "",
                "error": "no judge result returned",
            }
        else:
            row.judge_score = jr.to_dict()

    # Persist updated rows.
    _rewrite_outputs(result, output_dir)
    return result


def _rewrite_outputs(result: RunResult, output_dir: Optional[Path]) -> None:
    """Re-emit JSONL + latest.json with current row state.  Used after
    judge merging so the on-disk artifacts include judge_score."""
    target_dir = output_dir or result.latest_path.parent

    # JSONL — overwrite with current row state.
    with result.jsonl_path.open("w", encoding="utf-8") as fp:
        for row in result.rows:
            fp.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")

    # latest.json — keep meta intact, rewrite rows.
    try:
        existing = json.loads(result.latest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {}
    meta = existing.get("meta") or {}
    if result.judge_meta is not None:
        meta["judge"] = dict(result.judge_meta)
    payload = {"meta": meta, "rows": [row.to_dict() for row in result.rows]}
    result.latest_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


__all__ = [
    "BaselineMetrics",
    "BaselineRow",
    "ModeRoute",
    "RunnerConfig",
    "RunResult",
    "MODE_ROUTES",
    "apply_judge_to_result",
    "rejudge_existing",
    "run_baseline",
    "run_one_prompt",
]
