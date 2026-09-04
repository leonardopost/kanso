"""Scaffolding a hypothesis: the directory and the three files a run is scoped to.

`hyp new` creates `hypotheses/<id>/` and renders the packaged templates into it — the
`hypothesis.yaml` the operator fills in, the `program.md` an agent follows, and a
`strategy.py` stub. Those three files are exactly the run's scope, so the directory holds
them and nothing else; `results.tsv` appears later, rendered from state.

The stub is the sleeve's, because an unclassified hypothesis is a strategy of its own
until classification says otherwise. `stub` renders any construct's, which is what lets
classification replace an untouched stub with the one its construct actually needs: the
rendering is a pure function of the hypothesis id, the construct and the host, so a
caller compares content addresses rather than guessing whether the file was edited.

Scaffolding refuses to write over an existing directory. A hypothesis is scaffolded once
and everything after that is an edit; silently overwriting a `strategy.py` would throw
away work no other copy holds until the first card stores it.
"""

from __future__ import annotations

from datetime import date
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from kanso.classify.construct import MODIFIER_TEMPLATE, SLEEVE_TEMPLATE
from kanso.errors import PreconditionError, ValidationError
from kanso.schemas import HypId

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.workspace import Workspace

TEMPLATES: Final = "kanso.templates"
"""Where the packaged templates live, read through the package rather than the filesystem."""

HYPOTHESES: Final = "hypotheses"
"""The workspace directory holding one directory per hypothesis."""

HYPOTHESIS_FILE: Final = "hypothesis.yaml"
PROGRAM_FILE: Final = "program.md"
STRATEGY_FILE: Final = "strategy.py"
"""The three files a hypothesis directory holds, and a lane directory later copies."""

SLEEVE: Final = "sleeve"
"""The construct an unclassified hypothesis is scaffolded as: a strategy of its own."""

_ID: Final = TypeAdapter(HypId)


def hypothesis_dir(ws: Workspace, hyp_id: str) -> Path:
    """Where a hypothesis's files live."""
    return ws.path(HYPOTHESES, hyp_id)


def hypothesis_file(ws: Workspace, hyp_id: str) -> Path:
    """The `hypothesis.yaml` of one hypothesis."""
    return hypothesis_dir(ws, hyp_id) / HYPOTHESIS_FILE


def check_id(hyp_id: str) -> str:
    """The id, refused when it is not one; ids name directories and state rows."""
    try:
        return str(_ID.validate_python(hyp_id))
    except PydanticValidationError:
        raise ValidationError(
            f"id: {hyp_id!r} is not a hypothesis id",
            remedy="use 3 to 40 characters from a-z, 0-9 and _",
        ) from None


def stub(hyp_id: str, construct: str = SLEEVE, host: str | None = None) -> str:
    """The `strategy.py` a construct starts from, rendered for this hypothesis.

    A sleeve is a strategy of its own and takes no host; every other construct is layered
    on one and is written against it, so the host is part of what the stub says.
    """
    if construct == SLEEVE:
        if host is not None:
            raise ValidationError(
                f"host: a {SLEEVE} is a strategy of its own and attaches to nothing, "
                f"but {host!r} was named as its host"
            )
        return render(SLEEVE_TEMPLATE, hyp_id=hyp_id)
    if host is None:
        raise ValidationError(
            f"host: a {construct} is layered on a host strategy, and none was named"
        )
    return render(MODIFIER_TEMPLATE, hyp_id=hyp_id, construct=construct, host=host)


def scaffold(ws: Workspace, hyp_id: str) -> Path:
    """Create `hypotheses/<id>/` with the hypothesis, the program and the strategy stub.

    Returns the directory. Refuses (precondition) an id whose directory already exists.
    """
    identity = check_id(hyp_id)
    directory = hypothesis_dir(ws, identity)
    if directory.exists():
        raise PreconditionError(
            f"{directory} already exists; a hypothesis is scaffolded once",
            remedy=f"edit the files in place, or choose another id than {identity!r}",
        )
    directory.mkdir(parents=True)
    _write(directory / HYPOTHESIS_FILE, render(HYPOTHESIS_FILE, hyp_id=identity))
    _write(
        directory / PROGRAM_FILE,
        render(PROGRAM_FILE, hyp_id=identity, today=_today()),
    )
    _write(directory / STRATEGY_FILE, stub(identity))
    return directory


def render(name: str, **values: str) -> str:
    """A packaged template with its `{{placeholders}}` filled; an unfilled one is a fault."""
    text = resources.files(TEMPLATES).joinpath(name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    if "{{" in text:
        raise ValidationError(f"{name}: unfilled placeholder")
    return text


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _today() -> str:
    """Today as eight digits; `strftime` does not zero-pad a year below 1000 on glibc."""
    day = date.today()
    return f"{day.year:04d}{day.month:02d}{day.day:02d}"
