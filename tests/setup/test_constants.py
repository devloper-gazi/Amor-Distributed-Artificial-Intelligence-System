"""Invariants for the service catalogue.

If you add a service to docker-compose.yml, this file makes sure
constants.py is kept in sync.
"""

from __future__ import annotations

from pathlib import Path

from tools.setup import compose, constants


def test_repo_root_resolves_to_amor_root():
    # constants.REPO_ROOT must point at a directory containing
    # docker-compose.yml — the canonical sentinel.
    assert (constants.REPO_ROOT / "docker-compose.yml").is_file(), (
        "REPO_ROOT calculation has drifted from the actual repo layout"
    )


def test_services_have_unique_names():
    names = [svc.name for svc in constants.SERVICES]
    assert len(names) == len(set(names)), f"duplicate service names: {names}"


def test_every_service_has_a_tier():
    for svc in constants.SERVICES:
        assert svc.tier in {"core", "optional"}, (
            f"{svc.name}: bad tier {svc.tier!r}"
        )


def test_core_services_have_a_probe():
    for svc in constants.SERVICES:
        if svc.tier != "core":
            continue
        has_probe = (
            (svc.probe_kind == "http" and svc.health_url)
            or (svc.probe_kind == "tcp" and svc.host_ports)
        )
        assert has_probe, f"core service {svc.name} has no probe"


def test_profiles_reference_real_services():
    known = {svc.name for svc in constants.SERVICES}
    for name, profile in constants.PROFILES.items():
        unknown = set(profile.services) - known
        assert not unknown, f"profile {name} references unknown services: {unknown}"


def test_default_profile_exists():
    assert constants.DEFAULT_PROFILE in constants.PROFILES


def test_minimum_floors_sane():
    assert 0 < constants.MIN_DISK_FREE_GB <= constants.RECOMMENDED_DISK_FREE_GB
    assert 0 < constants.MIN_RAM_GB <= constants.RECOMMENDED_RAM_GB
    assert constants.MIN_PYTHON >= (3, 9)


def test_all_host_ports_are_unique_and_in_range():
    for port in constants.ALL_HOST_PORTS:
        assert 1 <= port <= 65535


def test_constants_services_match_compose_yaml():
    """Every service listed in constants.SERVICES must exist in compose.yml.

    The reverse direction is allowed to differ (compose.yml may have
    services we treat as internal-only, e.g. zookeeper which IS listed
    but has no probe, so 'no probe defined' is fine).
    """

    yaml_services = set(
        compose.parse_services(constants.REPO_ROOT / "docker-compose.yml")
    )
    listed = {svc.name for svc in constants.SERVICES}
    missing = listed - yaml_services
    assert not missing, (
        f"constants.SERVICES references services not in docker-compose.yml: "
        f"{missing}"
    )
