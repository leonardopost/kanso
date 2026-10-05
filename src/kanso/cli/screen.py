"""`kanso screen`: measuring a relationship in real data before a lane is spent on it.

`new` scaffolds `screens/<id>/screen.yaml`, bound to a registered hypothesis or free.
`validate` says whether the file is admissible and what running it would measure — the legs,
the window it reads, the cells of every measure, which is the family each measure's
correction is over, and the data it needs — and changes nothing. `run` gets the data, pins,
measures and records (`kanso.screen.run`); `show` reads results back.

A low result is evidence, not a failure: `run` exits 0 whatever it measured.

No command here calls a model, so none exits 2 for want of one.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer

from kanso import screen
from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.errors import ValidationError
from kanso.schemas.screen import Cell, LeadLag, ScreenResult
from kanso.screen import draft, hurdle, records, run
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


@app.command("run")
def run_command(ctx: typer.Context, path: PathArgument, as_json: JsonOption = False) -> None:
    """Get the data, pin, measure and record; the same pins return the stored result."""
    emit(as_json or global_json(ctx), lambda: _run(open_workspace(ctx), path))


@app.command("show")
def show_command(
    ctx: typer.Context,
    screen_id: Annotated[
        str | None, typer.Argument(metavar="ID", help="One screen (default: every screen).")
    ] = None,
    cell: Annotated[
        str | None, typer.Option("--cell", metavar="C", help="One cell of the newest result.")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Show a screen's results, newest first, one cell in full, or every screen."""
    emit(as_json or global_json(ctx), lambda: _show(open_workspace(ctx), screen_id, cell))


