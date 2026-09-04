"""Linking the packaged operator skills into the workspace.

The skills ship inside the package, one directory per skill in the Agent Skills format.
`sync` puts an absolute symlink named `kanso-<name>` into every `[skills] targets`
entry, so any agent whose skills directory is listed there reads the installed
package's copy and an upgrade of kanso upgrades the skills with it. The links are
generated, so the glob that matches them is added to `.gitignore`.

Syncing is idempotent: a correct link is left alone, a link pointing anywhere else is
replaced, and a missing target directory is created.
"""

from __future__ import annotations

from pathlib import Path

from kanso.errors import PreconditionError
from kanso.workspace import PACKAGE_ROOT, Workspace, append_gitignore

SKILLS = PACKAGE_ROOT / "skills"
"""The packaged skills, one directory each."""

LINK_PREFIX = "kanso-"


def packaged_skills() -> list[Path]:
    """The skill directories the package ships, by name."""
    return sorted((d for d in SKILLS.iterdir() if (d / "SKILL.md").is_file()), key=lambda d: d.name)


def sync(ws: Workspace) -> list[tuple[Path, Path]]:
    """Link every packaged skill into every configured target; return `(link, target)`.

    The returned pairs are every link the workspace now has, whether this call created
    it, repointed it or found it already correct.
    """
    skills = packaged_skills()
    pairs: list[tuple[Path, Path]] = []
    ignores: list[str] = []
    for target in ws.config.skills_targets:
        directory = _target_dir(ws, target)
        directory.mkdir(parents=True, exist_ok=True)
        for skill in skills:
            pairs.append((_link(directory / _link_name(skill.name), skill), skill))
        ignore = _ignore_glob(ws, target, directory)
        if ignore is not None:
            ignores.append(ignore)
    append_gitignore(ws.root, ignores)
    return pairs


def _link_name(skill_name: str) -> str:
    return skill_name if skill_name.startswith(LINK_PREFIX) else f"{LINK_PREFIX}{skill_name}"


def _target_dir(ws: Workspace, target: str) -> Path:
    path = Path(target).expanduser()
    return path if path.is_absolute() else ws.path(target)


def _link(link: Path, target: Path) -> Path:
    """Make `link` an absolute symlink to `target`, replacing a stale one."""
    if link.is_symlink():
        if Path(link.readlink()) == target:
            return link
        link.unlink()
    elif link.exists():
        if link.is_dir():
            raise PreconditionError(
                f"{link} is a real directory, not a kanso skill link",
                remedy="move or remove it, then run `kanso skills sync` again",
            )
        link.unlink()
    link.symlink_to(target, target_is_directory=True)
    return link


def _ignore_glob(ws: Workspace, target: str, directory: Path) -> str | None:
    """The `.gitignore` glob covering a target's links, or `None` outside the workspace."""
    try:
        relative = directory.relative_to(ws.root)
    except ValueError:
        return None
    return f"{relative.as_posix()}/{LINK_PREFIX}*"
