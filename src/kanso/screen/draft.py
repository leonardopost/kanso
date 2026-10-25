"""`kanso screen draft`: one cell that trades — a `response` or a `catch_up` — of a recorded
result, written as a draft hypothesis.

No model is called and nothing is registered. The ladder is `hyp explore`'s: an id nothing
holds, reserved or not, and a directory that did not exist. What is written:

* `hypothesis.yaml` — the universe is the cell's trigger (a catch-up's leader) and follower;
  the resolution and the data requirements are their types and grain, `funding` with them when
  a perpetual is held; the horizon is the cell's, or a catch-up's `max_hold`, in whole seconds;
  the costs are the screen's for the follower's venue, less any `latency_ms` there, which the
  screen ignores, and with the measure's `latency_ms` when it is above zero — the route the
  cell was measured under, which a hypothesis at tick resolution may not leave at zero, so a
  cell measured at zero drafts no latency and `hyp add` names it; the research window is the
  window the screen read, and the certification window is the operator's `--certify`, which
  has no default and is refused when it starts sooner than the embargo after the screened
  window ends.
  The mechanism follows from the cell: a fade is `mean_reversion`, a follow of the same name
  `momentum`, a follow of another `stat_arb`, and so is a catch-up, which always trades one name
  toward another. The risk limits are the template's, as `hyp new` writes them: a draft is the
  operator's to edit.
* `program.md` — the template, and a `Measured` block with the cell's numbers. They are
  research-side facts, measured on the research window, and the proposer may read them.
* `strategy.py` — the cell's own rule as a sleeve, one position at a time: a response's enters
  the follower on the trigger and leaves it after the horizon; a catch-up's enters it when the
  gap to its leader's implied move opens and leaves when the gap closes, stops or is capped,
  its fitted response written into it. The baseline card re-measures the cell through the
  runner, and a baseline far from the screen's ceiling is a finding before any proposal is paid.

A cell whose trigger or follower is a derived leg, or a book, is refused (exit 3): its rule
trades several legs, or a book, and is written by hand.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml

from kanso.data.instruments import current_definitions
from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import HYPOTHESIS_FILE, PROGRAM_FILE, STRATEGY_FILE, check_id, hypothesis_dir
from kanso.hyp.scaffold import render
from kanso.schemas import Hypothesis, dump_yaml, embargo_days, parse_yaml, render_duration
from kanso.schemas.screen import (
    CatchUp,
    Cell,
    LeadLag,
    Response,
    Screen,
    ScreenResult,
    span_ns,
)
from kanso.screen import records

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

SEED: Final = "strategy_screened.py"
"""The template a drafted response's strategy is rendered from."""

CAUGHT: Final = "strategy_caught.py"
"""The template a drafted catch-up's strategy is rendered from."""

DRAFTED: Final = "screen_drafted"
"""The event a draft appends, under the new hypothesis's id."""

RESERVED: Final = frozenset({"portfolio"})


@dataclass(frozen=True)
class Drafted:
    """What a draft wrote, and from what."""

    hyp_id: str
    directory: Path
    screen: str
    cell: str

    def payload(self) -> dict[str, object]:
        return {
            "id": self.hyp_id,
            "dir": str(self.directory),
            "screen": self.screen,
            "cell": self.cell,
            "files": sorted(path.name for path in self.directory.iterdir()),
        }


