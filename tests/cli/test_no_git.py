"""No kanso command invokes git, in any repository situation.

The workspace slice proves that its own functions spawn nothing at all. This is the
command-level guard: every M0 command runs inside a spy that watches every process
launch, lets the host probes through and fails the test the moment one of them is git.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import at, run

WATCHED = ("Popen", "run", "call", "check_call", "check_output", "getoutput")


class GitInvokedError(AssertionError):
    """Raised by the spy when a command tries to run git."""


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every process launch and refuse git; the host probes still run."""
    seen: list[list[str]] = []
    original = {name: getattr(subprocess, name) for name in WATCHED}

    def watcher(name: str) -> Any:
        def launch(*args: Any, **kwargs: Any) -> Any:
            argv = args[0] if args else kwargs.get("args", kwargs.get("cmd", ""))
            one = isinstance(argv, str | bytes | os.PathLike)
            parts = [str(argv)] if one else [str(arg) for arg in argv]
            seen.append(parts)
            if parts and os.path.basename(parts[0]) == "git":
                raise GitInvokedError(f"a kanso command ran git: {parts}")
            return original[name](*args, **kwargs)

        return launch

    for name in WATCHED:
        monkeypatch.setattr(subprocess, name, watcher(name))
    return seen


def _every_command(runner: CliRunner, root: Path) -> None:
    assert run(runner, "init", root).exit_code == Exit.OK
    assert at(runner, root, "migrate").exit_code == Exit.OK
    assert at(runner, root, "skills", "sync").exit_code == Exit.OK
    assert at(runner, root, "env", "detect").exit_code == Exit.OK
    assert at(runner, root, "doctor").exit_code == Exit.OK
    assert run(runner, "--version").exit_code == Exit.OK


def test_no_command_runs_git_in_a_fresh_directory(
    runner: CliRunner, fresh: Path, spy: list[list[str]]
) -> None:
    _every_command(runner, fresh)

    assert not [parts for parts in spy if os.path.basename(parts[0]) == "git"]


def test_no_command_runs_git_inside_a_repository(
    runner: CliRunner, repo: Path, spy: list[list[str]]
) -> None:
    _every_command(runner, repo)

    assert (repo / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"
    assert not [parts for parts in spy if os.path.basename(parts[0]) == "git"]


def test_no_command_runs_git_in_a_monorepo_subdirectory(
    runner: CliRunner, monorepo: Path, spy: list[list[str]]
) -> None:
    _every_command(runner, monorepo)

    assert not [parts for parts in spy if os.path.basename(parts[0]) == "git"]


def test_the_spy_would_catch_git(spy: list[list[str]]) -> None:
    """The spy is not vacuous."""
    with pytest.raises(GitInvokedError):
        subprocess.run(["git", "status"], check=False)


def test_the_command_layer_spawns_nothing_itself() -> None:
    """A cheaper guard: the CLI modules launch no process of their own."""
    import kanso.cli

    modules = sorted(Path(kanso.cli.__file__ or "").parent.glob("*.py"))
    assert len(modules) >= 4
    for module in modules:
        source = module.read_text(encoding="utf-8")
        assert "subprocess" not in source, module.name
        assert '"git"' not in source and "'git'" not in source, module.name
