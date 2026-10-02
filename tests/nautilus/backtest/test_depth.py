"""Under `depth` the venue is handed every change of the book and the author only its view.

The feed is a book of several changes per tenth of a second beside prints, built here to put
each rule in front of a case: an instant of eight changes, a top that moves by a size, a
change below the top three, an instant that takes the best offer away and puts a better one
in its place, a change below the levels shown, and a best bid that goes. What the author is
handed is checked against the same changes replayed into the engine's own `OrderBook`, so
the harness's copy of the book is never the reference it is checked against.
"""

from __future__ import annotations

import sys
from datetime import date
from hashlib import sha256
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import (
    Bar,
    CustomData,
    DataType,
    OrderBookDelta,
    OrderBookDeltas,
    TradeTick,
)
from nautilus_trader.model.enums import (
    AggressorSide,
    BookAction,
    BookType,
    OrderSide,
    RecordFlag,
)
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.data.loaders.points import make_delta
from kanso.data.types.funding import Funding
from kanso.errors import ValidationError
from kanso.nautilus.backtest import RunRequest, execute, execute_chunked
from kanso.schemas import Hypothesis
from tests.nautilus.backtest.conftest import RESEARCH, SYMBOL, _venue, hypothesis, instrument

MS_NS = 1_000_000
OPEN_NS = 14 * 3_600 * 1_000_000_000
"""The book opens at 14:00 UTC, as the other book tests' does."""

EVERY_MS = 100
LEVELS = 3

PROBE = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy

SEEN = []


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Records what it is handed of the book and of the prints, and trades nothing.\"\"\"

    config_cls = Config

    def on_order_book_deltas(self, deltas) -> None:
        SEEN.append(("depth", self.data_time, deltas))

    def on_quote_tick(self, tick) -> None:
        SEEN.append(("top", self.data_time, tick))

    def on_trade_tick(self, tick) -> None:
        SEEN.append(("trade", self.data_time, tick))
"""

POSTER = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"One hundred at the best bid when flat and at the best offer when long, re-posted when
    that side's best price moves: a rule that reads level one and nothing else.\"\"\"

    config_cls = Config

    def on_start(self) -> None:
        self.posted = None

    def on_quote_tick(self, tick) -> None:
        iid = tick.instrument_id
        if self.held(iid) <= 0:
            want = ("BUY", tick.bid_price.as_double())
        else:
            want = ("SELL", tick.ask_price.as_double())
        if want == self.posted:
            return
        self.cancel_all_orders(iid)
        if want[0] == "BUY":
            sent = self.submit_entry(iid, "BUY", qty=100, price=want[1])
        else:
            sent = self.submit_exit(iid, price=want[1])
        self.posted = want if sent is not None else None
"""

TAKER = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Bids the best offer of the first view of the book it is handed, which takes it.\"\"\"

    config_cls = Config

    def on_start(self) -> None:
        self.taken = False

    def on_order_book_deltas(self, deltas) -> None:
        offers = [d.order.price.as_double() for d in deltas.deltas if d.order.side == 2]
        if not self.taken and offers:
            self.taken = True
            self.submit_entry(deltas.instrument_id, "BUY", qty=100, price=min(offers))
