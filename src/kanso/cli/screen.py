"""`kanso screen`: measuring a relationship in real data before a lane is spent on it.

`new` scaffolds `screens/<id>/screen.yaml`, bound to a registered hypothesis or free.
`validate` says whether the file is admissible and what running it would measure — the legs,
the window it reads and the cells of every measure, which is the family each measure's
correction is over — and changes nothing.

No command here calls a model, so none exits 2 for want of one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from kanso import screen
from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.schemas.screen import LeadLag
from kanso.workspace import Workspace

app = typer.Typer(
    help="Screens: measure a relationship in real data before spending a lane on it.",
    no_args_is_help=True,
)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]
IdArgument = Annotated[str, typer.Argument(metavar="ID", help="The screen id.")]
PathArgument = Annotated[Path, typer.Argument(metavar="PATH", help="A `screen.yaml`.")]


@app.command("new")
def new_command(
    ctx: typer.Context,
    screen_id: IdArgument,
    hyp_id: Annotated[
        str | None,
        typer.Option("--hyp", metavar="H", help="Bind the screen to a registered hypothesis."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Scaffold `screens/<id>/screen.yaml`, bound to a hypothesis or free."""
    emit(as_json or global_json(ctx), lambda: _new(open_workspace(ctx), screen_id, hyp_id))


@app.command("validate")
def validate_command(ctx: typer.Context, path: PathArgument, as_json: JsonOption = False) -> None:
    """Say whether the file is admissible and what it would measure; change nothing."""
    emit(as_json or global_json(ctx), lambda: _validate(open_workspace(ctx), path))


# -- command bodies ---------------------------------------------------------------


def _new(ws: Workspace, screen_id: str, hyp_id: str | None) -> Report:
    directory = screen.scaffold(ws, screen_id, hyp_id)
    path = directory / screen.SCREEN_FILE
    data: dict[str, Any] = {"id": screen_id, "dir": str(directory), "hyp": hyp_id}
    lines = (
        field("screen", f"{screen_id} · {'bound to ' + hyp_id if hyp_id else 'free'}"),
        field("dir", directory),
        field("next", f"edit {path}, then `kanso screen validate {path}`"),
    )
    return Report(data=data, lines=lines)


def _validate(ws: Workspace, path: Path) -> Report:
    with store(ws) as opened:
        valid = screen.validate(ws, opened, path)
    return Report(data=_summary(valid, path), lines=_summary_lines(valid, path))


def _summary(valid: screen.Validated, path: Path) -> dict[str, Any]:
    spec = valid.screen
    return {
        "id": spec.id,
        "path": str(path),
        "sha": valid.sha,
        "hyp": spec.hyp,
        "window": [str(valid.window[0]), str(valid.window[1])],
        "legs": {
            name: leg.model_dump(mode="json", exclude_none=True) for name, leg in spec.legs.items()
        },
        "derived": sorted(spec.derived),
        "measures": [
            {"id": measure.id, "cells": count}
            for measure, count in zip(spec.measures, valid.cells, strict=True)
        ],
        "cells": sum(valid.cells),
        "verdict": None if spec.verdict is None else spec.verdict.model_dump(mode="json"),
    }


def _summary_lines(valid: screen.Validated, path: Path) -> tuple[str, ...]:
    spec = valid.screen
    lines = [
        field("valid", f"{spec.id} · {path}"),
        field("form", f"bound to {spec.hyp}" if spec.hyp else "free"),
        field("window", f"{valid.window[0]}..{valid.window[1]}"),
        field("legs", ", ".join(f"{name} {leg.instrument}" for name, leg in spec.legs.items())),
    ]
    for index, (measure, count) in enumerate(zip(spec.measures, valid.cells, strict=True)):
        detail = measure.estimator if isinstance(measure, LeadLag) else measure.side
        lines.append(indent(f"measure {index}  {measure.id} · {detail} · {count} cell(s)"))
    lines.append(field("cells", sum(valid.cells)))
    lines.append(field("verdict", "declared" if spec.verdict else "none declared"))
    return tuple(lines)
