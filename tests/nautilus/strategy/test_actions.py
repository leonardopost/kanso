"""A venue at an ex-date: cancel, adjust, resync — and what it deliberately does not repair.

`kanso.nautilus.actions` loads into the exchange, so every test here runs the real engine
over a real sleeve: what is under test is a binding to the engine — that `apply_adjustment`
changes a live position without touching its fills, that a resting order is gone *before*
the ex-date's bar is matched rather than after, that the portfolio index follows. A double
would agree with any of those.

The series is four sessions either side of the ex-date, priced ten before and a hundred
after: a one-for-ten reverse split as the tape carries it.
"""

from __future__ import annotations

from datetime import date

import pytest
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Quantity

from kanso.criteria.run import midnight_ns
from kanso.nautilus.strategy import KansoConfig, KansoStrategy

from .conftest import DEMO, HEDGE, VENUE, bar, equity

EX = date(2024, 1, 6)
DAY_NS = 86_400_000_000_000
SCHEDULE = {"splits": [{"ex_date": EX.isoformat(), "ratio": 0.1}]}
FORWARD = {"splits": [{"ex_date": EX.isoformat(), "ratio": 4.0}]}


def series(before: float = 10.0, after: float = 100.0, n: int = 8) -> list[object]:
    """Four sessions either side of the ex-date, priced as a one-for-ten reverse split."""
    made: list[object] = []
    for index in range(n):
        day = midnight_ns(EX) + (index - n // 2) * DAY_NS + 3_600_000_000_000
        close = before if index < n // 2 else after
        made.append(_dated(bar(DEMO, 0, close), day))
    return made


def _dated(point: object, ts_event: int) -> object:
    """The same bar, stamped at a day of its own and published a second later."""
    from nautilus_trader.model.data import Bar

    return Bar(
        point.bar_type,  # type: ignore[attr-defined]
        point.open,  # type: ignore[attr-defined]
        point.high,  # type: ignore[attr-defined]
        point.low,  # type: ignore[attr-defined]
        point.close,  # type: ignore[attr-defined]
        Quantity.from_int(10_000),
        ts_event=ts_event,
        ts_init=ts_event + 1_000_000_000,
    )


class Holder(KansoStrategy):
    """Buys once and holds, so what changes the position is the action and nothing else.

    `exit_price` rests a take-profit at that price on the second session; `exit_on` closes
    at market on that session. Both are the ordinary shapes of an exit.
    """

    config_cls = KansoConfig

    def __init__(
        self,
        config: KansoConfig | None = None,
        exit_price: float | None = None,
        exit_on: int = 0,
    ) -> None:
        super().__init__(config)
        self.exit_price = exit_price
        self.exit_on = exit_on
        self.seen: list[tuple[int, float]] = []

    def on_bar(self, bar: object) -> None:
        instrument_id = bar.bar_type.instrument_id  # type: ignore[attr-defined]
        count = len(self.seen)
        if count == 0:
            self.submit_entry(instrument_id, "BUY", notional=10_050.0)
        elif count == 1 and self.exit_price is not None:
            self.submit_exit(instrument_id, price=self.exit_price)
        elif count == self.exit_on:
            self.submit_exit(instrument_id)
        self.seen.append((self.data_time, float(self.portfolio.net_position(DEMO))))


def config(**changes: object) -> KansoConfig:
    fields: dict[str, object] = {
        "hyp_id": "split",
        "universe": (DEMO.value,),
        "resolution": "1m",
        "capital": 100_000.0,
        "max_position_pct": 20.0,
    }
    return KansoConfig(**{**fields, **changes})  # type: ignore[arg-type]


def held(run: object) -> object:
    """The one position the sleeve opened."""
    return run.engine.cache.positions()[0]  # type: ignore[attr-defined]


# --- the schedule the venue reads ---------------------------------------------


def test_a_venue_takes_its_schedules_off_the_instruments_it_holds(backtest) -> None:
    """Read from the exchange's own instrument map, in ex-date order, with the ratio intact."""
    run = backtest(Holder(config()), instruments=[equity(DEMO, info=FORWARD)], data=series(n=4))
    module = run.actions[0]

    assert module._applied == {(DEMO.value, EX)}
    assert [(name, split.ex_date, split.ratio) for _ns, name, split in _pending(module)] == [
        (DEMO.value, EX, 4.0)
    ]


def test_an_instrument_that_arrives_later_brings_its_schedule_with_it(backtest) -> None:
    """The pending list is rebuilt when the venue's instrument map grows, and only then."""
    run = backtest(Holder(config()), instruments=[equity(DEMO)], data=series())
    module = run.actions[0]
    assert module._due == []

    module.exchange.instruments[HEDGE] = equity(HEDGE, info=SCHEDULE)
    module._refresh()

    assert [name for _ns, name, _split in module._due] == [HEDGE.value]


def test_what_has_already_been_applied_is_not_scheduled_again(backtest) -> None:
    """A rebuild after an ex-date must not put a split back through a position twice."""
    run = backtest(Holder(config()), instruments=[equity(DEMO, info=SCHEDULE)], data=series())
    module = run.actions[0]

    module.exchange.instruments[HEDGE] = equity(HEDGE)
    module._refresh()

    assert module._due == []
    assert [float(event.quantity_change) for event in held(run).adjustments] == [-905.0]


def test_a_reset_venue_re_reads_its_schedules(backtest) -> None:
    run = backtest(Holder(config()), instruments=[equity(DEMO, info=SCHEDULE)], data=series())
    module = run.actions[0]

    module.reset()
    module._refresh()

    assert [name for _ns, name, _split in module._due] == [DEMO.value]


def test_a_venue_advanced_to_an_instant_with_no_point_applies_nothing(backtest) -> None:
    """`process` is the clock tick, and a clock tick has matched nothing."""
    run = backtest(Holder(config()), instruments=[equity(DEMO)], data=series())
    module = run.actions[0]
    module.exchange.instruments[HEDGE] = equity(HEDGE, info=SCHEDULE)
    module._refresh()

    module.process(midnight_ns(EX) + DAY_NS)
    module.log_diagnostics(None)

    assert [name for _ns, name, _split in module._due] == [HEDGE.value]


def _pending(module: object) -> list[tuple[int, str, object]]:
    """What the module would apply next, re-read from the venue after a run."""
    module.reset()  # type: ignore[attr-defined]
    module._refresh()  # type: ignore[attr-defined]
    return module._due  # type: ignore[attr-defined]


# --- what the ex-date does ----------------------------------------------------


def test_a_reverse_split_leaves_the_shares_the_ratio_leaves(backtest) -> None:
    """1,005 shares at ten become 100 at a hundred, with no order and no fill."""
    position = held(
        backtest(Holder(config()), instruments=[equity(DEMO, info=SCHEDULE)], data=series())
    )

    assert float(position.signed_qty) == 100.0
    assert [float(event.last_qty) for event in position.events] == [1_005.0]
    assert [float(event.quantity_change) for event in position.adjustments] == [-905.0]


def test_a_forward_split_multiplies_the_shares_the_same_way(backtest) -> None:
    run = backtest(
        Holder(config()),
        instruments=[equity(DEMO, info=FORWARD)],
        data=series(before=40.0, after=10.0),
    )

    assert float(held(run).signed_qty) == 1_004.0


def test_the_portfolio_follows_the_adjustment_rather_than_the_fills(backtest) -> None:
    """Without the resync the sleeve would size its next exit against 1,005 it no longer holds."""
    run = backtest(Holder(config()), instruments=[equity(DEMO, info=SCHEDULE)], data=series())

    counts = [count for _ts, count in run.strategy.seen]
    assert counts[:4] == [0.0, 1_005.0, 1_005.0, 1_005.0]
    assert counts[4:] == [100.0, 100.0, 100.0, 100.0]


def test_the_adjustment_is_stamped_at_the_reference_time_that_triggered_it(backtest) -> None:
    run = backtest(Holder(config()), instruments=[equity(DEMO, info=SCHEDULE)], data=series())

    assert int(held(run).adjustments[0].ts_event) == run.strategy.seen[4][0]
    assert held(run).adjustments[0].reason == "split"


def test_a_split_costs_the_position_nothing(backtest) -> None:
    """No order, no fill, no commission: the fills are the ones the sleeve actually sent."""
    run = backtest(Holder(config()), instruments=[equity(DEMO, info=SCHEDULE)], data=series())

    assert [float(money) for money in held(run).commissions()] == [0.0]
    assert len(run.strategy.intents) == 1


def test_an_exit_after_the_split_is_sized_against_the_shares_it_left(backtest) -> None:
    """The resync's whole purpose, seen from the sleeve: 100 sold, not 1,005."""
    run = backtest(
        Holder(config(), exit_on=6), instruments=[equity(DEMO, info=SCHEDULE)], data=series()
    )

    assert [(intent.side, intent.qty) for intent in run.strategy.intents] == [
        ("BUY", 1_005.0),
        ("SELL", 100.0),
    ]


# --- when the ex-date begins --------------------------------------------------

WINTER_EX = date(2024, 1, 17)
"""A Wednesday under EST, when New York midnight is 05:00Z and 19:00 New York is 00:00Z."""
EVE = date(2024, 1, 16)
NEW_YORK_SCHEDULE = {
    "splits": [{"ex_date": WINTER_EX.isoformat(), "ratio": 0.1}],
    "timezone": "America/New_York",
}
UTC_SCHEDULE = {"splits": [{"ex_date": WINTER_EX.isoformat(), "ratio": 0.1}]}


def new_york_ns(day: date, hour: int, minute: int = 0) -> int:
    """`hour:minute` New York time on `day`, in nanoseconds since the epoch."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    opened = datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo("America/New_York")
    )
    return int(opened.timestamp()) * 1_000_000_000


def tape(*prints: tuple[int, float]) -> list[object]:
    """One bar per `(ts_event, close)`, in the order given."""
    return [_dated(bar(DEMO, 0, close), ts_event) for ts_event, close in prints]


EVENING = (
    (new_york_ns(EVE, 18, 30), 10.0),
    (new_york_ns(EVE, 18, 45), 10.0),
    (new_york_ns(EVE, 19, 30), 10.0),
    (new_york_ns(EVE, 19, 59), 10.0),
)
"""Bought at 18:30 and sold at 19:59 New York on the eve of a winter ex-date: the old share
count's session, although everything after 19:00 New York is already the ex-date in UTC."""


def test_an_intraday_position_the_evening_before_the_ex_date_is_not_adjusted(backtest) -> None:
    run = backtest(
        Holder(config(), exit_on=3),
        instruments=[equity(DEMO, info=NEW_YORK_SCHEDULE)],
        data=tape(*EVENING),
    )

    assert list(held(run).adjustments) == []
    assert [(intent.side, intent.qty) for intent in run.strategy.intents] == [
        ("BUY", 1_005.0),
        ("SELL", 1_005.0),
    ]


def test_dated_in_utc_the_same_evening_is_already_the_ex_date(backtest) -> None:
    """What an instrument naming no zone gets, and why a US listing should name one: the
    split lands at 19:00 New York and rescales a position the evening's tape never split."""
    run = backtest(
        Holder(config(), exit_on=3),
        instruments=[equity(DEMO, info=UTC_SCHEDULE)],
        data=tape(*EVENING),
    )

    assert int(held(run).adjustments[0].ts_event) == new_york_ns(EVE, 19, 30)
    assert run.strategy.intents[-1].qty == 100.0


def test_a_position_held_overnight_is_adjusted_once_at_the_first_point_of_the_ex_date(
    backtest,
) -> None:
    run = backtest(
        Holder(config()),
        instruments=[equity(DEMO, info=NEW_YORK_SCHEDULE)],
        data=tape(
            (new_york_ns(EVE, 19, 30), 10.0),
            (new_york_ns(EVE, 19, 59), 10.0),
            (new_york_ns(WINTER_EX, 4, 1), 100.0),
            (new_york_ns(WINTER_EX, 9, 31), 100.0),
        ),
    )
    position = held(run)

    assert [float(event.quantity_change) for event in position.adjustments] == [-905.0]
    assert int(position.adjustments[0].ts_event) == new_york_ns(WINTER_EX, 4, 1)
    assert [count for _ts, count in run.strategy.seen] == [0.0, 1_005.0, 100.0, 100.0]


def test_a_bar_stamped_at_the_ex_date_s_midnight_is_matched_in_the_old_count(backtest) -> None:
    """A daily bar is stamped at its close, the midnight after its session, so the bar
    stamped at the ex-date's New York midnight is the eve's, priced in the old share count,
    and the split holds from the bar after it."""
    run = backtest(
        Holder(config()),
        instruments=[equity(DEMO, info=NEW_YORK_SCHEDULE)],
        data=tape(
            (new_york_ns(EVE, 0), 10.0),
            (new_york_ns(WINTER_EX, 0), 10.0),
            (new_york_ns(date(2024, 1, 18), 0), 100.0),
            (new_york_ns(date(2024, 1, 19), 0), 100.0),
        ),
    )

    assert [count for _ts, count in run.strategy.seen] == [0.0, 1_005.0, 100.0, 100.0]
    assert int(held(run).adjustments[0].ts_event) == new_york_ns(date(2024, 1, 18), 0)


# --- what a sleeve may know of a split ----------------------------------------


class Watcher(Holder):
    """A holder that, at every bar, writes down every split anything it holds names."""

    def on_bar(self, bar: object) -> None:
        from kanso.nautilus.splits import Split

        found: list[int] = []
        stack: list[object] = list(vars(self).values())
        while stack:
            item = stack.pop()
            if isinstance(item, Split):
                found.append(item.effective_ns)
            elif isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, list | tuple | set | frozenset):
                stack.extend(item)
        self.named.append((int(bar.ts_event), found))  # type: ignore[attr-defined]
        super().on_bar(bar)


