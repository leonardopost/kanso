"""A window with a split in it: what the card says, and what the runner refuses to run.

The measurement this file exists for: a 1,005-share position at ten dollars taken through a
one-for-ten reverse split used to close at a realised 90,450 and an equity of 190,450 on
100,000 of capital, because nothing in kanso knew the price had been restated rather than
earned. Every number asserted below was read off a real engine, and the ones that matter
are stated to the cent.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria import CardRun
from kanso.criteria.run import midnight_ns
from kanso.data.loader import to_ns
from kanso.data.types import CorporateAction
from kanso.errors import PreconditionError
from kanso.nautilus.backtest import _adjusted, execute, run, run_subprocess
from kanso.schemas import DateWindow

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


# --- when: past the midnight that opens the ex-date in New York ---------------

EVE, EX_EST = date(2024, 1, 16), date(2024, 1, 17)
"""A Tuesday and a Wednesday in January: under EST the UTC midnight opening the ex-date is
19:00 New York on the eve, inside its post-market, and New York's own midnight is 05:00Z."""

FIFTEEN = {"splits": [{"ex_date": EX_EST.isoformat(), "ratio": 15.0}]}
"""SOXL's fifteen-for-one of 2021-03-02, moved into the research window."""

NEW_YORK = ZoneInfo("America/New_York")

TAPE = (
    (EVE, 15, 0, 600.00),
    (EVE, 18, 30, 600.00),
    (EVE, 19, 30, 600.60),
    (EVE, 19, 45, 601.50),
    (EVE, 19, 51, 601.20),
    (EX_EST, 4, 21, 40.10),
    (EX_EST, 9, 31, 40.20),
)
"""Minute bars as `(day, hour, minute, close)` on New York's clock, each stamped at its close
and public at it: the eve's session into its post-market, then the ex-date's first print and
its open. It is the shape of SOXL's tape across its split, measured on the vendor's
unadjusted minute bars — the last print at the old price in the bar closing 18:51 New York on
2021-03-01, at 641.23, and the first at the new one in the bar closing 04:21 on 2021-03-02,
at 41.80 — at round prices that divide by fifteen."""

TIMED = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    enter_ns: int = 0
    exit_ns: int = 0


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        if bar.ts_event == self.kanso_config.enter_ns:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=12_000.0)
        elif bar.ts_event == self.kanso_config.exit_ns:
            self.submit_exit(bar.bar_type.instrument_id)
"""
"""Buys 12,000 of stock on the bar closing at `enter_ns` and sells whatever it then holds on
the bar closing at `exit_ns`."""


def new_york(day: date, hour: int = 0, minute: int = 0) -> int:
    """An instant on New York's wall clock, in UTC nanoseconds."""
    return to_ns(datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK))


def flat(grain: BarType, ts_event: int, close: float) -> Bar:
    """A bar that opened, traded and closed at one price, public the instant it closed."""
    price = Price(close, 2)
    volume = Quantity.from_int(100_000)
    return Bar(grain, price, price, price, price, volume, ts_event=ts_event, ts_init=ts_event)


def minute_card(request_for, enter_ns: int, exit_ns: int) -> CardRun:
    """`TIMED` over `TAPE`, on an instrument that schedules the fifteen-for-one, with a
    one-minute return period so the equity curve marks the book at every bar."""
    hyp = hypothesis().model_copy(update={"resolution": "1m"})
    request = request_for(
        source=TIMED,
        hypothesis_=hyp,
        overrides={"enter_ns": enter_ns, "exit_ns": exit_ns},
        period="1m",
    )
    grain = BarType.from_str(f"{INSTRUMENT}-1-MINUTE-LAST-EXTERNAL")
    tape = tuple(
        flat(grain, new_york(day, hour, minute), close) for day, hour, minute, close in TAPE
    )
    return execute(request, [instrument(info=FIFTEEN)], [tape]).run


