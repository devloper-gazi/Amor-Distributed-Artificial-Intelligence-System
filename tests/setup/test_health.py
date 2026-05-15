"""Coverage for tools/setup/health.py.

We don't run against real services — every probe is monkey-patched.
"""

from __future__ import annotations

from tools.setup import constants, health, util


def _make_svc(name="x", probe_kind="http", port=8000, health_url="http://x/h"):
    return constants.ServiceSpec(
        name=name,
        label=name.upper(),
        container=None,
        health_url=health_url if probe_kind == "http" else None,
        host_ports=(port,) if probe_kind == "tcp" else (),
        tier="core",
        description="",
        probe_kind=probe_kind,
    )


def test_probe_service_http_ok(monkeypatch):
    monkeypatch.setattr(util, "http_probe", lambda u, timeout=2.0: (True, 200))
    svc = _make_svc(probe_kind="http")
    res = health.probe_service(svc)
    assert res.ok is True
    assert "200" in res.detail


def test_probe_service_http_fail(monkeypatch):
    monkeypatch.setattr(util, "http_probe", lambda u, timeout=2.0: (False, 503))
    svc = _make_svc(probe_kind="http")
    res = health.probe_service(svc)
    assert res.ok is False
    assert "503" in res.detail


def test_probe_service_tcp(monkeypatch):
    monkeypatch.setattr(util, "tcp_probe", lambda h, p, timeout=1.0: True)
    svc = _make_svc(probe_kind="tcp")
    res = health.probe_service(svc)
    assert res.ok is True


def test_probe_service_tcp_fail(monkeypatch):
    monkeypatch.setattr(util, "tcp_probe", lambda h, p, timeout=1.0: False)
    svc = _make_svc(probe_kind="tcp")
    res = health.probe_service(svc)
    assert res.ok is False


def test_wait_for_returns_immediately_when_all_ok(monkeypatch):
    monkeypatch.setattr(util, "http_probe", lambda u, timeout=2.0: (True, 200))
    svcs = [_make_svc(name="a"), _make_svc(name="b")]
    rep = health.wait_for(svcs, timeout_s=5.0, initial_interval_s=0.01)
    assert rep.all_ok is True
    assert len(rep.results) == 2


def test_wait_for_times_out(monkeypatch):
    monkeypatch.setattr(util, "http_probe", lambda u, timeout=2.0: (False, None))
    svcs = [_make_svc(name="never")]
    rep = health.wait_for(
        svcs,
        timeout_s=0.2,            # tight budget
        initial_interval_s=0.05,
        max_interval_s=0.05,
    )
    assert rep.all_ok is False
    assert len(rep.failed) == 1


def test_wait_for_drains_pending_in_order(monkeypatch):
    """Once a service goes healthy, it should not be re-probed."""

    sequence: list[str] = []
    state = {"good": False}

    def fake_http_probe(url, timeout=2.0):
        sequence.append(url)
        if "good-host" in url:
            return (state["good"], 200 if state["good"] else 502)
        return (True, 200)

    monkeypatch.setattr(util, "http_probe", fake_http_probe)

    svcs = [
        _make_svc(name="ok-svc", health_url="http://ok-host/h"),
        _make_svc(name="bad-svc", health_url="http://good-host/h"),
    ]

    # Flip the "good" service healthy on the second round.
    rounds = [0]

    def tick(remaining, elapsed):
        rounds[0] += 1
        if rounds[0] == 1:
            state["good"] = True

    rep = health.wait_for(
        svcs,
        timeout_s=2.0,
        initial_interval_s=0.05,
        max_interval_s=0.1,
        on_attempt=tick,
    )
    assert rep.all_ok is True
    # ok-svc should appear only on the first round; bad-svc on both.
    good_count = sum(1 for u in sequence if "ok-host" in u)
    bad_count = sum(1 for u in sequence if "good-host" in u)
    assert good_count == 1
    assert bad_count >= 1


def test_core_and_optional_partitions_cover_all_services():
    core = {s.name for s in health.core_services()}
    optional = {s.name for s in health.optional_services()}
    all_names = {s.name for s in constants.SERVICES}
    assert core | optional == all_names
    assert core.isdisjoint(optional)
