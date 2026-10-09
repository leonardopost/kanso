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
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    AggregationSource,
    AggressorSide,
    BarAggregation,
    LiquiditySide,
    OrderSide,
    PriceType,
)
from nautilus_trader.model.events import OrderTriggered, OrderUpdated
from nautilus_trader.model.identifiers import (
    ClientOrderId,
    InstrumentId,
    StrategyId,
    TradeId,
    TraderId,
)
from nautilus_trader.model.objects import Price, Quantity

from kanso.nautilus.tape import PrintThrough, PrintThroughConfig, Rested, Tape, observe

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


def quoted(bid: float, ask: float, bid_size: str = "1", ask_size: str = "1") -> QuoteTick:
    return QuoteTick(
        COIN,
        Price(bid, 2),
        Price(ask, 2),
        Quantity.from_str(bid_size),
        Quantity.from_str(ask_size),
        2,
        2,
    )


def resting(*orders: Any) -> list[Rested]:
    """The orders resting before a print, each having taken its place after the one before."""
    return [
        Rested(each.client_order_id, each.side == OrderSide.BUY, each.price, each.leaves_qty.raw, n)
        for n, each in enumerate(orders)
    ]


def filled(answer: Any) -> list[tuple[float, str]]:
    return [(float(px), str(qty)) for px, qty in answer.simulate_fills(None, 2, 4, False)]


def test_a_print_through_fills_by_its_exact_size_and_shares_it() -> None:
    """Quantities are counted in the engine's integers: a print of 0.3333 through a resting
    1.0000 credits exactly that, the next of 0.6667 the rest; one print shared by two orders
    credits the better-priced first and the other what is left."""
    one, two, three = order("O-1", "1.0000"), order("O-2", "0.5000", 9.51), order("O-3", "1.0")
    fills = model()
    fills.seen(printed(9.49, "0.3333"), resting(one))
    first = filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, one, None, None))
    fills.seen(printed(9.48, "0.6667"), resting(one))
    second = filled(
        fills.get_orderbook_for_fill_simulation(INSTRUMENT, order("O-1", "0.6667"), None, None)
    )
    fills.seen(printed(9.49, "0.8000"), resting(two, three))
    shared = [
        filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, each, None, None))
        for each in (two, three)
    ]

    assert first == [(9.5, "0.3333"), (9.5, "0.0000")]
    assert second == [(9.5, "0.6667"), (9.5, "0.0000")]
    assert shared == [[(9.51, "0.5000"), (9.51, "0.0000")], [(9.5, "0.3000"), (9.5, "0.0000")]]


def test_a_print_is_shared_in_price_then_time_priority_whatever_order_the_engine_asks() -> None:
    """The shares are struck when the print arrives: asked the worse-priced buy first, the
    model still credits the better-priced first; at one price, the order that took its place
    there first is credited first, whichever the engine asks about first."""
    worse, better = order("O-1", "0.3000", 9.5), order("O-2", "0.3000", 9.51)
    later, earlier = order("O-3", "0.3000"), order("O-4", "0.3000")
    fills = model()
    fills.seen(printed(9.49, "0.4000"), resting(worse, better))
    by_price = [
        filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, each, None, None))
        for each in (worse, better)
    ]
    fills.seen(printed(9.49, "0.4000"), resting(earlier, later))
    by_time = [
        filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, each, None, None))
        for each in (later, earlier)
    ]

    assert by_price == [[(9.5, "0.1000"), (9.5, "0.0000")], [(9.51, "0.3000"), (9.51, "0.0000")]]
    assert by_time == [[(9.5, "0.1000"), (9.5, "0.0000")], [(9.5, "0.3000"), (9.5, "0.0000")]]


