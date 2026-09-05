"""`kanso replay`: running a target over historical data on either code path, and comparing.

`run` replays one target — a composed strategy version, or a hypothesis's card — from its
forward window through the last day the catalog serves, on the live code path (`node`) or
the research one (`engine`). `parity` runs both and compares the order intents they
produced, reporting the first divergence or that the two paths agreed. `show` prints one
session, or lists the sessions the workspace holds.

Replay always executes against the simulated client, whatever a stage is configured with: a
replay feeds history and a broker fills against current prices, so the pairing would fill
orders at prices unrelated to the data that triggered them.

Nothing here writes a card, moves a hypothesis's `best` or certifies anything. Replay is
evaluation over the one window nothing may backtest, and its numbers are for a person to
read rather than for the research loop to select on.
"""

from __future__ import annotations

from datetime import date
from textwrap import wrap
from typing import TYPE_CHECKING, Annotated, Any

import typer

from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.data import as_date
from kanso.cli.render import Report, emit, field, indent
from kanso.cli.strat import target
from kanso.replay import NODE
from kanso.replay import parity as run_parity
from kanso.replay import run as run_replay
from kanso.replay import sessions as list_sessions
from kanso.replay import show as read_session
from kanso.replay.parity import Parity

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.replay import Session
    from kanso.workspace import Workspace

app = typer.Typer(
    help="Replaying a target, and comparing the two code paths.", no_args_is_help=True
)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]
StrategyOption = Annotated[
    str | None,
    typer.Option(
        "--strategy", metavar="STRATEGY[@V]", help="A composed version (default: latest)."
    ),
]
HypOption = Annotated[
    str | None, typer.Option("--hyp", metavar="ID", help="A hypothesis, replayed as one card.")
]
ShaOption = Annotated[
    str | None, typer.Option("--sha", metavar="S", help="A card's sha (default: `best`).")
]
FromOption = Annotated[
    str | None, typer.Option("--from", metavar="DATE", help="First day (default: forward start).")
]
ToOption = Annotated[
    str | None, typer.Option("--to", metavar="DATE", help="Last day (default: the catalog's).")
]
SpeedOption = Annotated[float, typer.Option("--speed", metavar="N", help="Wall-clock pacing.")]
ModeOption = Annotated[str, typer.Option("--mode", metavar="MODE", help="`node` or `engine`.")]

MODE, LABEL = 8, 24
"""Widths of the mode and target columns of the human session list."""


@app.command("run")
def run_command(
    ctx: typer.Context,
    strategy: StrategyOption = None,
    hyp: HypOption = None,
    sha: ShaOption = None,
    from_: FromOption = None,
    to: ToOption = None,
    speed: SpeedOption = 0.0,
    mode: ModeOption = NODE,
    as_json: JsonOption = False,
) -> None:
    """Replay one target over the catalog and write the session it produced."""
    emit(
        as_json or global_json(ctx),
        lambda: _run(
            open_workspace(ctx),
            strategy,
            hyp,
            sha,
            as_date(from_, "--from"),
            as_date(to, "--to"),
            speed,
            mode,
        ),
    )


@app.command("parity")
def parity_command(
    ctx: typer.Context,
    strategy: StrategyOption = None,
    hyp: HypOption = None,
    sha: ShaOption = None,
    from_: FromOption = None,
    to: ToOption = None,
    speed: SpeedOption = 0.0,
    ts_ns: Annotated[
        int, typer.Option("--ts-ns", metavar="N", help="Instant tolerance, in nanoseconds.")
    ] = 0,
    as_json: JsonOption = False,
) -> None:
    """Replay on both code paths over the same days and compare the order intents."""
    emit(
        as_json or global_json(ctx),
        lambda: _parity(
            open_workspace(ctx),
            strategy,
            hyp,
            sha,
            as_date(from_, "--from"),
            as_date(to, "--to"),
            speed,
            ts_ns,
        ),
    )


