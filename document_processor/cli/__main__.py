"""
AMOR CLI entry — argparse dispatch.

Subcommands
-----------
* ``consortium <goal>`` — run the full Scope → Research → Think →
  Implement pipeline. Two execution modes:

    --remote URL    POST against a running AMOR server's
                    /api/consortium/start, then stream the SSE feed
                    to stdout (no Mongo / Redis required on this host).
    (default)       Run the orchestrator in-process. Useful for unit
                    smoke tests and offline batch builds.

Run ``python -m document_processor.cli consortium --help`` for the
flag list.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

VALID_DEPTHS = {"basic", "medium", "deep", "expert", "ultra"}


# ANSI colour codes — kept simple to avoid taking a dep on rich/click.
def _c(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _print_event(event: dict[str, Any]) -> None:
    """Pretty-print a single consortium event for the live CLI feed."""
    etype = str(event.get("type") or "")
    phase = event.get("consortium_phase") or event.get("phase")
    if etype == "consortium_started":
        print(_c("1;36", f"⚙  Consortium started — {event.get('session_id', '')}"))
    elif etype == "consortium_phase_start":
        print(_c("1;34", f"▶  {phase}"))
    elif etype == "consortium_phase_complete":
        print(_c("1;32", f"✓  {phase}") + f" (in {event.get('ts', '')})")
    elif etype == "consortium_gate":
        gate = event.get("gate") or {}
        badge = {"passed": "✓", "passed_warn": "⚠", "failed": "✗"}.get(
            gate.get("status"), "?",
        )
        col = {"passed": "32", "passed_warn": "33", "failed": "31"}.get(
            gate.get("status"), "0",
        )
        print(_c(col, f"   {badge} gate · {gate.get('phase')} · "
                       f"score {gate.get('score')} — {gate.get('summary')}"))
        for finding in gate.get("findings") or []:
            print(_c("33", f"      ! {finding}"))
    elif etype == "consortium_completed":
        col = "32" if event.get("status") == "ok" else "33"
        print(_c(f"1;{col}", f"●  done — status={event.get('status')}"))
    elif etype == "consortium_cancelled":
        print(_c("1;33", "○  cancelled"))
    elif etype == "consortium_error":
        print(_c("1;31", f"✗  error — {event.get('error', '')}"))
    elif etype.startswith("consortium:"):
        # Inner phase event — show a short, indented hint.
        inner = etype.split(":", 2)[-1] if etype.count(":") >= 2 else etype
        if inner in {"phase_start", "phase_complete"}:
            label = event.get("phase") or event.get("label") or ""
            print(_c("90", f"     · {phase}/{inner} {label}"))


# ─── execution modes ───────────────────────────────────────────────────────


async def _run_in_process(args: argparse.Namespace) -> int:
    """Run the orchestrator in this Python process. No HTTP, no Mongo,
    no Redis — direct engine import. Falls back gracefully if Ollama
    is unreachable (the orchestrator is designed to fail-soft per phase).
    """
    from ..consortium import ConsortiumOrchestrator, ConsortiumScope

    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    scope = ConsortiumScope(
        goal=args.goal,
        depth=args.depth,                       # type: ignore[arg-type]
        language=args.language,
        deliverable_type=args.deliverable_type,
        allow_external_research=not args.no_research,
        research_depth=(args.research_depth or args.depth),  # type: ignore[arg-type]
        thinking_effort=(args.thinking_effort or args.depth),  # type: ignore[arg-type]
        implementation_effort=(
            args.implementation_effort or args.depth
        ),  # type: ignore[arg-type]
    )

    async def on_event(event: dict[str, Any]) -> None:
        if not args.quiet:
            _print_event(event)

    orchestrator = ConsortiumOrchestrator(
        session_id=str(uuid4()),
        scope=scope,
        on_event=on_event,
        artifact_dir=out_dir,
    )
    bundle = await orchestrator.run()

    print()
    print(_c("1;36", "── Bundle ──"))
    print(f"  Title:    {bundle.scope.title}")
    print(f"  Status:   {len(bundle.verifications)} gates")
    for v in bundle.verifications:
        badge = {"passed": "✓", "passed_warn": "⚠", "failed": "✗"}.get(
            v.status, "?",
        )
        print(f"    {badge} {v.phase} (score {v.score})")
    print(f"  Output:   {out_dir}")
    return 0


async def _run_remote(args: argparse.Namespace) -> int:
    """Hit a running AMOR server's HTTP API + stream the SSE feed."""
    try:
        import httpx
    except ImportError:
        print(_c("1;31", "httpx is required for --remote mode"), file=sys.stderr)
        return 2

    base = args.remote.rstrip("/")
    body = {
        "goal": args.goal,
        "depth": args.depth,
        "language": args.language,
        "deliverable_type": args.deliverable_type,
        "allow_external_research": not args.no_research,
    }
    if args.research_depth:
        body["research_depth"] = args.research_depth
    if args.thinking_effort:
        body["thinking_effort"] = args.thinking_effort
    if args.implementation_effort:
        body["implementation_effort"] = args.implementation_effort

    headers = {"X-Client-Id": args.client_id or f"cli-{uuid4().hex[:8]}"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(
            f"{base}/api/consortium/start", json=body, headers=headers,
        )
        if resp.status_code >= 400:
            print(_c("1;31", f"start failed (HTTP {resp.status_code}): {resp.text[:400]}"),
                  file=sys.stderr)
            return 1
        session_id = resp.json().get("session_id")
        if not session_id:
            print(_c("1;31", f"no session_id in response: {resp.text[:400]}"),
                  file=sys.stderr)
            return 1
        print(_c("1;36", f"⚙  Consortium started — {session_id}"))

        async with client.stream(
            "GET", f"{base}/api/consortium/{session_id}/events",
            headers=headers,
        ) as event_resp:
            if event_resp.status_code >= 400:
                print(_c("1;31", f"events stream failed (HTTP {event_resp.status_code})"),
                      file=sys.stderr)
                return 1
            buf = ""
            async for chunk in event_resp.aiter_text():
                if not chunk:
                    continue
                buf += chunk
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    data_line = next(
                        (line for line in block.splitlines()
                         if line.startswith("data:")),
                        None,
                    )
                    if not data_line:
                        continue
                    try:
                        event = json.loads(data_line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if not args.quiet:
                        _print_event(event)
                    if event.get("type") in {"consortium_completed",
                                              "consortium_error",
                                              "consortium_cancelled"}:
                        return 0 if event.get("status") == "ok" else 1
    return 0


# ─── argparse + dispatch ───────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amor",
        description="AMOR command-line interface (Consortium pipeline)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    cons = sub.add_parser(
        "consortium",
        help="Run the Scope → Research → Think → Implement pipeline",
    )
    cons.add_argument("goal", help="Free-text project goal")
    cons.add_argument(
        "--depth", choices=sorted(VALID_DEPTHS), default="medium",
        help="Global depth knob (default: medium)",
    )
    cons.add_argument(
        "--research-depth", choices=sorted(VALID_DEPTHS),
        default=None, help="Override research depth",
    )
    cons.add_argument(
        "--thinking-effort", choices=sorted(VALID_DEPTHS),
        default=None, help="Override thinking effort",
    )
    cons.add_argument(
        "--implementation-effort", choices=sorted(VALID_DEPTHS),
        default=None, help="Override implementation effort",
    )
    cons.add_argument(
        "--language", default=None,
        help="Preferred output language (default: python)",
    )
    cons.add_argument(
        "--deliverable-type", default="code_module",
        help="What kind of artifact to build (default: code_module)",
    )
    cons.add_argument(
        "--no-research", action="store_true",
        help="Skip web research — fully offline build",
    )
    cons.add_argument(
        "--output", default="./consortium_out",
        help="Artifact output directory (default: ./consortium_out)",
    )
    cons.add_argument(
        "--remote", default=None, metavar="URL",
        help="Run against a remote AMOR server (e.g. http://localhost:8000) "
             "instead of in-process",
    )
    cons.add_argument(
        "--token", default=None,
        help="JWT for the --remote mode (optional, anon allowed)",
    )
    cons.add_argument(
        "--client-id", default=None,
        help="X-Client-Id for --remote mode (auto-generated if missing)",
    )
    cons.add_argument(
        "--quiet", action="store_true",
        help="Don't stream events to stdout — print only the final bundle",
    )
    cons.set_defaults(func=_dispatch_consortium)
    return p


async def _dispatch_consortium(args: argparse.Namespace) -> int:
    if args.remote:
        return await _run_remote(args)
    return await _run_in_process(args)


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    func = getattr(args, "func", None)
    if not func:
        parser.print_help()
        return 1
    try:
        return asyncio.run(func(args))
    except KeyboardInterrupt:
        print(_c("1;33", "\n○ interrupted"), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
