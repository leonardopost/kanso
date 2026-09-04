"""`kanso research`: the interactive loop a coding agent drives.

`begin` opens a run and prints the lane directory, which is the whole of the interface: an
agent edits `strategy.py` there and calls `card`, and the evaluation it gets is the same
one the autonomous driver gets. `end` closes the run and removes the directory — the
cards, the blobs and the hypothesis's best stay in state, so nothing a run learned is in
the directory to begin with.

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
from kanso.research import records
from kanso.schemas import GateResult
from kanso.state import StateStore
from kanso.workspace import Workspace

app = typer.Typer(help="Runs, cards and what a card left in state.", no_args_is_help=True)

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


@app.command("end")
def end_command(ctx: typer.Context, hyp_id: IdArgument, as_json: JsonOption = False) -> None:
    """End the active run and remove its lane directory, and nothing else."""
    emit(as_json or global_json(ctx), lambda: _end(open_workspace(ctx), hyp_id))


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
