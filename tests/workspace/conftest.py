"""Fixtures for the workspace slice: the three repository situations `init` must handle."""

from __future__ import annotations

from pathlib import Path

import pytest


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


def snapshot(root: Path, exclude: Path) -> dict[str, bytes]:
    """Every file under `root` outside `exclude`, by relative path."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir() or exclude in path.parents or path == exclude:
            continue
        out[str(path.relative_to(root))] = path.read_bytes()
    return out