def test_an_intraday_position_open_the_evening_before_an_ex_date_books_what_the_tape_paid(
    request_for,
) -> None:
    """Bought at 18:30 New York on the eve of an EST ex-date, still held at 19:30, sold at
    19:45. The split's instant is the ex-date's New York midnight, after the eve's last print
    at 19:51, so the position is never adjusted: the card marks 20 shares at 19:30 and books
    the 1.50 a share the tape paid, less its costs. Under the UTC midnight, 19:00 New York,
    the 19:30 print was past the split: the venue made the 20 shares 300 in a session still
    quoted at six hundred, the exit sold all 300, and the card booked 168,353.78 on 12,000 of
    stock."""
    made = minute_card(request_for, new_york(EVE, 18, 30), new_york(EVE, 19, 45))

    assert [(fill.side, fill.qty, fill.px) for fill in made.fills] == [
        ("BUY", 20.0, 600.0),
        ("SELL", 20.0, 601.5),
    ]
    assert [equity - CAPITAL for equity in made.equity] == pytest.approx(
        [0.0, -6.0, 6.0, 17.985, 17.985, 17.985, 17.985]
    )
    assert made.trades[0].cost == pytest.approx(12.015)
    assert made.trades[0].pnl_net == pytest.approx(30.0 - 12.015)


def test_a_position_held_overnight_is_adjusted_once_before_the_ex_date_s_first_print(
    request_for,
) -> None:
    """Bought at 15:00 New York on the eve and sold on the ex-date's first print, 04:21. The
    card marks 20 shares at every print of the eve's post-market and sells 300 on the first
    print of the ex-date, so the venue applied the split once, between the two sessions and
    before that print was matched; the trade books the ten cents a share the print moved
    from the restated basis of 40.00 — the 1.50 an opening share made — less its costs.
    Under the UTC midnight the card marked 300 shares at 600.60 at 19:30, an equity 168,174
    over its capital, and held that book to the eve's last print."""
    made = minute_card(request_for, new_york(EVE, 15, 0), new_york(EX_EST, 4, 21))

    assert [(fill.side, fill.qty, fill.px) for fill in made.fills] == [
        ("BUY", 20.0, 600.0),
        ("SELL", 300.0, 40.1),
    ]
    assert [equity - CAPITAL for equity in made.equity] == pytest.approx(
        [-6.0, -6.0, 6.0, 24.0, 18.0, 17.985, 17.985]
    )
    trade = made.trades[0]
    assert (trade.qty, trade.avg_open) == (20.0, 600.0)
    assert trade.avg_close == pytest.approx(601.5)
    assert trade.pnl_net == pytest.approx(30.0 - trade.cost)


@pytest.mark.parametrize(
    ("ex_date", "window"),
    [(EX_EST, RESEARCH), (date(2024, 7, 17), (date(2024, 7, 1), date(2024, 7, 31)))],
    ids=["winter", "summer"],
)
def test_the_eve_s_daily_bar_stamped_at_the_ex_date_s_midnight_is_in_the_old_shares(
    request_for, ex_date: date, window: tuple[date, date]
) -> None:
    """The vendor's daily bar is New York's calendar day, and kanso stamps a bar at its close:
    the next New York midnight, 05:00Z in winter and 04:00Z in summer, measured on SOXL's and
    SOXS's bars either side of their splits. So the eve's bar lands exactly on the instant the
    split takes effect and is matched in the old shares, and the ex-date's own bar, a midnight
    later, is the first in the new. Under the UTC midnight the split was applied ahead of the
    eve's bar and the card marked 240 shares at 603.00 for a day: an equity 135,067.18 over
    its capital."""
    sessions = [ex_date + timedelta(days=offset) for offset in (-5, -1, 0, 1, 2)]
    days = tuple(
        flat(bar_type(), new_york(day + timedelta(days=1)), 603.0 if day < ex_date else 40.2)
        for day in sessions
    )
    hyp = hypothesis()
    researched = DateWindow(start=window[0], end=window[1])
    hyp = hyp.model_copy(
        update={"windows": hyp.windows.model_copy(update={"research": researched})}
    )
    request = request_for(window, source=HOLDER, hypothesis_=hyp, overrides={"exit_on": 4})
    scheduled = {"splits": [{"ex_date": ex_date.isoformat(), "ratio": 15.0}]}

    made = execute(request, [instrument(info=scheduled)], [days]).run

    assert [(fill.side, fill.qty, fill.px) for fill in made.fills] == [
        ("BUY", 16.0, 603.0),
        ("SELL", 240.0, 40.2),
    ]
    assert max(made.equity) - min(made.equity) == pytest.approx(16 * 603.0 * 0.0005)


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


