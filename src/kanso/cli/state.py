"""`kanso state prune`: give back the space the redundancy rule's superseded books hold.

Every judged card stores its book under its run's pins, and nothing deletes one as research
moves on, so `signatures` keeps the books of every file since re-pinned, every kanso since
upgraded, every snapshot since moved past and every hypothesis since retired. No run can
select those again (`research.records.superseded` says exactly which), and they can be
nearly the whole of `state.db`.

The command deletes them and rewrites the file, and each of its refusals is there because
the alternative loses something. The daemon is held off by its own lock for the whole of
it, so no lane writes a card while the store is rewritten and no `start` begins one. The
database is copied whole into `runs/` before anything is deleted, so the books can be had
back by putting that copy in its place. And nothing starts unless the disk beside
`state.db` holds the copy and twice what is kept — SQLite rewrites the file through a
temporary copy and the write-ahead log — so a prune does not fill the disk it runs on.

A prune refused between the delete and the rewrite has deleted the books and copied them,
and given nothing back yet; the next prune finds no rows to delete and rewrites the file.
`--dry-run` counts and writes nothing, so it does not need the daemon stopped.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

import typer

from kanso.cli.context import STATE_DB, global_json, open_workspace, store
from kanso.cli.render import Report, emit, field
from kanso.criteria import criteria_version
from kanso.errors import PreconditionError
from kanso.research import daemon, records
from kanso.state import Footprint
from kanso.workspace import LANE_ROOT, Workspace

app = typer.Typer(help="The state store.", no_args_is_help=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]

PRUNED: Final = "signatures_pruned"
"""The event a prune appends: how many books went, how many stayed, and where the copy is."""

MB: Final = 1_000_000


@app.command("prune")
def prune_command(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Count what would go, and write nothing.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Delete the signatures no run can select, after a backup, and give the space back."""
    emit(as_json or global_json(ctx), lambda: _prune(open_workspace(ctx), dry_run))


# -- command body -----------------------------------------------------------------


def _prune(ws: Workspace, dry_run: bool) -> Report:
    criteria = criteria_version()
    if dry_run:
        with store(ws) as opened:
            plan = records.superseded(opened, criteria)
            before = opened.footprint()
        return _report(plan, before, None, None, criteria)
    with daemon.held_off(ws), store(ws) as opened:
        plan = records.superseded(opened, criteria)
        before = opened.footprint()
        backup: Path | None = None
        if plan.rows:
            _room(ws, before.used + 2 * max(0, before.used - plan.size))
            backup = ws.path(LANE_ROOT, f"state-{datetime.now(tz=UTC):%Y%m%dT%H%M%SZ}.db")
            opened.backup(backup)
            with opened.transaction():
                deleted = records.prune(opened, criteria)
                plan = replace(plan, rows=deleted, kept=plan.kept + plan.rows - deleted)
                opened.event(
                    PRUNED, "", {"rows": plan.rows, "kept": plan.kept, "backup": str(backup)}
                )
        freed = opened.footprint()
        if freed.free:
            _room(ws, 2 * freed.used)
            opened.vacuum()
        after = opened.footprint()
    return _report(plan, before, after, backup, criteria)


def _room(ws: Workspace, needed: int) -> None:
    """Refuse unless the disk `state.db` is on has `needed` bytes free."""
    free = shutil.disk_usage(ws.path(STATE_DB).parent).free
    if free < needed:
        raise PreconditionError(
            f"pruning {ws.path(STATE_DB)} needs {_mb(needed)} free on its disk — a copy of the "
            f"store and twice what it keeps, for SQLite to rewrite it — and there is {_mb(free)}",
            remedy="free the space, then run `kanso state prune` again",
        )


def _report(
    plan: records.Superseded,
    before: Footprint,
    after: Footprint | None,
    backup: Path | None,
    criteria: str,
) -> Report:
    """What went, what stayed and what the file came to; `after` is `None` on a dry run."""
    data: dict[str, object] = {
        "dry_run": after is None,
        "criteria_version": criteria,
        "pruned": plan.rows,
        "pruned_bytes": plan.size,
        "kept": plan.kept,
        "backup": None if backup is None else str(backup),
        "vacuumed": after is not None and after.size < before.size,
        "size_before": before.size,
        "size_after": None if after is None else after.size,
    }
    books = f"{plan.rows} signature(s), {_mb(plan.size)} of books · {plan.kept} kept"
    if after is None:
        lines = [
            field("would prune", books),
            field("size", f"{_mb(before.size)}, nothing written"),
        ]
    else:
        lines = [field("pruned", books), field("size", f"{_mb(before.size)} → {_mb(after.size)}")]
    if backup is not None:
        lines.append(field("backup", backup))
    return Report(data=data, lines=tuple(lines))


def _mb(size: int) -> str:
    return f"{size / MB:,.1f} MB"