"""


def _id() -> InstrumentId:
    return InstrumentId(Symbol(SYMBOL), _venue())


def _base(day: date) -> int:
    return midnight_ns(day) + OPEN_NS


def _delta(ms: int, action: BookAction, side: OrderSide, ticks: int, size: int, day: date) -> Any:
    ts = _base(day) + ms * MS_NS
    return make_delta(_id(), action, side, ticks, size, 0, 2, 0, ts, ts)


def book(day: date = RESEARCH[0]) -> list[OrderBookDelta]:
    """Four levels a side at the open, then a change at a time, two at 130 ms."""
    add, update, delete = BookAction.ADD, BookAction.UPDATE, BookAction.DELETE
    buy, sell = OrderSide.BUY, OrderSide.SELL
    opening = [
        *(_delta(0, add, buy, 1_000 - step, 500 - 100 * step, day) for step in range(4)),
        *(_delta(0, add, sell, 1_002 + step, 500 - 100 * step, day) for step in range(4)),
    ]
    return [
        *opening,
        _delta(30, update, buy, 1_000, 450, day),  # the best bid's size: level one moves
        _delta(60, update, buy, 998, 350, day),  # the third bid: depth moves, level one not
        _delta(130, delete, sell, 1_002, 0, day),  # the best offer goes ...
        _delta(130, add, sell, 1_001, 100, day),  # ... and a better one takes its place
        _delta(150, update, sell, 1_005, 250, day),  # the fourth offer: nothing shown moves
        _delta(250, add, buy, 996, 100, day),  # a fifth bid: nothing shown moves
        _delta(420, delete, buy, 1_000, 0, day),  # the best bid goes
        _delta(1_210, update, sell, 1_001, 300, day),
    ]


def prints(day: date = RESEARCH[0]) -> list[TradeTick]:
    """Sellers' prints at the bid, some between the book's changes and some on them."""
    made = []
    for index, (ms, ticks) in enumerate(
        ((50, 1_000), (105, 1_000), (110, 1_000), (130, 1_000), (205, 1_000), (430, 999))
        + ((700, 999), (990, 999), (1_250, 999), (1_400, 999))
    ):
        ts = _base(day) + ms * MS_NS
        made.append(
            TradeTick(
                _id(),
                Price(ticks / 100, 2),
                Quantity.from_int(100),
                AggressorSide.SELLER,
                TradeId(f"T-{index}"),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return made


def depth_hypothesis(
    *, every_ms: int | None = EVERY_MS, levels: int = LEVELS, latency_ms: float = 0.0
) -> Hypothesis:
    document = hypothesis().model_dump(mode="json", by_alias=True)
    document.update(resolution="tick", data_requirements=["book", "trade"])
    document["costs"] = {**document["costs"], "latency_ms": latency_ms}
    if every_ms is not None:
        document["depth"] = {"every_ms": every_ms, "levels": levels}
    return Hypothesis.model_validate(document)


def seen(source: bytes) -> list[tuple[str, int, Any]]:
    """What a probe recorded, read off the module the runner loaded its bytes as."""
    return sys.modules[f"kanso_sleeve_{sha256(source).hexdigest()[:12]}"].SEEN  # type: ignore[no-any-return]


def run(request: RunRequest, groups: list[tuple[object, ...]]) -> Any:
    result = execute(request, [instrument()], groups)
    assert not result.crashed, result.traceback_tail
    return result


def _top(book_: OrderBook, levels: int) -> tuple[list[tuple[float, float]], ...]:
    return tuple(
        [(level.price.as_double(), level.size()) for level in side[:levels]]
        for side in (book_.bids(), book_.asks())
    )


def expected_views(
    deltas: list[OrderBookDelta], prints_: list[TradeTick], every_ns: int, levels: int
) -> list[tuple[int, tuple[list[tuple[float, float]], ...]]]:
    """The views a sleeve is owed: at the first point after each grid instant, the top
    `levels` of the book as of that instant, whenever it differs from the last one owed."""
    replayed = OrderBook(_id(), BookType.L2_MBP)
    shown: tuple[list[tuple[float, float]], ...] = ([], [])
    last = -1
    owed = []
    points = sorted([*deltas, *prints_], key=lambda point: int(point.ts_init))
    for point in points:
        grid = (int(point.ts_init) - 1) // every_ns * every_ns
        if grid > last:
            last = grid
            view = _top(replayed, levels)
            if view != shown:
                owed.append((grid, view))
                shown = view
        if isinstance(point, OrderBookDelta):
            replayed.apply_delta(point)
    return owed


def expected_tops(deltas: list[OrderBookDelta]) -> list[tuple[int, tuple[float, ...]]]:
    """Every instant whose changes, all applied, left a top unlike the one before."""
    replayed = OrderBook(_id(), BookType.L2_MBP)
    owed: list[tuple[int, tuple[float, ...]]] = []
    instants = sorted({int(delta.ts_init) for delta in deltas})
    for instant in instants:
        for delta in deltas:
            if int(delta.ts_init) == instant:
                replayed.apply_delta(delta)
        if replayed.best_bid_price() is None or replayed.best_ask_price() is None:
            continue
        top = (
            replayed.best_bid_price().as_double(),
            replayed.best_bid_size().as_double(),
            replayed.best_ask_price().as_double(),
            replayed.best_ask_size().as_double(),
        )
        if not owed or owed[-1][1] != top:
            owed.append((instant, top))
    return owed


def handed_views(records: list[tuple[str, int, Any]]) -> list[tuple[int, Any]]:
    """The author's own book after each view it was handed, rebuilt from the views alone."""
    rebuilt = OrderBook(_id(), BookType.L2_MBP)
    views = []
    for kind, data_time, deltas in records:
        if kind == "depth":
            rebuilt.apply_deltas(deltas)
            views.append((data_time, _top(rebuilt, 400)))
    return views


