"""A window with a split in it: what the card says, and what the runner refuses to run.

The measurement this file exists for: a 1,005-share position at ten dollars taken through a
one-for-ten reverse split used to close at a realised 90,450 and an equity of 190,450 on
100,000 of capital, because nothing in kanso knew the price had been restated rather than
earned. Every number asserted below was read off a real engine, and the ones that matter
are stated to the cent.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria import CardRun
from kanso.criteria.run import midnight_ns
from kanso.data.types import CorporateAction
from kanso.errors import PreconditionError
from kanso.nautilus.backtest import _adjusted, execute, run, run_subprocess

from .conftest import (
    CAPITAL,
    CERTIFICATION,
    CLOSE_NS,
    INSTRUMENT,
    RESEARCH,
    SECOND_NS,
    bar_type,
    catalog,
    hypothesis,
    instrument,
)

EX = date(2024, 1, 16)
REVERSE = {"splits": [{"ex_date": EX.isoformat(), "ratio": 0.1}]}
FORWARD = {"splits": [{"ex_date": EX.isoformat(), "ratio": 4.0}]}
ACTIONS = ("bar", "corporate_action")

HOLDER = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    exit_on: int = 25


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=10_050.0)
        elif self.seen == self.kanso_config.exit_on:
            self.submit_exit(bar.bar_type.instrument_id)
"""
"""Buys 1,005 shares on the first session, holds them across the ex-date, sells on the 25th."""


def restated(
    window: tuple[date, date] = RESEARCH, before: float = 10.0, after: float = 100.0
) -> list[Bar]:
    """One bar a day, priced `before` up to the ex-date and `after` from it: a restatement."""
    made: list[Bar] = []
    for index in range((window[1] - window[0]).days + 1):
        ts_event = midnight_ns(window[0]) + index * 86_400 * SECOND_NS + CLOSE_NS
        close = before if ts_event < midnight_ns(EX) else after
        made.append(
            Bar(
                bar_type(),
                Price(close, 2),
                Price(close, 2),
                Price(close, 2),
                Price(close, 2),
                Quantity.from_int(100_000),
                ts_event=ts_event,
                ts_init=ts_event + SECOND_NS,
            )
        )
    return made


def action(kind: str = "split", ratio: float = 0.1, name: str = INSTRUMENT) -> CorporateAction:
    """A corporate-action point, announcing itself on its own ex-date as a split does."""
    ts = midnight_ns(EX)
    return CorporateAction(
        instrument_id=InstrumentId.from_str(name),
        kind=kind,
        ratio=ratio,
        cash=0.0,
        currency="USD",
        ex_date_ns=ts,
        ts_event=ts,
        ts_init=ts,
    )


def card(
    request_for,
    *,
    scheduled: dict[str, object] | None = REVERSE,
    points: list[Bar] | None = None,
    actions: tuple[CorporateAction, ...] = (),
    window: tuple[date, date] = RESEARCH,
    **overrides: float,
) -> CardRun:
    """One run of this window, with whatever schedule the instrument carries."""
    hyp = hypothesis(data_requirements=ACTIONS if actions else ("bar",))
    request = request_for(window, source=HOLDER, hypothesis_=hyp, overrides=overrides)
    held = instrument() if scheduled is None else instrument(info=scheduled)
    groups: list[tuple[object, ...]] = [tuple(restated(window) if points is None else points)]
    if actions:
        groups.append(actions)
    return execute(request, [held], groups).run


# --- what the card says -------------------------------------------------------


def test_a_reverse_split_moves_the_equity_curve_by_the_residue_and_nothing_else(
    request_for,
) -> None:
    """1,005 at ten becomes 100 at a hundred: 10,050 of value becomes 10,000, and that is all."""
    equity = card(request_for).equity

    assert equity[0] == pytest.approx(CAPITAL - 5.025)
    assert equity[(EX - RESEARCH[0]).days] == pytest.approx(CAPITAL - 55.025)
    assert max(equity) - min(equity) == pytest.approx(55.0)


def test_the_exit_is_sized_against_the_shares_the_split_left(request_for) -> None:
    """The whole point of the resync: 100 sold, not the 1,005 that no longer exist."""
    fills = card(request_for).fills

    assert [(fill.side, fill.qty, fill.px) for fill in fills] == [
        ("BUY", 1_005.0, 10.0),
        ("SELL", 100.0, 100.0),
    ]


def test_the_trade_is_measured_in_the_shares_it_opened_with(request_for) -> None:
    """`notional` is still what was put in, and the profit is the residue plus the costs."""
    trade = card(request_for).trades[0]

    assert trade.qty == 1_005.0
    assert trade.avg_open == 10.0
    assert trade.avg_close == pytest.approx(10_000.0 / 1_005.0)
    assert trade.notional == pytest.approx(10_050.0)
    assert trade.cost == pytest.approx(10.025)
    assert trade.pnl_net == pytest.approx(-60.025)


