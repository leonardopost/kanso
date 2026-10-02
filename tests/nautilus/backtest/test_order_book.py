"""A hypothesis that holds the book is filled behind what the book showed ahead of it."""

from __future__ import annotations

from datetime import date

from nautilus_trader.model.data import OrderBookDelta, TradeTick
from nautilus_trader.model.enums import AggressorSide, BookAction, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.data.loaders.points import make_delta
from kanso.nautilus.backtest import execute
from kanso.schemas import Hypothesis
from tests.nautilus.backtest.conftest import RESEARCH, SYMBOL, _venue, hypothesis, instrument

SECOND_NS = 1_000_000_000

JOINING = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Joins the displayed bid on the first print it sees, and rests there.\"\"\"

    config_cls = Config

    def on_start(self) -> None:
        self.posted = False

    def on_trade_tick(self, tick) -> None:
        if not self.posted:
            self.posted = True
            self.submit_entry(tick.instrument_id, "BUY", qty=300, price=10.00)
"""


def _id() -> InstrumentId:
    return InstrumentId(Symbol(SYMBOL), _venue())


def deltas(day: date, shown: int = 500) -> list[OrderBookDelta]:
    """A book at the open: `shown` on the bid at 10.00 and 500 on the offer at 10.02; with
    nothing shown at 10.00 the bid is 500 at 9.99, so an order at 10.00 has the level to itself."""
    base = midnight_ns(day) + 14 * 3_600 * SECOND_NS
    bid = (1_000, shown) if shown else (999, 500)
    return [
        make_delta(_id(), BookAction.ADD, OrderSide.BUY, bid[0], bid[1], 1, 2, 0, base, base),
        make_delta(_id(), BookAction.ADD, OrderSide.SELL, 1_002, 500, 2, 2, 0, base, base),
    ]


def prints(day: date) -> list[TradeTick]:
    """A buyer's print at the offer, then eight sellers' prints of 100 at the bid a second apart."""
    base = midnight_ns(day) + 14 * 3_600 * SECOND_NS
    made = [
        TradeTick(
            _id(),
            Price(10.02, 2),
            Quantity.from_int(100),
            AggressorSide.BUYER,
            TradeId("T-0"),
            ts_event=base + SECOND_NS,
            ts_init=base + SECOND_NS,
        )
    ]
    for index in range(1, 9):
        ts = base + (index + 1) * SECOND_NS
        made.append(
            TradeTick(
                _id(),
                Price(10.00, 2),
                Quantity.from_int(100),
                AggressorSide.SELLER,
                TradeId(f"T-{index}"),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return made


def _seconds_of_fills(
    data_requirements: tuple[str, ...],
    request_for,
    latency_ms: float = 0.0,
    shown: int = 500,
) -> list[int]:
    day = RESEARCH[0]
    document = hypothesis().model_dump(mode="json")
    document.update(resolution="tick", data_requirements=list(data_requirements))
    document["costs"] = {**document["costs"], "latency_ms": latency_ms}
    hyp = Hypothesis.model_validate(document)
    request = request_for(RESEARCH, source=JOINING, hypothesis_=hyp)
    groups = [tuple(prints(day))]
    if "book" in data_requirements:
        groups.insert(0, tuple(deltas(day, shown)))
    result = execute(request, [instrument()], groups)
    assert not result.crashed, result.traceback_tail
    base = midnight_ns(day) + 14 * 3_600 * SECOND_NS
    return [(fill.ts_ns - base) // SECOND_NS for fill in result.run.fills]


def test_a_venue_with_latency_sees_the_order_late_and_the_prints_in_between_miss_it(
    request_for,
) -> None:
    """The order is sent on the print at second one to a level of its own, 10.00, between the
    bid at 9.99 and the offer at 10.02; with no latency the sellers' prints at seconds two,
    three and four fill it. With a second and a half of latency it is in flight until second
    two and a half, and the venue acts on it at the first point after that — the print at
    second three, which is matched before the order is placed — so the prints at seconds
    two and three pass it and the next three fill it."""
    assert _seconds_of_fills(("book", "trade"), request_for, shown=0) == [2, 3, 4]
    assert _seconds_of_fills(("book", "trade"), request_for, shown=0, latency_ms=1_500) == [
        4,
        5,
        6,
    ]


def test_an_order_that_joins_a_level_waits_for_the_size_the_book_showed_ahead_of_it(
    request_for,
) -> None:
    """With the book held, 500 shown ahead trade through before the order's 300 fill in
    three prints; on the top-of-book venue the same order is filled by the first print."""
    assert _seconds_of_fills(("book", "trade"), request_for) == [7, 8, 9]
    assert _seconds_of_fills(("trade",), request_for) == [2, 3, 4]


def test_an_intent_sent_from_the_book_handler_carries_the_changes_own_instant(
    request_for,
) -> None:
    """A sleeve that holds only the book stamps what it sends with the change it is handling
    — its second, fifth and thirtieth — where every such intent used to carry the last
    print's instant, which for a sleeve that sees no print is zero."""
    from tests.nautilus.backtest.test_exit_flat import BOOK_ONLY, BOOK_OPEN_NS, book

    day = RESEARCH[0]
    document = hypothesis().model_dump(mode="json")
    document.update(resolution="tick", data_requirements=["book"])
    hyp = Hypothesis.model_validate(document)
    result = execute(
        request_for(RESEARCH, source=BOOK_ONLY, hypothesis_=hyp), [instrument()], [tuple(book(day))]
    )
    assert not result.crashed, result.traceback_tail

    base = midnight_ns(day) + BOOK_OPEN_NS
    assert [(intent[0] - base) // SECOND_NS for intent in result.intents] == [0, 3, 28]
