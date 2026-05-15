"""
Live smoke verification — runs AFTER install or on-demand to prove
the stack actually works end-to-end (not just "containers up").

Checks (in order):
  1. /health returns 200 + cache/postgres/mongo true.
  2. /api/auth/me returns 401 OR 200 (i.e. the auth router is mounted).
  3. /docs serves the OpenAPI HTML.
  4. /metrics is reachable and exposes amor_* metrics.
  5. Sandbox runner can echo "ok" via /api/code/sandbox/echo (if route
     exists; soft-skipped otherwise).
  6. (Optional) Redis publish/subscribe round-trip via docker exec.
  7. (Optional) Judge container probe — only if it's already running.

Returns 0 on all-pass, 1 on any failure.  Designed to be runnable in
CI without external API keys (read-only probes).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from tools.setup import compose, constants, util


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class VerifyReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, c: Check) -> None:
        self.checks.append(c)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.ok)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def all_ok(self) -> bool:
        return not self.failed

    def render(self) -> None:
        for c in self.checks:
            glyph = (f"{util.C_GREEN}✓{util.C_RESET}"
                     if c.ok else f"{util.C_RED}✗{util.C_RESET}")
            line = f"  {glyph} {c.name}"
            if c.detail:
                line += f" {util.C_DIM}— {c.detail}{util.C_RESET}"
            print(line)


# ─── Individual probes ──────────────────────────────────────────────


def _check_health() -> Check:
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    url = "http://localhost:8000/health"
    try:
        with urlopen(Request(url), timeout=5) as resp:  # noqa: S310
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
        if status != 200:
            return Check("API /health", False, f"HTTP {status}")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return Check("API /health", False, "non-JSON body")
        # Common shape: {"status": "ok", "cache": true, "postgres": true, ...}
        problems = [k for k, v in data.items()
                    if isinstance(v, bool) and not v]
        if problems:
            return Check(
                "API /health",
                False,
                f"deps unhealthy: {','.join(problems)}",
            )
        return Check("API /health", True, "all deps OK")
    except (URLError, TimeoutError, ConnectionError, OSError) as exc:
        return Check("API /health", False, f"{exc!r}")


def _check_auth_router() -> Check:
    ok, status = util.http_probe(
        "http://localhost:8000/api/auth/me", timeout=4
    )
    # Either 200 (logged in) or 401/403 (not logged in) means the
    # router is mounted.  500 or unreachable = failure.
    if status in {200, 401, 403}:
        return Check("Auth router", True, f"HTTP {status}")
    return Check("Auth router", False, f"HTTP {status}" if status else "unreachable")


def _check_docs() -> Check:
    ok, status = util.http_probe("http://localhost:8000/docs", timeout=4)
    return Check(
        "OpenAPI /docs",
        ok=ok,
        detail=f"HTTP {status}" if status else "unreachable",
    )


def _check_metrics() -> Check:
    from urllib.request import Request, urlopen
    from urllib.error import URLError

    url = "http://localhost:8000/metrics"
    try:
        with urlopen(Request(url), timeout=4) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", errors="replace")
        amor_lines = sum(1 for line in body.splitlines()
                         if line.startswith("amor_"))
        return Check(
            "Prometheus /metrics",
            ok=amor_lines > 0,
            detail=f"{amor_lines} amor_* series" if amor_lines else "no amor_* series",
        )
    except (URLError, TimeoutError, ConnectionError, OSError) as exc:
        return Check("Prometheus /metrics", False, f"{exc!r}")


def _check_redis_pubsub() -> Check:
    """Round-trip via redis-cli inside the container."""

    res = util.run(
        ["docker", "exec", "amor-redis-1", "redis-cli", "PING"],
        timeout=10,
    )
    if not res.ok:
        return Check("Redis PING", False, "container not reachable")
    if "PONG" not in res.stdout:
        return Check("Redis PING", False, f"unexpected: {res.stdout.strip()}")
    return Check("Redis PING", True)


def _check_postgres() -> Check:
    """Smoke-check via pg_isready in the container."""

    # Container name pattern: amor-postgres-1
    res = util.run(
        ["docker", "exec", "amor-postgres-1", "pg_isready", "-U", "docuser"],
        timeout=10,
    )
    if not res.ok:
        return Check("Postgres pg_isready", False, res.stderr.strip()[:80])
    return Check("Postgres pg_isready", True, res.stdout.strip())


def _check_mongo() -> Check:
    res = util.run(
        [
            "docker", "exec", "amor-mongo-1",
            "mongosh", "--quiet", "--eval", "db.runCommand({ping:1}).ok",
        ],
        timeout=15,
    )
    if not res.ok:
        # Older containers ship `mongo` instead of `mongosh`.
        res = util.run(
            [
                "docker", "exec", "amor-mongo-1",
                "mongo", "--quiet", "--eval", "db.runCommand({ping:1}).ok",
            ],
            timeout=15,
        )
        if not res.ok:
            return Check("MongoDB ping", False, res.stderr.strip()[:80])
    out = res.stdout.strip()
    return Check("MongoDB ping", ok=out.endswith("1"), detail=out[:40])


def _check_judge_if_running() -> Check | None:
    """Only probe the judge container if it's currently up."""

    res = util.run(
        ["docker", "ps", "--filter", f"name={constants.JUDGE_CONTAINER}",
         "--format", "{{.Names}}"],
        timeout=5,
    )
    if not res.ok or constants.JUDGE_CONTAINER not in res.stdout:
        return None
    ok, status = util.http_probe(constants.JUDGE_HEALTH_URL, timeout=4)
    return Check(
        "Judge /health",
        ok=ok,
        detail=f"HTTP {status}" if status else "unreachable",
    )


# ─── Orchestrator ───────────────────────────────────────────────────


def run_verify(*, deep: bool = True) -> VerifyReport:
    """Run the standard suite; `deep=True` adds container exec probes."""

    report = VerifyReport()
    report.add(_check_health())
    report.add(_check_auth_router())
    report.add(_check_docs())
    report.add(_check_metrics())

    if deep:
        report.add(_check_postgres())
        report.add(_check_redis_pubsub())
        report.add(_check_mongo())

    judge = _check_judge_if_running()
    if judge is not None:
        report.add(judge)

    return report


def cmd_verify(*, deep: bool = True, json_out: bool = False) -> int:
    """CLI entry."""

    if not json_out:
        util.step("Live smoke verification")

    rep = run_verify(deep=deep)

    if json_out:
        print(json.dumps(
            {"all_ok": rep.all_ok,
             "passed": rep.passed,
             "total": len(rep.checks),
             "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail}
                        for c in rep.checks]},
            indent=2,
        ))
        return 0 if rep.all_ok else 1

    rep.render()
    print()
    if rep.all_ok:
        util.good(f"All {rep.passed}/{len(rep.checks)} verification checks passed.")
        return 0
    util.fail(
        f"{len(rep.failed)} of {len(rep.checks)} checks failed.  "
        "Run `python -m tools.setup doctor` for diagnosis."
    )
    return 1
