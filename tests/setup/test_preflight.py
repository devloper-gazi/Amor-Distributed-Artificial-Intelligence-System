"""Coverage for tools/setup/preflight.py.

We don't actually need Docker to be running — every check that talks
to the outside world is stubbed via monkeypatch.
"""

from __future__ import annotations

import sys

import pytest

from tools.setup import constants, preflight, util


def test_check_python_version_passes_on_current_runtime():
    # Test runner ≥ 3.9 is guaranteed (project floor).
    res = preflight.check_python_version()
    assert res.ok is True
    assert res.blocker is False


def test_check_python_version_fails_when_floor_raised(monkeypatch):
    monkeypatch.setattr(constants, "MIN_PYTHON", (9, 99))
    res = preflight.check_python_version()
    assert res.ok is False
    assert res.blocker is True
    assert "Install Python" in res.remediation


def test_check_docker_present_reports_path(monkeypatch):
    monkeypatch.setattr(util, "which", lambda name: "/fake/docker")
    res = preflight.check_docker_present()
    assert res.ok is True
    assert res.blocker is False


def test_check_docker_present_blocks_when_missing(monkeypatch):
    monkeypatch.setattr(util, "which", lambda name: None)
    res = preflight.check_docker_present()
    assert res.ok is False
    assert res.blocker is True
    assert "Docker" in res.remediation


def test_check_disk_free_warns_below_recommended(monkeypatch):
    monkeypatch.setattr(util, "detect_disk_free_gb", lambda *_: 35.0)
    monkeypatch.setattr(constants, "RECOMMENDED_DISK_FREE_GB", 60.0)
    monkeypatch.setattr(constants, "MIN_DISK_FREE_GB", 30.0)
    res = preflight.check_disk_free()
    assert res.ok is False
    assert res.blocker is False  # warn, not block


def test_check_disk_free_blocks_below_minimum(monkeypatch):
    monkeypatch.setattr(util, "detect_disk_free_gb", lambda *_: 5.0)
    monkeypatch.setattr(constants, "MIN_DISK_FREE_GB", 30.0)
    res = preflight.check_disk_free()
    assert res.ok is False
    assert res.blocker is True


def test_check_ram_skipped_when_undetectable(monkeypatch):
    monkeypatch.setattr(util, "detect_ram_gb", lambda: None)
    res = preflight.check_ram()
    # When we can't tell, we don't block.
    assert res.ok is True


def test_check_ram_blocks_below_minimum(monkeypatch):
    monkeypatch.setattr(util, "detect_ram_gb", lambda: 4.0)
    monkeypatch.setattr(constants, "MIN_RAM_GB", 8.0)
    res = preflight.check_ram()
    assert res.ok is False
    assert res.blocker is True


def test_check_gpu_absent_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(util, "gpu_info", lambda: None)
    res = preflight.check_gpu()
    assert res.ok is True
    assert "no NVIDIA" in res.message or "CPU" in res.message


def test_check_gpu_present_returns_ok(monkeypatch):
    monkeypatch.setattr(
        util, "gpu_info",
        lambda: {"name": "RTX 4060", "vram_gb": 8.0, "driver": "551.61"},
    )
    res = preflight.check_gpu()
    assert res.ok is True
    assert "RTX 4060" in res.message


def test_check_ports_finds_busy(monkeypatch):
    monkeypatch.setattr(util, "port_in_use", lambda p: p in {8000, 9090})
    res = preflight.check_ports((8000, 8001, 9090))
    assert res.ok is False
    assert res.blocker is False
    assert "8000" in res.message
    assert "9090" in res.message


def test_check_ports_clean(monkeypatch):
    monkeypatch.setattr(util, "port_in_use", lambda p: False)
    res = preflight.check_ports((1, 2, 3))
    assert res.ok is True


def test_check_network_aggregates(monkeypatch):
    # Pretend every host is reachable.
    monkeypatch.setattr(util, "tcp_probe", lambda h, p, timeout=1.0: True)
    res = preflight.check_network()
    assert res.ok is True


def test_check_network_lists_unreachable(monkeypatch):
    def fake_probe(h, p, timeout=1.0):
        return "huggingface" not in h
    monkeypatch.setattr(util, "tcp_probe", fake_probe)
    res = preflight.check_network()
    assert res.ok is False
    assert res.blocker is False
    assert "HuggingFace" in res.message or "huggingface" in res.message.lower()


def test_check_compose_files_blocks_when_missing(tmp_path):
    res = preflight.check_compose_files(repo_root=tmp_path)
    assert res.ok is False
    assert res.blocker is True


def test_check_compose_files_passes_when_present(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}", encoding="utf-8")
    res = preflight.check_compose_files(repo_root=tmp_path)
    assert res.ok is True


def test_preflight_report_aggregates_blockers():
    report = preflight.PreflightReport()
    report.add(preflight.CheckResult("a", ok=True))
    report.add(preflight.CheckResult("b", ok=False, blocker=True))
    report.add(preflight.CheckResult("c", ok=False, blocker=False))
    assert report.fatal is True
    assert len(report.blockers) == 1
    assert len(report.warnings) == 1
    assert report.all_ok is False