# --- without the key ----------------------------------------------------------


def test_without_depth_the_author_is_handed_every_change_and_no_quote(request_for) -> None:
    """The key is opt-in: a sleeve without it is handed the book's changes as the venue is,
    and no level one, because nothing loaded a quote."""
    deltas = book()
    run(
        request_for(RESEARCH, source=PROBE, hypothesis_=depth_hypothesis(every_ms=None)),
        [tuple(deltas), tuple(prints())],
    )
    records = seen(PROBE)

    assert sum(len(data.deltas) for kind, _, data in records if kind == "depth") == len(deltas)
    assert not [kind for kind, _, _ in records if kind == "top"]


# --- with it --------------------------------------------------------------------


def test_depth_is_handed_on_the_grid_as_the_book_stood_at_it(request_for) -> None:
    """Every view is stamped with a grid instant, comes at most once per instant, and leaves
    the author's book equal to the top three of the full book as of that instant."""
    deltas, prints_ = book(), prints()
    run(
        request_for(RESEARCH, source=PROBE, hypothesis_=depth_hypothesis()),
        [tuple(deltas), tuple(prints_)],
    )
    records = seen(PROBE)
    owed = expected_views(deltas, prints_, EVERY_MS * MS_NS, LEVELS)
    base = _base(RESEARCH[0])

    assert [(stamp - base) // MS_NS for stamp, _ in owed] == [0, 100, 200, 600, 1_300], (
        "a change is shown at the first point after the grid instant after it: the bid that "
        "goes at 420 ms on the print at 700, the offer at 1,210 on the print at 1,400"
    )
    assert handed_views(records) == owed
    for kind, data_time, deltas_ in records:
        if kind == "depth":
            assert data_time % (EVERY_MS * MS_NS) == 0
            assert all(int(delta.ts_init) == data_time for delta in deltas_.deltas)
            assert [int(delta.flags) for delta in deltas_.deltas][-1] == RecordFlag.F_LAST
            assert BookAction.CLEAR not in [delta.action for delta in deltas_.deltas]


def test_a_level_below_the_ones_shown_is_never_handed(request_for) -> None:
    """The fourth offer and the fifth bid move, and no view moves with them."""
    deltas, prints_ = book(), prints()
    run(
        request_for(RESEARCH, source=PROBE, hypothesis_=depth_hypothesis()),
        [tuple(deltas), tuple(prints_)],
    )
    prices = {
        delta.order.price.as_double()
        for kind, _, data in seen(PROBE)
        if kind == "depth"
        for delta in data.deltas
    }

    assert 10.05 not in prices
    assert 9.96 not in prices


