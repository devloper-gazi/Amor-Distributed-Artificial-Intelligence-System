"""Coverage for tools/setup/envfile.py."""

from __future__ import annotations

from pathlib import Path

from tools.setup import envfile


def test_seeded_when_no_example(tmp_repo: Path):
    res = envfile.ensure_env_file(repo_root=tmp_repo)
    assert res.action == "seeded"
    assert res.created is True
    assert res.path == tmp_repo / ".env"
    body = res.path.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD" in body
    assert "REDIS_HOST" in body


def test_kept_when_env_exists(tmp_repo: Path):
    (tmp_repo / ".env").write_text("USER_VALUE=keepme\n", encoding="utf-8")
    res = envfile.ensure_env_file(repo_root=tmp_repo)
    assert res.action == "kept"
    assert res.created is False
    assert "USER_VALUE=keepme" in (tmp_repo / ".env").read_text(encoding="utf-8")


def test_copied_from_example_with_placeholder_reset(tmp_repo_with_compose: Path):
    res = envfile.ensure_env_file(repo_root=tmp_repo_with_compose)
    assert res.action == "copied-example"
    body = (tmp_repo_with_compose / ".env").read_text(encoding="utf-8")
    # The "your-google-api-key-here" placeholder must be wiped.
    assert "your-google-api-key-here" not in body
    assert "GOOGLE_TRANSLATE_API_KEY=" in body
    # Existing non-placeholder values are preserved.
    assert "POSTGRES_PASSWORD=docpass123" in body
    # DEBUG flipped to false (dev override).
    assert "DEBUG=false" in body
    assert "GOOGLE_TRANSLATE_API_KEY" in res.overrides_applied


def test_idempotent_second_call(tmp_repo: Path):
    first = envfile.ensure_env_file(repo_root=tmp_repo)
    second = envfile.ensure_env_file(repo_root=tmp_repo)
    assert first.action == "seeded"
    assert second.action == "kept"


def test_read_env_parses_quotes_and_comments(tmp_repo: Path):
    (tmp_repo / ".env").write_text(
        "# header comment\n"
        "KEY1=plain\n"
        'KEY2="quoted with spaces"\n'
        "KEY3='single quoted'\n"
        "EMPTY=\n",
        encoding="utf-8",
    )
    parsed = envfile.read_env(tmp_repo / ".env")
    assert parsed["KEY1"] == "plain"
    assert parsed["KEY2"] == "quoted with spaces"
    assert parsed["KEY3"] == "single quoted"
    assert parsed["EMPTY"] == ""
    assert "# header comment" not in parsed


def test_read_env_missing_returns_empty(tmp_path: Path):
    assert envfile.read_env(tmp_path / "nonexistent.env") == {}


def test_ensure_data_dirs_creates_targets(tmp_repo: Path):
    created = envfile.ensure_data_dirs(repo_root=tmp_repo)
    assert (tmp_repo / "data" / "baselines").is_dir()
    assert (tmp_repo / "data" / "setup_logs").is_dir()
    assert (tmp_repo / "models").is_dir()
    # data/ already existed via the tmp_repo fixture — should NOT be in
    # the created list.
    assert (tmp_repo / "data") not in created
    # Second call is a no-op.
    again = envfile.ensure_data_dirs(repo_root=tmp_repo)
    assert again == []
