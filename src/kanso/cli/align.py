"""`kanso align`: asking now whether a run still tests the idea it began with.

The driver runs this check on a clock; this command runs the same one on demand, which is
what an operator reaches for after editing the lane's `strategy.py` by hand. The
deterministic checks come first and the model is asked only when they pass, so a drift the
syntax tree can prove costs nothing.

Drift is not an error, so it is not an error exit either. A check that finds the run has
wandered has already rewound it — the lane copy is back on the last aligned keep, `best`
points at that card, the cards since the last check are marked, and an escalation is in
the inbox — and the command reports what it did with exit 0. The operator learns of it
where every other escalation lands rather than from an exit code they would have to
interpret.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field
from kanso.research import align, records
from kanso.research.lanes import DEFAULT_LANE
from kanso.workspace import Workspace

app = typer.Typer(help="Whether a run still tests its hypothesis.", no_args_is_help=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]
IdArgument = Annotated[str, typer.Argument(metavar="ID", help="The hypothesis id.")]


@app.command("check")
def check_command(
    ctx: typer.Context,
    hyp_id: IdArgument,
    as_json: JsonOption = False,
) -> None:
    """Check the active run against its thesis now, and rewind it if it has drifted."""
    emit(as_json or global_json(ctx), lambda: _check(open_workspace(ctx), hyp_id))


# -- command bodies ---------------------------------------------------------------


def _check(ws: Workspace, hyp_id: str) -> Report:
    with store(ws) as opened:
        aligned, reason = align.check(ws, opened, hyp_id, DEFAULT_LANE)
        run = records.require_active(opened, hyp_id, DEFAULT_LANE)
        # The checkpoint the check just wrote names the bytes the lane now holds, which
        # after a drift is what it was rewound to and not the run's `best`: a drift with
        # no aligned keep behind it rewinds to the base and clears `best` entirely.
        mark = align.checkpoint(opened, run)
    data: dict[str, Any] = {
        "id": hyp_id,
        "run_id": run.run_id,
        "lane": run.lane,
        "aligned": aligned,
        "reason": reason,
        "sha": mark.sha,
        "cards_checked": mark.cards,
        "best_sha": run.best_sha,
        "best_metric": run.best_metric,
    }
    lines = [
        field("hypothesis", f"{hyp_id} · run {run.run_id} · lane {run.lane}"),
        field("aligned", "yes" if aligned else f"no — {reason}"),
        field("cards", f"{mark.cards} checked · lane on {mark.sha[:7]}"),
    ]
    if not aligned:
        lines.append(field("next", f"kanso research show {hyp_id} · kanso inbox"))
    return Report(data=data, lines=tuple(lines))
