"""Workspace discovery and scaffolding.

A workspace is a plain directory holding `kanso.toml`. It MAY live inside a git
repository, in a subdirectory of one, or nowhere near one: the three situations are
identical to kanso, which never invokes git. `in_git_repo` is a filesystem check for an
enclosing `.git`; nothing here shells out, and `init` writes the same `.gitignore`
whether or not a repository encloses the workspace.

`find` walks up from a start directory to the nearest `kanso.toml`. The interactive
research loop runs commands with the cwd set to a lane directory `runs/<lane>/<hyp>/`,
so a `kanso.toml` found at or under such a path belongs to the run, not to a workspace:
discovery then re-resolves to the workspace that owns the lane.

`init` renders the file set from `templates/`. It never overwrites `kanso.toml`, never
touches an existing `AGENTS.md`, `CLAUDE.md` or `.env`, and appends to `.gitignore`
rather than replacing it. Anything the operator must do by hand comes back as
`Workspace.notices` for the CLI to print; this module prints nothing.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from kanso import __version__
from kanso.config import Config, load_config, render_config
from kanso.errors import PreconditionError

CONFIG_NAME = "kanso.toml"
"""The file whose presence makes a directory a workspace."""

LANE_ROOT = "runs"
"""Lane directories live at `<workspace>/runs/<lane>/<hyp>/`."""

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES = PACKAGE_ROOT / "templates"

GITIGNORE_HEADER = "# kanso"

AGENTS_FALLBACK = "AGENTS.kanso.md"
"""Where the operator instructions go when the workspace already has an `AGENTS.md`."""

AGENTS_IMPORT = "@AGENTS.md"
"""The one line `CLAUDE.md` contains."""


@dataclass(frozen=True)
class Workspace:
    """A workspace root and its parsed configuration.

    `notices` carries the sentences `init` wants the operator to read (a file it left
    alone and the line to add to it). It is empty for a workspace obtained from `find`.
    """

    root: Path
    config: Config
    notices: tuple[str, ...] = field(default=())

    def path(self, *parts: str) -> Path:
        """A path inside the workspace."""
        return self.root.joinpath(*parts)


def find(start: Path | None = None) -> Workspace:
    """The workspace enclosing `start` (*default* the current directory).

    Raises `PreconditionError` when no `kanso.toml` is at or above `start`.
    """
    here = Path(start).expanduser() if start is not None else Path.cwd()
    here = here.resolve()
    if here.is_file():
        here = here.parent
    found = _nearest_config(here)
    if found is None:
        raise PreconditionError(
            f"not inside a kanso workspace: no {CONFIG_NAME} at or above {here}",
            remedy=(
                "run `kanso init` here, or pass --workspace PATH to a directory holding "
                f"{CONFIG_NAME}"
            ),
        )
    root = _owning_workspace(found)
    return Workspace(root=root, config=load_config(root / CONFIG_NAME))


def lane_owner(path: Path) -> Path | None:
    """The workspace root owning `path` when `path` is a lane directory.

    A lane directory is `<workspace>/runs/<lane>/<hyp>/` and the workspace above it is
    the one that owns the run. Returns `None` when `path` is not shaped like one.
    """
    parents = path.parents
    if len(parents) < 3 or parents[1].name != LANE_ROOT:
        return None
    owner = parents[2]
    return owner if (owner / CONFIG_NAME).is_file() else None


def in_git_repo(directory: Path) -> Path | None:
    """The root of the repository enclosing `directory`, by filesystem check alone.

    Looks for `.git` at each ancestor — a directory in a normal clone, a file in a
    worktree or submodule. kanso never runs git, so this is the whole of its repository
    awareness and it reports rather than acts.
    """
    here = Path(directory).expanduser().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def gitignore_entries() -> list[str]:
    """The entries `init` guarantees, in template order.

    The template's comments are advisory (the commented `catalog/` line is the
    operator's choice); only its bare lines are normative.
    """
    lines = _template("gitignore").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def append_gitignore(root: Path, entries: Iterable[str]) -> list[str]:
    """Append the entries `.gitignore` lacks and return them; create the file if absent.

    Idempotent: an entry already present, commented or not, is never added twice.
    """
    path = root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    present = {ln.strip() for ln in text.splitlines()}
    missing = [e for e in dict.fromkeys(entries) if e not in present]
    if not missing:
        return []
    if text and not text.endswith("\n"):
        text += "\n"
    if text:
        text += "\n"
    path.write_text(text + "\n".join([GITIGNORE_HEADER, *missing]) + "\n", encoding="utf-8")
    return missing


def init(directory: Path, demo: bool = False) -> Workspace:
    """Scaffold a workspace in `directory`, creating it if needed.

    Writes `kanso.toml`, the operator instructions, `.gitignore`, an empty `.env` the
    operator owns, `models.yaml`, `instruments.yaml`, `portfolio.yaml`,
    `escalations/inbox.md`, and the `hypotheses/` and `catalog/` directories. With
    `demo` it renders the mock-only register, the synthetic loader spec, the `DEMO`
    instrument and the `demo_mr` hypothesis instead of the placeholders.

    Raises `PreconditionError` when `kanso.toml` already exists: a workspace is
    scaffolded once, and refreshing one is `skills sync` and `env detect`.
    """
    root = Path(directory).expanduser().resolve()
    config_path = root / CONFIG_NAME
    if config_path.exists():
        raise PreconditionError(
            f"{config_path} already exists; a workspace is scaffolded once",
            remedy="run `kanso skills sync` and `kanso env detect` to refresh this workspace",
        )
    root.mkdir(parents=True, exist_ok=True)

    _write(config_path, render_config(__version__))
    notices = _write_instructions(root)
    _write_gitignore(root)
    _write_env(root)
    prefix = "demo/" if demo else ""
    _write_new(root / "models.yaml", _template(f"{prefix}models.yaml"))
    _write_new(root / "instruments.yaml", _template(f"{prefix}instruments.yaml"))
    _write_new(root / "portfolio.yaml", _template("portfolio.yaml"))
    _write_new(root / "escalations" / "inbox.md", _template("inbox.md"))
    (root / "hypotheses").mkdir(exist_ok=True)
    (root / "catalog").mkdir(exist_ok=True)
    if demo:
        _write_demo(root)

    return Workspace(root=root, config=load_config(config_path), notices=tuple(notices))


def _nearest_config(here: Path) -> Path | None:
    for candidate in [here, *here.parents]:
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    return None


def _owning_workspace(root: Path) -> Path:
    """Climb out of any lane directory the candidate root sits in."""
    while True:
        owner = lane_owner(root)
        if owner is None:
            return root
        root = owner


def _template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _render(text: str, **values: str) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_new(path: Path, text: str) -> bool:
    """Write `text` unless the file exists; report whether it was written."""
    if path.exists():
        return False
    _write(path, text)
    return True


def _write_instructions(root: Path) -> list[str]:
    """Write `AGENTS.md` and `CLAUDE.md`, leaving either alone if it already exists."""
    notices: list[str] = []
    agents = _template("AGENTS.md")
    if not _write_new(root / "AGENTS.md", agents):
        _write(root / AGENTS_FALLBACK, agents)
        notices.append(
            f"AGENTS.md exists and was left untouched; the kanso instructions are in "
            f"{AGENTS_FALLBACK} — add this line to AGENTS.md: @{AGENTS_FALLBACK}"
        )
    if not _write_new(root / "CLAUDE.md", _template("CLAUDE.md")):
        notices.append(
            f"CLAUDE.md exists and was left untouched; add this line to it: {AGENTS_IMPORT}"
        )
    return notices


def _write_gitignore(root: Path) -> None:
    path = root / ".gitignore"
    if path.exists():
        append_gitignore(root, gitignore_entries())
        return
    _write(path, _template("gitignore"))


def _write_env(root: Path) -> None:
    """Create an empty `.env` readable only by its owner; never touch an existing one.

    kanso reads this file at each use and writes it never: it holds credentials.
    """
    path = root / ".env"
    if path.exists():
        return
    path.touch()
    os.chmod(path, 0o600)


def _write_demo(root: Path) -> None:
    _write_new(root / "demo.yaml", _template("demo/loader.yaml"))
    _write_new(root / "mock" / "responses.yaml", _template("demo/responses.yaml"))
    hyp = root / "hypotheses" / "demo_mr"
    _write_new(hyp / "hypothesis.yaml", _template("demo/hypothesis.yaml"))
    _write_new(
        hyp / "program.md",
        _render(
            _template("program.md"),
            hyp_id="demo_mr",
            today=date.today().strftime("%Y%m%d"),
        ),
    )