def test_the_observer_ranks_a_modified_order_from_its_modify_and_skips_an_untriggered_stop() -> (
    None
):
    """An order takes its place at its price when the venue accepts it or, later, modifies it,
    so a modify sends it behind an order that reached that price before; a partial fill does
    not move it. A stop's limit is not resting until it triggers, so it takes no share."""

    def held(
        name: str, accepted: int, modified: int | None = None, triggered: bool | None = None
    ) -> Any:
        rested = order(name, "0.3000", 9.55 if triggered is False else 9.5)
        events = [] if modified is None else [updated(rested, modified)]
        return SimpleNamespace(
            **vars(rested),
            has_trigger_price=triggered is not None,
            is_triggered=bool(triggered),
            ts_accepted=accepted,
            events=events,
        )

    def updated(rested: Any, ts: int) -> OrderUpdated:
        return OrderUpdated(
            TraderId("T-1"),
            StrategyId("S-1"),
            COIN,
            rested.client_order_id,
            None,
            None,
            Quantity.from_str("1.0000"),
            rested.price,
            None,
            UUID4(),
            ts,
            ts,
        )

    moved, stayed, stop = held("O-1", 1, modified=5), held("O-2", 3), held("O-3", 0, None, False)
    fills = model()
    engine = SimpleNamespace(get_open_orders=lambda: [moved, stop, stayed])
    observe(
        SimpleNamespace(fill_model=fills, get_matching_engine=lambda _: engine),
        printed(9.49, "0.5"),
    )

    asked = [
        filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, each, None, None))
        for each in (moved, stop, stayed)
    ]

    assert asked == [
        [(9.5, "0.2000"), (9.5, "0.0000")],
        [(9.55, "0.0000")],
        [(9.5, "0.3000"), (9.5, "0.0000")],
    ]


def test_the_observer_ranks_a_triggered_stop_from_its_trigger() -> None:
    """A stop the venue accepted first and triggered later took its place at its limit when it
    triggered, behind a plain limit accepted between the two: of a print of 0.5 the plain
    limit takes its 0.3 and the stop the 0.2 left, whichever the engine holds first."""
    stop, plain = order("O-1", "0.3000"), order("O-2", "0.3000")
    triggered = OrderTriggered(
        TraderId("T-1"),
        StrategyId("S-1"),
        COIN,
        stop.client_order_id,
        None,
        None,
        UUID4(),
        5,
        5,
    )
    held = [
        SimpleNamespace(
            **vars(stop),
            has_trigger_price=True,
            is_triggered=True,
            ts_accepted=1,
            events=[triggered],
        ),
        SimpleNamespace(
            **vars(plain), has_trigger_price=False, is_triggered=False, ts_accepted=3, events=[]
        ),
    ]
    fills = model()
    engine = SimpleNamespace(get_open_orders=lambda: held)
    observe(
        SimpleNamespace(fill_model=fills, get_matching_engine=lambda _: engine),
        printed(9.49, "0.5"),
    )

    asked = [
        filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, each, None, None))
        for each in (stop, plain)
    ]

    assert asked == [[(9.5, "0.2000"), (9.5, "0.0000")], [(9.5, "0.3000"), (9.5, "0.0000")]]


def test_a_print_credits_an_order_once_and_only_one_that_rested_before_it() -> None:
    rested, late = order("O-1", "1.0000"), order("O-2", "1.0000")
    fills = model()
    fills.seen(printed(9.49, "0.1000"), resting(rested))

    asked = [
        filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, each, None, None))
        for each in (rested, rested, late)
    ]

    assert asked == [[(9.5, "0.1000"), (9.5, "0.0000")], [(9.5, "0.0000")], [(9.5, "0.0000")]]


def test_whole_fills_all_that_is_left_and_a_quote_or_a_bar_ends_the_print() -> None:
    fills = model("whole")
    sell = order("O-1", "0.7500", 9.5, OrderSide.SELL)
    fills.seen(printed(9.52, "0.0100"), resting(sell))
    whole = filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, sell, None, None))
    fills.seen(printed(9.52, "0.0100"), resting(sell))
    fills.seen(quoted(9.4, 9.6), [])
    after_quote = filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, sell, None, None))
    fills.seen(printed(9.52, "0.0100"), resting(sell))
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
        [],
    )
    after_bar = filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, sell, None, None))

    assert whole == [(9.5, "0.7500"), (9.5, "0.0000")]
    assert after_quote == after_bar == [(9.5, "0.0000")]