@app.command("draft")
def draft_command(
    ctx: typer.Context,
    screen_id: IdArgument,
    cell: Annotated[str, typer.Option("--cell", metavar="C", help="A response cell.")],
    new_id: Annotated[str, typer.Option("--as", metavar="NEW", help="The new hypothesis id.")],
    certify: Annotated[
        str | None,
        typer.Option("--certify", metavar="A..B", help="The certification window; no default."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Write a draft hypothesis from one response cell; register nothing."""
    emit(
        as_json or global_json(ctx),
        lambda: _draft(open_workspace(ctx), screen_id, cell, new_id, certify),
    )


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
        plans = screen.plan(ws, opened, valid.screen, valid.window)
    venues = hurdle.described(ws, valid.screen, valid.hypothesis)
    unresolved = [item.instrument for item in plans if item.state == "unresolved"]
    data = {
        **_summary(valid, path),
        "data": [item.payload() for item in plans],
        "hurdles": venues,
        **({"remedy": screen.data.resolve_remedy(unresolved)} if unresolved else {}),
    }
    lines = _summary_lines(valid, path) + _plan_lines(plans)
    lines += tuple(field("hurdle", f"{venue} · {model['line']}") for venue, model in venues.items())
    return Report(data=data, lines=lines)


def _plan_lines(plans: tuple[screen.LegPlan, ...]) -> tuple[str, ...]:
    """One line per series the screen reads: what it is and what getting it takes."""
    lines: list[str] = []
    for item in plans:
        grain = f" {item.resolution}" if item.resolution else ""
        head = f"{','.join(item.legs)}  {item.instrument} {item.type}{grain} · {item.state}"
        if item.state == "fetchable":
            ready = "configured" if item.configured else "not configured"
            spans = ", ".join(f"{start}..{end}" for start, end in item.missing)
            head += f" · {item.loader} via {item.adapter} ({ready}) · {spans}"
        lines.append(field("data", head) if not lines else indent(head))
        if item.state in ("unresolved", "unserved") and item.reason:
            lines.append(indent(f"  {item.reason}"))
    unresolved = [item.instrument for item in plans if item.state == "unresolved"]
    if unresolved:
        lines.append(field("remedy", screen.data.resolve_remedy(unresolved)))
    return tuple(lines)


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


def _run(ws: Workspace, path: Path) -> Report:
    with store(ws) as opened:
        outcome = run.run(ws, opened, path)
    result = outcome.result
    data: dict[str, Any] = {
        **_result_summary(result),
        "stored": outcome.stored,
        "fetched": [item.payload() for item in outcome.fetched],
        "best": [cell.model_dump(mode="json", exclude_none=True) for cell in _best(result)],
        "result": str(outcome.path),
    }
    lines = [
        field("screen", f"{result.screen} · {result.sha[:7]} · snapshot {result.snapshot[:7]}"),
        field("window", f"{result.window.start}..{result.window.end}"),
    ]
    for item in outcome.fetched:
        grain = f" {item.plan.resolution}" if item.plan.resolution else ""
        lines.append(
            field(
                "fetched",
                f"{item.plan.instrument} {item.plan.type}{grain} · {item.requests} chunk(s)"
                f" · {item.rows} rows",
            )
        )
    lines += [field("cells", len(result.cells)), field("verdict", _verdict(result))]
    lines += [indent(_cell_line(cell)) for cell in _best(result)]
    lines.append(field("cost", f"{result.wall_s:.1f}s · {result.peak_mem_gb:.2f} GB"))
    lines.append(field("stored" if outcome.stored else "written", outcome.path))
    return Report(data=data, lines=tuple(lines))


def _show(ws: Workspace, screen_id: str | None, cell: str | None) -> Report:
    with store(ws) as opened:
        found = records.results(opened, screen_id)
    if screen_id is None:
        newest: dict[str, ScreenResult] = {}
        for result in found:
            newest.setdefault(result.screen, result)
        rows = [field(name, _verdict(result)) for name, result in sorted(newest.items())]
        data: dict[str, Any] = {
            "screens": [_result_summary(result) for _, result in sorted(newest.items())]
        }
        return Report(data=data, lines=tuple(rows) or (field("screens", "none measured"),))
    if not found:
        raise ValidationError(
            f"{screen_id!r} has no recorded result",
            remedy=f"run `kanso screen run screens/{screen_id}/screen.yaml`",
        )
    if cell is not None:
        match = next((item for item in found[0].cells if item.key == cell), None)
        if match is None:
            raise ValidationError(
                f"{cell!r} is not a cell of {screen_id}'s newest result",
                remedy=f"list them with `kanso screen show {screen_id}`",
            )
        document = match.model_dump(mode="json", exclude_none=True)
        return Report(data=document, lines=_cell_detail(match))
    data = {
        "screen": screen_id,
        "results": [_result_summary(item) for item in found],
        "cells": [item.model_dump(mode="json", exclude_none=True) for item in found[0].cells],
    }
    lines = [
        field(result.created_at.strftime("%Y-%m-%d"), f"{result.sha[:7]} · {_verdict(result)}")
        for result in found
    ]
    listed = [
        _cell_line(item) + (f" · {item.reason}" if item.reason else "")
        for item in _ranked(found[0])
    ]
    lines += [field("cells", listed[0]), *(indent(line) for line in listed[1:])] if listed else []
    return Report(data=data, lines=tuple(lines))


def _ranked(result: ScreenResult) -> list[Cell]:
    """Every cell of a result: the passing ones in the verdict's rank, then the rest by |t|."""
    by_key = {cell.key: cell for cell in result.cells}
    passing = [by_key[key] for key in result.summary.best]
    rest = sorted(
        (cell for cell in result.cells if cell.key not in set(result.summary.best)),
        key=lambda cell: (-abs(cell.t), cell.key),
    )
    return passing + rest


def _cell_detail(cell: Cell) -> tuple[str, ...]:
    """One cell in full: its line, why it was judged so, its folds and what qualifies it."""
    lines = [field("cell", _cell_line(cell))]
    if cell.reason:
        lines.append(field("reason", cell.reason))
    if cell.response is not None:
        stats = cell.response
        lines.append(
            field(
                "events",
                f"{stats.events} over {cell.sessions} session(s) · hit rate {stats.hit_rate:.3g}"
                f" · unfilled {stats.unfilled} · drift-adjusted {stats.drift_adjusted_bp:+.4g} bp"
                f" an event (what p tests)",
            )
        )
        unit = "net bp a day"
    else:
        unit = "mean"
    folds = ", ".join("—" if value is None else f"{value:+.4g}" for value in cell.folds)
    lines.append(field("folds", f"{folds} ({unit}) · {cell.folds_same_sign} share the sign"))
    if cell.staleness:
        stale = ", ".join(f"{leg} {value:.3g}" for leg, value in sorted(cell.staleness.items()))
        lines.append(field("staleness", stale))
    flags = [
        name
        for name, on in (("clock_bound", cell.clock_bound), ("in_sample_fit", cell.in_sample_fit))
        if on
    ]
    if flags:
        lines.append(field("flags", ", ".join(flags)))
    return tuple(lines)


def _result_summary(result: ScreenResult) -> dict[str, Any]:
    return {
        "screen": result.screen,
        "sha": result.sha,
        "snapshot": result.snapshot,
        "version": result.version,
        "window": [str(result.window.start), str(result.window.end)],
        "hyp": result.hyp,
        "created_at": result.created_at.isoformat(),
        "cells": len(result.cells),
        "verdict": result.summary.model_dump(mode="json"),
        "wall_s": result.wall_s,
        "peak_mem_gb": result.peak_mem_gb,
    }


def _best(result: ScreenResult, count: int = 5) -> list[Cell]:
    """The cells worth reading first: the passing ones in rank, else the strongest by |t|."""
    by_key = {cell.key: cell for cell in result.cells}
    ranked = [by_key[key] for key in result.summary.best]
    if not ranked:
        ranked = sorted(result.cells, key=lambda cell: (-abs(cell.t), cell.key))
    return ranked[:count]


def _verdict(result: ScreenResult) -> str:
    summary = result.summary
    if not summary.declared:
        return "none declared"
    worth = "yes" if summary.worth_a_lane else "no"
    return (
        f"worth a lane: {worth} · {summary.passed} pass · {summary.failed} fail"
        f" · {summary.thin} thin"
    )


def _cell_line(cell: Cell) -> str:
    judged = f"{cell.judged} · " if cell.judged else ""
    if cell.response is not None:
        stats = cell.response
        return (
            f"{judged}{cell.key}  ceiling {stats.ceiling_bp_day:+.4g} bp/day · margin "
            f"{stats.margin_bp:+.4g} bp (gross {stats.gross_bp:+.4g}, hurdle "
            f"{stats.hurdle_bp:.3g}) · {stats.events_per_day:.3g} a day · t {cell.t:+.2f}"
            f" · p {cell.p:.4g}"
        )
    return (
        f"{judged}{cell.key}  mean {cell.mean:+.4g} ± {cell.se:.2g} · t {cell.t:+.2f}"
        f" · p {cell.p:.4g} · {cell.sessions} session(s)"
    )


def _draft(ws: Workspace, screen_id: str, cell: str, new_id: str, certify: str | None) -> Report:
    span = None if certify is None else _span(certify)
    with store(ws) as opened:
        written = draft.draft(ws, opened, screen_id, cell, new_id, span)
    path = written.directory / "hypothesis.yaml"
    lines = (
        field("drafted", f"{written.hyp_id} · from {screen_id}, cell {cell}"),
        field("dir", written.directory),
        field("next", f"kanso hyp validate {path}, then `kanso hyp add` and `kanso classify`"),
    )
    return Report(data=written.payload(), lines=lines)


def _span(text: str) -> tuple[date, date]:
    """`YYYY-MM-DD..YYYY-MM-DD`, refused when it is not one or ends before it starts."""
    start, separator, end = text.partition("..")
    try:
        span = (date.fromisoformat(start), date.fromisoformat(end))
    except ValueError:
        span = None
    if not separator or span is None or span[1] < span[0]:
        raise ValidationError(
            f"--certify: {text!r} is not a window", remedy="write YYYY-MM-DD..YYYY-MM-DD"
        )
    return span
