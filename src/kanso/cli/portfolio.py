"""`kanso portfolio`, `kanso promote`, `kanso demote`: what is deployed, and moving it.

`portfolio show` reads three things at once: the file as it stands, whether each stage's
node has consumed everything the catalog holds, and what every deployed version has
realised over the windows its stage has closed. It writes nothing and is safe to run while
a stage is running.

`portfolio deploy --stage S` is the only way a version reaches a stage. It admits what
composition produced, funds it by the capital rule, validates what the stage's execution
client declares, renders the node configuration and restarts the node — and refuses, with
exit 2, a halted stage, a version certified under another engine, and a stage whose catalog
holds nothing in the forward window. Real capital with no approval on record is exit 4,
because that is a missing act rather than a broken precondition.

`portfolio clients` is the execution half of `data adapters`: every client a stage may
name, what each declares — whose money it trades and which clock it runs on — which stages
it may be configured on, and which of its credential variables resolve and from where. It
opens nothing and reads no value, so it answers in a workspace with every broker variable
unset, which is the ordinary state of a fresh one.

`promote` and `demote` are top-level commands rather than subcommands of `portfolio`,
because they are the two acts an operator takes on a strategy rather than on the deployment
surface. `promote` is the one command in kanso that can put money at risk and the only one
that requires a person: `--live --as NAME` is the whole of the approval, there is no
environment fallback, and without `--as` it exits 4 having changed nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

import typer

from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.cli.strat import target
from kanso.errors import ValidationError
from kanso.portfolio import demote as demote_version
from kanso.portfolio import deploy as deploy_stage
from kanso.portfolio import exec_client_declarations, stage_refusals
from kanso.portfolio import promote as promote_version
from kanso.portfolio import show as show_portfolio
from kanso.schemas.portfolio import STAGES

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.portfolio import Declared, Deployment
    from kanso.portfolio.show import StageReport
    from kanso.workspace import Workspace

app = typer.Typer(help="The stages, their capital and what runs on them.", no_args_is_help=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]
TargetArgument = Annotated[
    str, typer.Argument(metavar="STRATEGY[@V]", help="A strategy id, optionally a version.")
]

NAME = 18
"""Width of the version-name column of the human stage listing."""

CLIENT = 14
"""Width of the id column of the client listing; an execution client id is longer than
the two-column layout's label, and a wrapped id is harder to read than a wider column."""


@app.command("show")
def show_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """The two stages: how each is configured, whether it is current, and what it has made."""
    emit(as_json or global_json(ctx), lambda: _show(open_workspace(ctx)))


@app.command("clients")
def clients_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """List the execution clients: what each trades, on which clock, on which stage."""
    emit(as_json or global_json(ctx), lambda: _clients(open_workspace(ctx)))


@app.command("deploy")
def deploy_command(
    ctx: typer.Context,
    stage: Annotated[str, typer.Option("--stage", metavar="STAGE", help="`paper` or `live`.")],
    as_json: JsonOption = False,
) -> None:
    """Admit, fund and (re)start one stage's node."""
    emit(as_json or global_json(ctx), lambda: _deploy(open_workspace(ctx), stage))


