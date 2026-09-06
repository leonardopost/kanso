"""`kanso data`: filling the catalog, reporting what is in it, and freezing it.

The three writing verbs share one mechanism and differ only in which days they ask for.
`load` writes the range its spec names, `backfill` walks history backwards to what is held
and `sync` walks forwards from what is held; `show` reports the served spans and the holes
between them, and `snapshot` freezes what is held into the thing a run is pinned to.

`instruments resolve` and `instruments show` are the reference half, and `adapters` says
what is registered here: the package's own loaders, the manual instrument provider and
every vendor adapter, with the credential names each needs and where they resolve from —
never a value. Without `--check` it reaches nothing, so it answers in a workspace with no
vendor variable set at all.

`--check` asks the other question, and it is a different one: not what an adapter offers
but what *this* key reaches, and how far back. Entitlement and the history floor are both
facts about a plan on a day, so both are probed rather than declared, at the grain the
source gates on. Keeping them apart is the point: a dataset a plan excludes and a range
older than the source holds are one sentence at some vendors and two very different
problems here, and confusing them sends an operator to buy what they already have.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer

from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.data import commands
from kanso.data.registry import Survey
from kanso.errors import Exit, ValidationError
from kanso.workspace import Workspace

app = typer.Typer(
    help="The catalog: loaders, datasets, snapshots and instruments.", no_args_is_help=True
)
instruments_app = typer.Typer(help="The instrument registry.", no_args_is_help=True)
app.add_typer(instruments_app, name="instruments")

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]
LoaderOption = Annotated[str, typer.Option("--loader", metavar="ID", help="Which loader to run.")]
SpecOption = Annotated[Path, typer.Option("--spec", metavar="FILE", help="The loader's spec file.")]


def as_date(text: str | None, option: str) -> date | None:
    """A dated option as a date, refused as a validation failure when it is not one.

    Dates are taken as text and parsed here rather than by the argument parser, so a
    malformed one exits 3 like every other bad operator input instead of 2, which the
    exit-code contract reserves for a precondition.
    """
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValidationError(
            f"{option}: {text!r} is not a date", remedy="write it as YYYY-MM-DD"
        ) from None


@app.command("load")
def load_command(
    ctx: typer.Context,
    loader: LoaderOption,
    spec: SpecOption,
    replace: Annotated[
        bool, typer.Option("--replace", help="Delete and rewrite an overlapped span.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Run a loader over the range its spec names and write what it serves."""
    emit(as_json or global_json(ctx), lambda: _load(open_workspace(ctx), loader, spec, replace))


@app.command("show")
def show_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """List the datasets, the spans they served and the gaps between them."""
    emit(as_json or global_json(ctx), lambda: _show(open_workspace(ctx)))


@app.command("snapshot")
def snapshot_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Freeze what the workspace holds into a snapshot a run can be pinned to."""
    emit(as_json or global_json(ctx), lambda: _snapshot(open_workspace(ctx)))


@app.command("backfill")
def backfill_command(
    ctx: typer.Context,
    loader: LoaderOption,
    spec: SpecOption,
    start: Annotated[
        str | None, typer.Option("--from", metavar="DATE", help="Start here, not at the floor.")
    ] = None,
    end: Annotated[
        str | None, typer.Option("--to", metavar="DATE", help="Stop here, not at what is held.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the chunks and the estimate, fetch nothing.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Fill history down to the source's floor and close the gaps inside what is held."""
    emit(
        as_json or global_json(ctx),
        lambda: _backfill(open_workspace(ctx), loader, spec, start, end, dry_run),
    )


