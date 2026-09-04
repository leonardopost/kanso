"""`skills sync` links the packaged skills into every configured target."""

from __future__ import annotations

from pathlib import Path

import pytest

from kanso.errors import Exit, PreconditionError
from kanso.skills_sync import LINK_PREFIX, SKILLS, packaged_skills, sync
from kanso.workspace import Workspace, init


def test_packaged_skills_are_shipped() -> None:
    names = [d.name for d in packaged_skills()]

    assert names, "the package ships no skills"
    assert names == sorted(names)
    assert all(name.startswith(LINK_PREFIX) for name in names)
    assert all((SKILLS / name / "SKILL.md").is_file() for name in names)


def test_links_every_skill_into_every_target(fresh: Path) -> None:
    ws = init(fresh)

    pairs = sync(ws)

    targets = ws.config.skills_targets
    assert len(pairs) == len(targets) * len(packaged_skills())
    for link, target in pairs:
        assert link.is_symlink()
        assert link.readlink().is_absolute()
        assert link.readlink() == target
        assert target.parent == SKILLS
        assert link.name == target.name
        assert (link / "SKILL.md").is_file(), "the link resolves to a usable skill"
    for entry in targets:
        assert ws.path(entry).is_dir()


def test_sync_is_idempotent(fresh: Path) -> None:
    ws = init(fresh)

    first = sync(ws)
    second = sync(ws)

    assert first == second
    assert all(link.is_symlink() for link, _ in second)


def test_a_stale_link_is_repointed(fresh: Path) -> None:
    ws = init(fresh)
    sync(ws)
    link = ws.path(ws.config.skills_targets[0], packaged_skills()[0].name)
    elsewhere = ws.path("elsewhere")
    elsewhere.mkdir()
    link.unlink()
    link.symlink_to(elsewhere, target_is_directory=True)

    sync(ws)

    assert link.readlink() == packaged_skills()[0]


def test_a_missing_link_is_recreated(fresh: Path) -> None:
    ws = init(fresh)
    sync(ws)
    link = ws.path(ws.config.skills_targets[0], packaged_skills()[0].name)
    link.unlink()

    sync(ws)

    assert link.is_symlink()


def test_a_plain_file_at_a_link_path_is_replaced(fresh: Path) -> None:
    ws = init(fresh)
    link = ws.path(ws.config.skills_targets[0], packaged_skills()[0].name)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_text("stale\n", encoding="utf-8")

    sync(ws)

    assert link.is_symlink()


def test_a_real_directory_at_a_link_path_is_refused(fresh: Path) -> None:
    ws = init(fresh)
    link = ws.path(ws.config.skills_targets[0], packaged_skills()[0].name)
    link.mkdir(parents=True)
    (link / "SKILL.md").write_text("mine\n", encoding="utf-8")

    with pytest.raises(PreconditionError) as caught:
        sync(ws)

    assert caught.value.code == Exit.PRECONDITION
    assert (link / "SKILL.md").read_text(encoding="utf-8") == "mine\n"


def test_the_link_glob_is_gitignored_once(fresh: Path) -> None:
    ws = init(fresh)

    sync(ws)
    sync(ws)

    lines = ws.path(".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in ws.config.skills_targets:
        assert lines.count(f"{entry}/{LINK_PREFIX}*") == 1


def test_an_absolute_target_is_linked_but_not_gitignored(fresh: Path, tmp_path: Path) -> None:
    ws = init(fresh)
    outside = tmp_path / "agent" / "skills"
    ws = Workspace(
        root=ws.root,
        config=ws.config.model_copy(update={"skills_targets": [str(outside)]}),
    )

    pairs = sync(ws)

    assert outside.is_dir()
    assert all(str(link).startswith(str(outside)) for link, _ in pairs)
    assert str(outside) not in ws.path(".gitignore").read_text(encoding="utf-8")