def test_a_sleeve_never_holds_a_split_that_has_not_happened(backtest) -> None:
    """Anything the strategy base holds a researched `strategy.py` can read, so a schedule
    cached there would hand it every split of the instrument's life, the certification
    window's among them. The venue announces a split as it applies it, and that is all a
    sleeve ever holds."""
    watcher = Watcher(config(), exit_on=6)
    watcher.named = []  # type: ignore[attr-defined]
    run = backtest(watcher, instruments=[equity(DEMO, info=SCHEDULE)], data=series())

    assert run.strategy.named
    assert all(instant <= at for at, found in run.strategy.named for instant in found)
    assert any(found for _at, found in run.strategy.named)


def test_the_sleeve_books_the_payment_the_venue_announces(backtest) -> None:
    """The announcement carries the adjustment, and the sleeve folds its payment into cash
    once: 1,005 bought at ten, 100 kept and fifty paid, so the balance holds the fifty."""
    run = backtest(Holder(config()), instruments=[equity(DEMO, info=SCHEDULE)], data=series())
    strategy = run.strategy

    assert strategy._cash == pytest.approx(100_000.0 - 10_050.0 - strategy_costs(run) + 50.0)


def strategy_costs(run: object) -> float:
    """What the runner's cost arithmetic charged the one fill, as the harness booked it."""
    booked = run.strategy  # type: ignore[attr-defined]
    event = held(run).events[0]
    return booked._paid(event) - float(event.last_qty) * float(event.last_px)


