"""A quote or a print published after a later one, and the venue that now applies it.

The engine's top-of-book venue ignored a point stamped before its book's last update while
the sleeve was handed it (`kanso.nautilus.facts`). `kanso.nautilus.availability` empties
that book first, so every test here runs a real card through the runner, with kanso's own
venue model, and reads what the venue filled and what the sleeve was handed. Instants are
milliseconds after 15:00 UTC on the second session of the research window.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, OrderBookDeltas, QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    AccountType,
    AggressorSide,
    BookAction,
    BookType,
    OmsType,
    OrderSide,
)
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Money, Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.data.loaders.points import make_delta
from kanso.nautilus import actions
from kanso.nautilus.availability import Availability, admit
from kanso.nautilus.backtest import RunResult, execute

from .conftest import INSTRUMENT, RESEARCH, SECOND_NS, hypothesis, instrument, second_bar_type

T0 = midnight_ns(date(2024, 1, 2)) + 15 * 3_600 * SECOND_NS
MS = 1_000_000

COSTS = {"commission_bps": 1.0, "slippage_bps": 2.0, "spread": "fixed_bps", "fixed_bps": 4.0}

SEEN: list[tuple[str, int, int, int]] = []
"""Every quote, print and bar `WATCHER` was handed: its kind, its two instants and the
`data_time` the sleeve read in its handler, all in milliseconds after `T0`."""

WATCHER = b"""
import sys

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    qty: int = 100
    limit: float = 0.0
    on: int = 1
    exits: bool = False


class Strategy(KansoStrategy):
    \"\"\"Buys `qty` from the handler of the `on`th quote or print, at `limit` or at market
    when it is zero, sells what it holds at market from the first handler that finds it
    holding when `exits` is set, and writes down every point it is handed.\"\"\"

    config_cls = Config

    def on_start(self):
        self.seen = 0
        self.exited = False

    def _see(self, kind, point):
        module = sys.modules["tests.nautilus.backtest.test_availability"]
        module.SEEN.append(
            (
                kind,
                (int(point.ts_event) - module.T0) // module.MS,
                (int(point.ts_init) - module.T0) // module.MS,
                (self.data_time - module.T0) // module.MS,
            )
        )

    def _handle(self, kind, tick):
        self._see(kind, tick)
        self.seen += 1
        if self.seen == self.kanso_config.on:
            self.submit_entry(
                tick.instrument_id,
                "BUY",
                qty=self.kanso_config.qty,
                price=self.kanso_config.limit or None,
            )
        elif self.kanso_config.exits and not self.exited and self.held(tick.instrument_id) > 0:
            self.exited = True
            self.submit_exit(tick.instrument_id)

    def on_quote_tick(self, tick):
        self._handle("quote", tick)

    def on_trade_tick(self, tick):
        self._handle("print", tick)

    def on_bar(self, bar):
        self._see("bar", bar)
"""


def _id() -> InstrumentId:
    return InstrumentId.from_str(INSTRUMENT)


def quote(
    bid: float,
    ask: float,
    event_ms: float,
    init_ms: float,
    bid_size: int = 1_000,
    ask_size: int = 1_000,
) -> QuoteTick:
    """A quote stamped `event_ms` by the participant and published at `init_ms`."""
    return QuoteTick(
        _id(),
        Price(bid, 2),
        Price(ask, 2),
        Quantity.from_int(bid_size),
        Quantity.from_int(ask_size),
        T0 + int(event_ms * MS),
        T0 + int(init_ms * MS),
    )


def trade(px: float, size: int, event_ms: float, init_ms: float, number: int) -> TradeTick:
    """A print with no aggressor, stamped and published the same way."""
    return TradeTick(
        _id(),
        Price(px, 2),
        Quantity.from_int(size),
        AggressorSide.NO_AGGRESSOR,
        TradeId(f"T{number}"),
        T0 + int(event_ms * MS),
        T0 + int(init_ms * MS),
    )


def card(
    request_for,
    points: list[object],
    *,
    qty: int = 100,
    limit: float = 9.9,
    on: int = 1,
    exits: bool = False,
    rule: str = "touch",
    requirements: tuple[str, ...] = ("quote", "trade"),
    resolution: str = "tick",
) -> RunResult:
    """One card of `WATCHER` over these points, under a venue whose `limit_fill` is `rule`."""
    hyp = hypothesis(
        costs={**COSTS, "limit_fill": rule},
        data_requirements=requirements,
        resolution=resolution,
    )
    request = request_for(
        RESEARCH,
        source=WATCHER,
        hypothesis_=hyp,
        overrides={"qty": qty, "limit": limit, "on": on, "exits": exits},
    )
    groups: list[tuple[object, ...]] = []
    for kind in (Bar, QuoteTick, TradeTick):
        group = tuple(point for point in points if isinstance(point, kind))
        if group:
            groups.append(group)
    SEEN.clear()
    result = execute(request, [instrument()], groups)
    assert not result.crashed, result.traceback_tail
    return result


def fills_of(result: RunResult) -> list[tuple[int, float, float, bool]]:
    """Each fill as its instant in milliseconds after `T0`, its quantity, price and maker flag."""
    return [((fill.ts_ns - T0) // MS, fill.qty, fill.px, fill.maker) for fill in result.run.fills]


def without_the_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """The venue as it was before `Availability`: the corporate actions alone."""
    loaded = actions.modules
    monkeypatch.setattr(actions, "modules", lambda venue: loaded(venue)[:1])


# --- what the venue fills -------------------------------------------------------

THROUGH_QUOTE = [quote(9.99, 10.01, 10, 10), quote(9.95, 10.0, 20, 20), quote(9.8, 9.85, 15, 30)]
"""A buy of 100 rests at 9.90 from the first quote; at 30 ms a quote whose ask of 9.85 goes
through it is published, stamped at 15 ms — before the venue's last update at 20 ms."""