def test_a_print_reaches_only_the_side_it_hit() -> None:
    buy = order("O-1", "1.0000")
    fills = model()
    fills.seen(printed(9.49, "0.1000", AggressorSide.BUYER), resting(buy))

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
    rested = order("O-1", "1.0000")

    observe(exchange, printed(9.49, "1"))

    assert filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, rested, None, None)) == [
        (9.5, "0.0000")
    ]


def test_a_print_already_credited_whole_fills_the_next_order_nothing() -> None:
    better, worse = order("O-1", "0.5000", 9.51), order("O-2", "1.0000")
    fills = model()
    fills.seen(printed(9.49, "0.5000"), resting(better, worse))

    asked = [
        filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, each, None, None))
        for each in (better, worse)
    ]

    assert asked == [[(9.51, "0.5000"), (9.51, "0.0000")], [(9.5, "0.0000")]]


def test_an_order_moved_after_the_print_is_not_credited_by_it() -> None:
    """A buy resting at 9.48 before a print at 9.49, then modified to 9.50: matched again at its
    new price, it is an order that arrived after the print; one moved and moved back to the
    price it rested at is the order the print found, and it is not through that."""
    before = order("O-1", "1.0000", 9.48)
    fills = model()
    fills.seen(printed(9.49, "0.1000"), resting(before))

    moved = filled(
        fills.get_orderbook_for_fill_simulation(INSTRUMENT, order("O-1", "1.0000", 9.5), None, None)
    )
    back = filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, before, None, None))

    assert moved == [(9.5, "0.0000")]
    assert back == [(9.48, "0.0000")]


def test_a_print_strictly_outside_the_quote_ends_it_and_one_at_its_edges_does_not() -> None:
    """A print at the bid or the ask leaves the quote in force; one under the bid or over the
    ask ends it, so a taker has no quote until the next; a side the quote shows at size zero
    is no edge to trade outside of."""

    def in_force(*points: Any) -> bool:
        fills = model()
        for point in points:
            fills.seen(point, [])
        return COIN in fills._quote

    assert in_force(quoted(9.48, 9.52), printed(9.48, "1"), printed(9.52, "1"))
    assert not in_force(quoted(9.48, 9.52), printed(9.47, "1"))
    assert not in_force(quoted(9.48, 9.52), printed(9.53, "1", AggressorSide.SELLER))
    assert in_force(quoted(9.48, 9.52, ask_size="0"), printed(9.6, "1"))
    assert not in_force(quoted(9.48, 9.52, ask_size="0"), printed(9.4, "1"))
    assert in_force(quoted(9.48, 9.52), printed(9.6, "1"), quoted(9.59, 9.62))


def test_a_split_restates_the_model_s_own_quote_and_ends_the_print_in_hand() -> None:
    """A one-for-ten reverse split restates the last quote at ten times its prices and a tenth
    of its sizes, a size shown staying one increment at least and one not shown at zero; the
    print in hand ends; with no quote there is nothing to restate."""
    definition = SimpleNamespace(
        id=COIN,
        size_increment=Quantity.from_str("0.0001"),
        make_price=lambda value: Price(value, 2),
        make_qty=lambda value: Quantity(value, 4),
    )
    fills = model()
    rested = order("O-1", "1.0000")
    fills.restate(definition, 0.1, 3, 3)
    fills.seen(quoted(9.48, 9.52, bid_size="0.0005", ask_size="0.0000"), [])
    fills.seen(printed(9.49, "0.1000"), resting(rested))

    fills.restate(definition, 0.1, 3, 3)

    quote = fills._quote[COIN]
    assert (float(quote.bid_price), float(quote.ask_price)) == (94.8, 95.2)
    assert (str(quote.bid_size), str(quote.ask_size)) == ("0.0001", "0.0000")
    assert (quote.ts_event, quote.ts_init) == (3, 3)
    assert filled(fills.get_orderbook_for_fill_simulation(INSTRUMENT, rested, None, None)) == [
        (9.5, "0.0000")
    ]