# --- the order that must not survive the action -------------------------------


def test_a_take_profit_resting_above_the_market_is_cancelled_before_the_ex_date_is_matched(
    backtest,
) -> None:
    """The measurement this module exists for.

    A sleeve buys 1,005 at ten and rests a take-profit at fifty. The one-for-ten reverse
    split restates the tape to a hundred, so on the ex-date bar that order is marketable
    for 1,005 shares at fifty — a 40,169.85 profit on a bookkeeping change. It is the most
    ordinary exit there is, and it is cancelled by the venue one call before the bar it
    would have been matched against.
    """
    run = backtest(
        Holder(config(), exit_price=50.0),
        instruments=[equity(DEMO, info=SCHEDULE)],
        data=series(),
    )

    resting = [order for order in run.engine.cache.orders() if order.side == OrderSide.SELL]
    assert [order.is_canceled for order in resting] == [True]
    assert [float(event.last_qty) for event in held(run).events] == [1_005.0]
    assert float(held(run).signed_qty) == 100.0


def test_a_venue_with_nothing_resting_sends_no_cancel(backtest) -> None:
    """Only the entry was ever submitted, and it was filled long before the ex-date."""
    run = backtest(Holder(config()), instruments=[equity(DEMO, info=SCHEDULE)], data=series())

    assert [order.is_canceled for order in run.engine.cache.orders()] == [False]


