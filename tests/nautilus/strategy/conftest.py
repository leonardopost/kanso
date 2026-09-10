"""A real in-process backtest engine on synthetic bars.

Every test in this package runs the engine rather than a stand-in, because what is under
test is a binding to it: which handler the engine calls, which clock it hands out, whether
an override is dispatched at all. A mock would agree with any of those.

The venue is built as `kanso.nautilus.backtest` builds one, corporate-action module
included, so what a sleeve trades against here is what it trades against in a card.

The series is a deterministic saw-tooth on one instrument plus a flat hedge instrument,
seeded by nothing at all: the same bars every run, on every host.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import pytest
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    AccountType,
    AggregationSource,
    AggressorSide,
    BarAggregation,
    OmsType,
    PriceType,
)
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Price, Quantity

from kanso.nautilus import actions

VENUE = Venue("XNAS")
DEMO = InstrumentId(Symbol("DEMO"), VENUE)
HEDGE = InstrumentId(Symbol("HEDGE"), VENUE)
MINUTE_NS = 60_000_000_000
SECOND_NS = 1_000_000_000
LATENCY_NS = 1_000
"""Every bar is published a microsecond after it closes, so ts_init > ts_event."""


def equity(instrument_id: InstrumentId, **extra: object) -> Equity:
    """One US equity; `extra` is whatever else the class accepts, such as `info`."""
    return Equity(
        instrument_id=instrument_id,
        raw_symbol=instrument_id.symbol,
        currency=USD,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
        **extra,
    )


def bar_type(instrument_id: InstrumentId) -> BarType:
    return BarType(
        instrument_id,
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )


def second_bar_type(instrument_id: InstrumentId) -> BarType:
    return BarType(
        instrument_id,
        BarSpecification(1, BarAggregation.SECOND, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )


def bar(instrument_id: InstrumentId, index: int, close: float) -> Bar:
    ts_event = (index + 1) * MINUTE_NS
    price = Price(close, 2)
    return Bar(
        bar_type(instrument_id),
        price,
        Price(close + 0.5, 2),
        Price(close - 0.5, 2),
        price,
        Quantity.from_int(1_000),
        ts_event=ts_event,
        ts_init=ts_event + LATENCY_NS,
    )


def second_bar(
    instrument_id: InstrumentId,
    index: int,
    close: float,
    *,
    origin_ns: int = 0,
    ts_init: int | None = None,
) -> Bar:
    ts_event = origin_ns + (index + 1) * SECOND_NS
    published = ts_event + LATENCY_NS if ts_init is None else ts_init
    price = Price(close, 2)
    return Bar(
        second_bar_type(instrument_id),
        price,
        Price(close + 0.5, 2),
        Price(close - 0.5, 2),
        price,
        Quantity.from_int(1_000),
        ts_event=ts_event,
        ts_init=published,
    )


def saw_tooth(instrument_id: InstrumentId, n: int = 20) -> list[Bar]:
    """A deterministic series: 10.00, 10.50, 11.00, 10.50, 10.00, ..."""
    return [bar(instrument_id, i, 10.0 + 0.5 * min(i % 8, 8 - i % 8)) for i in range(n)]


def flat(instrument_id: InstrumentId, n: int = 20, close: float = 10.0) -> list[Bar]:
    """A series that never moves, so a sizing assertion is not a price forecast."""
    return [bar(instrument_id, i, close) for i in range(n)]


def quote(instrument_id: InstrumentId, index: int, mid: float = 10.0) -> QuoteTick:
    ts_event = (index + 1) * MINUTE_NS
    return QuoteTick(
        instrument_id,
        Price(mid - 0.01, 2),
        Price(mid + 0.01, 2),
        Quantity.from_int(100),
        Quantity.from_int(100),
        ts_event=ts_event,
        ts_init=ts_event + LATENCY_NS,
    )


def trade(instrument_id: InstrumentId, index: int, px: float = 10.0) -> TradeTick:
    ts_event = (index + 1) * MINUTE_NS
    return TradeTick(
        instrument_id,
        Price(px, 2),
        Quantity.from_int(100),
        AggressorSide.BUYER,
        TradeId(f"T-{index}"),
        ts_event=ts_event,
        ts_init=ts_event + LATENCY_NS,
    )


def every_grain(instrument_id: InstrumentId, n: int = 6) -> list[object]:
    """Bars, quotes and trades for one instrument, so all three handlers run."""
    points: list[object] = []
    for i in range(n):
        points.append(bar(instrument_id, i, 10.0 + 0.5 * (i % 3)))
        points.append(quote(instrument_id, i, 10.0 + 0.5 * (i % 3)))
        points.append(trade(instrument_id, i, 10.0 + 0.5 * (i % 3)))
    return points


@dataclass
class Run:
    """One completed backtest and the components that ran in it."""

    engine: BacktestEngine
    strategy: object
    modifiers: tuple[object, ...]
    actions: tuple[object, ...]


@pytest.fixture
def backtest():
    """Run a strategy, with optional modifiers, over a synthetic series."""
    engines: list[BacktestEngine] = []

    def run(
        strategy: object,
        modifiers: Sequence[object] = (),
        data: Iterable[Bar] | None = None,
        instruments: Sequence[InstrumentId | Equity] = (DEMO,),
        capital: int = 100_000,
    ) -> Run:
        engine = BacktestEngine(
            config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)),
        )
        engines.append(engine)
        loaded = actions.modules(VENUE.value)
        engine.add_venue(
            venue=VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(capital, USD)],
            # The venue kanso builds, corporate-action module and all, so a test of a sleeve
            # is a test of the exchange the sleeve actually runs against.
            modules=loaded,
        )
        for named in instruments:
            engine.add_instrument(equity(named) if isinstance(named, InstrumentId) else named)
        engine.add_data(list(saw_tooth(DEMO) if data is None else data))
        for modifier in modifiers:
            engine.add_actor(modifier)
        engine.add_strategy(strategy)
        engine.run()
        return Run(
            engine=engine,
            strategy=strategy,
            modifiers=tuple(modifiers),
            actions=tuple(loaded),
        )

    yield run
    for engine in engines:
        engine.dispose()
