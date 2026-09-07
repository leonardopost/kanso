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
from kanso.errors import PreconditionError
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


def test_a_position_a_split_would_wipe_out_is_refused_by_name(backtest) -> None:
    """kanso holds no cash to pay a fractional position out in lieu, and says so."""
    with pytest.raises(PreconditionError) as raised:
        backtest(
            Holder(config(capital=50.0, max_position_pct=100.0)),
            instruments=[equity(DEMO, info=SCHEDULE)],
            data=series(),
        )

    assert "less than one lot of it" in raised.value.message
    assert "max_position_pct" in (raised.value.remedy or "")


# --- what the venue does not repair, and why ----------------------------------


def test_the_account_is_unmoved_at_the_ex_date_and_wrong_from_the_closing_fill(
    backtest,
) -> None:
    """Measured on nautilus_trader 1.231.0, and the reason `criteria.integrity` denies it.

    Nothing moves the balance at the action: `Portfolio.initialize_positions` recomputes
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
    assert float(position.realized_pnl) == 9_000.0
    assert (float(position.avg_px_open), float(position.peak_qty)) == (10.0, 1_005.0)
    assert run.engine.cache.positions()[0].is_closed