def test_a_position_under_one_new_lot_is_paid_out_whole_and_left_flat(backtest) -> None:
    """What an issuer does with a holding a reverse split leaves under one share, and what
    used to stop the card: the shares are paid at the last price in the old count, and the
    position is flat and indexed as closed, so the next entry opens a fresh one."""
    run = backtest(
        Holder(config(capital=50.0, max_position_pct=100.0)),
        instruments=[equity(DEMO, info=SCHEDULE)],
        data=series(),
    )
    position = held(run)
    bought = float(position.events[0].last_qty)

    assert 0.0 < bought < 10.0
    assert position.is_closed
    assert [float(event.quantity_change) for event in position.adjustments] == [-bought]
    assert [event.pnl_change.as_double() for event in position.adjustments] == [bought * 10.0]
    assert run.engine.cache.positions_open() == []


class Book:
    """An L1 book as `last_price` reads it: a best bid and a best ask, or none."""

    def __init__(self, bid: float | None, ask: float | None) -> None:
        self.bid, self.ask = bid, ask

    def best_bid_price(self) -> float | None:
        return self.bid

    def best_ask_price(self) -> float | None:
        return self.ask


class Filled:
    last_px = 9.5


@pytest.mark.parametrize(
    ("bid", "ask", "price"), [(9.9, 10.1, 10.0), (None, 10.1, 9.5), (9.9, None, 9.5)]
)
def test_the_fraction_is_valued_at_the_book_s_midpoint_or_the_last_fill(
    bid: float | None, ask: float | None, price: float
) -> None:
    """The book still quotes the close before the ex-date when a split is applied; a book
    with no quote falls back to the last price the position itself traded at."""
    from kanso.nautilus.actions import last_price

    assert last_price(Book(bid, ask), Filled()) == pytest.approx(price)


