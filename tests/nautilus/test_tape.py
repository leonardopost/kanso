"""`kanso.nautilus.tape` as a fill model asked directly: what a print credits, to whom, and once.

The venue-level behaviour — both code paths, every scenario measured — is in
`tests/replay/test_print_through.py`; here the model is handed points and asked about orders
the way the matching engine asks, so each answer reads off its arguments.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from nautilus_trader.backtest.config import SimulationModuleConfig
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    AggregationSource,
    AggressorSide,
    BarAggregation,
    LiquiditySide,
    OrderSide,
    PriceType,
)
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from kanso.nautilus.tape import PrintThrough, PrintThroughConfig, Tape, observe

COIN = InstrumentId.from_str("FRAC.XNAS")
INSTRUMENT = SimpleNamespace(id=COIN, size_precision=4, price_precision=2)


def model(size: str = "print") -> PrintThrough:
    return PrintThrough(config=PrintThroughConfig(size=size))


def order(name: str, leaves: str, price: float = 9.5, side: Any = OrderSide.BUY) -> Any:
    """A resting limit as the engine asks about it: a maker, with what is left of it."""
    return SimpleNamespace(
        client_order_id=ClientOrderId(name),
        liquidity_side=LiquiditySide.MAKER,
        side=side,
        price=Price(price, 2),
        leaves_qty=Quantity.from_str(leaves),
        has_price=True,
    )


def printed(price: float, size: str, side: Any = AggressorSide.NO_AGGRESSOR) -> TradeTick:
    return TradeTick(COIN, Price(price, 2), Quantity.from_str(size), side, TradeId("P"), 1, 1)


def filled(answer: Any) -> list[tuple[float, str]]:
    return [(float(px), str(qty)) for px, qty in answer.simulate_fills(None, 2, 4, False)]


def test_a_print_through_fills_by_its_exact_size_and_shares_it() -> None:
    """Quantities are counted in the engine's integers: a print of 0.3333 through a resting
    1.0000 credits exactly that, the next of 0.6667 the rest; one print shared by two orders
    credits the better-priced first and the other what is left."""
    one, two, three = order("O-1", "1.0000"), order("O-2", "0.5000", 9.51), order("O-3", "1.0")
    fills = model()
    fills.seen(printed(9.49, "0.3333"), frozenset({one.client_order_id}))
    first = filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, one, None, None))
    fills.seen(printed(9.48, "0.6667"), frozenset({one.client_order_id}))
    second = filled(
        fills.get_orderbook_for_fill_simulation(INSTRUMENT, order("O-1", "0.6667"), None, None)
    )
    fills.seen(printed(9.49, "0.8000"), frozenset({two.client_order_id, three.client_order_id}))
    shared = [
        filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, each, None, None))
        for each in (two, three)
    ]

    assert first == [(9.5, "0.3333"), (9.5, "0.0000")]
    assert second == [(9.5, "0.6667"), (9.5, "0.0000")]
    assert shared == [[(9.51, "0.5000"), (9.51, "0.0000")], [(9.5, "0.3000"), (9.5, "0.0000")]]


def test_a_print_credits_an_order_once_and_only_one_that_rested_before_it() -> None:
    resting, late = order("O-1", "1.0000"), order("O-2", "1.0000")
    fills = model()
    fills.seen(printed(9.49, "0.1000"), frozenset({resting.client_order_id}))

    asked = [
        filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, each, None, None))
        for each in (resting, resting, late)
    ]

    assert asked == [[(9.5, "0.1000"), (9.5, "0.0000")], [(9.5, "0.0000")], [(9.5, "0.0000")]]


def test_whole_fills_all_that_is_left_and_a_quote_or_a_bar_ends_the_print() -> None:
    fills = model("whole")
    resting = order("O-1", "0.7500", 9.5, OrderSide.SELL)
    fills.seen(printed(9.52, "0.0100"), frozenset({resting.client_order_id}))
    whole = filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, resting, None, None))
    fills.seen(printed(9.52, "0.0100"), frozenset({resting.client_order_id}))
    fills.seen(
        QuoteTick(
            COIN, Price(9.4, 2), Price(9.6, 2), Quantity.from_str("1"), Quantity.from_str("1"), 2, 2
        ),
        frozenset(),
    )
    after_quote = filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, resting, None, None))
    fills.seen(printed(9.52, "0.0100"), frozenset({resting.client_order_id}))
    bar_type = BarType(
        COIN, BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST), AggregationSource.EXTERNAL
    )
    fills.seen(
        Bar(
            bar_type,
            Price(9.5, 2),
            Price(9.6, 2),
            Price(9.4, 2),
            Price(9.55, 2),
            Quantity.from_str("1"),
            3,
            3,
        ),
        frozenset(),
    )
    after_bar = filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, resting, None, None))

    assert whole == [(9.5, "0.7500"), (9.5, "0.0000")]
    assert after_quote == after_bar == [(9.5, "0.0000")]


def test_a_print_reaches_only_the_side_it_hit() -> None:
    buy = order("O-1", "1.0000")
    fills = model()
    fills.seen(printed(9.49, "0.1000", AggressorSide.BUYER), frozenset({buy.client_order_id}))

    assert filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, buy, None, None)) == [
        (9.5, "0.0000")
    ]


def test_the_observer_leaves_any_other_fill_model_alone_and_the_module_s_hooks_do_nothing() -> None:
    """Under `touch` and `through` the venue's fill model is the engine's and nothing is told
    it (that the module itself moves no fill there is measured on both paths, in
    `tests/replay/test_print_through.py`); the hooks the exchange calls after a drain, at the
    end of a run and on a reset hold nothing to act on."""
    asked: list[object] = []
    exchange = SimpleNamespace(fill_model=FillModel(), get_matching_engine=asked.append)
    module = Tape(SimulationModuleConfig(component_id="Tape-XNAS"))

    observe(exchange, printed(9.49, "1"))
    module.process(1)
    module.log_diagnostics(None)
    module.reset()

    assert asked == []


def test_a_print_on_an_instrument_the_venue_has_no_engine_for_has_nothing_resting() -> None:
    fills = model()
    exchange = SimpleNamespace(fill_model=fills, get_matching_engine=lambda _: None)
    resting = order("O-1", "1.0000")

    observe(exchange, printed(9.49, "1"))

    assert filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, resting, None, None)) == [
        (9.5, "0.0000")
    ]


def test_a_print_already_credited_whole_fills_the_next_order_nothing() -> None:
    better, worse = order("O-1", "0.5000", 9.51), order("O-2", "1.0000")
    fills = model()
    fills.seen(printed(9.49, "0.5000"), frozenset({better.client_order_id, worse.client_order_id}))

    asked = [
        filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, each, None, None))
        for each in (better, worse)
    ]

    assert asked == [[(9.51, "0.5000"), (9.51, "0.0000")], [(9.5, "0.0000")]]
