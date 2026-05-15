"""
Service control: start / stop / restart / status / logs.

Thin wrappers around `compose` that print friendly summaries and
exit codes the CLI can surface.
"""

from __future__ import annotations

from typing import Sequence

from tools.setup import compose, constants, health, util


def _require_engine() -> compose.ComposeEngine:
    engine = compose.detect_engine()
    if engine is None:
        util.fail("Docker Compose not available.  Run `python -m tools.setup doctor`.")
        raise SystemExit(2)
    return engine


def cmd_start(services: Sequence[str] = (), *, build: bool = False) -> int:
    engine = _require_engine()
    util.step(f"Starting AMOR services via {engine.label}...")
    res = compose.up(engine, services or None, build=build, stream=True)
    if not res.ok:
        util.fail(f"compose up failed (exit {res.code}).")
        return 1
    util.good("Containers started.  Waiting for health checks...")
    # If caller restricted to a subset, only wait on that subset's
    # core intersection — otherwise wait on every core service.
    if services:
        services_set = set(services)
        to_wait_for = [
            svc for svc in health.core_services() if svc.name in services_set
        ]
    else:
        to_wait_for = health.core_services()
    report = health.wait_for(
        to_wait_for,
        engine=engine,
        timeout_s=180.0,
    )
    if not report.all_ok:
        util.warn("Some core services did not report healthy in time:")
        for r in report.failed:
            print(f"  - {r.name}: {r.detail}")
        return 1
    util.good("All core services healthy.")
    print()
    print(f"{util.C_BOLD}URLs:{util.C_RESET}")
    for label, url in constants.POST_INSTALL_URLS:
        print(f"  • {label:<14} {util.C_CYAN}{url}{util.C_RESET}")
    return 0


def cmd_stop(services: Sequence[str] = (), *, volumes: bool = False) -> int:
    engine = _require_engine()
    if volumes:
        util.warn("`--volumes` will DELETE persistent data (databases, models).")
        util.warn("Pass --yes to confirm.")
        return 1
    if services:
        # Stop a subset; compose stop is gentler than down.
        util.step(f"Stopping services: {' '.join(services)}")
        res = util.run(engine.cmd("stop", *services), stream=True, timeout=120)
    else:
        util.step(f"Stopping AMOR via {engine.label}...")
        res = compose.down(engine, volumes=False, stream=True)
    if not res.ok:
        util.fail(f"compose stop failed (exit {res.code}).")
        return 1
    util.good("Services stopped.")
    return 0


def cmd_destroy(*, force_volumes: bool = False) -> int:
    """Hard teardown — stop + remove containers (+ optionally volumes)."""

    engine = _require_engine()
    if force_volumes:
        util.warn(
            "Destroying ALL persistent state (volumes). "
            "Sessions / Postgres / Mongo / models will be lost."
        )
    util.step("Tearing down AMOR...")
    res = compose.down(engine, volumes=force_volumes, stream=True)
    if not res.ok:
        util.fail(f"compose down failed (exit {res.code}).")
        return 1
    util.good("Teardown complete.")
    return 0


def cmd_restart(services: Sequence[str] = ()) -> int:
    engine = _require_engine()
    util.step(
        "Restarting "
        + (" ".join(services) if services else "AMOR (all services)")
        + "..."
    )
    res = compose.restart(engine, services or None, stream=True)
    if not res.ok:
        util.fail(f"compose restart failed (exit {res.code}).")
        return 1
    util.good("Restart issued.  Re-running health checks...")
    # Same intersection trick as cmd_start — don't wait for core
    # services the caller didn't restart.
    if services:
        services_set = set(services)
        to_wait_for = [
            svc for svc in health.core_services() if svc.name in services_set
        ]
    else:
        to_wait_for = health.core_services()
    report = health.wait_for(
        to_wait_for,
        engine=engine,
        timeout_s=120.0,
    )
    if not report.all_ok:
        for r in report.failed:
            print(f"  - {r.name}: {r.detail}")
        return 1
    util.good("All core services healthy.")
    return 0


def cmd_status() -> int:
    engine = _require_engine()
    util.step("Compose status")
    res = util.run(engine.cmd("ps"), timeout=20)
    if not res.ok:
        util.fail(f"compose ps failed (exit {res.code}).")
        return 1
    print(res.stdout.rstrip())
    print()

    report = health.probe_all(constants.SERVICES, engine=engine)
    util.step("Health probes")
    rows = []
    for svc, r in zip(constants.SERVICES, report.results):
        glyph = (f"{util.C_GREEN}OK{util.C_RESET}"
                 if r.ok else f"{util.C_YELLOW}–{util.C_RESET}")
        rows.append([svc.name, svc.tier, glyph, r.detail])
    util.table(rows, ["service", "tier", "health", "detail"])
    return 0 if all(r.ok for r in report.results if _is_core(r.name)) else 1


def _is_core(label: str) -> bool:
    for svc in constants.SERVICES:
        if svc.label == label:
            return svc.tier == "core"
    return False


def cmd_logs(
    services: Sequence[str] = (),
    *,
    tail: int = 100,
    follow: bool = False,
) -> int:
    engine = _require_engine()
    res = compose.logs(engine, services or None, tail=tail, follow=follow)
    if not follow and res.stdout:
        print(res.stdout.rstrip())
    return 0 if res.ok else 1