@app.command("sync")
def sync_command(
    ctx: typer.Context,
    loader: Annotated[
        str | None, typer.Option("--loader", metavar="ID", help="Only this loader's datasets.")
    ] = None,
    dataset: Annotated[
        str | None,
        typer.Option("--dataset", metavar="D", help="Only this one, newest of its series or not."),
    ] = None,
    to: Annotated[
        str | None, typer.Option("--to", metavar="DATE", help="Stop here (default: today).")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Extend each held series from its newest dataset's served end into a successor."""
    emit(as_json or global_json(ctx), lambda: _sync(open_workspace(ctx), loader, dataset, to))


@app.command("adapters")
def adapters_command(
    ctx: typer.Context,
    check: Annotated[
        bool, typer.Option("--check", help="Probe what each configured adapter reaches.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """List what is registered here: its kind, its credentials and what it can do."""
    emit(as_json or global_json(ctx), lambda: _adapters(open_workspace(ctx), check))


@instruments_app.command("resolve")
def instruments_resolve(
    ctx: typer.Context,
    ids: Annotated[
        list[str] | None, typer.Argument(metavar="ID...", help="Ids (default: the cache's own).")
    ] = None,
    as_of: Annotated[
        str | None, typer.Option("--as-of", metavar="DATE", help="The date to resolve as of.")
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Resolve again rather than answer from the cache.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Resolve ids into the catalog's instrument store and the workspace cache."""
    emit(
        as_json or global_json(ctx),
        lambda: _resolve(open_workspace(ctx), ids or [], as_of, refresh),
    )


@instruments_app.command("show")
def instruments_show(
    ctx: typer.Context,
    instrument: Annotated[
        str | None, typer.Argument(metavar="ID", help="One instrument (default: list them).")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Show a resolved definition, or list the ones the catalog holds."""
    emit(as_json or global_json(ctx), lambda: _instruments_show(open_workspace(ctx), instrument))


# -- command bodies ---------------------------------------------------------------


def _load(ws: Workspace, loader: str, spec: Path, replace: bool) -> Report:
    with store(ws) as opened:
        result = commands.load(ws, opened, loader, spec, replace=replace)
    lines = [field("loader", f"{result.loader} · {result.spec}")]
    for written in result.written:
        manifest = written.manifest
        lines.append(
            f"{manifest.dataset_id} · {manifest.start}..{manifest.end} · "
            f"{manifest.row_count} rows · {manifest.publication}"
        )
        if written.shortfall:
            lines.append(indent(written.shortfall))
        for replaced in written.replaced:
            lines.append(indent(f"replaced {replaced}"))
    lines.append(field("total", f"{len(result.written)} dataset(s) · {result.rows} rows"))
    return Report(data=result.payload(), lines=tuple(lines))


def _show(ws: Workspace) -> Report:
    found = commands.series(ws)
    data: dict[str, Any] = {
        "series": [item.payload() for item in found],
        "datasets": len(list(commands.dataset_ids(found))),
        "rows": sum(item.rows for item in found),
    }
    if not found:
        return Report(data=data, lines=(field("datasets", "none held"),))
    lines: list[str] = []
    for item in found:
        resolution = f" {item.resolution}" if item.resolution else ""
        spans = ", ".join(f"{start}..{end}" for start, end in item.spans)
        lines.append(f"{item.instrument} {item.type}{resolution} · {spans} · {item.rows} rows")
        for manifest in item.datasets:
            lines.append(
                indent(f"{manifest.dataset_id} · {manifest.source} · {manifest.row_count} rows")
            )
        for start, end in item.gaps:
            lines.append(indent(f"gap {start}..{end}"))
    lines.append(field("total", f"{data['datasets']} dataset(s) · {data['rows']} rows"))
    return Report(data=data, lines=tuple(lines))


def _snapshot(ws: Workspace) -> Report:
    with store(ws) as opened:
        frozen = commands.freeze(ws, opened)
    data = frozen.model_dump(mode="json", by_alias=True)
    reproducible = "reproducible" if frozen.reproducible else "not reproducible (adjusted data)"
    lines = (
        field("snapshot", frozen.snapshot_id),
        field("datasets", f"{len(frozen.datasets)} · {reproducible}"),
        field("instrument", frozen.instruments_checksum),
    )
    return Report(data=data, lines=lines)


def _backfill(
    ws: Workspace, loader: str, spec: Path, start: str | None, end: str | None, dry_run: bool
) -> Report:
    with store(ws) as opened:
        result = commands.backfill(
            ws,
            opened,
            loader,
            spec,
            start=as_date(start, "--from"),
            end=as_date(end, "--to"),
            dry_run=dry_run,
        )
    lines = [field("loader", f"{result.loader} · {result.spec}")]
    lines += [indent(clamp) for clamp in result.clamps]
    lines += [indent(note) for note in result.notes]
    for fetch in result.fetches:
        detail = (
            f"{fetch.est_rows} rows, {fetch.est_bytes} bytes (estimated)"
            if dry_run
            else f"{fetch.outcome}{'' if fetch.rows == 0 else f' · {fetch.rows} rows'}"
        )
        lines.append(
            f"{fetch.chunk.start}..{fetch.chunk.end} {fetch.instrument} {fetch.type} → {detail}"
        )
    summary = f"{result.requests} chunk(s)"
    lines.append(
        field("total", f"{summary} · {'planned' if dry_run else f'{result.rows} rows written'}")
    )
    return Report(data=result.payload(), lines=tuple(lines))


def _sync(ws: Workspace, loader: str | None, dataset: str | None, to: str | None) -> Report:
    with store(ws) as opened:
        result = commands.sync(
            ws, opened, loader_id=loader, dataset=dataset, to=as_date(to, "--to")
        )
    lines = [field("to", result.to)]
    for fetch in result.fetches:
        lines.append(
            f"{fetch.chunk.start}..{fetch.chunk.end} {fetch.instrument} {fetch.type} → "
            f"{fetch.outcome}{'' if fetch.rows == 0 else f' · {fetch.rows} rows'}"
        )
    lines += [indent(note) for note in result.notes]
    lines.append(field("total", f"{len(result.fetches)} chunk(s) · {result.rows} rows"))
    return Report(data=result.payload(), lines=tuple(lines))


def _adapters(ws: Workspace, check: bool) -> Report:
    found, notes = commands.adapters(ws)
    surveys: list[Survey] = []
    if check:
        surveys, checked = commands.check_adapters(ws)
        notes = [*notes, *checked]
    data: dict[str, Any] = {
        "adapters": [item.payload() for item in found],
        "checked": check,
        "reach": [item.payload() for item in surveys],
        "notes": notes,
    }
    lines: list[str] = []
    for item in found:
        lines.append(
            f"{item.id} · {item.kind} · {item.provider} · {_credentials(item)} · "
            f"{', '.join(item.capabilities)}"
        )
        if item.quota is not None:
            lines.append(indent(f"quota {item.quota}"))
        if item.loaders:
            lines.append(indent(f"loaders {', '.join(item.loaders)}"))
        lines += [indent(f"{name}: {origin or 'unset'}") for name, origin in _origins(item)]
    lines += [line for survey in surveys for line in _reach_lines(survey)]
    lines += [indent(note) for note in notes]
    # A configured key the vendor will not accept is a precondition failure, not a
    # listing: every later command through that adapter stops on it, and an operator
    # agent branches on the code before it reads the object.
    refused = [survey.adapter for survey in surveys if not survey.reachable]
    return Report(data=data, lines=tuple(lines), code=Exit.PRECONDITION if refused else Exit.OK)


def _credentials(item: commands.Registered) -> str:
    """How this source's credentials stand, in three words and never a value."""
    if not item.credentials:
        return "no credential"
    resolved = sum(1 for name in item.credentials if item.origins.get(name) is not None)
    return f"{resolved}/{len(item.credentials)} credentials resolve"


def _origins(item: commands.Registered) -> list[tuple[str, str | None]]:
    """Each declared variable and where it resolves from — `.env`, `environment`, nowhere."""
    return [(name, item.origins.get(name)) for name in item.credentials]


def _reach_lines(survey: Survey) -> list[str]:
    """One adapter's measured reach: what its key gets, and from when.

    The outcome and the floor are printed side by side because they are the two answers an
    operator confuses at their own expense: a dataset the plan excludes and a range older
    than the source holds look identical at the vendor and are different problems here.
    """
    head = "reachable" if survey.reachable else "did not authenticate"
    lines = [field(survey.adapter, f"{head} · {survey.detail} · {survey.requests} request(s)")]
    lines += [indent(item.line()) for item in survey.reach]
    lines += [indent(note) for note in survey.notes]
    return lines


def _resolve(ws: Workspace, ids: list[str], as_of: str | None, refresh: bool) -> Report:
    with store(ws) as opened:
        when, resolved = commands.resolve(
            ws, opened, ids, as_of=as_date(as_of, "--as-of"), refresh=refresh
        )
    data: dict[str, Any] = {
        "as_of": str(when),
        "refresh": refresh,
        "instruments": [item.payload() for item in resolved],
    }
    lines = [field("as of", when)]
    lines += [indent(f"{item.id} → {item.fields.get('id', item.id)}") for item in resolved]
    lines.append(field("resolved", f"{len(resolved)} instrument(s)"))
    return Report(data=data, lines=tuple(lines))


def _instruments_show(ws: Workspace, instrument: str | None) -> Report:
    found = commands.held_instruments(ws, instrument)
    data: dict[str, Any] = {"instruments": [item.payload() for item in found]}
    if instrument is None:
        lines = [f"{item.id} · {type(item.definition).__name__}" for item in found] or [
            field("instruments", "none resolved")
        ]
        return Report(data=data, lines=tuple(lines))
    lines = []
    for item in found:
        lines += [field(str(name), value) for name, value in sorted(item.fields.items())]
    return Report(data=data, lines=tuple(lines))
