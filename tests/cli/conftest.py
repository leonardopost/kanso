"""Fixtures for the CLI slice: a runner, the three repository situations, a clean environment.

Every command is exercised through typer's `CliRunner`, which is the closest thing to the
console script that a test can invoke without a subprocess: the same argument parsing, the
same exit code, and standard output and standard error kept apart, so a `--json` test can
assert that the object is alone on standard output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from kanso.cli import app


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `KANSO_` variable of the developer's shell reaches a test."""
    for name in [name for name in os.environ if name.startswith("KANSO_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fresh(tmp_path: Path) -> Path:
    """A directory with nothing in it and no repository anywhere above."""
    directory = tmp_path / "ws"
    directory.mkdir()
    return directory


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A directory that is itself the root of a git repository."""
    directory = tmp_path / "repo"
    (directory / ".git" / "objects").mkdir(parents=True)
    (directory / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (directory / "README.md").write_text("# repo\n", encoding="utf-8")
    return directory


@pytest.fixture
def monorepo(repo: Path) -> Path:
    """A subdirectory deep inside an existing repository."""
    directory = repo / "apps" / "research"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def workspace(runner: CliRunner, fresh: Path) -> Path:
    """A scaffolded workspace: `kanso init` in a fresh directory, asserted green."""
    assert run(runner, "init", fresh).exit_code == 0
    return fresh


def run(runner: CliRunner, *args: object) -> Result:
    """Invoke the application with the arguments stringified, as a shell would."""
    return runner.invoke(app, [str(arg) for arg in args])


def at(runner: CliRunner, root: Path, *args: object) -> Result:
    """Invoke a command against a workspace, as `kanso --workspace ROOT ...`."""
    return run(runner, "--workspace", root, *args)


def payload(result: Result) -> dict[str, Any]:
    """The single JSON object a `--json` invocation printed on standard output."""
    document = json.loads(result.stdout)
    assert isinstance(document, dict)
    return document
