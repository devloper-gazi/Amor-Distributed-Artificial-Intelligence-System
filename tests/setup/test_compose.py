"""Coverage for tools/setup/compose.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.setup import compose


def test_parse_services_extracts_top_level(tmp_path: Path):
    yaml = tmp_path / "docker-compose.yml"
    yaml.write_text(
        "services:\n"
        "  alpha:\n"
        "    image: a\n"
        "  beta:\n"
        "    image: b\n"
        "    healthcheck:\n"
        "      test: ['echo', 'ok']\n"
        "  gamma:\n"
        "    image: c\n"
        "\n"
        "volumes:\n"
        "  alpha-data:\n"
        "  beta-data:\n",
        encoding="utf-8",
    )
    assert compose.parse_services(yaml) == ["alpha", "beta", "gamma"]


def test_parse_services_ignores_comments_and_blank(tmp_path: Path):
    yaml = tmp_path / "docker-compose.yml"
    yaml.write_text(
        "# top comment\n"
        "services:\n"
        "  # a service comment\n"
        "  alpha:\n"
        "    image: a\n"
        "\n"
        "  beta:\n"
        "    image: b\n",
        encoding="utf-8",
    )
    assert compose.parse_services(yaml) == ["alpha", "beta"]


def test_compose_engine_label_joins_bin_list():
    engine = compose.ComposeEngine(
        bin=["docker", "compose"], compose_files=()
    )
    assert engine.label == "docker compose"


def test_compose_engine_file_flags_round_trip(tmp_path: Path):
    a = tmp_path / "a.yml"
    b = tmp_path / "b.yml"
    a.touch()
    b.touch()
    engine = compose.ComposeEngine(
        bin=["docker", "compose"], compose_files=(a, b),
    )
    assert engine.file_flags() == ["-f", str(a), "-f", str(b)]
    assert engine.cmd("ps") == [
        "docker", "compose", "-f", str(a), "-f", str(b), "ps",
    ]


def test_compose_engine_cmd_with_project_name(tmp_path: Path):
    engine = compose.ComposeEngine(
        bin=["docker", "compose"],
        compose_files=(),
        project="amor",
    )
    assert engine.cmd("ps") == ["docker", "compose", "-p", "amor", "ps"]


def test_detect_engine_returns_none_when_no_docker(tmp_path: Path, monkeypatch):
    # `util.which` is the seam — mock it to return None.
    from tools.setup import util as setup_util
    monkeypatch.setattr(setup_util, "which", lambda name: None)

    # docker-compose.yml shape doesn't matter when docker is absent.
    result = compose.detect_engine(repo_root=tmp_path)
    assert result is None


def test_detect_engine_skips_windows_overlay_when_disabled(tmp_path: Path, monkeypatch):
    """include_windows_overlay=False must not add the Windows overlay."""

    from tools.setup import util as setup_util

    # Pretend docker compose works.
    monkeypatch.setattr(setup_util, "which", lambda name: "/fake/docker")

    def fake_run(cmd, **_kw):
        from tools.setup.util import CmdResult
        return CmdResult(0, "Docker Compose version v2.20.0", "")

    monkeypatch.setattr(setup_util, "run", fake_run)

    (tmp_path / "docker-compose.yml").write_text("services: {}", encoding="utf-8")
    (tmp_path / "docker-compose.windows.yml").write_text("services: {}", encoding="utf-8")

    engine = compose.detect_engine(
        repo_root=tmp_path, include_windows_overlay=False
    )
    assert engine is not None
    assert len(engine.compose_files) == 1
    assert engine.compose_files[0].name == "docker-compose.yml"


def test_detect_engine_includes_windows_overlay_when_enabled(tmp_path: Path, monkeypatch):
    from tools.setup import util as setup_util

    monkeypatch.setattr(setup_util, "which", lambda name: "/fake/docker")

    def fake_run(cmd, **_kw):
        from tools.setup.util import CmdResult
        return CmdResult(0, "Docker Compose version v2.20.0", "")

    monkeypatch.setattr(setup_util, "run", fake_run)

    (tmp_path / "docker-compose.yml").write_text("services: {}", encoding="utf-8")
    (tmp_path / "docker-compose.windows.yml").write_text("services: {}", encoding="utf-8")

    engine = compose.detect_engine(
        repo_root=tmp_path, include_windows_overlay=True
    )
    assert engine is not None
    file_names = [f.name for f in engine.compose_files]
    assert "docker-compose.yml" in file_names
    assert "docker-compose.windows.yml" in file_names
