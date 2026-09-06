"""Lane directories: the three scoped files a run works in, and how they are restored.

A lane is a worker with a directory of its own. `runs/<lane>/<hyp>/` holds exactly
`hypothesis.yaml`, `program.md` and `strategy.py`, and nothing a lane writes is visible
to another lane: two lanes researching two hypotheses, or the interactive lane `op`
beside a daemon lane, never share a path. That isolation is what makes the scope rule
checkable at all — the lane directory is the whole of what a card may touch.

Every file a lane holds also exists as a blob in the state store, so restoring one is a
copy out of the store rather than a diff or a revert: kanso versions the scoped files
itself and never invokes git. Writes go through a temporary file and a rename, so a
reader either sees the previous bytes or the new ones and never a half-written file —
the hypothesis's own `strategy.py` is rewritten after every keep while an agent may be
reading it.

Ending a run removes the lane directory and nothing else. What happened in it is already
elsewhere: every card, its bytes and its verdict are in the state store, `results.tsv` is
rendered from there, and the daemon's own stream goes to `runs/daemon.log`. A lane writes
no log of its own, which is why the directory is disposable.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from kanso.criteria import SCOPED_FILES
from kanso.errors import ValidationError
from kanso.hyp import HYPOTHESIS_FILE, PROGRAM_FILE, STRATEGY_FILE
from kanso.schemas.run import LaneName
from kanso.workspace import LANE_ROOT

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "DEFAULT_LANE",
    "HYPOTHESIS_FILE",
    "LANE_ROOT",
    "PROGRAM_FILE",
    "SCOPED_FILES",
    "STRATEGY_FILE",
    "check_lane",
    "lane_dir",
    "prepare",
    "remove",
    "restore",
    "write_atomic",
]

DEFAULT_LANE: Final = "op"
"""The interactive lane, where an operator or a coding agent works by hand."""

_LANE: Final = TypeAdapter(LaneName)


def check_lane(lane: str) -> str:
    """The lane name, refused when it is not one a directory may be named for."""
    try:
        return str(_LANE.validate_python(lane))
    except PydanticValidationError:
        raise ValidationError(
            f"lane: {lane!r} is not a lane name",
            remedy="use 1 to 16 characters from a-z, 0-9 and _, for example 'op' or 'l1'",
        ) from None


def lane_dir(ws: Workspace, lane: str, hyp_id: str) -> Path:
    """The directory this lane works this hypothesis in: `runs/<lane>/<hyp>/`."""
    return ws.path(LANE_ROOT, check_lane(lane), hyp_id)


def prepare(directory: Path) -> Path:
    """An empty lane directory at `directory`, replacing whatever a dead run left.

    A lane directory holds only copies of blobs, so discarding one loses nothing: the
    bytes of every scoped file of every run are in the state store.
    """
    remove(directory)
    directory.mkdir(parents=True)
    return directory


def remove(directory: Path) -> None:
    """Remove the lane directory and everything in it. Idempotent."""
    shutil.rmtree(directory, ignore_errors=True)


def write_atomic(path: Path, data: bytes) -> Path:
    """Write bytes through a temporary file and a rename, never in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return path


def restore(store: StateStore, directory: Path, files: Mapping[str, str]) -> None:
    """Rewrite each named file in the lane directory from the blob it is pinned to."""
    for name in sorted(files):
        write_atomic(directory / name, store.get_blob(files[name]))