def test_the_fraction_a_split_leaves_is_paid_at_the_close_before_the_ex_date(backtest) -> None:
    """1,005 at ten through one-for-ten: 100 shares held and five old shares paid at ten."""
    position = held(
        backtest(Holder(config()), instruments=[equity(DEMO, info=SCHEDULE)], data=series())
    )

    assert [event.pnl_change.as_double() for event in position.adjustments] == [50.0]


# --- what the venue does not repair, and why ----------------------------------


def test_the_account_is_unmoved_at_the_ex_date_and_wrong_from_the_closing_fill(
    backtest,
) -> None:
    """Measured on nautilus_trader 1.231.0, and the reason `criteria.integrity` denies it.

    Nothing moves the balance at the action — not the 50 the split pays in lieu either, which
    `apply_adjustment` adds to the position's own `realized_pnl` and to nothing else:
    `Portfolio.initialize_positions` recomputes
    maintenance margin alone, and kanso's resolved instruments carry a zero margin rate, so
    the account reads its 100,000 at every bar up to and including the ex-date. The 9,000
    arrives at the closing fill, where `MarginAccount.calculate_pnls` credits
    `(fill_px - avg_px_open) x qty` with `avg_px_open` still at the pre-split ten — a
    `cdef readonly` attribute `apply_adjustment` does not rescale. There is nothing to
    write back at the ex-date and nothing that can be repaired afterwards, so kanso reads
    none of it: the true realised profit on this position is -50, and the runner's
    extraction computes exactly that from the fills and this module's adjustment.
    """
    run = backtest(
        Holder(config(), exit_on=6), instruments=[equity(DEMO, info=SCHEDULE)], data=series()
    )
    account = run.engine.cache.account_for_venue(VENUE)
    position = held(run)

    assert float(account.balance_total(None)) == 109_000.0
    assert float(position.realized_pnl) == 9_050.0  # the engine adds the 50 paid in lieu
    assert (float(position.avg_px_open), float(position.peak_qty)) == (10.0, 1_005.0)
    assert run.engine.cache.positions()[0].is_closed