PAIR = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    ex_ns: int = 0
    exits: int = 0


class Strategy(KansoStrategy):
    \"\"\"Buys HEDGE on its first bar; on DEMO's ex-date bar, adds to HEDGE or sells it.\"\"\"

    config_cls = Config

    def on_start(self):
        self.bought = False
        self.acted = False

    def on_bar(self, bar):
        key = str(bar.bar_type.instrument_id)
        if key == "HEDGE.XNAS" and not self.bought:
            self.bought = True
            self.submit_entry("HEDGE.XNAS", "BUY", notional=10_000.0)
        if key == "DEMO.XNAS" and bar.ts_event >= self.kanso_config.ex_ns and not self.acted:
            self.acted = True
            if self.kanso_config.exits:
                self.submit_exit("HEDGE.XNAS")
            else:
                self.submit_entry("HEDGE.XNAS", "BUY")
"""
"""Trades the split leg from the other leg's handler on the ex-date."""


def published(symbol: str, before: float, after: float, lag_s: int) -> list[Bar]:
    """A flat daily series restated on the ex-date, each bar published `lag_s` after it closes."""
    made: list[Bar] = []
    for index in range((RESEARCH[1] - RESEARCH[0]).days + 1):
        ts_event = midnight_ns(RESEARCH[0]) + index * 86_400 * SECOND_NS + CLOSE_NS
        close = before if ts_event < midnight_ns(EX) else after
        price = Price(close, 2)
        made.append(
            Bar(
                bar_type(symbol),
                price,
                price,
                price,
                price,
                Quantity.from_int(100_000),
                ts_event=ts_event,
                ts_init=ts_event + lag_s * SECOND_NS,
            )
        )
    return made


@pytest.mark.parametrize("exits", [0, 1], ids=["adds", "exits"])
def test_an_order_sent_into_the_split_name_before_it_prints_fills_at_the_restated_price(
    request_for, exits: int
) -> None:
    """DEMO's ex-date bar is published before HEDGE's, so DEMO's point applies HEDGE's split
    and DEMO's handler trades HEDGE while its book still holds yesterday's 20.00. Matched
    there, the exit sold 50 restated shares at 20.00 and the card lost 9,005.50."""
    hyp = hypothesis(universe=("DEMO.XNAS", "HEDGE.XNAS"))
    request = request_for(
        RESEARCH, source=PAIR, hypothesis_=hyp, overrides={"ex_ns": midnight_ns(EX), "exits": exits}
    )
    points = sorted(
        [*published("DEMO", 10.0, 10.0, 1), *published("HEDGE", 20.0, 200.0, 2)],
        key=lambda point: point.ts_init,
    )
    held = [instrument("DEMO"), instrument("HEDGE", info=REVERSE)]

    run = execute(request, held, [tuple(points)]).run

    on_the_ex_date = [fill for fill in run.fills if fill.ts_ns >= midnight_ns(EX)]
    assert [(fill.side, fill.px) for fill in on_the_ex_date][:1] == [
        ("SELL" if exits else "BUY", 200.0)
    ]


def test_a_split_dated_before_the_window_changes_nothing_in_it(request_for) -> None:
    """The venue applies every due split at the first point it matches, an old one included.
    With nothing held and no book yet to restate, the window runs as if there were none."""
    long_ago = {"splits": [{"ex_date": "2023-06-01", "ratio": 0.1}]}
    flat_series = restated(before=10.0, after=10.0)

    old = card(request_for, scheduled=long_ago, points=flat_series)
    none = card(request_for, scheduled=None, points=flat_series)

    assert old.fills and old.equity == none.equity
