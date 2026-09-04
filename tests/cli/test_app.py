"""The application itself: its global options, its exit codes and its one-object output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kanso import __version__
from kanso.cli import main
from kanso.errors import Exit

from .conftest import at, payload, run


def test_version_prints_the_package_version(runner: CliRunner) -> None:
    result = run(runner, "--version")

    assert result.exit_code == Exit.OK
    assert result.stdout.strip() == f"kanso {__version__}"


def test_version_as_json_names_every_version_in_play(runner: CliRunner) -> None:
    result = run(runner, "--json", "--version")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["kanso"] == __version__
    assert set(document) == {"kanso", "python", "nautilus_trader"}


def test_the_application_without_a_command_is_a_usage_error(runner: CliRunner) -> None:
    assert run(runner).exit_code == Exit.PRECONDITION
    assert run(runner, "--workspace", ".").exit_code == Exit.PRECONDITION


def test_help_lists_the_command_set(runner: CliRunner) -> None:
    result = run(runner, "--help")

    assert result.exit_code == Exit.OK
    for command in (
        "init",
        "doctor",
        "migrate",
        "skills",
        "env",
        "classify",
        "research",
        "models",
        "align",
        "status",
    ):
        assert command in result.stdout


@pytest.mark.parametrize(
    "command",
    [
        ("doctor",),
        ("migrate",),
        ("skills", "sync"),
        ("env", "detect"),
        ("status",),
        ("research", "status"),
    ],
)
def test_json_is_accepted_before_and_after_the_command(
    runner: CliRunner, workspace: Path, command: tuple[str, ...]
) -> None:
    """`kanso --json <command>` and `kanso <command> --json` are the same invocation."""
    early = at(runner, workspace, "--json", *command)
    late = at(runner, workspace, *command, "--json")

    assert early.exit_code == late.exit_code
    assert isinstance(json.loads(early.stdout), dict)
    assert isinstance(json.loads(late.stdout), dict)


def test_a_command_outside_a_workspace_fails_with_a_precondition(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = at(runner, tmp_path / "nowhere", "doctor")

    assert result.exit_code == Exit.PRECONDITION
    assert "kanso.toml" in result.stderr
    assert result.stdout == ""


def test_the_error_envelope_is_the_only_object_on_stdout(runner: CliRunner, tmp_path: Path) -> None:
    result = at(runner, tmp_path / "nowhere", "migrate", "--json")

    assert result.exit_code == Exit.PRECONDITION
    document = payload(result)
    assert document["code"] == int(Exit.PRECONDITION)
    assert "kanso.toml" in str(document["error"])
    assert document["remedy"]


def test_a_malformed_configuration_exits_with_the_validation_code(
    runner: CliRunner, fresh: Path
) -> None:
    (fresh / "kanso.toml").write_text(
        'kanso_version = "0.1.0"\nschema_version = 1\nnope = 3\n', encoding="utf-8"
    )

    result = at(runner, fresh, "migrate", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert payload(result)["code"] == int(Exit.VALIDATION)


def test_the_default_workspace_is_the_current_directory(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(workspace)

    result = run(runner, "migrate", "--json")

    assert result.exit_code == Exit.OK
    assert payload(result)["schema_version"] >= 1


def test_a_workspace_is_found_from_a_subdirectory(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inside = workspace / "hypotheses"
    monkeypatch.chdir(inside)

    result = run(runner, "migrate", "--json")

    assert result.exit_code == Exit.OK


def test_an_unexpected_fault_is_still_one_object_and_the_error_code(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_: Path) -> None:
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(main, "StateStore", broken)

    result = at(runner, workspace, "migrate", "--json")

    assert result.exit_code == Exit.ERROR
    assert payload(result) == {"error": "RuntimeError: the disk went away", "code": int(Exit.ERROR)}


def test_an_unexpected_fault_reads_as_one_line_for_a_human(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_: Path) -> None:
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(main, "StateStore", broken)

    result = at(runner, workspace, "migrate")

    assert result.exit_code == Exit.ERROR
    assert result.stderr.strip() == "error: RuntimeError: the disk went away"
    assert result.stdout == ""