def test_level_one_is_handed_on_every_instant_the_top_moved_and_never_partway(
    request_for,
) -> None:
    """The instant that takes the best offer away and puts a better one in its place is
    handed once, with the better one: the offer at 10.03 that stood between the two
    changes is never a top the author sees. A change below the top hands nothing."""
    deltas = book()
    run(
        request_for(RESEARCH, source=PROBE, hypothesis_=depth_hypothesis()),
        [tuple(deltas), tuple(prints())],
    )
    handed = [
        (
            data_time,
            (
                tick.bid_price.as_double(),
                tick.bid_size.as_double(),
                tick.ask_price.as_double(),
                tick.ask_size.as_double(),
            ),
        )
        for kind, data_time, tick in seen(PROBE)
        if kind == "top"
    ]
    base = _base(RESEARCH[0])

    assert handed == expected_tops(deltas)
    assert [(stamp - base) // MS_NS for stamp, _ in handed] == [0, 30, 130, 420, 1_210]
    assert 10.03 not in [top[2] for _, top in handed]


def test_level_one_of_a_feed_whose_every_instant_is_one_change_is_handed_at_once(
    request_for,
) -> None:
    """With no instant of two changes the feed is unmarked, every change is an instant, and
    its level one is handed from the change itself rather than from a marker."""
    deltas = [delta for delta in book() if int(delta.ts_init) != _base(RESEARCH[0]) + 130 * MS_NS]
    opening = [delta for delta in deltas if int(delta.ts_init) == _base(RESEARCH[0])]
    spread = [
        OrderBookDelta(
            delta.instrument_id,
            delta.action,
            delta.order,
            0,
            0,
            int(delta.ts_init) - (len(opening) - index) * MS_NS,
            int(delta.ts_init) - (len(opening) - index) * MS_NS,
        )
        for index, delta in enumerate(opening)
    ]
    deltas = [*spread, *deltas[len(opening) :]]
    run(
        request_for(RESEARCH, source=PROBE, hypothesis_=depth_hypothesis()),
        [tuple(deltas)],
    )
    handed = [
        (data_time, (tick.bid_price.as_double(), tick.ask_price.as_double()))
        for kind, data_time, tick in seen(PROBE)
        if kind == "top"
    ]

    assert [(stamp, (top[0], top[2])) for stamp, top in expected_tops(deltas)] == handed


def test_a_rule_that_reads_level_one_fills_alike_whatever_the_grid(request_for) -> None:
    """The venue is handed every change whatever the author is shown, so the queue ahead of
    a resting order falls on the prints between grid instants alike: a rule reading level
    one alone sends and fills the same on a grid of a tenth of a second and of a minute."""
    groups: list[tuple[object, ...]] = [tuple(book()), tuple(prints())]
    fine = run(request_for(RESEARCH, source=POSTER, hypothesis_=depth_hypothesis()), groups)
    coarse = run(
        request_for(RESEARCH, source=POSTER, hypothesis_=depth_hypothesis(every_ms=60_000)),
        groups,
    )

    assert fine.run.fills
    assert fine.intents == coarse.intents
    assert fine.run.fills == coarse.run.fills


def test_an_order_sent_from_a_view_reaches_the_venue_a_latency_after_its_point(
    request_for,
) -> None:
    """The first view is handed at the book's change at 30 ms, the first point after the open,
    and stamped with the open; a bid at the best offer sent from it fills on that change with
    no latency, and with 20 ms on the print at 50, the first point once 20 ms have passed
    since the point that carried it. The view itself is not delayed again: the latency is
    the whole round trip."""
    groups: list[tuple[object, ...]] = [tuple(book()), tuple(prints())]
    base = _base(RESEARCH[0])
    at_once = run(request_for(RESEARCH, source=TAKER, hypothesis_=depth_hypothesis()), groups)
    late = run(
        request_for(RESEARCH, source=TAKER, hypothesis_=depth_hypothesis(latency_ms=20.0)),
        groups,
    )

    assert [(intent[0] - base) // MS_NS for intent in at_once.intents] == [0]
    assert at_once.intents == late.intents
    assert [(fill.ts_ns - base) // MS_NS for fill in at_once.run.fills] == [30]
    assert [(fill.ts_ns - base) // MS_NS for fill in late.run.fills] == [50]


QUOTE_TAKER = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Takes the first offer of 10.01 it is shown at level one, and nothing else.\"\"\"

    config_cls = Config

    def on_start(self) -> None:
        self.taken = False

    def on_quote_tick(self, tick) -> None:
        if not self.taken and tick.ask_price.as_double() == 10.01:
            self.taken = True
            self.submit_entry(tick.instrument_id, "BUY", qty=100, price=10.01)
"""

BOOK_ROUTES = {
    "subscribe_order_book_deltas": "(InstrumentId.from_str('DEMO.XNAS'))",
    "subscribe_order_book_at_interval": (
        "(InstrumentId.from_str('DEMO.XNAS'), interval_ms=3_600_000)"
    ),
    "subscribe_order_book_depth": "(InstrumentId.from_str('DEMO.XNAS'))",
}
"""Each way an engine actor subscribes a book of its own, as an attached construct would."""


def subscriber(route: str) -> bytes:
    """A filter that subscribes its host's book by `route` and counts the changes it is
    handed, allowing every order."""
    return f"""
from nautilus_trader.model.identifiers import InstrumentId

from kanso.nautilus.strategy import Decision, KansoModifier, KansoModifierConfig

SEEN = []


class Config(KansoModifierConfig):
    pass


class Modifier(KansoModifier):
    construct = "filter"
    config_cls = Config

    def on_start(self) -> None:
        self.{route}{BOOK_ROUTES[route]}

    def on_order_book_deltas(self, deltas) -> None:
        SEEN.append(len(deltas.deltas))

    def evaluate(self, ctx):
        return Decision(allow=True)
""".encode()


def test_level_one_of_a_marked_instant_is_handed_at_its_marker(request_for) -> None:
    """The instant at 130 ms takes the best offer away and puts 10.01 in its place. Its level
    one is handed at the flush marker after its two changes, so a taker of the new offer
    fills at 130 ms with no latency and at 150 ms, the next point, with 10 ms; were it handed
    only at the next change, at 150 ms, those would be 150 and 250."""
    base = _base(RESEARCH[0])
    filled = {}
    for latency_ms in (0.0, 10.0):
        result = run(
            request_for(
                RESEARCH, source=QUOTE_TAKER, hypothesis_=depth_hypothesis(latency_ms=latency_ms)
            ),
            [tuple(book())],
        )
        assert [(intent[0] - base) // MS_NS for intent in result.intents] == [130]
        filled[latency_ms] = [(fill.ts_ns - base) // MS_NS for fill in result.run.fills]

    assert filled == {0.0: [130], 10.0: [150]}


def test_an_owed_exit_is_paid_on_a_change_the_author_is_not_shown(request_for) -> None:
    """BOOK_ONLY exits at market once, on its thirtieth view, and never asks again; the
    market exit it cancelled its resting one for is refused by the latency, so the exit is
    owed. Every change after the thirtieth is to a bid below the one level shown, so the
    author is shown nothing more, and the harness still asks for the owed exit on each of
    those changes: the position is closed rather than held to the end of the window."""
    from tests.nautilus.backtest.test_exit_flat import BOOK_ONLY, BOOK_OPEN_NS, chasing_costs

    base = midnight_ns(RESEARCH[0]) + BOOK_OPEN_NS
    changes = [
        make_delta(_id(), BookAction.ADD, OrderSide.BUY, 1_000, 500, 1, 2, 0, base, base),
        make_delta(_id(), BookAction.ADD, OrderSide.SELL, 1_002, 500, 2, 2, 0, base, base),
    ]
    for second in range(1, 80):
        ts = base + second * 1_000_000_000
        if second <= 30:
            change = (OrderSide.SELL, 1_002, 500 + second)  # level one moves: a view
        else:
            change = (OrderSide.BUY, 998, 100 + second)  # below level one: no view
        side, ticks, size = change
        changes.append(make_delta(_id(), BookAction.UPDATE, side, ticks, size, 2, 2, 0, ts, ts))
    document = depth_hypothesis(levels=1).model_dump(mode="json", by_alias=True)
    document.update(data_requirements=["book"], costs=chasing_costs(20.0))
    result = run(
        request_for(RESEARCH, source=BOOK_ONLY, hypothesis_=Hypothesis.model_validate(document)),
        [tuple(changes)],
    )

    assert [(fill.side, fill.qty) for fill in result.run.fills] == [("BUY", 100.0), ("SELL", 100.0)]


def _bars(day: date, at_ms: tuple[int, ...]) -> list[Bar]:
    from kanso.nautilus.strategy import _bar_type

    made = []
    for ms in at_ms:
        ts = _base(day) + ms * MS_NS
        made.append(
            Bar(
                _bar_type(_id(), "1s"),
                Price(10.0, 2),
                Price(10.0, 2),
                Price(10.0, 2),
                Price(10.0, 2),
                Quantity.from_int(100),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return made


def _settlements(day: date, at_ms: tuple[int, ...]) -> list[CustomData]:
    """Funding settlements of nothing, wrapped as the catalog hands a custom point over."""
    return [
        CustomData(
            DataType(Funding),
            Funding(
                instrument_id=_id(),
                rate=0.0,
                ts_event=_base(day) + ms * MS_NS,
                ts_init=_base(day) + ms * MS_NS,
            ),
        )
        for ms in at_ms
    ]


@pytest.mark.parametrize("requirement", ["bar", "funding"])
def test_the_view_is_handed_at_a_bar_or_a_custom_point_after_the_grid_instant(
    request_for, requirement: str
) -> None:
    """The view comes at the first point of any data published after a grid instant, not
    only a print or a change: the best bid that goes at 420 ms is shown at the point at
    700 ms, here a bar or a funding settlement, rather than at the next change at 1,210."""
    deltas = book()
    at_ms = (50, 105, 205, 700, 1_400)
    points: list[Any] = (
        _bars(RESEARCH[0], at_ms) if requirement == "bar" else _settlements(RESEARCH[0], at_ms)
    )
    document = depth_hypothesis().model_dump(mode="json", by_alias=True)
    document.update(
        resolution="1s" if requirement == "bar" else "tick",
        data_requirements=["book", requirement],
    )
    source = PROBE + f"# handed at a {requirement}\n".encode()
    run(
        request_for(RESEARCH, source=source, hypothesis_=Hypothesis.model_validate(document)),
        [tuple(deltas), tuple(points)],
    )
    owed = expected_views(deltas, points, EVERY_MS * MS_NS, LEVELS)
    base = _base(RESEARCH[0])

    assert [(stamp - base) // MS_NS for stamp, _ in owed] == [0, 100, 200, 600, 1_300]
    assert handed_views(seen(source)) == owed


@pytest.mark.parametrize("route", sorted(BOOK_ROUTES))
def test_an_attached_construct_is_refused_a_book_of_its_own_under_depth(
    request_for, route: str
) -> None:
    """Without the key a construct may subscribe its host's book, and by the changes it is
    handed every one of them; under it, where its host is handed only the view, asking for a
    book is refused, so it cannot decide the host's orders on depth the account does not
    see."""
    deltas = book()
    source = subscriber(route)
    groups: list[tuple[object, ...]] = [tuple(deltas), tuple(prints())]
    free = execute(
        request_for(
            RESEARCH,
            source=PROBE,
            hypothesis_=depth_hypothesis(every_ms=None),
            modifiers=(("filter", source, {}),),
        ),
        [instrument()],
        groups,
    )
    handed = sys.modules[f"kanso_modifier_{sha256(source).hexdigest()[:12]}"].SEEN

    assert not free.crashed, free.traceback_tail
    if route == "subscribe_order_book_deltas":
        assert sum(handed) == len(deltas)
    with pytest.raises(ValidationError, match=f"Modifier.{route}: under `depth`") as refused:
        execute(
            request_for(
                RESEARCH,
                source=PROBE,
                hypothesis_=depth_hypothesis(),
                modifiers=(("filter", source, {}),),
            ),
            [instrument()],
            groups,
        )
    assert refused.value.remedy == "remove the subscription from strategy.py"


@pytest.mark.parametrize("every_ms", [None, EVERY_MS])
def test_a_marked_chunk_after_an_unmarked_one_is_handed_as_one_window_hands_it(
    request_for, every_ms: int | None
) -> None:
    """The second day holds an instant of two changes and the first none, so a run chunked
    by day starts unmarked and is armed for markers on the second chunk. It hands the author
    what one chunk of both days hands, every print of the second day included."""
    first, second = date(2024, 1, 2), date(2024, 1, 3)
    unmarked = [
        OrderBookDelta(
            delta.instrument_id,
            delta.action,
            delta.order,
            0,
            0,
            _base(first) + index * 7 * MS_NS + 1,
            _base(first) + index * 7 * MS_NS + 1,
        )
        for index, delta in enumerate(book(first))
    ]
    changed = {int(delta.ts_init) for delta in unmarked}
    early = [tick for tick in prints(first) if int(tick.ts_init) not in changed]
    hyp = depth_hypothesis(every_ms=every_ms)

    def handed(chunks: list[list[tuple[object, ...]]], tag: str) -> list[tuple[str, int]]:
        source = PROBE + f"# {tag} {every_ms}\n".encode()
        result = execute_chunked(
            request_for(RESEARCH, source=source, hypothesis_=hyp), [instrument()], chunks
        )
        assert not result.crashed, result.traceback_tail
        return [(kind, data_time) for kind, data_time, _ in seen(source)]

    whole = handed([[tuple(unmarked + book(second)), tuple(early + prints(second))]], "one")
    by_day = handed(
        [[tuple(unmarked), tuple(early)], [tuple(book(second)), tuple(prints(second))]], "two"
    )

    assert by_day == whole
    assert [stamp for kind, stamp in by_day if kind == "trade" and stamp > _base(second)] == [
        int(tick.ts_init) for tick in prints(second)
    ]


def test_depth_reaches_the_sleeve_as_grid_and_levels() -> None:
    """The runner injects the grid in nanoseconds and the levels, read off the hypothesis."""
    from kanso.nautilus.backtest import _sleeve

    request = RunRequest(
        hyp=depth_hypothesis(),
        strategy_source=PROBE,
        window=RESEARCH,
        snapshot_id="a" * 64,
        venue_model={},
        capital=1.0,
    )
    _, config = _sleeve(request)

    assert config.depth == (EVERY_MS * MS_NS, LEVELS)


CHANGES = st.lists(
    st.tuples(
        st.sampled_from([BookAction.ADD, BookAction.UPDATE, BookAction.DELETE, BookAction.CLEAR]),
        st.sampled_from([OrderSide.BUY, OrderSide.SELL]),
        st.integers(990, 1_010),
        st.integers(1, 900),
    ),
    max_size=60,
)


@given(CHANGES, st.integers(1, 6))
def test_the_harness_book_is_the_engine_s_book(changes, levels: int) -> None:
    """Whatever the changes, the harness's copy shows the top levels and the best of each
    side exactly as the engine's own `OrderBook` holds them after the same changes."""
    from kanso.nautilus.strategy import _apply, _Ladder

    engine = OrderBook(_id(), BookType.L2_MBP)
    harness = (_Ladder(1), _Ladder(-1))
    for action, side, ticks, size in changes:
        if action == BookAction.CLEAR:
            delta = OrderBookDelta.clear(_id(), 0, 1, 1)
        else:
            delta = make_delta(_id(), action, side, ticks, size, 0, 2, 0, 1, 1)
        engine.apply_delta(delta)
        _apply(harness, OrderBookDeltas(_id(), [delta]))
        bids, asks = harness
        shown = tuple(
            [(price.as_double(), size_.as_double()) for price, size_ in ladder.top(levels).items()]
            for ladder in (bids, asks)
        )
        assert shown == _top(engine, levels)
        best = (bids.best(), asks.best())
        assert [None if level is None else level[0] for level in best] == [
            engine.best_bid_price(),
            engine.best_ask_price(),
        ]