def draft(
    ws: Workspace,
    store: StateStore,
    screen_id: str,
    cell_key: str,
    new_id: str,
    certify: tuple[date, date] | None,
) -> Drafted:
    """Write `hypotheses/<new_id>/` from one cell of the screen's newest result."""
    found = records.results(store, screen_id)
    if not found:
        raise ValidationError(
            f"{screen_id!r} has no recorded result to draft from",
            remedy=f"run `kanso screen run screens/{screen_id}/screen.yaml`",
        )
    result = found[0]
    screen = parse_yaml(Screen, store.get_blob(result.sha).decode("utf-8"), "screen.yaml")
    if screen.hyp is not None:
        raise ValidationError(
            f"{screen_id} is bound to {screen.hyp}, which already exists",
            remedy=f"research {screen.hyp}; a draft is written from a free screen",
        )
    cell = _cell(result, cell_key)
    measure = screen.measures[cell.measure]
    assert not isinstance(measure, LeadLag)
    trigger, follower = _leader(measure), str(cell.params["follower"])
    _refuse_untradable(screen, (trigger, follower))
    identity = _free_id(ws, store, new_id)
    if certify is None:
        raise ValidationError(
            "--certify: a draft's certification window is the operator's, and has no default",
            remedy="pass --certify YYYY-MM-DD..YYYY-MM-DD, after the embargo",
        )
    held = measure.max_hold if isinstance(measure, CatchUp) else str(cell.params["horizon"])
    hold = math.ceil(span_ns(held) / 1_000_000_000)
    horizon = render_duration(timedelta(seconds=hold), "horizon")
    earliest = result.window.end + timedelta(days=embargo_days(horizon))
    if certify[0] < earliest:
        raise ValidationError(
            f"--certify: {certify[0]} is inside the {embargo_days(horizon)}-day embargo after "
            f"the screened window, which ends {result.window.end}",
            remedy=f"start certification on or after {earliest}",
        )
    hypothesis = _hypothesis(ws, screen, result, cell, identity, horizon, certify)
    directory = hypothesis_dir(ws, identity)
    directory.mkdir(parents=True)
    (directory / HYPOTHESIS_FILE).write_text(
        f"# drafted by `kanso screen draft` from {screen_id}, cell {cell.key}; registered by "
        f"nothing\n{dump_yaml(hypothesis)}",
        encoding="utf-8",
    )
    (directory / PROGRAM_FILE).write_text(
        render(PROGRAM_FILE, hyp_id=identity, today=_today()) + _measured(result, cell),
        encoding="utf-8",
    )
    (directory / STRATEGY_FILE).write_text(_seed(identity, screen, measure, cell), encoding="utf-8")
    store.event(DRAFTED, identity, {"screen": screen_id, "sha": result.sha, "cell": cell.key})
    return Drafted(identity, directory, screen_id, cell.key)


def _cell(result: ScreenResult, key: str) -> Cell:
    cell = next((item for item in result.cells if item.key == key), None)
    if cell is None:
        raise ValidationError(
            f"{key!r} is not a cell of {result.screen}'s newest result",
            remedy=f"list them with `kanso screen show {result.screen} --json`",
        )
    if cell.id == "lead_lag":
        raise ValidationError(
            f"{key} is a lead_lag cell, which names no rule to trade",
            remedy="draft from a response or a catch_up cell: each names a rule, a follower "
            "and when it leaves",
        )
    return cell


def _refuse_untradable(screen: Screen, names: tuple[str, str]) -> None:
    for name in names:
        if name not in screen.legs:
            raise ValidationError(
                f"{name} is a derived leg; its rule trades several legs and is written by hand",
                remedy="write the hypothesis with `kanso hyp new`",
            )
        if screen.legs[name].type == "book":
            raise ValidationError(
                f"{name} is read as a book; a seed trading on a book is written by hand",
                remedy="write the hypothesis with `kanso hyp new`",
            )


def _free_id(ws: Workspace, store: StateStore, new_id: str) -> str:
    identity = check_id(new_id)
    taken = store.connection.execute(
        "SELECT 1 FROM hypotheses WHERE hyp_id = ?", (identity,)
    ).fetchone()
    if identity in RESERVED or taken is not None or hypothesis_dir(ws, identity).exists():
        raise PreconditionError(
            f"{identity!r} is taken: registered, reserved, or already a directory",
            remedy="choose another id with --as",
        )
    return identity


