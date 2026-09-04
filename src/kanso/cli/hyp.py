"""`kanso hyp`: scaffolding a hypothesis, validating it, registering it, retiring it.

A hypothesis is a file the operator writes and kanso pins. `new` renders the three files a
run is scoped to; `validate` says whether the file is admissible and changes nothing;
`add` registers it or re-pins it under the sha256 of its bytes; `show` reports what is
registered and `retire` ends its life.

`validate` and `add` are the operator's override path for a classification: edit the file,
check it, re-pin it. Both refuse a file that is not admissible with exit 3, and `add` and
`retire` refuse a hypothesis with an active run with exit 2, because a run is pinned to the
bytes it began with and moving the pin under it would silently change what it is testing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, cast

import typer

from kanso import hyp
from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.hyp import Registration
from kanso.schemas import Hypothesis
from kanso.state import StateStore
from kanso.workspace import Workspace

app = typer.Typer(
    help="Hypotheses: scaffold, validate, register, show, retire.", no_args_is_help=True
)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]
IdArgument = Annotated[str, typer.Argument(metavar="ID", help="The hypothesis id.")]
PathArgument = Annotated[Path, typer.Argument(metavar="PATH", help="A `hypothesis.yaml`.")]


@app.command("new")
def new_command(ctx: typer.Context, hyp_id: IdArgument, as_json: JsonOption = False) -> None:
    """Scaffold `hypotheses/<id>/` with the hypothesis, the program and a strategy stub."""
    emit(as_json or global_json(ctx), lambda: _new(open_workspace(ctx), hyp_id))


@app.command("validate")
def validate_command(ctx: typer.Context, path: PathArgument, as_json: JsonOption = False) -> None:
    """Say whether the file is admissible, and change nothing either way."""
    emit(as_json or global_json(ctx), lambda: _validate(open_workspace(ctx), path))


@app.command("add")
def add_command(ctx: typer.Context, path: PathArgument, as_json: JsonOption = False) -> None:
    """Register the hypothesis, or re-pin an already registered one."""
    emit(as_json or global_json(ctx), lambda: _add(open_workspace(ctx), path))


@app.command("show")
def show_command(
    ctx: typer.Context,
    hyp_id: Annotated[
        str | None, typer.Argument(metavar="ID", help="One hypothesis (default: list them).")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Show one registration, or list every one of them."""
    emit(as_json or global_json(ctx), lambda: _show(open_workspace(ctx), hyp_id))


@app.command("retire")
def retire_command(ctx: typer.Context, hyp_id: IdArgument, as_json: JsonOption = False) -> None:
    """Retire a hypothesis. Its cards, blobs and certificates stay in state."""
    emit(as_json or global_json(ctx), lambda: _retire(open_workspace(ctx), hyp_id))


# -- command bodies ---------------------------------------------------------------


def _new(ws: Workspace, hyp_id: str) -> Report:
    directory = hyp.scaffold(ws, hyp_id)
    files = sorted(path.name for path in directory.iterdir())
    data: dict[str, Any] = {"id": hyp_id, "dir": str(directory), "files": files}
    lines = (
        field("hypothesis", hyp_id),
        field("dir", directory),
        field("files", ", ".join(files)),
        field("next", f"edit the files, then `kanso hyp add {directory / hyp.HYPOTHESIS_FILE}`"),
    )
    return Report(data=data, lines=lines)


def _validate(ws: Workspace, path: Path) -> Report:
    hypothesis = hyp.validate(ws, path)
    return Report(data=_summary(hypothesis, path), lines=_summary_lines(hypothesis, path, "valid"))


def _add(ws: Workspace, path: Path) -> Report:
    with store(ws) as opened:
        hypothesis = hyp.add(ws, opened, path)
        registration = _registration(ws, opened, hypothesis.id)
    data = {**_summary(hypothesis, path), **registration.payload()}
    lines = _summary_lines(hypothesis, path, "registered") + (
        field("status", registration.status),
        field("sha", registration.hypothesis_sha),
    )
    return Report(data=data, lines=lines)


def _show(ws: Workspace, hyp_id: str | None) -> Report:
    with store(ws) as opened:
        found = hyp.show(ws, opened, hyp_id)
    if isinstance(found, Registration):
        lines = [
            field("hypothesis", found.hyp_id),
            field("status", found.status),
            field("sha", found.hypothesis_sha),
            field("pinned", "yes" if found.pinned else "no (the workspace file has moved)"),
            field("construct", found.construct or "unclassified"),
            field("objective", found.objective or "unclassified"),
            field("best", _best(found)),
            field("run", found.active_run or "none active"),
        ]
        return Report(data=found.payload(), lines=tuple(lines))
    data: dict[str, Any] = {"hypotheses": [item.payload() for item in found]}
    rows = [
        field(item.hyp_id, f"{item.status} · {item.construct or 'unclassified'} · {_best(item)}")
        for item in found
    ]
    return Report(data=data, lines=tuple(rows) or (field("hypotheses", "none registered"),))


def _registration(ws: Workspace, opened: StateStore, hyp_id: str) -> Registration:
    """The registration of one id; `show` answers with a list only when given no id."""
    return cast(Registration, hyp.show(ws, opened, hyp_id))


def _retire(ws: Workspace, hyp_id: str) -> Report:
    with store(ws) as opened:
        hyp.retire(ws, opened, hyp_id)
        registration = _registration(ws, opened, hyp_id)
    return Report(
        data=registration.payload(),
        lines=(field("hypothesis", hyp_id), field("status", registration.status)),
    )


def _best(registration: Registration) -> str:
    if registration.best_sha is None:
        return "no keep yet"
    return f"{registration.best_sha[:7]} at {registration.best_metric:.6f}"


def _summary(hypothesis: Hypothesis, path: Path) -> dict[str, Any]:
    windows = hypothesis.windows
    return {
        "id": hypothesis.id,
        "path": str(path),
        "mechanism": hypothesis.mechanism,
        "universe": list(hypothesis.universe),
        "resolution": hypothesis.resolution,
        "data_requirements": list(hypothesis.data_requirements),
        "windows": {
            "research": [str(windows.research.start), str(windows.research.end)],
            "certification": [str(windows.certification.start), str(windows.certification.end)],
            "forward": [str(windows.forward.start), None],
        },
        "construct": None if hypothesis.construct is None else hypothesis.construct.id,
        "objective": None if hypothesis.objective is None else hypothesis.objective.id,
        "constraints": [item.id for item in hypothesis.constraints or []],
    }


def _summary_lines(hypothesis: Hypothesis, path: Path, verdict: str) -> tuple[str, ...]:
    windows = hypothesis.windows
    return (
        field(verdict, f"{hypothesis.id} · {path}"),
        field("universe", ", ".join(hypothesis.universe)),
        field("grain", f"{hypothesis.resolution} · {', '.join(hypothesis.data_requirements)}"),
        indent(f"research      {windows.research.start}..{windows.research.end}"),
        indent(f"certification {windows.certification.start}..{windows.certification.end}"),
        indent(f"forward       {windows.forward.start}.."),
    )
