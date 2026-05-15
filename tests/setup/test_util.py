"""Coverage for tools/setup/util.py."""

from __future__ import annotations

import socket
import threading
from pathlib import Path

from tools.setup import util


def test_detect_os_returns_known_value():
    assert util.detect_os() in {"windows", "macos", "linux", "other"}


def test_detect_cpu_count_positive():
    assert util.detect_cpu_count() >= 1


def test_detect_disk_free_gb_returns_float():
    val = util.detect_disk_free_gb(".")
    assert isinstance(val, float)
    assert val > 0


def test_humanize_bytes_progression():
    assert util.humanize_bytes(0) == "0.0 B"
    assert util.humanize_bytes(1024) == "1.0 KiB"
    assert util.humanize_bytes(1024 * 1024) == "1.0 MiB"
    assert util.humanize_bytes(1024 ** 3) == "1.0 GiB"


def test_humanize_seconds_progression():
    assert "s" in util.humanize_seconds(30)
    assert "m" in util.humanize_seconds(120)
    assert "h" in util.humanize_seconds(7200)


def test_port_in_use_detects_bound_port():
    # Bind to ephemeral port, then assert port_in_use returns True for it.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert util.port_in_use(port) is True
    finally:
        sock.close()


def test_port_in_use_false_for_unbound():
    # Pick an unlikely-to-be-bound high port.  Race-safe enough for CI.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    sock.close()
    # After close, the port is free again (modulo TIME_WAIT — but for
    # the SO_REUSEADDR-less bind() probe, it should report free).
    assert util.port_in_use(port) is False


def test_tcp_probe_succeeds_for_listening_port():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]

    accepted_event = threading.Event()

    def accept_once():
        try:
            conn, _ = server.accept()
            conn.close()
        finally:
            accepted_event.set()

    t = threading.Thread(target=accept_once, daemon=True)
    t.start()

    try:
        assert util.tcp_probe("127.0.0.1", port, timeout=2.0) is True
    finally:
        accepted_event.wait(timeout=2.0)
        server.close()


def test_tcp_probe_fails_for_closed_port():
    # 1 is reserved; nothing should be listening.
    assert util.tcp_probe("127.0.0.1", 1, timeout=0.5) is False


def test_run_captures_stdout():
    import sys
    # Cross-platform: ask Python itself to print "hello".
    res = util.run([sys.executable, "-c", "print('hello')"])
    assert res.ok
    assert "hello" in res.stdout


def test_run_handles_missing_command():
    res = util.run(["this-binary-does-not-exist-12345"])
    assert not res.ok
    assert res.code == 127


def test_cmdresult_ok_property():
    r = util.CmdResult(0, "", "")
    assert r.ok is True
    r2 = util.CmdResult(1, "", "boom")
    assert r2.ok is False


def test_setup_log_file_creates_under_root(tmp_path: Path):
    path = util.setup_log_file("test_run", root=tmp_path)
    assert path.parent == tmp_path
    assert path.exists()
    assert path.name.startswith("test_run_")
    assert path.name.endswith(".log")


def test_log_to_appends(tmp_path: Path):
    path = tmp_path / "log.log"
    util.log_to(path, "line1")
    util.log_to(path, "line2")
    body = path.read_text(encoding="utf-8").splitlines()
    assert body == ["line1", "line2"]


def test_write_json_round_trip(tmp_path: Path):
    import json
    path = tmp_path / "out.json"
    util.write_json(path, {"a": 1, "b": [2, 3]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "b": [2, 3]}
