"""`kanso env detect` writes the envelope and prints what it detected."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from kanso.env import FILENAME
from kanso.errors import Exit

from .conftest import at, payload


def test_detect_writes_the_envelope_and_names_the_file(runner: CliRunner, workspace: Path) -> None:
    (workspace / FILENAME).unlink()

    result = at(runner, workspace, "env", "detect", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["path"] == str(workspace / FILENAME)
    assert (workspace / FILENAME).is_file()


def test_the_written_envelope_is_the_object_printed(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "env", "detect", "--json")

    envelope: Any = payload(result)["envelope"]
    assert envelope["schema"] == 1
    assert envelope["plan"]["lanes"] >= 1
    assert envelope["detected"]["cores_total"] >= 1
    assert envelope["detected_at"]


def test_detect_prints_the_host_the_engine_and_the_plan(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "env", "detect")

    assert result.exit_code == Exit.OK
    for label in ("host", "engine", "plan", "written"):
        assert label in result.stdout
    assert "lane(s)" in result.stdout
    assert "nautilus_trader" in result.stdout


def test_the_generated_file_says_so(runner: CliRunner, workspace: Path) -> None:
    assert at(runner, workspace, "env", "detect").exit_code == Exit.OK

    assert (workspace / FILENAME).read_text(encoding="utf-8").startswith("# envelope.yaml")


def test_detect_needs_a_workspace(runner: CliRunner, tmp_path: Path) -> None:
    result = at(runner, tmp_path / "nowhere", "env", "detect", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert payload(result)["code"] == int(Exit.PRECONDITION)
