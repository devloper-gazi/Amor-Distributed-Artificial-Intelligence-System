"""
AMOR CLI entry — argparse dispatch.

Subcommands
-----------
* ``consortium <goal>`` — run the full Scope → Research → Think →
  Implement pipeline.  Optional ``--implementation-engine quick_code``
  swaps the 9-phase Code Intelligence engine for the lighter QuickCode
  pipeline at the implement step.

* ``quickcode <prompt>`` — run the 5-phase reasoning-first pipeline
  directly: Triage → Reason → Implement → Verify → Refine. Reasons
  about clarity / mathematical soundness / performance / edge cases
  before writing code, then runs + tests it before delivering.

Both subcommands support two execution modes:

    --remote URL    POST against a running AMOR server, stream the
                    SSE feed to stdout (no Mongo / Redis on this host).
    (default)       Run the engine in-process — useful for offline
                    batch builds and unit smoke tests.

Run ``python -m document_processor.cli <subcommand> --help`` for the
flag list of each.
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


def _print_quick_event(event: dict[str, Any]) -> None:
    """Pretty-print a single QuickCode event for the live CLI feed.

    The reasoning phase emits a structured payload — render the
    alternatives with their composite scores so the user can see at a
    glance which approach the engine picked and why.
    """
    etype = str(event.get("type") or "")
    phase = event.get("phase")
    if etype == "quick_code_started":
        print(_c("1;36", "⚙  QuickCode started"))
    elif etype == "quick_code_phase_start":
        print(_c("1;34", f"▶  {phase}"))
    elif etype == "quick_code_phase_complete":
        if phase == "reason":
            reasoning = event.get("reasoning") or {}
            chosen = reasoning.get("chosen_label") or "?"
            for alt in reasoning.get("alternatives") or []:
                label = alt.get("label", "?")
                composite = float(alt.get("composite_score") or 0.0)
                scores = alt.get("scores") or {}
                tick = "  ← chosen" if label == chosen else ""
                col = "32" if label == chosen else "90"
                print(_c(col, (
                    f"   ◆ {label} · clarity {scores.get('clarity', 0):.2f} · "
                    f"math {scores.get('math_soundness', 0):.2f} · "
                    f"perf {scores.get('performance', 0):.2f} · "
                    f"edge {scores.get('edge_cases', 0):.2f} → "
                    f"composite {composite:.2f}{tick}"
                )))
            rationale = reasoning.get("rationale") or ""
            if rationale:
                print(_c("90", f"      _Rationale_: {rationale[:240]}"))
        elif phase == "verify":
            v = event.get("verification") or {}
            score = v.get("score") or 0.0
            exec_data = v.get("execution") or {}
            if exec_data.get("skipped"):
                exec_str = "skipped"
            elif exec_data.get("success"):
                exec_str = "✓"
            else:
                exec_str = f"✗ exit={exec_data.get('exit_code', '?')}"
            print(_c("90", (
                f"     verify: score {score:.0f}/100 · exec {exec_str} · "
                f"static {'ran' if v.get('static') is not None else 'n/a'}"
            )))
        else:
            print(_c("1;32", f"✓  {phase}"))
    elif etype == "quick_code_gate":
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
    elif etype == "quick_code_refine_iteration":
        improved = "✓ improved" if event.get("improved") else "≈ no change"
        print(_c("90", f"     · refine iter {event.get('iteration')} — {improved}"))
    elif etype == "quick_code_completed":
        print(_c("1;32", f"●  done — code={event.get('code_chars', 0)}c "
                          f"tests={event.get('tests_chars', 0)}c"))
    elif etype == "quick_code_cancelled":
        print(_c("1;33", "○  cancelled"))
    elif etype == "quick_code_error":
        print(_c("1;31", f"✗  error — {event.get('error', '')}"))


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
        implementation_engine=getattr(
            args, "implementation_engine", "code_intelligence",
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
    impl_engine = getattr(args, "implementation_engine", None)
    if impl_engine and impl_engine != "code_intelligence":
        body["implementation_engine"] = impl_engine

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
        "--implementation-engine",
        choices=["code_intelligence", "quick_code"],
        default="code_intelligence",
        help=(
            "Which engine to run for the Implement phase. "
            "`code_intelligence` (default) is the full 9-phase pipeline; "
            "`quick_code` is the lighter 5-phase reasoning-first lite "
            "engine — faster, with structured alternative scoring."
        ),
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

    # ─── quickcode subcommand ─────────────────────────────────────────
    qc = sub.add_parser(
        "quickcode",
        help="Run the 5-phase reasoning-first QuickCode pipeline",
    )
    qc.add_argument("prompt", help="Free-text task description")
    qc.add_argument(
        "--language", default=None,
        help="Output language hint (default: auto-detected via triage)",
    )
    qc.add_argument(
        "--effort", choices=sorted(VALID_DEPTHS), default="medium",
        help="Effort tier — controls reasoning max_tokens (default: medium)",
    )
    qc.add_argument(
        "--code-context", type=Path, default=None, metavar="PATH",
        help="File whose contents are passed as code_context (optional)",
    )
    qc.add_argument(
        "--max-refine", type=int, default=2,
        help="Refine iterations cap, clamped to [0,3] (default: 2)",
    )
    qc.add_argument(
        "--no-refine", action="store_true",
        help="Skip refinement entirely (sets max_refine=0)",
    )
    qc.add_argument(
        "--output", default="./quickcode_out",
        help="Artifact output directory (default: ./quickcode_out)",
    )
    qc.add_argument(
        "--remote", default=None, metavar="URL",
        help="Run against a remote AMOR server instead of in-process",
    )
    qc.add_argument(
        "--token", default=None,
        help="JWT for the --remote mode (optional)",
    )
    qc.add_argument(
        "--client-id", default=None,
        help="X-Client-Id for --remote mode (auto-generated if missing)",
    )
    qc.add_argument(
        "--quiet", action="store_true",
        help="Don't stream events to stdout — print only the final summary",
    )
    qc.add_argument(
        "--json", dest="emit_json", action="store_true",
        help="Emit a single machine-readable JSON envelope at the end",
    )
    qc.set_defaults(func=_dispatch_quickcode)
    return p


async def _dispatch_consortium(args: argparse.Namespace) -> int:
    if args.remote:
        return await _run_remote(args)
    return await _run_in_process(args)


async def _dispatch_quickcode(args: argparse.Namespace) -> int:
    # --no-refine wins over --max-refine. Clamp upper bound to engine cap.
    if args.no_refine:
        args.max_refine = 0
    args.max_refine = max(0, min(3, int(args.max_refine or 0)))
    if args.remote:
        return await _run_quickcode_remote(args)
    return await _run_quickcode_in_process(args)


async def _run_quickcode_in_process(args: argparse.Namespace) -> int:
    """Run the QuickCode engine in this Python process. Falls back
    gracefully when Ollama is unreachable (each phase is fail-soft)."""
    from ..quick_code import QuickCodeEngine, QuickCodeRequest

    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    code_context: str | None = None
    if args.code_context:
        try:
            code_context = Path(args.code_context).read_text(encoding="utf-8")
        except Exception as exc:
            print(_c("1;33", f"⚠  could not read --code-context: {exc}"),
                  file=sys.stderr)

    request = QuickCodeRequest(
        prompt=args.prompt,
        language=args.language,
        effort=args.effort,                 # type: ignore[arg-type]
        code_context=code_context,
        allow_refine=args.max_refine > 0,
        max_refine=args.max_refine,
    ).normalize()

    async def on_event(event: dict[str, Any]) -> None:
        if not args.quiet:
            _print_quick_event(event)

    engine = QuickCodeEngine(
        session_id=str(uuid4()), request=request, on_event=on_event,
    )
    bundle = await engine.run()

    # Reuse the artifact-bundle writer the API route uses so the CLI
    # output matches what /api/quick-code/{sid}/artifact would produce.
    try:
        from ..api.quick_code_routes import _write_artifact  # noqa: PLC0415
        await _write_artifact(bundle, out_dir)
    except Exception as exc:
        print(_c("1;33", f"⚠  artifact write failed: {exc}"), file=sys.stderr)

    if args.emit_json:
        json.dump(bundle.to_dict(), sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0

    print()
    print(_c("1;36", "── QuickCode bundle ──"))
    if bundle.reasoning and bundle.reasoning.chosen:
        chosen = bundle.reasoning.chosen
        print(f"  Chosen:   {chosen.label} · composite {chosen.composite:.2f}")
    if bundle.verification:
        print(f"  Verify:   score {bundle.verification.score:.0f}/100")
    print(f"  Refine:   {bundle.refine_iterations} iteration(s)")
    print(f"  Output:   {out_dir}")
    # Non-zero exit when any gate failed so CI scripts can catch it.
    if any(g.status == "failed" for g in bundle.gates):
        return 1
    return 0


async def _run_quickcode_remote(args: argparse.Namespace) -> int:
    """Hit a running AMOR server's /api/quick-code/* + stream the SSE feed."""
    try:
        import httpx
    except ImportError:
        print(_c("1;31", "httpx is required for --remote mode"),
              file=sys.stderr)
        return 2

    base = args.remote.rstrip("/")
    code_context: str | None = None
    if args.code_context:
        try:
            code_context = Path(args.code_context).read_text(encoding="utf-8")
        except Exception as exc:
            print(_c("1;33", f"⚠  could not read --code-context: {exc}"),
                  file=sys.stderr)

    body: dict[str, Any] = {
        "prompt": args.prompt,
        "effort": args.effort,
        "max_refine": args.max_refine,
        "allow_refine": args.max_refine > 0,
    }
    if args.language:
        body["language"] = args.language
    if code_context:
        body["code_context"] = code_context

    headers = {"X-Client-Id": args.client_id or f"cli-{uuid4().hex[:8]}"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(
            f"{base}/api/quick-code/start", json=body, headers=headers,
        )
        if resp.status_code >= 400:
            print(_c("1;31", f"start failed (HTTP {resp.status_code}): "
                              f"{resp.text[:400]}"), file=sys.stderr)
            return 1
        session_id = resp.json().get("session_id")
        if not session_id:
            print(_c("1;31", f"no session_id in response: {resp.text[:400]}"),
                  file=sys.stderr)
            return 1
        # X-Model-Used is a useful debugging crumb for the user.
        model_used = resp.headers.get("x-model-used") or "?"
        print(_c("1;36", f"⚙  QuickCode started — {session_id} · "
                          f"model={model_used}"))

        last_event: dict[str, Any] | None = None
        async with client.stream(
            "GET", f"{base}/api/quick-code/{session_id}/events",
            headers=headers,
        ) as event_resp:
            if event_resp.status_code >= 400:
                print(_c("1;31",
                         f"events stream failed (HTTP {event_resp.status_code})"),
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
                    last_event = event
                    if not args.quiet:
                        _print_quick_event(event)
                    etype = event.get("type")
                    if etype == "quick_code_completed":
                        if args.emit_json:
                            json.dump(event, sys.stdout, indent=2, ensure_ascii=False)
                            print()
                        return 0
                    if etype == "quick_code_cancelled":
                        return 130
                    if etype == "quick_code_error":
                        return 1
    return 0 if (last_event and last_event.get("type") == "quick_code_completed") else 1


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
