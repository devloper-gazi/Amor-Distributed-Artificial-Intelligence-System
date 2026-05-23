"""Coverage for tools/setup/cli.py."""

from __future__ import annotations

import pytest

from tools.setup import cli, constants


def test_parser_recognizes_all_documented_subcommands():
    parser = cli._build_parser()
    # Every advertised subcommand should be a recognized argparse choice.
    expected = {
        "install", "start", "stop", "restart", "destroy",
        "status", "logs", "doctor", "verify", "preflight",
    }
    # Walk subparser action to introspect choices.
    sub_actions = [a for a in parser._actions if a.dest == "cmd"]
    assert sub_actions, "main parser is missing the subcommand action"
    choices = set(sub_actions[0].choices)
    missing = expected - choices
    assert not missing, f"missing subcommands: {missing}"


def test_install_profile_choices_match_constants():
    parser = cli._build_parser()
    args = parser.parse_args(["install", "--profile", constants.DEFAULT_PROFILE])
    assert args.cmd == "install"
    assert args.profile == constants.DEFAULT_PROFILE


def test_install_rejects_unknown_profile():
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["install", "--profile", "no-such-profile"])


def test_logs_subcommand_accepts_follow_and_tail():
    parser = cli._build_parser()
    args = parser.parse_args(["logs", "app", "redis", "-f", "-n", "50"])
    assert args.cmd == "logs"
    assert args.services == ["app", "redis"]
    assert args.follow is True
    assert args.tail == 50


def test_destroy_requires_yes_for_volumes(monkeypatch):
    # When --volumes is passed without --yes, main() should reject (exit 1).
    monkeypatch.setattr(cli.util, "fail", lambda msg: None)
    rc = cli.main(["destroy", "--volumes"])
    assert rc == 1


def test_doctor_json_flag():
    parser = cli._build_parser()
    args = parser.parse_args(["doctor", "--json"])
    assert args.cmd == "doctor"
    assert args.json is True


def test_verify_shallow_flag():
    parser = cli._build_parser()
    args = parser.parse_args(["verify", "--shallow"])
    assert args.cmd == "verify"
    assert args.shallow is True


def test_install_skip_flags_combine():
    parser = cli._build_parser()
    args = parser.parse_args([
        "install",
        "--skip-pull", "--skip-build", "--skip-models", "--skip-verify",
        "--yes",
    ])
    assert args.skip_pull and args.skip_build
    assert args.skip_models and args.skip_verify
    assert args.yes


def test_version_subcommand():
    parser = cli._build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    # argparse exits with code 0 for --version
    assert exc.value.code == 0