@app.command("show")
def show_command(
    ctx: typer.Context,
    session: Annotated[
        str | None,
        typer.Argument(metavar="SESSION", help="A session id, or nothing to list them."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Print one session, or list every session this workspace holds."""
    emit(as_json or global_json(ctx), lambda: _show(open_workspace(ctx), session))


# -- command bodies ---------------------------------------------------------------


def _run(
    ws: Workspace,
    strategy: str | None,
    hyp: str | None,
    sha: str | None,
    start: date | None,
    end: date | None,
    speed: float,
    mode: str,
) -> Report:
    name, version = _target(strategy)
    with store(ws) as opened:
        made = run_replay(
            ws,
            opened,
            strategy=name,
            version=version,
            hyp=hyp,
            sha=sha,
            start=start,
            end=end,
            speed=speed,
            mode=mode,
        )
    data: dict[str, Any] = {
        **made.model_dump(mode="json", by_alias=True),
        "path": str(_session_dir(ws, made)),
    }
    return Report(data=data, lines=(*_session_lines(ws, made), field("next", "kanso replay show")))


def _parity(
    ws: Workspace,
    strategy: str | None,
    hyp: str | None,
    sha: str | None,
    start: date | None,
    end: date | None,
    speed: float,
    ts_ns: int,
) -> Report:
    name, version = _target(strategy)
    with store(ws) as opened:
        found = run_parity(
            ws,
            opened,
            strategy=name,
            version=version,
            hyp=hyp,
            sha=sha,
            start=start,
            end=end,
            speed=speed,
            ts_ns=ts_ns,
        )
    data: dict[str, Any] = found.payload()
    lines = (
        field("parity", "identical" if found.divergence is None else found.divergence.render()),
        *_cause(found),
        field("node", f"{found.node} · {len(found.node_orders)} intent(s)"),
        field("engine", f"{found.engine} · {len(found.engine_orders)} intent(s)"),
        field("compared", f"{found.compared} · tolerance {found.ts_ns} ns"),
        field("widest", f"{found.max_ts_delta_ns} ns apart"),
    )
    return Report(data=data, lines=lines)


CAUSE_WIDTH = 78
"""How wide the wrapped explanation runs, leaving room for the label column."""


def _cause(found: Parity) -> tuple[str, ...]:
    """The known cause this divergence has the shape of, wrapped under the parity line.

    Nothing at all when the paths agreed, or when the difference is not one this module
    recognises: a reader looking at a divergence with no explanation should see no
    explanation rather than a paragraph hedging about one.
    """
    cause = None if found.divergence is None else found.divergence.likely_cause
    if cause is None:
        return ()
    return tuple(indent(line) for line in wrap(cause, width=CAUSE_WIDTH))


def _show(ws: Workspace, session: str | None) -> Report:
    if session is not None:
        found = read_session(ws, session)
        data: dict[str, Any] = {
            **found.model_dump(mode="json", by_alias=True),
            "path": str(_session_dir(ws, found)),
        }
        return Report(data=data, lines=_session_lines(ws, found))
    held = list_sessions(ws)
    listing: dict[str, Any] = {
        "sessions": [one.model_dump(mode="json", by_alias=True) for one in held]
    }
    lines = [field("sessions", f"{len(held)} held")]
    lines += [
        f"{one.mode:<{MODE}}{one.target:<{LABEL}}{one.session_id}  "
        f"{one.from_}..{one.to} · {one.intents} intent(s)"
        for one in held
    ]
    return Report(data=listing, lines=tuple(lines))


# -- rendering --------------------------------------------------------------------


def _target(strategy: str | None) -> tuple[str | None, int | None]:
    """The `--strategy` argument split into an id and a version, or neither."""
    if strategy is None:
        return None, None
    return target(strategy)


def _session_dir(ws: Workspace, session: Session) -> Any:
    """Where a session's record and its two streams live."""
    from kanso.replay.record import session_dir

    return session_dir(ws, session.session_id)


def _session_lines(ws: Workspace, session: Session) -> tuple[str, ...]:
    """One session as a human reads it: what ran, over what, and what came back."""
    return (
        field("session", f"{session.session_id} · {session.mode} · {session.target}"),
        field(
            "range",
            f"{session.from_}..{session.to} · speed {session.speed:g} · exec {session.exec_}",
        ),
        field("stream", f"{session.released} point(s) · {session.intents} intent(s)"),
        indent(", ".join(session.instruments)),
        field("written", _session_dir(ws, session)),
    )
