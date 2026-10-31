"""`kanso research`: the interactive loop a coding agent drives, and the autonomous one.

`begin` opens a run and prints the lane directory, which is the whole of the interface: an
agent edits `strategy.py` there and calls `card`, and the evaluation it gets is the same
one the autonomous driver gets. `end` closes the run and removes the directory — the
cards, the blobs and the hypothesis's best stay in state, so nothing a run learned is in
the directory to begin with.

`run` is the same sequence with the model in the agent's seat: it begins a run if the
hypothesis has none and then proposes cards until the count it was given, or until the run
stalls. `--cards` counts what this invocation proposes, so a baseline and everything a
previous invocation left behind are not in it.

`start`, `stop` and `status` are the daemon around that: one worker per lane the envelope
allows, taking hypotheses off the queue `queue add` fills, each started again if it dies
while the daemon runs — `status` lists every one that was. Stopping keeps everything —
active runs and their lane directories stay exactly where they are, and the next `start`
picks them up before it takes anything new — so stopping is a cheap act rather than a
decision.

A run that stalls with a keep nothing has certified certifies it before it returns, so
`run` reports the certificate that stall produced and points at it.

`show` prints what is in state: a card's stored `strategy.py`, the unified diff between
two of them, or — under `--measured` — what the newest card of those bytes that kept a
measurement was scored from: its objective and folds, the criteria version that scored it,
and the run it measured, period by period. A sha is written as any
unique prefix of one that belongs to this hypothesis; a prefix matching none of its cards,
or several, is a validation failure, because guessing which one was meant is how the wrong
subject gets certified.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

import typer

from kanso import research
from kanso.certify import certificate
from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.criteria import criteria_version
from kanso.errors import PreconditionError, ValidationError
from kanso.research import daemon, records
from kanso.research.lanes import DEFAULT_LANE
from kanso.schemas import Card, CardDigest, GateResult
from kanso.state import StateStore
from kanso.workspace import Workspace

app = typer.Typer(help="Runs, cards and what a card left in state.", no_args_is_help=True)
queue_app = typer.Typer(help="What waits for a free lane.", no_args_is_help=True)
app.add_typer(queue_app, name="queue")

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]
IdArgument = Annotated[str, typer.Argument(metavar="ID", help="The hypothesis id.")]


@app.command("begin")
def begin_command(
    ctx: typer.Context,
    hyp_id: IdArgument,
    tag: Annotated[str | None, typer.Option("--tag", metavar="T", help="Name this run.")] = None,
    from_workspace: Annotated[
        bool,
        typer.Option("--from-workspace", help="Start from the workspace file and clear `best`."),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Start a run: the lane directory, the pins, the snapshot and the baseline card."""
    emit(
        as_json or global_json(ctx),
        lambda: _begin(open_workspace(ctx), hyp_id, tag, from_workspace),
    )


@app.command("card")
def card_command(
    ctx: typer.Context,
    hyp_id: IdArgument,
    desc: Annotated[str, typer.Option("--desc", metavar="TEXT", help="What was tried (≤120).")],
    as_json: JsonOption = False,
) -> None:
    """Evaluate the lane directory's `strategy.py` as one card of the active run."""
    emit(as_json or global_json(ctx), lambda: _card(open_workspace(ctx), hyp_id, desc))