def promote_command(
    ctx: typer.Context,
    strategy: TargetArgument,
    live: Annotated[
        bool, typer.Option("--live", help="The stage promoted to; there is only one.")
    ] = False,
    operator: Annotated[
        str | None,
        typer.Option("--as", metavar="NAME", help="The operator approving this, by name."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Move a promotable version onto the live stage under a named operator's approval."""
    emit(
        as_json or global_json(ctx), lambda: _promote(open_workspace(ctx), strategy, live, operator)
    )


def demote_command(
    ctx: typer.Context, strategy: TargetArgument, as_json: JsonOption = False
) -> None:
    """Take a live version off the live stage and restart the stages that are not halted."""
    emit(as_json or global_json(ctx), lambda: _demote(open_workspace(ctx), strategy))


# -- command bodies ---------------------------------------------------------------


def _show(ws: Workspace) -> Report:
    with store(ws) as opened:
        report = show_portfolio(ws, opened)
    funding = {one.id: one for one in exec_client_declarations(ws)}
    data: dict[str, Any] = {
        "limits": report.portfolio.limits.model_dump(mode="json"),
        "stages": [_stage_payload(one, funding.get(one.exec_id)) for one in report.stages],
    }
    lines: list[str] = []
    for one in report.stages:
        lines.append(
            field(
                one.stage,
                f"{'up' if one.live else 'down'} · exec {one.exec_id} "
                f"({_funding(funding.get(one.exec_id))}) · data {one.data} · "
                f"speed {one.speed:g} · capital {one.capital:,.0f}"
                + (" · HALTED" if one.kill_switch else ""),
            )
        )
        lines.append(
            indent(
                f"clock {one.clock or 'never run'} · catalog to {one.served_to or 'nothing'} · "
                f"allocated {one.allocated:,.0f} · pnl {one.pnl:+,.2f}"
            )
        )
        for held in one.strategies:
            lines.append(
                indent(
                    f"{held.label:<{NAME}}{held.capital:>12,.0f}  "
                    f"pnl {held.pnl:+,.2f} over {held.windows} window(s)"
                )
            )
    limits = report.portfolio.limits
    lines.append(
        field(
            "limits",
            f"gross {limits.max_gross_pct:g}% · net {limits.max_net_pct:g}% · "
            f"per strategy {limits.per_strategy_max_pct:g}% · "
            f"daily loss {limits.daily_loss_pct:g}%",
        )
    )
    return Report(data=data, lines=tuple(lines))


def _clients(ws: Workspace) -> Report:
    """Every execution client, and what each stage's configuration would do with it.

    Two questions, answered together because an operator has both at once: what may be
    named, and what the stage that names it would be refused for. The refusals are the
    ones `deploy` makes, called here rather than restated, so this cannot drift from what
    the command would actually do.
    """
    found = exec_client_declarations(ws)
    problems = stage_refusals(ws)
    data: dict[str, Any] = {
        "clients": [one.payload() for one in found],
        "stages": problems,
    }
    lines = [line for one in found for line in _client_lines(one)]
    lines += [
        field(stage, "ok" if problem is None else problem) for stage, problem in problems.items()
    ]
    return Report(data=data, lines=tuple(lines))


def _client_lines(one: Declared) -> list[str]:
    """One client as its declarations, then where each variable it needs resolves from."""
    lines = [
        f"{one.id:<{CLIENT}}{one.capital} · clock {one.clock} · {one.source} · "
        f"stages {', '.join(one.stages)}"
    ]
    if not one.credentials:
        return [*lines, indent("no credential", CLIENT)]
    lines += [
        indent(f"{name}: {one.origins.get(name) or 'unset'}", CLIENT) for name in one.credentials
    ]
    return lines


def _deploy(ws: Workspace, stage: str) -> Report:
    named = _stage(stage)
    with store(ws) as opened:
        done = deploy_stage(ws, opened, named)
    data: dict[str, Any] = _deployment_payload(done)
    lines = [
        field(
            "deployed",
            f"{named} · {len(done.admitted)} version(s) · {done.capital:,.0f}"
            + (f" · blocked {', '.join(done.blocked)}" if done.blocked else "")
            + (f" · halted: {done.halted}" if done.halted else ""),
        ),
    ]
    lines += [
        indent(
            f"{one.label:<{NAME}}{one.capital:>12,.0f}"
            + (f"  replaces @{one.replaced}" if one.replaced is not None else "")
        )
        for one in done.admitted
    ]
    lines.append(
        field("session", done.session.session_id if done.session is not None else "no node ran")
    )
    if done.session is not None:
        lines.append(
            indent(
                f"{done.session.from_}..{done.session.to} · {done.session.released} point(s) · "
                f"{done.session.intents} intent(s)"
            )
        )
    return Report(data=data, lines=tuple(lines))


def _promote(ws: Workspace, strategy: str, live: bool, operator: str | None) -> Report:
    name, chosen = target(strategy)
    if not live:
        raise ValidationError(
            f"promote: {name} is promoted to the live stage and there is no other",
            remedy=f"kanso promote {strategy} --live --as NAME",
        )
    with store(ws) as opened:
        done = promote_version(ws, opened, name, chosen, operator)
    data: dict[str, Any] = {
        "strategy": done.strategy_id,
        "version": done.version,
        "state": "live",
        "operator": done.operator,
        "approved_at": done.approval.created_at,
        "retired": done.retired,
        "deployments": [_deployment_payload(one) for one in done.deployments],
    }
    lines = [
        field(
            "promoted",
            f"{done.label} · live"
            + (f" · replacing @{done.retired}" if done.retired is not None else ""),
        ),
        field("approved", f"{done.operator} at {done.approval.created_at}"),
        *[_deployment_line(one) for one in done.deployments],
    ]
    return Report(data=data, lines=tuple(lines))


def _demote(ws: Workspace, strategy: str) -> Report:
    name, chosen = target(strategy)
    with store(ws) as opened:
        done = demote_version(ws, opened, name, chosen)
    data: dict[str, Any] = {
        "strategy": done.strategy_id,
        "version": done.version,
        "state": done.state,
        "halted": list(done.halted),
        "deployments": [_deployment_payload(one) for one in done.deployments],
    }
    lines = [field("demoted", f"{done.label} · now {done.state}")]
    lines += [_deployment_line(one) for one in done.deployments]
    if done.halted:
        lines.append(field("halted", f"{', '.join(done.halted)} · kill switch on, left alone"))
    return Report(data=data, lines=tuple(lines))


# -- rendering --------------------------------------------------------------------


def _stage(stage: str) -> str:
    """The stage a command names, refusing anything that is not one."""
    if stage not in STAGES:
        raise ValidationError(
            f"stage: {stage!r} is not a deployment stage",
            remedy=f"one of {', '.join(STAGES)}",
        )
    return stage


def _funding(client: Declared | None) -> str:
    """Whose money a stage's execution client trades, in one word.

    A stage may name a client this workspace no longer provides — an adapter that was
    removed, an extension that stopped loading — and `show` must still print the stage
    rather than fail on it. `deploy` is where that is refused; here it is reported.
    """
    return "unknown" if client is None else client.capital


def _stage_payload(one: StageReport, client: Declared | None) -> dict[str, Any]:
    """One stage as JSON: its configuration, its liveness and what it holds."""
    return {
        "stage": one.stage,
        "exec": one.exec_id,
        "funding": _funding(client),
        "exec_clock": None if client is None else client.clock,
        "data": one.data,
        "speed": one.speed,
        "capital": one.capital,
        "kill_switch": one.kill_switch,
        "live": one.live,
        "clock": None if one.clock is None else str(one.clock),
        "served_to": None if one.served_to is None else str(one.served_to),
        "allocated": one.allocated,
        "pnl": one.pnl,
        "strategies": [
            {
                "strategy": held.strategy_id,
                "version": held.version,
                "capital": held.capital,
                "joined_at": held.joined_at.isoformat(),
                "windows": held.windows,
                "pnl": held.pnl,
            }
            for held in one.strategies
        ],
    }


def _deployment_payload(one: Deployment) -> dict[str, Any]:
    """One deployment as JSON: what it admitted, retired, blocked and ran."""
    return {
        "stage": one.stage,
        "admitted": [
            {
                "strategy": admitted.strategy_id,
                "version": admitted.version,
                "capital": admitted.capital,
                "replaced": admitted.replaced,
                "joined_at": admitted.joined_at.isoformat(),
            }
            for admitted in one.admitted
        ],
        "retired": list(one.retired),
        "blocked": list(one.blocked),
        "capital": one.capital,
        "session": None if one.session is None else one.session.session_id,
        "halted": one.halted,
    }


def _deployment_line(one: Deployment) -> str:
    """What a redeploy did to one stage, in one line."""
    return field(
        one.stage,
        f"{len(one.admitted)} version(s) · {one.capital:,.0f}"
        + (f" · blocked {', '.join(one.blocked)}" if one.blocked else "")
        + (f" · {one.session.session_id}" if one.session is not None else " · no node ran"),
    )
