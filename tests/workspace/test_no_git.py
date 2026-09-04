"""kanso is not a git actor: no workspace operation spawns a process, let alone `git`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kanso import skills_sync
from kanso.workspace import append_gitignore, find, gitignore_entries, in_git_repo, init


class SpawnedError(AssertionError):
    """Raised by the spy when anything tries to start a process."""


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Fail the test on any process launch, naming git explicitly."""
    seen: list[list[str]] = []

    def record(*args: Any, **kwargs: Any) -> Any:
        argv = args[0] if args else kwargs.get("args", kwargs.get("cmd", ""))
        one = isinstance(argv, (str, bytes, os.PathLike))
        parts = [str(argv)] if one else [str(a) for a in argv]
        seen.append(parts)
        head = os.path.basename(parts[0]) if parts else ""
        raise SpawnedError(f"kanso spawned {'git' if head == 'git' else 'a subprocess'}: {parts}")

    for name in ("Popen", "run", "call", "check_call", "check_output", "getoutput"):
        monkeypatch.setattr(subprocess, name, record)
    for name in ("system", "posix_spawn", "posix_spawnp", "execvp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, record)
    return seen


def test_init_and_sync_spawn_nothing(fresh: Path, spy: list[list[str]]) -> None:
    ws = init(fresh, demo=True)
    skills_sync.sync(ws)
    find(ws.root)
    in_git_repo(ws.root)
    append_gitignore(ws.root, gitignore_entries())

    assert spy == []


def test_init_inside_a_repository_spawns_nothing(repo: Path, spy: list[list[str]]) -> None:
    ws = init(repo)
    skills_sync.sync(ws)

    assert spy == []
    assert (repo / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"


def test_the_spy_would_catch_git(spy: list[list[str]]) -> None:
    """The spy is not vacuous."""
    with pytest.raises(SpawnedError, match="git"):
        subprocess.run(["git", "status"], check=False)
    assert spy == [["git", "status"]]


def test_no_workspace_source_mentions_git_commands() -> None:
    """A second, cheaper guard: the modules contain no git invocation at all."""
    import kanso.skills_sync as sync_module
    import kanso.workspace as workspace_module

    for module in (workspace_module, sync_module):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "subprocess" not in source
        assert '"git"' not in source and "'git'" not in source