@app.command("run")
def run_command(
    ctx: typer.Context,
    hyp_id: IdArgument,
    cards: Annotated[
        int | None,
        typer.Option("--cards", metavar="N", help="Stop after N cards (default: until it stalls)."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Research a hypothesis with no human in the loop: propose, apply, evaluate, repeat."""
    emit(as_json or global_json(ctx), lambda: _run(open_workspace(ctx), hyp_id, cards))


@app.command("end")
def end_command(ctx: typer.Context, hyp_id: IdArgument, as_json: JsonOption = False) -> None:
    """End the active run and remove its lane directory, and nothing else."""
    emit(as_json or global_json(ctx), lambda: _end(open_workspace(ctx), hyp_id))


@app.command("start")
def start_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Start the daemon: one worker per lane the envelope allows, plus the monitor."""
    emit(as_json or global_json(ctx), lambda: _start(open_workspace(ctx)))


@app.command("stop")
def stop_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Stop the daemon. Active runs and their lane directories stay exactly as they are."""
    emit(as_json or global_json(ctx), lambda: _stop(open_workspace(ctx)))


@app.command("status")
def status_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """The daemon, its lanes, the active runs and the queue."""
    emit(as_json or global_json(ctx), lambda: _status(open_workspace(ctx)))


@queue_app.command("add")
def queue_add_command(
    ctx: typer.Context,
    hyp_id: IdArgument,
    priority: Annotated[
        int, typer.Option("--priority", metavar="P", help="Higher is served first.")
    ] = 0,
    as_json: JsonOption = False,
) -> None:
    """Put a hypothesis in the queue, or raise the priority of one already in it."""
    emit(as_json or global_json(ctx), lambda: _queue_add(open_workspace(ctx), hyp_id, priority))


@queue_app.command("remove")
def queue_remove_command(
    ctx: typer.Context, hyp_id: IdArgument, as_json: JsonOption = False
) -> None:
    """Take a hypothesis out of the queue, or out of a lane's hands before its run begins."""
    emit(as_json or global_json(ctx), lambda: _queue_remove(open_workspace(ctx), hyp_id))


@app.command("show")
def show_command(
    ctx: typer.Context,
    hyp_id: IdArgument,
    sha: Annotated[
        str | None, typer.Option("--sha", metavar="S", help="A card's sha (default: `best`).")
    ] = None,
    diff: Annotated[
        str | None, typer.Option("--diff", metavar="S2", help="Diff `--sha` against this one.")
    ] = None,
    measured: Annotated[
        bool,
        typer.Option("--measured", help="Print what the card's metric was computed from."),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Print a card's stored `strategy.py`, the diff between two, or what one measured."""
    emit(
        as_json or global_json(ctx),
        lambda: _show(open_workspace(ctx), hyp_id, sha, diff, measured=measured),
    )


# -- command bodies ---------------------------------------------------------------


def _begin(ws: Workspace, hyp_id: str, tag: str | None, from_workspace: bool) -> Report:
    with store(ws) as opened:
        run = research.begin(ws, opened, hyp_id, tag=tag, from_workspace=from_workspace)
        cards = records.cards_of(opened, hyp_id)
    directory = ws.root / run.dir
    baseline = cards[-1]
    data: dict[str, Any] = {
        **run.model_dump(mode="json", by_alias=True),
        "lane_dir": str(directory),
        "baseline": baseline.model_dump(mode="json", by_alias=True),
    }
    lines = (
        # The lane directory is the interface: an agent edits `strategy.py` there.
        field("lane dir", directory),
        field("run", f"{run.run_id} · {run.tag} · lane {run.lane}"),
        field("snapshot", run.snapshot_id),
        field(
            "baseline",
            f"{baseline.status} · metric {baseline.metric:.6f} · {baseline.wall_s:.1f}s · "
            f"budget {run.card_budget_s:.0f}s",
        ),
        field("next", f"edit {directory / 'strategy.py'}, then `kanso research card {hyp_id}`"),
    )
    return Report(data=data, lines=lines)


def _card(ws: Workspace, hyp_id: str, desc: str) -> Report:
    with store(ws) as opened:
        # Recording the card renders `results.tsv` from state, so the path is reported
        # rather than the file written again here.
        card = research.card(ws, opened, hyp_id, desc)
        run = records.require_active(opened, hyp_id)
        trials = records.n_trials(opened, hyp_id)
    data: dict[str, Any] = {
        **card.model_dump(mode="json", by_alias=True),
        "n_trials": trials,
        "best_sha": run.best_sha,
        "best_metric": run.best_metric,
        "card_budget_s": run.card_budget_s,
        "budget_sha": run.budget_sha,
        "results": str(research.results_file(ws, hyp_id)),
    }
    struck = "the baseline's" if run.budget_sha is None else f"struck by {run.budget_sha[:7]}"
    lines = [
        field("card", f"{card.sha7} · {card.status} · {desc}"),
        field("metric", f"{card.metric:.6f} ± {card.metric_se:.6f} · {card.n_trades} trade(s)"),
        *_digest_lines(card.digest),
        field("cost", f"{card.wall_s:.1f}s · {card.peak_mem_gb:.2f} GB · trial {trials}"),
        field("budget", f"{run.card_budget_s:.0f}s · {struck}"),
        field("best", "none yet" if run.best_sha is None else f"{run.best_sha[:7]}"),
    ]
    lines += [
        indent(f"{result.id}: {'pass' if result.passed else 'fail'} — {_evidence(result)}")
        for result in card.gate_results
    ]
    if card.crash_tail:
        lines += [indent(line) for line in card.crash_tail.splitlines()[-5:]]
    return Report(data=data, lines=tuple(lines))


def _digest_lines(digest: CardDigest | None) -> list[str]:
    """What a card's trades did, in two lines: how it traded, and what a trade and a session
    earned. Nothing for a card that never ran to a result."""
    if digest is None:
        return []
    traded = [
        f"{_number(digest.trades_per_session, '.2f')} a session over {digest.sessions}",
        f"hit {_share(digest.hit_rate)}",
        f"hold {_seconds(digest.median_hold_s)}",
        f"maker {_share(digest.maker_share)}",
        f"filled {_share(digest.fill_ratio)}",
    ]
    edge = (
        f"{_number(digest.net_bps_per_trade, '+.3f')} ± "
        f"{_number(digest.net_bps_per_trade_se, '.3f')} bp a trade net "
        f"(gross {_number(digest.gross_bps_per_trade, '+.3f')}, "
        f"cost {_number(digest.cost_bps_per_trade, '.3f')}) · "
        f"{_number(digest.net_bps_per_session, '+.3f')} bp a session net "
        f"(gross {_number(digest.gross_bps_per_session, '+.3f')}, "
        f"cost {_number(digest.cost_bps_per_session, '.3f')})"
    )
    return [field("trades", " · ".join(traded)), field("edge", edge)]


def _number(value: float | None, spec: str) -> str:
    return "n/a" if value is None else format(value, spec)


def _share(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _seconds(value: float | None) -> str:
    """A hold as the unit it is easiest to read in: seconds, minutes or hours."""
    if value is None:
        return "n/a"
    if value < 120:
        return f"{value:.0f}s"
    if value < 7200:
        return f"{value / 60:.0f}m"
    return f"{value / 3600:.1f}h"


def _run(ws: Workspace, hyp_id: str, cards: int | None) -> Report:
    with store(ws) as opened:
        outcome = research.drive(ws, opened, hyp_id, cards=cards, lane=DEFAULT_LANE)
        trials = records.n_trials(opened, hyp_id)
        # A run that stalls on a keep nothing has certified certifies it before it
        # returns, so the certificate of the best is part of what this invocation did.
        # One of another subject is not: a rewound `best` leaves an older certificate
        # newest, and reporting that as this run's outcome would be a lie.
        newest = certificate.latest(opened, hyp_id) if outcome.ended else None
    certified = newest if newest is not None and newest.strategy_sha == outcome.best_sha else None
    data: dict[str, Any] = {
        **outcome.payload(),
        "n_trials": trials,
        "certificate": None
        if certified is None
        else {"sha7": certified.sha7, "verdict": certified.verdict},
    }
    best = (
        "none yet"
        if outcome.best_sha is None
        else f"{outcome.best_sha[:7]} at {outcome.best_metric:.6f}"
    )
    lines = [
        field("run", f"{outcome.run_id} · lane {outcome.lane} · {outcome.reason}"),
        field(
            "cards",
            f"{outcome.proposed} proposed · {outcome.keeps} keep · "
            f"{outcome.runner_ups} runner_up · "
            f"{outcome.discards} discard · {outcome.crashes} crash · "
            f"{outcome.missed} repeated · {outcome.redundant} redundant · trial {trials}",
        ),
        field("aligned", f"{outcome.checks} check(s) · {outcome.drifts} drift(s)"),
        field("best", best),
    ]
    if certified is not None:
        lines.append(field("certified", f"{certified.sha7} · {certified.verdict}"))
    lines.append(
        field(
            "next",
            f"kanso cert show {hyp_id}"
            if certified is not None
            else f"kanso research show {hyp_id}"
            if outcome.ended
            else f"kanso research run {hyp_id} --cards {outcome.proposed}",
        )
    )
    return Report(data=data, lines=tuple(lines))


def _start(ws: Workspace) -> Report:
    pid = daemon.start(ws)
    lanes = daemon.lane_names(ws)
    data: dict[str, Any] = {
        "running": True,
        "pid": pid,
        "lanes": list(lanes),
        "log": str(daemon.log_path(ws)),
    }
    lines = (
        field("daemon", f"started · pid {pid}"),
        field("lanes", ", ".join(lanes)),
        field("log", daemon.log_path(ws)),
        field("next", "kanso research status"),
    )
    return Report(data=data, lines=lines)


def _stop(ws: Workspace) -> Report:
    stopped = daemon.stop(ws)
    data: dict[str, Any] = stopped.payload()
    lines = [field("daemon", "stopped" if stopped.pid is None else f"stopped · pid {stopped.pid}")]
    if stopped.orphans:
        orphans = ", ".join(child.label for child in stopped.orphans)
        lines.append(
            field("orphans", f"{orphans} · ended, still running after the daemon was gone")
        )
    if stopped.ending:
        lines.append(
            field(
                "ending",
                f"{_started_by(stopped.ending)} · still running; it ends itself, "
                "and `kanso research status` names it until it has",
            )
        )
    # No run is ended and nothing is removed, so the next `start` resumes.
    lines.append(field("runs", "left open, with their lane directories"))
    return Report(data=data, lines=tuple(lines))


def stopped_line(children: Sequence[daemon.ChildPid], ending: Sequence[daemon.ChildPid]) -> str:
    """A daemon that is not running: never a bare `stopped` beside a child of it still alive,
    or beside a card one of them started that has yet to end itself."""
    named = [child.label for child in children]
    if ending:
        named.append(_started_by(ending))
    if not named:
        return "stopped"
    return "stopped · still running: " + ", ".join(named)


def _started_by(ending: Sequence[daemon.ChildPid]) -> str:
    """`what lane l1 (pid 4242) started`: a card, certification or demotion of a child gone."""
    return ", ".join(f"what {child.label} started" for child in ending)


def _status(ws: Workspace) -> Report:
    with store(ws) as opened:
        found = daemon.status(ws, opened)
    data: dict[str, Any] = found.payload()
    running = (
        f"running · pid {found.pid}"
        if found.running
        else stopped_line(found.children, found.ending)
    )
    deaths = sum(item.deaths for item in found.restarts)
    lines = [
        field("daemon", running),
        field("lanes", ", ".join(found.lanes) or "none (run `kanso env detect`)"),
        field("restarts", f"{deaths} since the daemon started" if deaths else "none"),
    ]
    lines += [indent(_restart_line(item)) for item in found.restarts]
    lines.append(field("runs", f"{len(found.runs)} active"))
    lines += [indent(_run_line(item)) for item in found.runs]
    lines.append(field("queue", f"{len(found.queue)} waiting"))
    lines += [
        indent(f"{item.hyp_id:<20}priority {item.priority:<4}since {item.enqueued_at}")
        for item in found.queue
    ]
    return Report(data=data, lines=tuple(lines))


def _restart_line(item: daemon.Restart) -> str:
    """One child the daemon started again: how often, and how the newest death went."""
    how = f"exit {item.exit}" if item.signal is None else f"killed by {item.signal}"
    back = (
        "started again at once"
        if item.restart_in_s == 0.0
        else f"started again after {item.restart_in_s:.0f}s"
    )
    return (
        f"{item.child:<9}{item.deaths} death(s) · last {item.at} · "
        f"{how} after {item.lived_s:.0f}s · {back}"
    )


def _run_line(item: daemon.LaneRun) -> str:
    """One active run: where it is, and how the three shas relate."""
    lane_sha = "gone" if item.lane_sha is None else item.lane_sha[:7]
    best = "none" if item.run.best_sha is None else item.run.best_sha[:7]
    return (
        f"{item.run.lane:<5}{item.run.hyp_id:<20}lane {lane_sha} · "
        f"best {best} · base {item.run.base_sha[:7]}"
    )


def _queue_add(ws: Workspace, hyp_id: str, priority: int) -> Report:
    with store(ws) as opened:
        item = research.enqueue(opened, hyp_id, priority)
        waiting = research.queued(opened)
    place = [entry.hyp_id for entry in waiting].index(item.hyp_id) + 1
    data: dict[str, Any] = {**item.payload(), "place": place, "waiting": len(waiting)}
    lines = (
        field("queued", f"{item.hyp_id} · priority {item.priority}"),
        field("place", f"{place} of {len(waiting)}"),
        field("since", item.enqueued_at),
    )
    return Report(data=data, lines=lines)


def _queue_remove(ws: Workspace, hyp_id: str) -> Report:
    with store(ws) as opened:
        where = research.remove(opened, hyp_id)
        waiting = research.queued(opened)
    data: dict[str, Any] = {"id": hyp_id, "removed_from": where, "waiting": len(waiting)}
    lines = (
        field("removed", f"{hyp_id} · from the {where}"),
        field("waiting", len(waiting)),
    )
    return Report(data=data, lines=lines)


def _end(ws: Workspace, hyp_id: str) -> Report:
    with store(ws) as opened:
        run = research.end(ws, opened, hyp_id)
        cards = records.cards_of(opened, hyp_id)
        path = research.write_results(ws, opened, hyp_id)
    mine = [card for card in cards if card.run_id == run.run_id]
    data: dict[str, Any] = {
        **run.model_dump(mode="json", by_alias=True),
        "cards": len(mine),
        "keeps": sum(1 for card in mine if card.status == "keep"),
        "results": str(path),
    }
    lines = (
        field("run", f"{run.run_id} · {run.tag} ended"),
        field("cards", f"{data['cards']} · {data['keeps']} keep(s)"),
        field("best", "none" if run.best_sha is None else f"{run.best_sha[:7]}"),
        field("results", path),
    )
    return Report(data=data, lines=lines)


def _show(
    ws: Workspace, hyp_id: str, sha: str | None, diff: str | None, *, measured: bool = False
) -> Report:
    if measured and diff is not None:
        raise ValidationError(
            "--measured prints what one card was measured from and --diff compares two "
            "cards' sources, so the two are not asked together",
            remedy=f"kanso research show {hyp_id} --sha S --measured",
        )
    with store(ws) as opened:
        known = _shas(opened, hyp_id)
        subject = _resolve(opened, hyp_id, sha, known)
        if diff is None:
            # Every card that evaluated these bytes, newest last. The source is shown with the
            # newest, its digest included, and a best a store holds no card of reads as none
            # rather than refusing the source.
            evaluated = [c for c in records.cards_of(opened, hyp_id) if c.strategy_sha == subject]
            if measured:
                return _measurement(opened, hyp_id, subject, evaluated)
            card = evaluated[-1] if evaluated else None
            source = opened.get_blob(subject).decode("utf-8", errors="replace")
            data: dict[str, Any] = {
                "id": hyp_id,
                "sha": subject,
                "source": source,
                "card": None if card is None else card.model_dump(mode="json", by_alias=True),
            }
            return Report(data=data, lines=tuple(source.splitlines()))
        other = _resolve(opened, hyp_id, diff, known)
        left = opened.get_blob(subject).decode("utf-8", errors="replace")
        right = opened.get_blob(other).decode("utf-8", errors="replace")
    rendered = "".join(
        difflib.unified_diff(
            left.splitlines(keepends=True),
            right.splitlines(keepends=True),
            fromfile=f"{subject[:7]}/strategy.py",
            tofile=f"{other[:7]}/strategy.py",
        )
    )
    data = {"id": hyp_id, "sha": subject, "diff_of": other, "diff": rendered}
    return Report(data=data, lines=tuple(rendered.splitlines()))


def _measurement(
    opened: StateStore, hyp_id: str, subject: str, evaluated: Sequence[Card]
) -> Report:
    """What the newest card of these bytes that kept a measurement was scored from: the
    objective, its folds and the criteria version that scored it, the trades, and the run's
    return and equity at each period end — a UTC date, and the time beside it where two ends
    share one.

    `evaluated` is every card of the bytes, newest last. A newer card that kept none — a
    crash, a card refused before or during its backtest, one recorded by a kanso that kept
    none — does not hide what an older card kept: the `card` line counts the cards passed
    over. Refused only when no card of the bytes kept one. A measurement a newer one of the
    same bytes has replaced here — a later run's baseline measured another way, say — is read
    with `records.measurement_of` on its card.
    """
    passed = next(
        (n for n, each in enumerate(reversed(evaluated)) if each.measured_sha is not None), None
    )
    card = None if passed is None else evaluated[-1 - passed]
    found = None if card is None else records.measurement_of(opened, card)
    if card is None or found is None:
        raise PreconditionError(
            f"no card of {hyp_id} that evaluated {subject[:7]} kept a measurement: "
            "a card keeps one when its backtest ran to a result, and a crash, a card refused "
            "before or during its backtest and a card recorded before cards kept one have none",
            remedy=f"kanso research show {hyp_id} --sha {subject[:7]} --json",
        )
    run = found.run
    data: dict[str, Any] = {
        "id": hyp_id,
        "sha": subject,
        "card": card.model_dump(mode="json", by_alias=True),
        "measurement": found.payload(),
    }
    against = [
        name
        for name, other in (("host", found.host), ("benchmark", found.benchmark))
        if other is not None
    ]
    instants = [datetime.fromtimestamp(ts // 1_000_000_000, tz=UTC) for ts in run.period_ends_ns]
    dated = len({instant.date() for instant in instants}) == len(instants)
    stamp = "%Y-%m-%d" if dated else "%Y-%m-%d %H:%M:%S"
    newer = "" if passed == 0 else f" · {passed} newer card(s) of these bytes kept none"
    installed = criteria_version()
    scored = found.criteria_version
    if scored != installed:
        scored = f"{scored}, not this kanso's {installed}"
    lines = [
        field("card", f"{card.sha7} · {card.status} · {card.desc}{newer}"),
        field(
            "measured",
            f"{found.objective.id} over {found.folds} fold(s)"
            + (f" · against the {' and the '.join(against)} run" if against else "")
            + f" · criteria {scored}",
        ),
        field(
            "metric",
            f"{card.metric:.6f} ± {card.metric_se:.6f} · {len(run.trades)} trade(s) · "
            f"{len(run.fills)} fill(s)",
        ),
        field(
            "periods",
            f"{len(instants)} of {run.period} · capital {run.capital:.2f} {run.currency} · UTC",
        ),
        *(
            indent(f"{instant.strftime(stamp)}  {change:>+12.2f}  {equity:>14.2f}")
            for instant, change, equity in zip(instants, run.returns, run.equity, strict=True)
        ),
    ]
    return Report(data=data, lines=tuple(lines))


def _evidence(result: GateResult) -> str:
    """A gate's evidence as one short line, or why it judged nothing."""
    if result.skipped is not None:
        return f"skipped: {result.skipped}"
    return (
        ", ".join(f"{key}={value}" for key, value in sorted(result.evidence.items()))
        or "no evidence"
    )


def _shas(opened: StateStore, hyp_id: str) -> list[str]:
    """Every strategy sha this hypothesis's cards recorded, newest last."""
    return list(dict.fromkeys(card.strategy_sha for card in records.cards_of(opened, hyp_id)))


def _resolve(opened: StateStore, hyp_id: str, prefix: str | None, known: list[str]) -> str:
    """A sha prefix as the one card sha of this hypothesis it names.

    With no prefix the hypothesis's `best` is meant, which is what an operator asking to
    see "the strategy" means. A prefix belonging to no card of this hypothesis is foreign
    and one belonging to several is ambiguous; both are refused rather than guessed.
    """
    if prefix is None:
        best, _ = records.best_of(opened, hyp_id)
        if best is None:
            raise PreconditionError(
                f"{hyp_id} has no keep yet, so there is no `best` to show",
                remedy="name a card with --sha, or run cards until one keeps",
            )
        return best
    matches = sorted(sha for sha in known if sha.startswith(prefix))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValidationError(
            f"--sha {prefix!r} names no card of {hyp_id}",
            remedy=f"pass a prefix of one of its {len(known)} card sha(s)",
        )
    raise ValidationError(
        f"--sha {prefix!r} is ambiguous: it names {', '.join(sha[:12] for sha in matches)}",
        remedy="pass more characters of the sha",
    )