def test_a_position_still_open_at_the_close_is_marked_at_the_shares_it_holds(request_for) -> None:
    """No exit at all, so the last equity is cash plus 100 shares rather than plus 1,005."""
    held = card(request_for, exit_on=0)

    assert held.trades == ()
    assert held.equity[-1] == pytest.approx(CAPITAL - 55.025)


def test_a_forward_split_is_the_same_arithmetic_the_other_way(request_for) -> None:
    """Four-for-one at forty: 251 shares become 1,004 at ten, with nothing left over."""
    made = card(request_for, scheduled=FORWARD, points=restated(before=40.0, after=10.0))

    assert [(fill.side, fill.qty) for fill in made.fills] == [("BUY", 251.0), ("SELL", 1_004.0)]
    assert made.trades[0].qty == 251.0
    assert made.trades[0].avg_open == 40.0
    assert made.trades[0].avg_close == 40.0
    assert made.trades[0].pnl_net == pytest.approx(-made.trades[0].cost)


# --- what the runner refuses --------------------------------------------------


def test_a_split_the_window_holds_and_the_definition_does_not_is_refused(request_for) -> None:
    """The half that protects an operator who has not adopted the schedule."""
    with pytest.raises(PreconditionError) as raised:
        card(request_for, scheduled=None, actions=(action(),))

    assert "the window holds a split effective 2024-01-16 at a ratio of 0.1" in raised.value.message
    assert "its definition schedules none" in raised.value.message
    assert "info.splits" in (raised.value.remedy or "")


def test_a_schedule_that_disagrees_with_the_window_is_refused_too(request_for) -> None:
    """A schedule contradicting the data is worse than none, so the two are named together."""
    with pytest.raises(PreconditionError, match="its definition schedules 0.1"):
        card(request_for, actions=(action(ratio=0.25),))


def test_a_scheduled_split_the_window_also_carries_is_run(request_for) -> None:
    """Agreement is silence: the point and the definition say the same thing."""
    assert [fill.qty for fill in card(request_for, actions=(action(),)).fills] == [1_005.0, 100.0]


def test_a_cash_event_is_not_a_split(request_for) -> None:
    """A dividend has an announcement date and belongs in the data; it is not this refusal's."""
    made = card(request_for, scheduled=None, actions=(action("dividend", 1.0),))

    assert made.fills


def test_a_split_effective_after_the_window_is_another_window_s_problem(request_for) -> None:
    """This refusal is about one window; which windows a dataset serves is `_covers`'s question."""
    announced = action()
    later = CorporateAction(
        instrument_id=announced.instrument_id,
        kind="split",
        ratio=0.1,
        cash=0.0,
        currency="USD",
        ex_date_ns=midnight_ns(CERTIFICATION[0]),
        ts_event=announced.ts_event,
        ts_init=announced.ts_init,
    )

    made = card(request_for, scheduled=None, actions=(later,))

    assert made.fills


# --- across the process boundary ----------------------------------------------


def test_a_card_run_in_a_child_carries_the_schedule_with_it(tmp_path: Path, request_for) -> None:
    """A card is handed its instruments by pickle, and a schedule lost on the way across
    would be a silent 905% in the one place kanso cannot watch."""
    store = catalog(tmp_path / "catalog", restated(), [instrument(info=REVERSE)])

    result = run_subprocess(request_for(source=HOLDER), store, tmp_path)

    assert not result.crashed, result.traceback_tail
    assert [fill.qty for fill in result.run.fills] == [1_005.0, 100.0]


def test_the_same_window_read_from_a_catalog_says_the_same_thing(
    tmp_path: Path, request_for
) -> None:
    """The schedule survives parquet as well as pickle, because `info` is part of `to_dict`."""
    store = catalog(tmp_path / "catalog", restated(), [instrument(info=REVERSE)])

    made = run(request_for(source=HOLDER), store).run

    assert [fill.qty for fill in made.fills] == [1_005.0, 100.0]


# --- the adjustment ledger the extraction reads -------------------------------


class Adjustment:
    """One `PositionAdjusted` as the extraction reads it: an id, an instant and a change."""

    def __init__(self, key: str, change: float | None = -905.0) -> None:
        self.id = key
        self.ts_event = 1_000
        self.instrument_id = INSTRUMENT
        self.quantity_change = change


class Held:
    """A position as `_adjusted` sees it: nothing but its adjustment ledger."""

    def __init__(self, *events: Adjustment) -> None:
        self.adjustments = list(events)


def test_one_adjustment_held_by_two_positions_is_counted_once() -> None:
    """A netting position lives in the cache twice while a snapshot of it survives, and a
    quantity change counted twice would halve the position the equity curve marks."""
    shared = Adjustment("A")

    assert _adjusted([Held(shared), Held(shared)]) == ((1_000, INSTRUMENT, -905.0),)


def test_an_adjustment_that_changes_no_quantity_changes_no_holding() -> None:
    """`PositionAdjusted` also carries pure P&L adjustments; those move no shares."""
    assert _adjusted([Held(Adjustment("A", None))]) == ()
