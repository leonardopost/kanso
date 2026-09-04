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
allows, taking hypotheses off the queue `queue add` fills. Stopping keeps everything —
active runs and their lane directories stay exactly where they are, and the next `start`
picks them up before it takes anything new — so stopping is a cheap act rather than a
decision.

`show` prints what is in state: a card's stored `strategy.py`, or the unified diff between
two of them. A sha is written as any unique prefix of one that belongs to this hypothesis;
a prefix matching none of its cards, or several, is a validation failure, because guessing
which one was meant is how the wrong subject gets certified.
"""

from __future__ import annotations

import difflib
from typing import Annotated, Any

import typer

from kanso import research
from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.errors import PreconditionError, ValidationError
from kanso.research import daemon, records
from kanso.research.lanes import DEFAULT_LANE
from kanso.schemas import GateResult
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
    as_json: JsonOption = False,
) -> None:
    """Print a card's stored `strategy.py`, or the diff between two of them."""
    emit(as_json or global_json(ctx), lambda: _show(open_workspace(ctx), hyp_id, sha, diff))


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
        "results": str(research.results_file(ws, hyp_id)),
    }
    lines = [
        field("card", f"{card.sha7} · {card.status} · {desc}"),
        field("metric", f"{card.metric:.6f} ± {card.metric_se:.6f} · {card.n_trades} trade(s)"),
        field("cost", f"{card.wall_s:.1f}s · {card.peak_mem_gb:.2f} GB · trial {trials}"),
        field("best", "none yet" if run.best_sha is None else f"{run.best_sha[:7]}"),
    ]
    lines += [
        indent(f"{result.id}: {'pass' if result.passed else 'fail'} — {_evidence(result)}")
        for result in card.gate_results
    ]
    if card.crash_tail:
        lines += [indent(line) for line in card.crash_tail.splitlines()[-5:]]
    return Report(data=data, lines=tuple(lines))


def _run(ws: Workspace, hyp_id: str, cards: int | None) -> Report:
    with store(ws) as opened:
        outcome = research.drive(ws, opened, hyp_id, cards=cards, lane=DEFAULT_LANE)
        trials = records.n_trials(opened, hyp_id)
    data: dict[str, Any] = {**outcome.payload(), "n_trials": trials}
    best = (
        "none yet"
        if outcome.best_sha is None
        else f"{outcome.best_sha[:7]} at {outcome.best_metric:.6f}"
    )
    lines = (
        field("run", f"{outcome.run_id} · lane {outcome.lane} · {outcome.reason}"),
        field(
            "cards",
            f"{outcome.proposed} proposed · {outcome.keeps} keep · "
            f"{outcome.discards} discard · {outcome.crashes} crash · trial {trials}",
        ),
        field("aligned", f"{outcome.checks} check(s) · {outcome.drifts} drift(s)"),
        field("best", best),
        field(
            "next",
            f"kanso research show {hyp_id}"
            if outcome.ended
            else f"kanso research run {hyp_id} --cards {outcome.proposed}",
        ),
    )
    return Report(data=data, lines=lines)


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
    pid = daemon.stop(ws)
    data: dict[str, Any] = {"running": False, "pid": pid}
    lines = (
        field("daemon", f"stopped · pid {pid}"),
        # Nothing is ended and nothing is removed, so the next `start` resumes.
        field("runs", "left open, with their lane directories"),
    )
    return Report(data=data, lines=lines)


def _status(ws: Workspace) -> Report:
    with store(ws) as opened:
        found = daemon.status(ws, opened)
    data: dict[str, Any] = found.payload()
    running = f"running · pid {found.pid}" if found.running else "stopped"
    lines = [
        field("daemon", running),
        field("lanes", ", ".join(found.lanes) or "none (run `kanso env detect`)"),
        field("runs", f"{len(found.runs)} active"),
    ]
    lines += [indent(_run_line(item)) for item in found.runs]
    lines.append(field("queue", f"{len(found.queue)} waiting"))
    lines += [
        indent(f"{item.hyp_id:<20}priority {item.priority:<4}since {item.enqueued_at}")
        for item in found.queue
    ]
    return Report(data=data, lines=tuple(lines))


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


def _show(ws: Workspace, hyp_id: str, sha: str | None, diff: str | None) -> Report:
    with store(ws) as opened:
        known = _shas(opened, hyp_id)
        subject = _resolve(opened, hyp_id, sha, known)
        if diff is None:
            source = opened.get_blob(subject).decode("utf-8", errors="replace")
            data: dict[str, Any] = {"id": hyp_id, "sha": subject, "source": source}
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