def _hypothesis(
    ws: Workspace,
    screen: Screen,
    result: ScreenResult,
    cell: Cell,
    identity: str,
    horizon: str,
    certify: tuple[date, date],
) -> Hypothesis:
    measure = screen.measures[cell.measure]
    assert not isinstance(measure, LeadLag)
    trigger = screen.legs[_leader(measure)]
    follower = screen.legs[str(cell.params["follower"])]
    legs = [trigger, follower]
    universe = list(dict.fromkeys(leg.instrument for leg in legs))
    grains = {leg.resolution for leg in legs if leg.type == "bar"}
    if len(grains) > 1:
        raise ValidationError(
            f"{cell.key}: its legs are bars of {', '.join(sorted(map(str, grains)))}, and a "
            "hypothesis reads one bar size",
            remedy="screen the two at one grain, or write the hypothesis by hand",
        )
    types: list[str] = sorted({str(leg.type) for leg in legs})
    resolution = next(iter(grains)) if grains else ("trade" if "trade" in types else "quote")
    own: dict[str, list[str]] = {}
    for leg in legs:
        own.setdefault(leg.instrument, [])
        if leg.type not in own[leg.instrument]:
            own[leg.instrument].append(str(leg.type))
    if _perpetual(ws, universe):
        types.append("funding")
        own = {name: [*kinds, "funding"] for name, kinds in own.items()}
    mixed = any(len(kinds) < len(types) for kinds in own.values())
    if isinstance(measure, CatchUp):
        mechanism = "stat_arb"
    elif measure.side == "against":
        mechanism = "mean_reversion"
    else:
        mechanism = "momentum" if trigger.instrument == follower.instrument else "stat_arb"
    venue = follower.instrument.rpartition(".")[2]
    template = yaml.safe_load(render(HYPOTHESIS_FILE, hyp_id=identity))
    document: dict[str, Any] = {
        "schema": 1,
        "id": identity,
        "title": f"{screen.title} — {cell.key}",
        "thesis": screen.thesis,
        "mechanism": mechanism,
        "universe": universe,
        "horizon": horizon,
        "resolution": resolution,
        "data_requirements": types,
        **(
            {"data_by_instrument": {name: sorted(kinds) for name, kinds in own.items()}}
            if mixed
            else {}
        ),
        "risk_limits": template["risk_limits"],
        "windows": {
            "research": {"start": result.window.start, "end": result.window.end},
            "certification": {"start": certify[0], "end": certify[1]},
            "forward": {"start": certify[1] + timedelta(days=1)},
        },
    }
    costs = (screen.costs or {}).get(venue)
    stated = {} if costs is None else costs.model_dump(exclude_none=True, exclude={"latency_ms"})
    if measure.latency_ms > 0:
        stated["latency_ms"] = measure.latency_ms
    if stated:
        document["costs"] = stated
    return Hypothesis.model_validate(document)


def _perpetual(ws: Workspace, universe: list[str]) -> bool:
    from nautilus_trader.model.instruments import CryptoPerpetual

    defined = current_definitions(ws)
    return any(isinstance(defined.get(name), CryptoPerpetual) for name in universe)


def _measured(result: ScreenResult, cell: Cell) -> str:
    """The cell's numbers, as the proposer reads them."""
    stats = cell.response
    assert stats is not None
    return (
        f"\n## Measured by screen {result.screen}\n\n"
        f"Cell `{cell.key}`, measured on {result.window.start}..{result.window.end} — this "
        f"hypothesis's research window — by screen bytes {result.sha[:7]} on snapshot "
        f"{result.snapshot[:7]}. In-sample facts about the research window, not a promise:\n\n"
        f"- {stats.events_per_day:.4g} events a day over {cell.sessions} sessions, "
        f"{stats.unfilled} unfilled\n"
        f"- gross {stats.gross_bp:+.4g} bp an event against a hurdle of "
        f"{stats.hurdle_bp:.4g} bp: margin {stats.margin_bp:+.4g} bp, hit rate "
        f"{stats.hit_rate:.2%}\n"
        f"- ceiling {stats.ceiling_bp_day:+.4g} bp a day at one notional an event, no sizing, "
        f"no capacity limit: a search approaches it and never exceeds it\n"
        f"- t {cell.t:+.3g}, family-wise p {cell.p:.4g}; folds "
        f"{', '.join('—' if fold is None else f'{fold:+.3g}' for fold in cell.folds)}\n"
        + _exits(cell)
    )


