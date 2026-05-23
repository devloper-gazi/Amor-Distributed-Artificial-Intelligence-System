"""Cycle H.0.4 follow-on — VRAM envelope aggregator coverage.

Tests the helpers + CLI scaffolding for ``tools/aggregate_vram_envelope.py``.
Real nvidia-smi is exercised on the GPU host; here we mock the
subprocess + scrape paths so the test runs everywhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ─── _poll_nvidia_smi — subprocess parsing ──────────────────────────


def test_poll_nvidia_smi_parses_single_gpu():
    """Single-GPU host: nvidia-smi emits one comma-separated row."""
    from tools.aggregate_vram_envelope import _poll_nvidia_smi
    fake = MagicMock(returncode=0, stdout="4096, 8188\n", stderr="")
    with patch("subprocess.run", return_value=fake):
        out = _poll_nvidia_smi()
    assert out == [(4096.0, 8188.0)]


def test_poll_nvidia_smi_parses_multi_gpu():
    """Two GPUs → two rows, in nvidia-smi index order."""
    from tools.aggregate_vram_envelope import _poll_nvidia_smi
    fake = MagicMock(returncode=0, stdout="2048, 8188\n3072, 8188\n", stderr="")
    with patch("subprocess.run", return_value=fake):
        out = _poll_nvidia_smi()
    assert out == [(2048.0, 8188.0), (3072.0, 8188.0)]


def test_poll_nvidia_smi_handles_blank_lines():
    """Tolerates trailing newlines / empty rows."""
    from tools.aggregate_vram_envelope import _poll_nvidia_smi
    fake = MagicMock(returncode=0, stdout="1024, 8188\n\n\n", stderr="")
    with patch("subprocess.run", return_value=fake):
        out = _poll_nvidia_smi()
    assert out == [(1024.0, 8188.0)]


def test_poll_nvidia_smi_returns_none_on_missing_binary():
    """nvidia-smi absent → return None (caller marks poll failure)."""
    from tools.aggregate_vram_envelope import _poll_nvidia_smi
    with patch("subprocess.run", side_effect=FileNotFoundError("nvidia-smi")):
        assert _poll_nvidia_smi() is None


def test_poll_nvidia_smi_returns_none_on_nonzero_exit():
    """nvidia-smi returns nonzero (driver error / permission) → None."""
    from tools.aggregate_vram_envelope import _poll_nvidia_smi
    fake = MagicMock(returncode=2, stdout="", stderr="No devices were found\n")
    with patch("subprocess.run", return_value=fake):
        assert _poll_nvidia_smi() is None


def test_poll_nvidia_smi_returns_none_on_timeout():
    """nvidia-smi hung longer than 5s → caller treats as poll failure."""
    from tools.aggregate_vram_envelope import _poll_nvidia_smi
    import subprocess as _sp
    with patch("subprocess.run", side_effect=_sp.TimeoutExpired("nvidia-smi", 5.0)):
        assert _poll_nvidia_smi() is None


# ─── _poll_from_exporter — Prometheus scrape ────────────────────────


def test_poll_from_exporter_parses_amor_gpu_metrics(monkeypatch):
    """Exporter emits Prometheus text-format; we parse the used+total
    gauges keyed on the GPU index."""
    from tools.aggregate_vram_envelope import _poll_from_exporter
    body = (
        "# HELP amor_gpu_memory_used_mb GPU memory used in MB.\n"
        "# TYPE amor_gpu_memory_used_mb gauge\n"
        'amor_gpu_memory_used_mb{index="0",name="RTX 4060"} 4096.0\n'
        "# HELP amor_gpu_memory_total_mb GPU memory capacity in MB.\n"
        "# TYPE amor_gpu_memory_total_mb gauge\n"
        'amor_gpu_memory_total_mb{index="0",name="RTX 4060"} 8188.0\n'
    )

    class _Resp:
        def read(self):
            return body.encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: _Resp())
    out = _poll_from_exporter("http://localhost:9835/metrics")
    assert out == [(4096.0, 8188.0)]


def test_poll_from_exporter_handles_unreachable(monkeypatch):
    """Exporter not running → None (caller falls back / marks poll failure)."""
    from tools.aggregate_vram_envelope import _poll_from_exporter
    def _fail(*a, **kw):
        raise ConnectionRefusedError("no exporter")
    monkeypatch.setattr("urllib.request.urlopen", _fail)
    assert _poll_from_exporter("http://localhost:9835/metrics") is None


# ─── CLI run() — peak tracking + persistence ────────────────────────


def test_run_one_shot_writes_snapshot(tmp_path, monkeypatch):
    """--one-shot fires one poll + writes the snapshot.  v20 gate
    condition #6 reads ``peak_vram_mb`` from this file."""
    from tools import aggregate_vram_envelope as mod
    monkeypatch.setattr(mod, "_OUT_ROOT", tmp_path)
    # Force the in-container host-visible path to NOT exist so the
    # fallback to _OUT_ROOT fires.
    monkeypatch.setattr(mod.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(mod, "_poll_nvidia_smi", lambda: [(5500.0, 8188.0)])

    args = mod.build_parser().parse_args(["--one-shot"])
    rc = mod.run(args)
    assert rc == 0
    snap = json.loads((tmp_path / "vram_envelope_latest.json").read_text(encoding="utf-8"))
    assert snap["peak_vram_mb"] == 5500.0
    assert abs(snap["peak_vram_gb"] - 5.37) < 0.01
    assert snap["poll_count"] == 1
    assert snap["source"] == "nvidia-smi"


def test_run_multi_poll_tracks_peak(tmp_path, monkeypatch):
    """Multiple polls → snapshot stores the MAXIMUM observed across
    the window, not just the last reading.  This is the 'envelope'."""
    from tools import aggregate_vram_envelope as mod
    monkeypatch.setattr(mod, "_OUT_ROOT", tmp_path)
    monkeypatch.setattr(mod.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(mod, "_poll_nvidia_smi",
                        # 4 polls; peak is 6500 at index 1
                        MagicMock(side_effect=[
                            [(5000.0, 8188.0)],
                            [(6500.0, 8188.0)],
                            [(4000.0, 8188.0)],
                            [(5500.0, 8188.0)],
                        ]))
    monkeypatch.setattr(mod.time, "sleep", lambda _x: None)

    args = mod.build_parser().parse_args(["--interval-s", "0.1", "--max-polls", "4"])
    rc = mod.run(args)
    assert rc == 0
    snap = json.loads((tmp_path / "vram_envelope_latest.json").read_text(encoding="utf-8"))
    assert snap["peak_vram_mb"] == 6500.0
    assert snap["poll_count"] == 4
    assert snap["poll_failures"] == 0


def test_run_returns_error_when_all_polls_fail(tmp_path, monkeypatch):
    """If every poll fails, no snapshot is written and exit code is 1."""
    from tools import aggregate_vram_envelope as mod
    monkeypatch.setattr(mod, "_OUT_ROOT", tmp_path)
    monkeypatch.setattr(mod.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(mod, "_poll_nvidia_smi", lambda: None)
    monkeypatch.setattr(mod.time, "sleep", lambda _x: None)

    args = mod.build_parser().parse_args(["--one-shot"])
    rc = mod.run(args)
    assert rc == 1
    assert not (tmp_path / "vram_envelope_latest.json").exists()


def test_run_uses_exporter_path_when_flagged(tmp_path, monkeypatch):
    """``--from-exporter`` switches the source from nvidia-smi to the
    monitoring/nvidia_smi_exporter /metrics scrape."""
    from tools import aggregate_vram_envelope as mod
    monkeypatch.setattr(mod, "_OUT_ROOT", tmp_path)
    monkeypatch.setattr(mod.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(mod, "_poll_from_exporter",
                        lambda url: [(3072.0, 8188.0)])
    monkeypatch.setattr(mod, "_poll_nvidia_smi",
                        lambda: pytest.fail("nvidia-smi path must not fire"))

    args = mod.build_parser().parse_args(["--one-shot", "--from-exporter"])
    rc = mod.run(args)
    assert rc == 0
    snap = json.loads((tmp_path / "vram_envelope_latest.json").read_text(encoding="utf-8"))
    assert snap["source"] == "exporter"
    assert snap["peak_vram_mb"] == 3072.0
