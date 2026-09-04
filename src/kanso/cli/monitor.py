"""`kanso monitor run`: one pass of the watch a deployed version lives under.

The daemon runs this on a timer; the command runs it once, which is what an operator wants
after a redeploy and what a test wants at all. A pass judges every deployed version against
the paper or live gates of its sleeve hypothesis's plan, and enforces the two limits only a
whole-stage view can see.

The pass acts rather than reports: a paper version whose gates all pass becomes
`promotable` and reaches the inbox; a live version that fails one is demoted; a live
version that fails the daily loss halts its stage instead, because halting is the stronger
act and demoting into a halted stage would change nothing about the money. Every action is
taken once, on the transition, so running the command twice in a row is not two
escalations.

The command exits 0 whatever the verdicts are. A failing gate is a fact about a deployment,
not a failure of the pass that found it, and an operator watching for exit codes would
otherwise be told that the watch itself broke.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

import typer

from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent, verdict
from kanso.monitor import run_once

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.monitor import Outcome
    from kanso.workspace import Workspace

app = typer.Typer(help="The pass that watches every deployed version.", no_args_is_help=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]

MARK, GATE = 6, 22
"""Widths of the verdict and gate-id columns of the human gate lists."""


@app.command("run")
def run_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """One pass of the paper and live gate loop, and whatever its verdicts require."""
    emit(as_json or global_json(ctx), lambda: _run(open_workspace(ctx)))


# -- command bodies ---------------------------------------------------------------


def _run(ws: Workspace) -> Report:
    with store(ws) as opened:
        outcomes = run_once(ws, opened)
    actions = [action for one in outcomes for action in one.actions]
    escalations = [entry for one in outcomes for entry in one.escalations]
    data: dict[str, Any] = {
        "outcomes": [one.payload() for one in outcomes],
        "actions": actions,
        "escalations": escalations,
    }
    lines = [
        field(
            "pass",
            f"{len(outcomes)} judgement(s) · {len(actions)} action(s) · "
            f"{len(escalations)} escalation(s)",
        )
    ]
    for one in outcomes:
        lines += _outcome_lines(one)
    return Report(data=data, lines=tuple(lines))


# -- rendering --------------------------------------------------------------------


def _outcome_lines(one: Outcome) -> list[str]:
    """One judgement: a stage's exposure, or a version's gates and what followed."""
    if one.exposure is not None:
        exposure = one.exposure
        return [
            field(
                one.stage,
                f"gross {exposure.gross:,.0f}/{exposure.max_gross:,.0f} · "
                f"net {exposure.net:+,.0f}/{exposure.max_net:,.0f} · "
                + ("BREACHED" if exposure.breached else "within limits")
                + (f" · {', '.join(one.actions)}" if one.actions else ""),
            )
        ]
    lines = [
        field(
            one.stage,
            f"{one.subject} · "
            + (one.skipped or f"{len(one.judged)} judged · {'pass' if one.passed else 'fail'}")
            + (f" · {', '.join(one.actions)}" if one.actions else ""),
        )
    ]
    lines += [indent(verdict(result, MARK, GATE)) for result in one.gates]
    return lines
