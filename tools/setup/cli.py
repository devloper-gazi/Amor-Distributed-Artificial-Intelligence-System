"""
CLI entry point — argparse subcommand dispatch.

Subcommands:
  install   Bootstrap a fresh AMOR install (or re-run idempotently).
  start     `compose up -d` + wait-for-health.
  stop      Stop containers (keeps volumes).
  restart   Restart services.
  destroy   `compose down` (use --volumes to nuke persistent data).
  status    Show compose ps + health probes.
  logs      Tail container logs.
  doctor    Full read-only diagnostic.
  verify    Live smoke probes against the running stack.
  preflight Just the preflight checks (no install).
"""

from __future__ import annotations

import argparse
import sys

from tools.setup import (
    __version__,
    constants,
    doctor as doctor_mod,
    install as install_mod,
    preflight as preflight_mod,
    services,
    util,
    verify as verify_mod,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amor-setup",
        description=(
            "Cross-platform install / start / verify orchestrator for AMOR. "
            "Works on Windows / macOS / Linux."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m tools.setup install            # full bootstrap\n"
            "  python -m tools.setup install --profile minimal --skip-models\n"
            "  python -m tools.setup start              # bring stack up\n"
            "  python -m tools.setup status             # health snapshot\n"
            "  python -m tools.setup doctor             # diagnose issues\n"
            "  python -m tools.setup verify             # live smoke\n"
            "  python -m tools.setup logs app -f        # follow app logs\n"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"amor-setup {__version__}"
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    # install ────────────────────────────────────────────────────────
    p = sub.add_parser("install", help="Full bootstrap (preflight + up + verify)")
    p.add_argument(
        "--profile",
        default=constants.DEFAULT_PROFILE,
        choices=sorted(constants.PROFILES),
        help=(
            "Install profile.  "
            f"Default: {constants.DEFAULT_PROFILE}.  "
            f"Options: {', '.join(constants.PROFILES)}"
        ),
    )
    p.add_argument("--skip-pull", action="store_true",
                   help="Don't `compose pull` (use cached images).")
    p.add_argument("--skip-build", action="store_true",
                   help="Don't `compose build` (use existing image).")
    p.add_argument("--skip-models", action="store_true",
                   help="Don't pull judge GGUFs / ollama tags.")
    p.add_argument("--skip-verify", action="store_true",
                   help="Don't run live smoke after install.")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Auto-confirm soft preflight warnings.")

    # start / stop / restart / destroy ───────────────────────────────
    p = sub.add_parser("start", help="Start services (`compose up -d`).")
    p.add_argument("services", nargs="*", help="Restrict to listed services.")
    p.add_argument("--build", action="store_true", help="Rebuild before starting.")

    p = sub.add_parser("stop", help="Stop services (keeps volumes).")
    p.add_argument("services", nargs="*", help="Stop only listed services.")

    p = sub.add_parser("restart", help="Restart services + re-check health.")
    p.add_argument("services", nargs="*", help="Restrict to listed services.")

    p = sub.add_parser(
        "destroy",
        help="Tear down (`compose down`).  --volumes also wipes data.",
    )
    p.add_argument(
        "--volumes", action="store_true",
        help="DELETE persistent volumes (databases, models). Destructive.",
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Confirm destructive operations.",
    )

    # status / logs ──────────────────────────────────────────────────
    sub.add_parser("status", help="Compose status + health snapshot.")

    p = sub.add_parser("logs", help="Tail container logs.")
    p.add_argument("services", nargs="*", help="Restrict to listed services.")
    p.add_argument("-n", "--tail", type=int, default=200,
                   help="Lines per service (default 200).")
    p.add_argument("-f", "--follow", action="store_true", help="Follow output.")

    # doctor / verify / preflight ────────────────────────────────────
    p = sub.add_parser("doctor", help="Full read-only diagnostic.")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    p = sub.add_parser("verify", help="Live smoke probes against the stack.")
    p.add_argument("--shallow", action="store_true",
                   help="Skip docker-exec probes (HTTP probes only).")
    p.add_argument("--json", action="store_true", help="Emit JSON.")

    sub.add_parser("preflight", help="Run preflight checks (read-only).")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # ─── install ────────────────────────────────────────────────────
    if args.cmd == "install":
        return install_mod.run_install(
            install_mod.InstallOptions(
                profile=args.profile,
                skip_build=args.skip_build,
                skip_pull=args.skip_pull,
                skip_models=args.skip_models,
                skip_verify=args.skip_verify,
                yes=args.yes,
            )
        )

    # ─── services ───────────────────────────────────────────────────
    if args.cmd == "start":
        return services.cmd_start(args.services, build=args.build)
    if args.cmd == "stop":
        return services.cmd_stop(args.services)
    if args.cmd == "restart":
        return services.cmd_restart(args.services)
    if args.cmd == "destroy":
        if args.volumes and not args.yes:
            util.fail(
                "`--volumes` will delete ALL persistent data.  "
                "Pass `--yes` to confirm."
            )
            return 1
        return services.cmd_destroy(force_volumes=args.volumes)
    if args.cmd == "status":
        return services.cmd_status()
    if args.cmd == "logs":
        return services.cmd_logs(args.services, tail=args.tail, follow=args.follow)

    # ─── diagnostics ────────────────────────────────────────────────
    if args.cmd == "doctor":
        return doctor_mod.run_doctor(json_out=args.json)
    if args.cmd == "verify":
        return verify_mod.cmd_verify(deep=not args.shallow, json_out=args.json)
    if args.cmd == "preflight":
        rep = preflight_mod.run_preflight()
        rep.render()
        if rep.fatal:
            util.fail(f"{len(rep.blockers)} blocker(s).")
            return 2
        if rep.warnings:
            util.warn(f"{len(rep.warnings)} warning(s).")
            return 1
        util.good("Preflight clean.")
        return 0

    parser.error(f"unknown command: {args.cmd}")  # pragma: no cover
    return 2