@pytest.mark.parametrize("rule", ["touch", "through"])
def test_a_quote_published_after_a_later_one_moves_the_venue(request_for, rule: str) -> None:
    """The venue skipped the late quote and the buy never filled; now the quote is applied
    and the buy fills at its price as a maker, under either rule, at the quote's `ts_init`."""
    assert fills_of(card(request_for, THROUGH_QUOTE, rule=rule)) == [(30, 100.0, 9.9, True)]


def test_without_the_module_the_late_quote_never_reached_the_venue(
    request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the module is for, measured on the same card."""
    without_the_module(monkeypatch)

    assert fills_of(card(request_for, THROUGH_QUOTE)) == []


AT_THE_PRICE = [
    quote(9.99, 10.01, 10, 10),
    quote(9.95, 10.0, 20, 20),
    *[trade(9.9, 100, 15 + 10 * k, 30 + 10 * k, k) for k in range(4)],
]
"""A buy of 320 at 9.90, then four prints of 100 at its price, each stamped 15 ms before it
is published and so the first before the venue's last update."""


def test_a_print_published_after_a_later_one_fills_by_its_size(request_for) -> None:
    """The venue skipped the first print and filled 100, 100 and 100 on the other three; now
    each print fills by its own size, the last the 20 left."""
    filled = fills_of(card(request_for, AT_THE_PRICE, qty=320))

    assert filled == [
        (30, 100.0, 9.9, True),
        (40, 100.0, 9.9, True),
        (50, 100.0, 9.9, True),
        (60, 20.0, 9.9, True),
    ]


def test_a_market_order_fills_against_the_quote_the_sleeve_was_shown(request_for) -> None:
    """The market moves up a dollar on two quotes published after the one at a second and
    stamped before it; the sleeve buys at market on the first. The venue used to hold the
    book of a second before and fill at 100.10; it fills at the ask the sleeve was shown."""
    points = [
        quote(100.0, 100.1, 0, 0, 100, 100),
        quote(100.0, 100.1, 1_000, 1_000, 100, 100),
        quote(101.0, 101.1, 990, 1_005, 100, 100),
        quote(101.0, 101.1, 995, 1_050, 100, 100),
        quote(101.0, 101.1, 2_000, 2_000, 100, 100),
    ]

    result = card(request_for, points, limit=0.0, on=3, requirements=("quote",))

    assert fills_of(result) == [(1_005, 100.0, 101.1, False)]


def second_bar(close: float, event_ms: float, init_ms: float) -> Bar:
    """A one-second bar flat at `close`, stamped and published at the instants given."""
    price = Price(close, 2)
    return Bar(
        second_bar_type(),
        price,
        price,
        price,
        price,
        Quantity.from_int(400),
        ts_event=T0 + int(event_ms * MS),
        ts_init=T0 + int(init_ms * MS),
    )


def test_a_quote_published_inside_a_bar_it_follows_moves_the_venue(request_for) -> None:
    """The venue walks a bar into its book stamped at the bar's `ts_init`, a second in, so a
    quote published half a millisecond later and stamped a millisecond before it was skipped,
    and the buy resting at 99.95 never filled on its ask of 99.90. It fills now."""
    points = [
        quote(100.0, 100.1, 0, 0, 100, 100),
        quote(100.0, 100.1, 100, 100, 100, 100),
        second_bar(100.05, 0, 1_000),
        quote(99.8, 99.9, 999, 1_000.5, 100, 100),
        quote(100.0, 100.1, 2_000, 2_000, 100, 100),
    ]

    result = card(request_for, points, limit=99.95, requirements=("bar", "quote"), resolution="1s")

    assert [(qty, px, maker) for _ms, qty, px, maker in fills_of(result)] == [(100.0, 99.95, True)]
    assert result.run.fills[0].ts_ns == T0 + 1_000 * MS + MS // 2


# --- what the sleeve is handed ----------------------------------------------------


def test_the_sleeve_is_handed_what_it_was_handed_before(
    request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The module copies and re-stamps nothing: the sleeve is handed the same points in the
    same order, each with its own `ts_event` as `data_time`, with the module and without it.
    This sleeve sends one order whatever it is filled, so it sends the same one either way;
    what the venue fills moves, and with it what a sleeve sends because of a fill (below)."""
    with_it = card(request_for, AT_THE_PRICE, qty=320)
    handed = list(SEEN)
    without_the_module(monkeypatch)
    without = card(request_for, AT_THE_PRICE, qty=320)

    assert list(SEEN) == handed
    assert [event for _kind, event, _init, _data_time in handed] == [10, 20, 15, 25, 35, 45]
    assert all(data_time == event for _kind, event, _init, data_time in handed)
    assert with_it.intents == without.intents
    assert len(with_it.run.fills) == 4
    assert len(without.run.fills) == 3


def test_a_sleeve_that_acts_on_its_fill_sends_what_the_fill_leads_to(
    request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sleeve that sells once it holds: the late quote now fills its buy, so it sells from
    that quote's handler, at the quote's own `ts_event`; without the module the buy waited for
    the next quote through it, and so did the sale. No point moved, and the order did."""
    points = [*THROUGH_QUOTE, quote(9.8, 9.85, 60, 60)]

    def sent(result: RunResult) -> list[tuple[int, str, str]]:
        return [((row[0] - T0) // MS, row[2], row[4]) for row in result.intents]

    with_it = card(request_for, points, exits=True)
    without_the_module(monkeypatch)
    without = card(request_for, points, exits=True)

    assert sent(with_it) == [(10, "BUY", "LIMIT"), (15, "SELL", "MARKET")]
    assert sent(without) == [(10, "BUY", "LIMIT"), (60, "SELL", "MARKET")]


# --- what the module leaves alone -------------------------------------------------


def _engine(book_type: BookType, points: list[object]) -> tuple[BacktestEngine, Any]:
    """A venue loading kanso's modules over these points with nothing trading, and the
    `Availability` it loaded, which holds the exchange."""
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    loaded = actions.modules("XNAS")
    engine.add_venue(
        venue=instrument().id.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(1_000_000, USD)],
        book_type=book_type,
        modules=loaded,
    )
    engine.add_instrument(instrument())
    engine.add_data(points)
    engine.run()
    return engine, loaded[1]


def test_a_level_two_book_keeps_its_depth() -> None:
    """A level-two book filters nothing by `ts_event` and holds depth a reset would delete,
    so a print stamped before its last change leaves its three bids where they were, and
    `admit` asked about an instant earlier than its last update does nothing to it."""
    at = T0 + 10 * MS
    levels = [
        make_delta(_id(), BookAction.ADD, OrderSide.BUY, price, 100, price, 2, 0, at, at)
        for price in (999, 998, 997)
    ]
    offer = make_delta(_id(), BookAction.ADD, OrderSide.SELL, 1_001, 100, 1_001, 2, 0, at, at)
    changes = OrderBookDeltas(_id(), [*levels, offer])
    engine, module = _engine(BookType.L2_MBP, [changes, trade(10.0, 50, 5, 20, 1)])
    try:
        matching = module.exchange.get_matching_engine(_id())
        book = matching.get_book()
        depth = [float(level.price) for level in book.bids()]

        admit(matching, 0)

        assert matching.book_type == BookType.L2_MBP
        assert depth == [9.99, 9.98, 9.97]
        assert [float(level.price) for level in book.bids()] == depth
        assert book.ts_last == at
    finally:
        engine.dispose()


def test_admit_leaves_a_book_it_would_apply_alone() -> None:
    """A top-of-book book is emptied only for an instant earlier than its last update — the
    point the engine would skip — and never for one at or after it, which the engine applies
    over the book as it stands. An instrument the venue has built no engine for is left to the
    venue, and the module's three other calls change nothing."""
    engine, module = _engine(BookType.L1_MBP, [quote(9.99, 10.01, 10, 10)])
    try:
        matching = module.exchange.get_matching_engine(_id())
        book = matching.get_book()
        held = (book.best_bid_price(), book.update_count, book.ts_last)

        admit(None, 0)
        admit(matching, book.ts_last)
        admit(matching, book.ts_last + 1)
        module.process(book.ts_last)
        module.log_diagnostics(None)
        module.reset()
        assert isinstance(module, Availability)
        assert (book.best_bid_price(), book.update_count, book.ts_last) == held

        admit(matching, book.ts_last - 1)

        assert (book.best_bid_price(), book.best_ask_price()) == (None, None)
        assert (book.update_count, book.ts_last) == (0, 0)
    finally:
        engine.dispose()