def _exits(cell: Cell) -> str:
    """How a catch-up's positions ended: the holds its conditions produced, and its response."""
    caught = cell.catch_up
    if caught is None:
        return ""
    beta = "none" if caught.beta is None else f"{caught.beta:+.4g}"
    return (
        f"- exits: {caught.closed} closed, {caught.stopped} stopped, {caught.capped} capped at "
        f"the max hold; hold median {caught.hold_median_s:.4g} s, p90 {caught.hold_p90_s:.4g} s\n"
        f"- the follower's fitted response to its leader: beta {beta}\n"
    )


def _seed(identity: str, screen: Screen, measure: Response | CatchUp, cell: Cell) -> str:
    if isinstance(measure, CatchUp):
        return _caught(identity, screen, measure, cell)
    trigger = screen.legs[measure.trigger.leg]
    follower = screen.legs[str(cell.params["follower"])]
    scored = measure.trigger.z is not None
    window = measure.trigger.lookback if scored else measure.trigger.within
    assert window is not None
    threshold = float(cell.params["threshold"])
    rule = (
        f"is {threshold:g} standard deviations from its mean over {window}"
        if scored
        else f"moves {threshold:g} bp within {window}"
    )
    return render(
        SEED,
        hyp_id=identity,
        screen_id=screen.id,
        cell=cell.key,
        trigger=measure.trigger.leg,
        follower=str(cell.params["follower"]),
        rule=rule,
        direction="the way it moved" if measure.side == "with" else "against the move",
        hold=str(cell.params["horizon"]),
        trigger_id=trigger.instrument,
        follower_id=follower.instrument,
        side="1" if measure.side == "with" else "-1",
        scored=str(scored),
        threshold=repr(threshold),
        window_s=repr(span_ns(window) / 1e9),
        hold_s=repr(span_ns(str(cell.params["horizon"])) / 1e9),
    )


def _caught(identity: str, screen: Screen, measure: CatchUp, cell: Cell) -> str:
    follower = str(cell.params["follower"])
    beta = None if cell.catch_up is None else cell.catch_up.beta
    if beta is None:
        raise ValidationError(
            f"{cell.key}: its leader never moved over the fit span, so it has no response to trade",
            remedy="screen it on a span its leader moves in, or draft another cell",
        )
    return render(
        CAUGHT,
        hyp_id=identity,
        screen_id=screen.id,
        cell=cell.key,
        leader=measure.leader,
        follower=follower,
        within=measure.within,
        max_hold=measure.max_hold,
        leader_id=screen.legs[measure.leader].instrument,
        follower_id=screen.legs[follower].instrument,
        beta=repr(beta),
        enter_bp=repr(float(cell.params["enter_bp"])),
        close=repr(float(cell.params["close"])),
        stop=repr(float(cell.params["stop"])),
        within_s=repr(span_ns(measure.within) / 1e9),
        max_hold_s=repr(span_ns(measure.max_hold) / 1e9),
    )


def _leader(measure: Response | CatchUp) -> str:
    """The leg whose move opens a position: a response's trigger, a catch-up's leader."""
    return measure.leader if isinstance(measure, CatchUp) else measure.trigger.leg


def _today() -> str:
    """Today in UTC as eight digits, spelled field by field as `hyp new` spells it."""
    day = datetime.now(tz=UTC).date()
    return f"{day.year:04d}{day.month:02d}{day.day:02d}"
