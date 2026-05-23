#!/usr/bin/env python3
"""
CLI entry for Sprint 0 canonical baseline.

Usage::

    python tools/run_sprint0_baseline.py \\
        --base-url http://localhost:8000 \\
        --auth-token "$AMOR_BASELINE_TOKEN" \\
        --client-id e2e-baseline \\
        --no-judge   # Day 1: skip judge; Day 2 enables Mistral-Small-3 critic

Environment fallbacks (any flag can come from these):
    AMOR_BASELINE_BASE_URL       (default: http://localhost:8000)
    AMOR_BASELINE_TOKEN          (Bearer; required unless backend skips auth)
    AMOR_BASELINE_CLIENT_ID      (X-Client-Id; auto-generated if unset)
    AMOR_BASELINE_PROMPTS        (default: tests/baselines/sprint0_prompts.json)
    AMOR_BASELINE_OUTPUT         (default: data/baselines/)
    AMOR_BASELINE_TIMEOUT_S      (per-prompt cap, default: 600.0)
    AMOR_BASELINE_BACKEND        (label only; default: ollama)
    AMOR_BASELINE_NO_VRAM        (any non-empty value disables nvidia-smi poll)

Exit codes:
    0 — every prompt completed
    1 — one or more prompts failed/timed out
    2 — fatal init error (corpus missing, server unreachable on /health)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

# Allow running as `python tools/run_sprint0_baseline.py` from repo root
# without first installing the package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402

from document_processor.services.baseline_runner import (  # noqa: E402
    RunnerConfig,
    apply_judge_to_result,
    rejudge_existing,
    run_baseline,
)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_sprint0_baseline",
        description=(
            "Run Sprint 0 canonical baseline corpus against a running AMOR "
            "instance and persist a JSON baseline snapshot."
        ),
    )
    p.add_argument(
        "--base-url",
        default=os.getenv("AMOR_BASELINE_BASE_URL", "http://localhost:8000"),
        help="AMOR base URL (default: http://localhost:8000)",
    )
    p.add_argument(
        "--auth-token",
        default=os.getenv("AMOR_BASELINE_TOKEN"),
        help="Bearer token (env: AMOR_BASELINE_TOKEN)",
    )
    p.add_argument(
        "--auth-username",
        default=os.getenv("AMOR_BASELINE_USERNAME"),
        help=(
            "Username for re-login on token expiry "
            "(env: AMOR_BASELINE_USERNAME)"
        ),
    )
    p.add_argument(
        "--auth-password",
        default=os.getenv("AMOR_BASELINE_PASSWORD"),
        help=(
            "Password for re-login on token expiry "
            "(env: AMOR_BASELINE_PASSWORD).  Plaintext on the CLI is bad; "
            "prefer the env var."
        ),
    )
    p.add_argument(
        "--client-id",
        default=os.getenv("AMOR_BASELINE_CLIENT_ID"),
        help=(
            "X-Client-Id header value (env: AMOR_BASELINE_CLIENT_ID); "
            "auto-generated if unset"
        ),
    )
    p.add_argument(
        "--prompts",
        default=os.getenv(
            "AMOR_BASELINE_PROMPTS",
            str(_REPO_ROOT / "tests" / "baselines" / "sprint0_prompts.json"),
        ),
        help="Path to corpus JSON (default: tests/baselines/sprint0_prompts.json)",
    )
    p.add_argument(
        "--output",
        default=os.getenv(
            "AMOR_BASELINE_OUTPUT",
            str(_REPO_ROOT / "data" / "baselines"),
        ),
        help="Output dir (default: data/baselines/)",
    )
    p.add_argument(
        "--timeout-s",
        type=float,
        default=float(os.getenv("AMOR_BASELINE_TIMEOUT_S", "600")),
        help="Per-prompt timeout seconds (default: 600)",
    )
    p.add_argument(
        "--backend",
        default=os.getenv("AMOR_BASELINE_BACKEND", "ollama"),
        choices=["ollama", "llama-cpp", "llama-swap", "stub", "openai-compat"],
        help="Backend label for the meta block (default: ollama)",
    )
    p.add_argument(
        "--no-judge",
        action="store_true",
        default=os.getenv("AMOR_BASELINE_NO_JUDGE", "") not in ("", "0", "false"),
        help=(
            "Skip Mistral-Small-3 judge.  Default if env "
            "AMOR_BASELINE_NO_JUDGE is set non-empty / non-zero."
        ),
    )
    p.add_argument(
        "--judge-url",
        default=os.getenv("AMOR_BASELINE_JUDGE_URL", "http://localhost:9101"),
        help=(
            "Judge llama-server base URL (default: http://localhost:9101). "
            "Start it via tools/judge/start_judge.sh."
        ),
    )
    p.add_argument(
        "--judge-timeout-s",
        type=float,
        default=float(os.getenv("AMOR_BASELINE_JUDGE_TIMEOUT_S", "240")),
        help="Per-judge-call timeout (default: 240; CPU 24B is slow)",
    )
    # Cycle E v18 — profile-driven judge selection.  Mistral primary,
    # Phi-4 fallback, Mistral-fast (Q3_K_M) emergency.  See
    # ``tools/judge/judge_profiles.json``.
    p.add_argument(
        "--judge-profile",
        default=os.getenv("AMOR_SPRINT0_JUDGE", ""),
        help=(
            "Judge profile from tools/judge/judge_profiles.json "
            "(env: AMOR_SPRINT0_JUDGE).  Override per-profile defaults "
            "for model name + timeout.  Leave empty to use whatever "
            "model the running judge container exposes."
        ),
    )
    p.add_argument(
        "--no-vram",
        action="store_true",
        default=bool(os.getenv("AMOR_BASELINE_NO_VRAM", "")),
        help="Disable nvidia-smi polling (e.g. CI without GPU)",
    )
    p.add_argument(
        "--only",
        default=None,
        help=(
            "Comma-separated prompt-id allowlist (smoke testing); "
            "default: run all 10"
        ),
    )
    p.add_argument(
        "--rejudge",
        action="store_true",
        default=False,
        help=(
            "Skip the pipeline pass entirely; load the existing "
            "data/baselines/sprint0_latest.json and re-run only the "
            "judge step.  Useful when the first run's judge errored "
            "(e.g. CPU 24B 5-min timeout) and we want to retry "
            "without re-paying the 20-min pipeline cost."
        ),
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="Suppress per-prompt progress lines",
    )
    return p


def _filter_corpus(prompts_path: Path, only: str | None) -> Path:
    """If ``--only`` is set, write a temp corpus with the filtered set."""
    if not only:
        return prompts_path
    keep = {x.strip() for x in only.split(",") if x.strip()}
    full = json.loads(prompts_path.read_text(encoding="utf-8"))
    full["prompts"] = [p for p in full.get("prompts", []) if p.get("id") in keep]
    if not full["prompts"]:
        raise SystemExit(
            f"--only={only!r} selected zero prompts; "
            f"available: {[p['id'] for p in json.loads(prompts_path.read_text())['prompts']]}"
        )
    tmp = prompts_path.with_name(f".sprint0_filtered_{uuid.uuid4().hex[:8]}.json")
    tmp.write_text(
        json.dumps(full, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return tmp


async def _check_health(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(base_url.rstrip("/") + "/health")
            return r.status_code == 200
    except Exception:
        return False


async def _amain(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    prompts_path = Path(args.prompts).resolve()
    # --rejudge doesn't read the corpus (works off the existing
    # latest.json instead), so skip the corpus check in that mode.
    if not args.rejudge and not prompts_path.exists():
        print(f"[FATAL] corpus not found: {prompts_path}", file=sys.stderr)
        return 2

    output_dir = Path(args.output).resolve()
    healthy = await _check_health(args.base_url)
    if not healthy:
        print(
            f"[FATAL] {args.base_url}/health not 200 — is AMOR running?",
            file=sys.stderr,
        )
        return 2

    client_id = args.client_id or f"baseline-{uuid.uuid4().hex[:12]}"
    cfg = RunnerConfig(
        base_url=args.base_url.rstrip("/"),
        auth_token=args.auth_token,
        auth_username=args.auth_username,
        auth_password=args.auth_password,
        client_id=client_id,
        per_prompt_timeout_s=args.timeout_s,
        poll_vram=not args.no_vram,
    )

    # --rejudge skips corpus reading entirely.  _filter_corpus reads
    # the prompts file even with no --only filter (just to copy it),
    # which fails when the corpus isn't in the container (rejudge mode
    # only needs the existing latest.json).
    filtered_path = (
        _filter_corpus(prompts_path, args.only) if not args.rejudge else prompts_path
    )

    if not args.quiet and not args.rejudge:
        n = len(json.loads(filtered_path.read_text())["prompts"])
        print(
            f"[sprint0] running {n} prompt(s) against {args.base_url} "
            f"(backend label: {args.backend}, judge: "
            f"{'OFF' if args.no_judge else 'ON'}, vram: "
            f"{'OFF' if args.no_vram else 'ON'})",
            file=sys.stderr,
        )

    # Cycle E v18 — load profile (Mistral primary / Phi-4 fallback /
    # Mistral-fast emergency).  Profile overrides judge model name +
    # request timeout.  Empty profile means "use whatever's running".
    profile_data: dict | None = None
    if args.judge_profile:
        profiles_path = (
            Path(__file__).resolve().parent / "judge" / "judge_profiles.json"
        )
        try:
            profiles_json = json.loads(profiles_path.read_text(encoding="utf-8"))
            profile_data = profiles_json["profiles"].get(args.judge_profile)
            if profile_data is None:
                print(
                    f"[FATAL] judge profile {args.judge_profile!r} not found in "
                    f"{profiles_path}; available: "
                    f"{list(profiles_json['profiles'].keys())}",
                    file=sys.stderr,
                )
                return 2
        except Exception as exc:
            print(
                f"[FATAL] failed to read {profiles_path}: {exc}",
                file=sys.stderr,
            )
            return 2

    judge_meta = None
    if not args.no_judge:
        if profile_data:
            method_label = profile_data.get("model_name", "unknown")
            model_path = (
                f"/data/custom_models/judge/{profile_data['gguf_filename']}"
            )
            # Bump runner timeout to whatever the profile recommends; the
            # CLI flag is still authoritative if explicitly set above its
            # env default of 240s.
            recommended_timeout = float(profile_data.get("request_timeout_s", 240))
            if args.judge_timeout_s == 240.0:
                args.judge_timeout_s = recommended_timeout
        else:
            method_label = "mistral-small-3-q4km-cpu"  # legacy default
            model_path = (
                "/data/custom_models/judge/"
                "Mistral-Small-24B-Instruct-2501-Q4_K_M.gguf"
            )
        judge_meta = {
            "method": method_label,
            "rubrics": ["correctness:1-5", "completeness:1-5"],
            "position_swap": True,
            "model_path": model_path,
            "base_url": args.judge_url,
            "profile": args.judge_profile or "(legacy default)",
            "protocol_version": "v18.0",
        }

    # --rejudge short-circuit: skip the pipeline entirely, just re-run
    # the judge over the existing latest.json on disk.
    if args.rejudge:
        if args.no_judge:
            print(
                "[FATAL] --rejudge requires the judge enabled (drop --no-judge).",
                file=sys.stderr,
            )
            return 2
        from document_processor.services.baseline_judge import JudgeConfig
        latest_path = output_dir / "sprint0_latest.json"
        if not args.quiet:
            print(
                f"[sprint0] rejudge mode — reading {latest_path}",
                file=sys.stderr,
            )
        rejudge_cfg_kwargs: dict = {
            "base_url": args.judge_url.rstrip("/"),
            "request_timeout_s": args.judge_timeout_s,
        }
        if profile_data and profile_data.get("model_name"):
            rejudge_cfg_kwargs["model"] = profile_data["model_name"]
        result = await rejudge_existing(
            latest_path=latest_path,
            judge_cfg=JudgeConfig(**rejudge_cfg_kwargs),
            output_dir=output_dir,
        )
        # Re-use the same summary/exit logic below.
        completed = sum(1 for r in result.rows if r.status == "completed")
        total = len(result.rows)
        if not args.quiet:
            for row in result.rows:
                judge_summary = "—"
                if isinstance(row.judge_score, dict):
                    if "correctness" in row.judge_score:
                        u = "?" if row.judge_score.get("uncertain") else " "
                        judge_summary = (
                            f"{row.judge_score['correctness']}/"
                            f"{row.judge_score['completeness']}{u}"
                        )
                    elif "error" in row.judge_score:
                        judge_summary = f"err"
                print(
                    f"  {row.prompt_id:<28s} {row.mode:<9s} "
                    f"status={row.status} judge={judge_summary}",
                    file=sys.stderr,
                )
        print(
            f"[sprint0] rejudge {completed}/{total}; "
            f"latest={result.latest_path}",
            file=sys.stderr,
        )
        return 0 if completed == total else 1

    try:
        result = await run_baseline(
            prompts_path=filtered_path,
            output_dir=output_dir,
            cfg=cfg,
            backend_name=args.backend,
            models_used={},  # Day 1: empty; Sprint 1 fills via /api/admin/llm
            judge_meta=judge_meta,
        )

        # Day 2: post-process — run the judge over completed rows.
        if not args.no_judge:
            from document_processor.services.baseline_judge import JudgeConfig
            judge_cfg_kwargs: dict = {
                "base_url": args.judge_url.rstrip("/"),
                "request_timeout_s": args.judge_timeout_s,
            }
            if profile_data and profile_data.get("model_name"):
                judge_cfg_kwargs["model"] = profile_data["model_name"]
            judge_cfg = JudgeConfig(**judge_cfg_kwargs)
            if not args.quiet:
                completed = sum(
                    1 for r in result.rows
                    if r.status == "completed" and r.output
                )
                print(
                    f"[sprint0] judge pass: {completed} row(s) → "
                    f"{judge_cfg.base_url}",
                    file=sys.stderr,
                )
            await apply_judge_to_result(
                result,
                judge_cfg,
                output_dir=output_dir,
            )
    finally:
        # Clean up temp filtered corpus.
        if filtered_path != prompts_path and filtered_path.exists():
            try:
                filtered_path.unlink()
            except Exception:
                pass

    # Summary.
    total = len(result.rows)
    completed = sum(1 for r in result.rows if r.status == "completed")
    failed = total - completed

    if not args.quiet:
        for row in result.rows:
            wc = row.metrics.wall_clock_ms / 1000.0
            ft = (
                f"{row.metrics.first_token_ms} ms"
                if row.metrics.first_token_ms is not None
                else "—"
            )
            tag = "✓" if row.status == "completed" else "✗"
            judge_summary = ""
            if isinstance(row.judge_score, dict):
                if "correctness" in row.judge_score:
                    c = row.judge_score["correctness"]
                    m = row.judge_score["completeness"]
                    u = "?" if row.judge_score.get("uncertain") else ""
                    judge_summary = f"  judge={c}/{m}{u}"
                elif "error" in row.judge_score:
                    judge_summary = f"  judge=err"
            print(
                f"  {tag} {row.prompt_id:<28s} {row.mode:<9s} "
                f"wc={wc:6.1f}s  ftt={ft:>8s}  "
                f"toks={row.metrics.prompt_tokens}+{row.metrics.completion_tokens}  "
                f"retries={row.metrics.retries}  "
                f"vram={row.metrics.peak_vram_mb} MB  "
                f"status={row.status}"
                f"{judge_summary}"
                f"{f' err={row.error[:60]}' if row.error else ''}",
                file=sys.stderr,
            )

    print(
        f"[sprint0] {completed}/{total} completed; "
        f"jsonl={result.jsonl_path}; latest={result.latest_path}",
        file=sys.stderr,
    )

    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
