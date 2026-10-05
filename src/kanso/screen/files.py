"""A screen's file: where it lives, how it is scaffolded, and what makes it admissible.

`screens/<id>/screen.yaml` is the operator's or an agent's statement of what to measure.
`scaffold` writes it once from the packaged template, bound to a registered hypothesis or
free; `validate` reads it and refuses it unless every rule holds — the file's own, the
measure library's ranges, and, for a bound screen, its hypothesis's: the hypothesis must be
registered, and every leg must be an instrument of its universe read at a type and grain
the hypothesis requires, because a bound screen measures what that hypothesis's research
would see and nothing else. A bound screen's window is its hypothesis's research window,
read from the pinned bytes rather than the workspace file, so an edit nobody has pinned
does not move what the screen reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import hypothesis_of, read_source, show
from kanso.hyp.scaffold import render
from kanso.schemas import HypId, Hypothesis, parse_yaml
from kanso.schemas.screen import Screen, cells
from kanso.screen import library

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

SCREENS: Final = "screens"
"""The workspace directory holding one directory per screen."""

SCREEN_FILE: Final = "screen.yaml"

FREE_BINDING: Final = (
    "window: {start: , end: }           # YYYY-MM-DD; clear of every registered hypothesis's "
    "certification span on a shared instrument\n"
    "# costs: {OKX: {commission_bps: 5}}  # optional, per venue: what the hurdle is struck "
    "under, beside the workspace's venue model"
)
"""What a free screen's template states in place of a hypothesis."""

_ID: Final = TypeAdapter(HypId)


@dataclass(frozen=True)
class Validated:
    """An admissible screen: the file, its pin, the window it reads, the family per measure."""

    screen: Screen
    sha: str
    window: tuple[date, date]
    hypothesis: Hypothesis | None
    cells: tuple[int, ...]


def screen_dir(ws: Workspace, screen_id: str) -> Path:
    """Where a screen's files live."""
    return ws.path(SCREENS, screen_id)


def check_id(screen_id: str) -> str:
    """The id, refused when it is not one; ids name directories and state rows."""
    try:
        return str(_ID.validate_python(screen_id))
    except PydanticValidationError:
        raise ValidationError(
            f"id: {screen_id!r} is not a screen id",
            remedy="use 3 to 40 characters from a-z, 0-9 and _",
        ) from None


def scaffold(ws: Workspace, screen_id: str, hyp_id: str | None = None) -> Path:
    """Write `screens/<id>/screen.yaml` from the template; refuse an id already a directory."""
    identity = check_id(screen_id)
    directory = screen_dir(ws, identity)
    if directory.exists():
        raise PreconditionError(
            f"{directory} already exists; a screen is scaffolded once",
            remedy=f"edit {directory / SCREEN_FILE} in place, or choose another id",
        )
    binding = FREE_BINDING
    if hyp_id is not None:
        binding = (
            f"hyp: {_ID.validate_python(hyp_id)}"
            "                     # bound: its research window, its costs, legs from its universe"
        )
    directory.mkdir(parents=True)
    text = render(SCREEN_FILE, screen_id=identity, binding=binding)
    (directory / SCREEN_FILE).write_text(text, encoding="utf-8")
    return directory


def validate(ws: Workspace, store: StateStore, path: Path) -> Validated:
    """The screen at `path`, refused unless the file, the library and its binding admit it."""
    source = read_source(path)
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{path}: is not UTF-8 text: {exc}") from None
    screen = parse_yaml(Screen, text, str(path))
    _check_location(ws, path, screen)
    library.check(screen)
    hypothesis = None if screen.hyp is None else _bound(ws, store, screen)
    if hypothesis is not None:
        window = (hypothesis.windows.research.start, hypothesis.windows.research.end)
    else:
        assert screen.window is not None
        window = (screen.window.start, screen.window.end)
    return Validated(
        screen=screen,
        sha=sha256(source).hexdigest(),
        window=window,
        hypothesis=hypothesis,
        cells=cells(screen),
    )


def _check_location(ws: Workspace, path: Path, screen: Screen) -> None:
    """A file under `screens/` sits in the directory its own id names."""
    root = ws.path(SCREENS).resolve()
    directory = path.resolve().parent
    if directory.parent == root and directory.name != screen.id:
        raise ValidationError(
            f"id: {screen.id!r} is declared in {SCREENS}/{directory.name}/, and a screen "
            f"lives in the directory its id names",
            remedy=f"rename the directory to {screen.id!r}, or set id to {directory.name!r}",
        )


def _bound(ws: Workspace, store: StateStore, screen: Screen) -> Hypothesis:
    """The registered hypothesis a bound screen measures, its legs held to its universe."""
    assert screen.hyp is not None
    show(ws, store, screen.hyp)
    hypothesis = hypothesis_of(ws, store, screen.hyp)
    for name, leg in screen.legs.items():
        if leg.instrument not in hypothesis.universe:
            raise ValidationError(
                f"legs.{name}: {leg.instrument} is not in {hypothesis.id}'s universe, and a "
                f"bound screen measures what that hypothesis trades",
                remedy="name an instrument of the universe, or make the screen free",
            )
        if leg.type not in hypothesis.data_requirements:
            raise ValidationError(
                f"legs.{name}: {hypothesis.id} does not require {leg.type} data, so its "
                f"research never reads {leg.instrument} as {leg.type}",
                remedy=f"read the leg as one of {', '.join(hypothesis.data_requirements)}",
            )
        if leg.type == "bar" and leg.resolution != hypothesis.resolution:
            raise ValidationError(
                f"legs.{name}: {leg.resolution} bars, where {hypothesis.id} researches "
                f"{hypothesis.resolution}",
                remedy=f"set the leg's resolution to {hypothesis.resolution}",
            )
    return hypothesis
