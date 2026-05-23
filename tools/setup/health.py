"""
Health-check loop with exponential backoff.

Used by `install` to wait until every required service answers OK
before declaring the bootstrap done.  Also used by `doctor` and
`verify` for one-shot readouts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from tools.setup import compose, constants, util


@dataclass
class HealthResult:
    name: str
    ok: bool
    detail: str = ""
    elapsed_s: float = 0.0
    attempts: int = 1


@dataclass
class HealthReport:
    results: list[HealthResult] = field(default_factory=list)

    def add(self, r: HealthResult) -> None:
        self.results.append(r)

    @property
    def all_ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failed(self) -> list[HealthResult]:
        return [r for r in self.results if not r.ok]


# ─── Single-probe helpers ───────────────────────────────────────────


def probe_service(
    svc: constants.ServiceSpec,
    *,
    engine: compose.ComposeEngine | None = None,
    timeout_s: float = 3.0,
) -> HealthResult:
    """One-shot probe.  Returns immediately, never retries."""

    start = time.monotonic()

    if svc.probe_kind == "http" and svc.health_url:
        ok, status = util.http_probe(svc.health_url, timeout=timeout_s)
        return HealthResult(
            name=svc.label,
            ok=ok,
            detail=(f"HTTP {status}" if status is not None else "unreachable"),
            elapsed_s=time.monotonic() - start,
        )

    if svc.probe_kind == "tcp" and svc.host_ports:
        port = svc.host_ports[0]
        ok = util.tcp_probe("127.0.0.1", port, timeout=timeout_s)
        return HealthResult(
            name=svc.label,
            ok=ok,
            detail=f"tcp 127.0.0.1:{port}",
            elapsed_s=time.monotonic() - start,
        )

    # Container-only services — we can only check `compose ps`.
    if engine is not None:
        running = compose.is_running(engine, svc.name)
        return HealthResult(
            name=svc.label,
            ok=running,
            detail="container running" if running else "not running",
            elapsed_s=time.monotonic() - start,
        )

    return HealthResult(
        name=svc.label,
        ok=False,
        detail="no probe configured",
        elapsed_s=time.monotonic() - start,
    )


# ─── Wait-loop with exponential backoff ─────────────────────────────


def wait_for(
    services: Sequence[constants.ServiceSpec],
    *,
    engine: compose.ComposeEngine | None = None,
    timeout_s: float = 180.0,
    initial_interval_s: float = 1.0,
    max_interval_s: float = 5.0,
    on_attempt: callable | None = None,
) -> HealthReport:
    """Poll all services until each is healthy or `timeout_s` elapses.

    Backoff doubles the interval after each unhealthy round, capped at
    `max_interval_s`.  Healthy services drop out of the polling loop.

    `on_attempt(remaining, elapsed)` is called once per round if given —
    useful for the spinner.
    """

    report = HealthReport()
    pending: list[constants.ServiceSpec] = list(services)
    counters: dict[str, int] = {svc.name: 0 for svc in services}
    interval = initial_interval_s
    started = time.monotonic()

    while pending:
        for svc in list(pending):
            counters[svc.name] += 1
            result = probe_service(svc, engine=engine, timeout_s=2.0)
            result.attempts = counters[svc.name]
            if result.ok:
                report.add(result)
                pending.remove(svc)

        elapsed = time.monotonic() - started
        if on_attempt is not None:
            try:
                on_attempt(len(pending), elapsed)
            except Exception:  # pragma: no cover (defensive)
                pass

        if not pending:
            break

        if elapsed >= timeout_s:
            # Record outstanding failures.
            for svc in pending:
                result = probe_service(svc, engine=engine, timeout_s=1.0)
                result.attempts = counters[svc.name]
                report.add(result)
            break

        time.sleep(min(interval, max(0.1, timeout_s - elapsed)))
        interval = min(interval * 1.5, max_interval_s)

    return report


def probe_all(
    services: Sequence[constants.ServiceSpec],
    *,
    engine: compose.ComposeEngine | None = None,
    timeout_s: float = 2.5,
) -> HealthReport:
    """One-shot probe of every service (no retry)."""

    report = HealthReport()
    for svc in services:
        report.add(probe_service(svc, engine=engine, timeout_s=timeout_s))
    return report


def core_services() -> list[constants.ServiceSpec]:
    return [s for s in constants.SERVICES if s.tier == "core"]


def optional_services() -> list[constants.ServiceSpec]:
    return [s for s in constants.SERVICES if s.tier == "optional"]
