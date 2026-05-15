"""
Install orchestrator — the top-level `bootstrap AMOR` workflow.

Phases (each one idempotent):
  1. Preflight     — gate on Docker + disk + RAM + ports.
  2. Repo          — ensure .env + data dirs.
  3. Compose pull  — fetch latest images for the chosen profile.
  4. Compose build — rebuild local images (`--no-build` skips this).
  5. Compose up    — `up -d` for the profile's services.
  6. Health        — wait for every core service to report healthy.
  7. Models        — pull judge GGUFs / ollama tags per profile.
  8. Verify        — live smoke probes.

Exit codes:
  0  success
  1  recoverable failure during a non-preflight phase
  2  preflight blocker
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from tools.setup import (
    compose,
    constants,
    envfile,
    health,
    models as models_mod,
    preflight,
    util,
    verify,
)


@dataclass
class InstallOptions:
    profile: str = constants.DEFAULT_PROFILE
    skip_build: bool = False
    skip_pull: bool = False
    skip_models: bool = False
    skip_verify: bool = False
    yes: bool = False  # auto-confirm soft warnings


def _phase(num: int, total: int, title: str) -> None:
    print(
        f"\n{util.C_BOLD}{util.C_CYAN}[{num}/{total}]{util.C_RESET} "
        f"{util.C_BOLD}{title}{util.C_RESET}"
    )


def run_install(opts: InstallOptions) -> int:
    profile = constants.PROFILES.get(opts.profile)
    if profile is None:
        util.fail(
            f"Unknown profile: {opts.profile}.  "
            f"Known: {', '.join(constants.PROFILES)}"
        )
        return 2

    log_path = util.setup_log_file(f"install_{opts.profile}")
    util.banner(
        "AMOR install",
        f"profile={profile.name}  •  {util.os_label()}  •  log={log_path.name}",
    )
    util.dim(profile.description)

    total = 8
    started = time.monotonic()

    # ─── 1. Preflight ───────────────────────────────────────────────
    _phase(1, total, "Preflight")
    pre = preflight.run_preflight()
    pre.render()
    util.log_to(log_path, "preflight: blockers=%d warnings=%d" %
                (len(pre.blockers), len(pre.warnings)))
    if pre.fatal:
        util.fail("Preflight blocker(s) — install cannot proceed.")
        return 2
    if pre.warnings and not opts.yes:
        util.warn(
            f"{len(pre.warnings)} soft warning(s) — install will continue.  "
            "Pass `--yes` to suppress this notice."
        )

    # ─── 2. Repo state ──────────────────────────────────────────────
    _phase(2, total, "Repository (.env + data dirs)")
    env_res = envfile.ensure_env_file()
    if env_res.action == "kept":
        util.good(f".env present (kept) at {env_res.path.name}")
    elif env_res.action == "copied-example":
        util.good(f".env created from .env.example "
                  f"({len(env_res.overrides_applied)} placeholder(s) reset)")
    else:
        util.good(f".env seeded with safe defaults at {env_res.path}")
    created = envfile.ensure_data_dirs()
    if created:
        for p in created:
            util.dim(f"  created {p.relative_to(constants.REPO_ROOT)}/")
    else:
        util.dim("  (data directories already exist)")

    # ─── 3-5. Compose pull / build / up ─────────────────────────────
    engine = compose.detect_engine()
    if engine is None:
        util.fail("Compose engine vanished between preflight and install (?!)")
        return 2

    if not opts.skip_pull:
        _phase(3, total, f"Pull images via {engine.label}")
        res = compose.pull(engine, profile.services, stream=True)
        if not res.ok:
            util.fail(f"compose pull failed (exit {res.code}).")
            return 1
    else:
        _phase(3, total, "Pull images (SKIPPED)")

    if not opts.skip_build:
        _phase(4, total, "Build local images")
        res = compose.build(engine, profile.services, stream=True)
        if not res.ok:
            util.fail(f"compose build failed (exit {res.code}).")
            return 1
    else:
        _phase(4, total, "Build local images (SKIPPED)")

    _phase(5, total, "Start containers (compose up -d)")
    res = compose.up(engine, profile.services, build=False, stream=True)
    if not res.ok:
        util.fail(f"compose up failed (exit {res.code}).")
        return 1

    # ─── 6. Health wait ─────────────────────────────────────────────
    _phase(6, total, "Wait for core services to become healthy")
    spinner = util.Spinner("polling...")
    def _tick(remaining: int, elapsed: float) -> None:
        spinner.text = f"polling — {remaining} service(s) not yet healthy ({elapsed:.0f}s)"
        spinner.tick()

    # Only wait for the intersection of core services AND services
    # the chosen profile actually starts.  This keeps `minimal`
    # (which deliberately omits llama-swap / ollama) from waiting
    # forever on inference services it never started.
    profile_set = set(profile.services)
    to_wait_for = [
        svc for svc in health.core_services() if svc.name in profile_set
    ]
    report = health.wait_for(
        to_wait_for,
        engine=engine,
        timeout_s=240.0,
        on_attempt=_tick,
    )
    spinner.done(
        ok=report.all_ok,
        msg=("All core services healthy." if report.all_ok
             else "Some core services failed to become healthy:"),
    )
    if not report.all_ok:
        for r in report.failed:
            print(f"  - {r.name}: {r.detail}")
        util.warn(
            "Run `python -m tools.setup doctor` for a full diagnostic, "
            "or `python -m tools.setup logs <service>` to inspect failure."
        )
        return 1

    # ─── 7. Models ──────────────────────────────────────────────────
    if opts.skip_models:
        _phase(7, total, "Model bootstrap (SKIPPED)")
    else:
        _phase(7, total, "Model bootstrap")
        results = models_mod.apply_profile(profile)
        if not results:
            util.dim("  (no models scheduled for this profile)")
        for r in results:
            glyph = {
                "present": util.C_GREEN + "✓" + util.C_RESET,
                "pulled":  util.C_GREEN + "↓" + util.C_RESET,
                "skipped": util.C_DIM + "·" + util.C_RESET,
                "failed":  util.C_RED + "✗" + util.C_RESET,
            }.get(r.action, "?")
            print(f"  {glyph} {r.name}  {util.C_DIM}({r.action}){util.C_RESET}"
                  + (f"  {util.C_DIM}— {r.detail}{util.C_RESET}" if r.detail else ""))

    # ─── 8. Verify ──────────────────────────────────────────────────
    if opts.skip_verify:
        _phase(8, total, "Live verification (SKIPPED)")
    else:
        _phase(8, total, "Live verification")
        rep = verify.run_verify(deep=True)
        rep.render()
        if not rep.all_ok:
            util.warn(
                f"{len(rep.failed)} of {len(rep.checks)} verification checks failed.  "
                "Run `python -m tools.setup doctor`."
            )
            return 1
        util.good(f"All {rep.passed}/{len(rep.checks)} verification checks passed.")

    # ─── Summary ────────────────────────────────────────────────────
    elapsed = time.monotonic() - started
    print()
    util.banner(
        "Install complete",
        f"profile={profile.name}  •  elapsed={util.humanize_seconds(elapsed)}",
    )
    print(f"{util.C_BOLD}URLs:{util.C_RESET}")
    for label, url in constants.POST_INSTALL_URLS:
        print(f"  • {label:<14} {util.C_CYAN}{url}{util.C_RESET}")
    print()
    util.dim(f"Log: {log_path}")
    print()
    util.dim(
        "Next steps:\n"
        "  • `python -m tools.setup status`   — current service health\n"
        "  • `python -m tools.setup doctor`   — full diagnostic\n"
        "  • `python -m tools.setup stop`     — stop services\n"
    )
    return 0
