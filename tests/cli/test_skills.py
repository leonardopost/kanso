"""`kanso skills sync` links the packaged skills into every configured target."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.skills_sync import packaged_skills

from .conftest import at, payload


def test_sync_links_every_skill_into_every_target(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "skills", "sync", "--json")

    assert result.exit_code == Exit.OK
    links = payload(result)["links"]
    assert isinstance(links, list)
    assert len(links) == len(packaged_skills()) * len(_targets(workspace))
    for entry in links:
        link = Path(str(entry["link"]))
        assert link.is_symlink()
        assert link.readlink() == Path(str(entry["target"]))


def test_sync_restores_a_link_that_was_removed(runner: CliRunner, workspace: Path) -> None:
    link = sorted((workspace / ".claude" / "skills").iterdir())[0]
    link.unlink()

    assert at(runner, workspace, "skills", "sync").exit_code == Exit.OK

    assert link.is_symlink()


def test_sync_repoints_a_link_that_points_elsewhere(runner: CliRunner, workspace: Path) -> None:
    link = sorted((workspace / ".claude" / "skills").iterdir())[0]
    target = link.readlink()
    link.unlink()
    link.symlink_to(workspace, target_is_directory=True)

    assert at(runner, workspace, "skills", "sync").exit_code == Exit.OK

    assert link.readlink() == target


def test_sync_reports_the_directories_it_wrote(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "skills", "sync")

    assert f"{len(packaged_skills()) * 3} skills" in result.stdout
    for target in _targets(workspace):
        assert str(workspace / target) in result.stdout


def test_sync_needs_a_workspace(runner: CliRunner, tmp_path: Path) -> None:
    result = at(runner, tmp_path / "nowhere", "skills", "sync", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert payload(result)["code"] == int(Exit.PRECONDITION)


def _targets(workspace: Path) -> list[str]:
    from kanso.config import load_config

    return load_config(workspace).skills_targets
