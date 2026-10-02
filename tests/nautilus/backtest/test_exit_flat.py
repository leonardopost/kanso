"""An exit never goes past flat, counting the exits still working.

A sleeve that follows the ask with its exit cancels the resting one and sends another on
every quote that moves it. Under a stated latency the cancel is in flight as long as the
new order is, so the old exit can fill before its cancel lands; if the new one were sized
to the whole position it would fill as well, and a long-only sleeve would end short with
nothing in it to buy the shares back. Measured on origin/main before the repair: the run
below sold more than it bought and held the short across the night.
"""

from __future__ import annotations

from datetime import date

import pytest
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.enums import BookAction, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol

from kanso.criteria.gates import max_hold
from kanso.criteria.run import midnight_ns
from kanso.data.loaders.points import make_delta
from kanso.data.loaders.synthetic import SyntheticLoader
from kanso.nautilus.backtest import execute
from kanso.schemas import Hypothesis
from tests.criteria.builders import context
from tests.nautilus.backtest.conftest import (
    RESEARCH,
    SYMBOL,
    VENUE,
    _venue,
    hypothesis,
    instrument,
)

SESSIONS = (date(2024, 1, 2), date(2024, 1, 3))
"""Two sessions, so a position stranded by the race is held across the night."""

CHASING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Buys once at market, then rests its exit at the ask and follows the ask with it:
    on every quote that moves it, cancel the resting exit and send another."""

    config_cls = Config

    def on_start(self):
        self.bought = False
        self.asked = None

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        if not self.bought:
            self.bought = self.submit_entry(instrument_id, "BUY", qty=100) is not None
            return
        held = self.held(instrument_id)
        if held <= 0:
            return
        ask = float(tick.ask_price)
        if ask == self.asked:
            return
        self.cancel_all_orders(instrument_id)
        placed = self.submit_exit(instrument_id, qty=held, price=ask)
        self.asked = ask if placed is not None else None
'''


HEAD = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass
"""

MARKET_AFTER_CANCEL = (
    HEAD
    + b'''

class Strategy(KansoStrategy):
    """Buys once, rests an exit far above the market on its fifth quote, and on its
    thirtieth cancels it and exits at market once, never asking again."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        elif self.seen == 5:
            self.submit_exit(instrument_id, price=round(float(tick.ask_price) * 1.5, 2))
        elif self.seen == 30:
            self.cancel_all_orders(instrument_id)
            self.submit_exit(instrument_id)
'''
)

STOP_OVER_TAKE_PROFIT = (
    HEAD
    + b'''

class Strategy(KansoStrategy):
    """Buys once, rests a take-profit far above the market on its fifth quote, and from its
    thirtieth to its thirty-fourth stops out at market without cancelling it."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        elif self.seen == 5:
            self.submit_exit(instrument_id, price=round(float(tick.ask_price) * 1.5, 2))
        elif 30 <= self.seen < 35:
            self.submit_exit(instrument_id)
'''
)


HOUSEKEEPING = (
    HEAD
    + b'''

class Strategy(KansoStrategy):
    """Buys once, rests a take-profit far above the market on its fifth quote, and from its
    thirtieth cancels every order it has on every quote, and never exits by itself."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        elif self.seen == 5:
            self.submit_exit(instrument_id, price=round(float(tick.ask_price) * 1.5, 2))
        elif self.seen >= 30:
            self.cancel_all_orders(instrument_id)
'''
)


def tp_in_flight_then_stop(gap: int) -> bytes:
    """Buys once, sends a take-profit far above the market on its thirtieth quote, and exits
    at market once, `gap` quotes later — in the same handler, while the take-profit is still
    in flight to the venue, when `gap` is 0."""
    return (
        HEAD
        + f"""

class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        if self.seen == 30:
            self.submit_exit(instrument_id, price=round(float(tick.ask_price) * 1.5, 2))
        if self.seen == 30 + {gap}:
            self.submit_exit(instrument_id)
""".encode()
    )


def exit_once_from(ts_ns: int) -> bytes:
    """An exit rule that says so exactly once, at the first point at or after `ts_ns`."""
    return f"""
from kanso.nautilus.strategy import Decision, KansoModifier, KansoModifierConfig


class Config(KansoModifierConfig):
    pass


class Modifier(KansoModifier):
    construct = "exit"
    config_cls = Config

    def evaluate(self, ctx):
        said = getattr(self, "said", False)
        self.said = said or ctx.ts_event >= {ts_ns}
        return Decision(exit=self.said and not said)
""".encode()


CANCELLED_IN_FLIGHT = (
    HEAD
    + b'''

class Strategy(KansoStrategy):
    """Buys once; on its thirtieth quote sends an exit below the bid, cancels it at once,
    while it is still in flight to the venue, and exits at market; on its fortieth sells
    short."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        elif self.seen == 30:
            order = self.submit_exit(instrument_id, price=round(float(tick.bid_price) - 0.05, 2))
            CANCEL
            self.submit_exit(instrument_id)
        elif self.seen == 40:
            self.submit_entry(instrument_id, "SELL", qty=100)
'''
)


CANCELLED_OPEN = (
    HEAD
    + b'''

class Strategy(KansoStrategy):
    """Buys once, rests an exit above the market on its twenty-eighth quote, and on its
    thirtieth, with the venue holding that exit open, cancels it and exits at market once."""

    config_cls = Config

    def on_start(self):
        self.seen = 0
        self.resting = None

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        elif self.seen == 28:
            self.resting = self.submit_exit(instrument_id, price=PRICE)
        elif self.seen == 30:
            order = self.resting
            CANCEL
            self.submit_exit(instrument_id)
'''
)

SENT_AND_CANCELLED = (
    HEAD
    + b'''

class Strategy(KansoStrategy):
    """Buys once, then on every quote from its fourth rests one share at the ask and cancels
    it in the same handler, before the venue has taken it."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        elif self.seen >= 4:
            order = self.submit_exit(instrument_id, qty=1, price=float(tick.ask_price))
            if order is not None:
                CANCEL
'''
)

BOOK_ONLY = (
    HEAD
    + b'''

class Strategy(KansoStrategy):
    """Holds the book and nothing else: buys at the offer on its second change, rests an
    exit far above it on its fifth, and on its thirtieth cancels it and exits at market
    once, never asking again."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_order_book_deltas(self, deltas):
        instrument_id = deltas.instrument_id
        self.seen += 1
        if self.seen == 2:
            self.submit_entry(instrument_id, "BUY", qty=100, price=10.02)
        elif self.seen == 5:
            self.submit_exit(instrument_id, price=20.00)
        elif self.seen == 30:
            self.cancel_all_orders(instrument_id)
            self.submit_exit(instrument_id)
'''
)

EMULATED_STOP = (
    HEAD
    + b'''
from nautilus_trader.model.enums import OrderSide, TriggerType
from nautilus_trader.model.objects import Price, Quantity


class Strategy(KansoStrategy):
    """Buys once; on its fifth quote sends a stop far below the market that the engine's
    order emulator holds rather than the venue; on its thirtieth exits at market once, and
    never asks again."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        elif self.seen == 5:
            stop = self.order_factory.stop_market(
                instrument_id,
                OrderSide.SELL,
                Quantity.from_int(100),
                Price(round(float(tick.bid_price) * 0.5, 2), 2),
                emulation_trigger=TriggerType.BID_ASK,
            )
            self.submit_order(stop)
        elif self.seen == 30:
            self.submit_exit(instrument_id)
'''
)

BATCH_WITH_EMULATED = (
    HEAD
    + b'''
from nautilus_trader.model.enums import OrderSide, TriggerType
from nautilus_trader.model.objects import Price, Quantity


class Strategy(KansoStrategy):
    """Buys once, rests an exit just above the ask on its twenty-eighth quote and sends a
    buy stop far above the market, held by the order emulator, on its twenty-ninth; on its
    thirtieth cancels both in one batch and exits at market once."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        elif self.seen == 28:
            self.resting = self.submit_exit(
                instrument_id, price=round(float(tick.ask_price) + 0.03, 2)
            )
        elif self.seen == 29:
            self.emulated = self.order_factory.stop_market(
                instrument_id,
                OrderSide.BUY,
                Quantity.from_int(1),
                Price(round(float(tick.ask_price) * 2.0, 2), 2),
                emulation_trigger=TriggerType.BID_ASK,
            )
            self.submit_order(self.emulated)
        elif self.seen == 30:
            self.cancel_orders([self.resting, self.emulated])
            self.submit_exit(instrument_id)
'''
)

BRACKET_UNFILLED = (
    HEAD
    + b'''
from nautilus_trader.model.enums import OrderSide, OrderType, TriggerType
from nautilus_trader.model.objects import Price, Quantity


class Strategy(KansoStrategy):
    """Buys once; on its fifth quote sends a bracket to buy ten more with its entry far below
    the market, so its stop-loss and take-profit, on the closing side, never become live; on
    its thirtieth exits at market once, and never asks again."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        elif self.seen == 5:
            bid = float(tick.bid_price)
            self.submit_order_list(
                self.order_factory.bracket(
                    instrument_id,
                    OrderSide.BUY,
                    Quantity.from_int(10),
                    entry_order_type=OrderType.LIMIT,
                    entry_price=Price(round(bid * 0.5, 2), 2),
                    sl_trigger_price=Price(round(bid * 0.4, 2), 2),
                    tp_price=Price(round(bid * 2.0, 2), 2),
                    EMULATION
                )
            )
        elif self.seen == 30:
            self.submit_exit(instrument_id)
'''
)

BRACKETS = {"plain": b"", "emulated": b"emulation_trigger=TriggerType.BID_ASK,"}
"""A bracket the venue holds, whose children it keeps back until the entry fills, and one
the engine's order emulator holds, whose children it keeps unsent."""

MODIFIED_OPEN = CANCELLED_OPEN.replace(b"PRICE", b"round(float(tick.ask_price) + 1.0, 2)").replace(
    b"CANCEL",
    b"from nautilus_trader.model.objects import Price; "
    b"self.modify_order(order, price=Price(round(float(tick.bid_price) - 0.05, 2), 2))",
)
"""CANCELLED_OPEN with the exit the venue holds open modified to a marketable price, rather
than cancelled, in the handler that then exits at market."""

MODIFIED_EVERY_QUOTE = (
    HEAD
    + b'''
from nautilus_trader.model.objects import Price


class Strategy(KansoStrategy):
    """Buys once, rests an exit a dollar above the ask on its fifth quote, and from then on
    modifies it on every quote, a cent up or down, so it never becomes marketable and is
    pending update whenever the handler ends; on its thirtieth, having modified it, exits at
    market."""

    config_cls = Config

    def on_start(self):
        self.seen = 0
        self.resting = None

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        elif self.seen == 5:
            self.resting = self.submit_exit(
                instrument_id, price=round(float(tick.ask_price) + 1.0, 2)
            )
        elif self.seen > 5:
            order = self.resting
            if order is not None and order.is_open and not order.is_pending_cancel:
                step = 0.01 if self.seen % 2 else -0.01
                self.modify_order(order, price=Price(round(float(order.price) + step, 2), 2))
            if self.seen == 30:
                self.submit_exit(instrument_id)
'''
)
"""A sleeve whose own modify of its resting exit is in flight at every point after its
handler, so an exit at market owed behind that exit never finds it settled."""

IN_FLIGHT_BESIDE_MARKET = (
    HEAD
    + b'''

class Strategy(KansoStrategy):
    """Buys once, and on its thirtieth quote sends an exit a dollar above the ask and then,
    in the same handler, an exit at market, which the first, still on its way to the venue,
    cuts to nothing."""

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        elif self.seen == 30:
            self.submit_exit(instrument_id, price=round(float(tick.ask_price) + 1.0, 2))
            self.submit_exit(instrument_id)
'''
)
"""A sleeve whose exit at market is owed behind an exit of its own still in flight."""

UNSETTLED = {"modified": MODIFIED_EVERY_QUOTE, "in_flight": IN_FLIGHT_BESIDE_MARKET}
"""The two sleeves whose exit at market waits on an order the venue has not settled."""


def exiting_on(source: bytes, quote: int) -> bytes:
    """One of UNSETTLED, exiting at market on its `quote`-th quote rather than its thirtieth."""
    return source.replace(b"self.seen == 30", f"self.seen == {quote}".encode())


def last_quotes(sessions: tuple[date, date] = SESSIONS) -> dict[str, tuple[int, int]]:
    """The count of the first session's last quote and of the window's last, each with the
    instant it became available."""
    quotes = quotes_only(points(sessions))[0]
    first = sum(1 for quote in quotes if int(quote.ts_init) < midnight_ns(sessions[1]))
    return {
        "session": (first, int(quotes[first - 1].ts_init)),
        "window": (len(quotes), int(quotes[-1].ts_init)),
    }


BOOK_OPEN_NS = 14 * 3_600 * 1_000_000_000
"""Where the book below opens, from midnight UTC of its session."""


def book(day: date) -> list[OrderBookDelta]:
    """A book of 500 bid at 10.00 and 500 offered at 10.02, then a change to the offer's size
    every second for eighty seconds, so a sleeve that holds only the book has a point to act
    on each second."""
    base = midnight_ns(day) + BOOK_OPEN_NS
    ident = InstrumentId(Symbol(SYMBOL), _venue())
    changes = [
        make_delta(ident, BookAction.ADD, OrderSide.BUY, 1_000, 500, 1, 2, 0, base, base),
        make_delta(ident, BookAction.ADD, OrderSide.SELL, 1_002, 500, 2, 2, 0, base, base),
    ]
    for second in range(1, 80):
        ts = base + second * 1_000_000_000
        changes.append(
            make_delta(
                ident, BookAction.UPDATE, OrderSide.SELL, 1_002, 500 + second, 2, 2, 0, ts, ts
            )
        )
    return changes


def taken_back(cancel: str, *, priced: bool = False) -> bytes:
    """MARKET_AFTER_CANCEL, which on its thirty-first quote cancels its exits again with
    `cancel`; with `priced` its exit on the thirtieth is a limit below the bid, one the
    market would take at once, rather than an order at market."""
    source = MARKET_AFTER_CANCEL
    if priced:
        source = source.replace(
            b"            self.submit_exit(instrument_id)\n",
            b"            self.submit_exit(instrument_id, "
            b"price=round(float(tick.bid_price) - 0.05, 2))\n",
        )
    calls = {
        "cancel_all_orders": "self.cancel_all_orders(instrument_id)",
        "cancel_orders": "self.cancel_orders(self.cache.orders(strategy_id=self.id)[1:2])",
        "cancel_order": "self.cancel_order(self.cache.orders(strategy_id=self.id)[1])",
    }
    return (
        source + ("\n        elif self.seen == 31:\n            " + calls[cancel] + "\n").encode()
    )


def points(
    sessions: tuple[date, date] = SESSIONS, names: tuple[str, ...] = (SYMBOL,)
) -> list[tuple[object, ...]]:
    """Quotes and prints a second apart over ten minutes of each session, from the generator,
    for each of `names`."""
    loader = SyntheticLoader()
    spec = {
        "seed": 7,
        "instruments": list(names),
        "venue": VENUE,
        "resolution": "1s",
        "types": ["quote", "trade"],
        "start": sessions[0],
        "end": sessions[1],
        "start_price": 10.0,
        "sigma_bps": 10.0,
        "session_start": "09:30",
        "session_end": "09:40",
    }
    return [tuple(loader.load(ref, sessions)) for ref in loader.discover(spec)]


def chasing_costs(latency_ms: float) -> dict[str, object]:
    """A venue that fills a resting limit the market reaches, after `latency_ms`."""
    return {
        "commission_bps": 0.0,
        "slippage_bps": 0.0,
        "spread": "fixed_bps",
        "fixed_bps": 1.0,
        "limit_fill": "touch",
        "latency_ms": latency_ms,
    }


def never_short(fills: list[object]) -> tuple[float, float, float]:
    """What the run bought, what it sold, and the lowest position it reached at any fill."""
    position, lowest, bought, sold = 0.0, 0.0, 0.0, 0.0
    for fill in fills:
        signed = fill.qty if fill.side == "BUY" else -fill.qty  # type: ignore[attr-defined]
        position += signed
        lowest = min(lowest, position)
        bought += max(signed, 0.0)
        sold += max(-signed, 0.0)
    return bought, sold, lowest


def quotes_only(groups: list[tuple[object, ...]]) -> list[tuple[object, ...]]:
    """The quote groups of `points`, without the prints."""
    return [group for group in groups if type(group[0]).__name__ == "QuoteTick"]


def _run(
    request_for,
    latency_ms: float,
    source: bytes = CHASING,
    modifiers=(),
    *,
    quotes: bool = False,
    names: tuple[str, ...] = (SYMBOL,),
):
    document = hypothesis().model_dump(mode="json")
    requirements = ["quote"] if quotes else ["quote", "trade"]
    document.update(resolution="tick", data_requirements=requirements)
    document["universe"] = [f"{name}.{VENUE}" for name in names]
    document["costs"] = chasing_costs(latency_ms)
    hyp = Hypothesis.model_validate(document)
    request = request_for(RESEARCH, source=source, hypothesis_=hyp, modifiers=modifiers)
    groups = points(names=names)
    if quotes:
        groups = quotes_only(groups)
    result = execute(request, [instrument(name) for name in names], groups)
    assert not result.crashed, result.traceback_tail
    return result.run


def flat_within_the_session(run) -> None:
    """Bought 100, sold exactly that, never short at any fill, and never held overnight."""
    assert never_short(run.fills) == (100.0, 100.0, 0.0), never_short(run.fills)
    held = max_hold.evaluate(context(run, params={"days": 1})).evidence
    assert held["longest_days"] < 0.01, held


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_an_exit_that_follows_the_ask_never_sells_what_it_did_not_buy(
    request_for, latency_ms: float
) -> None:
    """Bought once, sold no more than that, never short at any fill, and the hold is the
    strategy's own — seconds, not the night a stranded short would be carried across."""
    run = _run(request_for, latency_ms)
    bought, sold, lowest = never_short(run.fills)
    assert bought == 100.0
    assert sold <= bought, f"sold {sold:g} of the {bought:g} bought"
    assert lowest >= 0.0, f"the position reached {lowest:g}"
    held = max_hold.evaluate(context(run, params={"days": 1})).evidence
    assert held["longest_days"] < 0.01, held


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_an_exit_sent_once_after_a_cancel_still_closes_the_position(
    request_for, latency_ms: float
) -> None:
    """The exit that rested far above is cancelled and a market exit is asked for once.
    Under a latency the cancel is still in flight when it is asked for, so the old exit
    could still take the whole position and the market exit is sized to nothing; what it
    held back is owed and goes at market once the cancel has landed. This guards the
    sizing, not origin/main: an exit sized to nothing and then dropped would never sell and
    would hold the 100 shares overnight. origin/main passes, because it did not count
    working exits at all."""
    flat_within_the_session(_run(request_for, latency_ms, MARKET_AFTER_CANCEL))


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_a_stop_at_market_cancels_the_take_profit_it_would_otherwise_wait_on(
    request_for, latency_ms: float
) -> None:
    """A market exit the sleeve's own resting take-profit would cut is not held back by it:
    the take-profit is cancelled and the stop takes the position. At 0 ms this guards the
    sizing, not origin/main: a stop that counted the take-profit would be sized to nothing
    and the sleeve would hold overnight, and origin/main passes. At 20 ms origin/main fails:
    it counted no working exit, each stop in flight was joined by another, and the sleeve
    sold 200 of the 100 it had bought, then bought 200 and held 100 across the night."""
    run = _run(request_for, latency_ms, STOP_OVER_TAKE_PROFIT)
    flat_within_the_session(run)
    assert [(fill.side, fill.qty) for fill in run.fills] == [("BUY", 100.0), ("SELL", 100.0)]


STOP_ASKED_TWICE = STOP_OVER_TAKE_PROFIT.replace(
    b"        elif 30 <= self.seen < 35:\n            self.submit_exit(instrument_id)\n",
    b"        elif self.seen == 30:\n"
    b"            self.submit_exit(instrument_id)\n"
    b"            self.submit_exit(instrument_id)\n",
)
"""STOP_OVER_TAKE_PROFIT asking for its stop twice in one handler, so the second call finds
the first stop still working."""


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_a_stop_asked_for_again_keeps_the_stop_already_working(
    request_for, latency_ms: float
) -> None:
    """A second exit at market asked for in the handler that sent the first leaves the first
    alone — a market order is the venue's to fill, and is never cancelled to make room — and
    adds nothing to it: the take-profit is cancelled and the position is sold once. With no
    latency the take-profit's cancel is spent when sent, so the first ask sends the stop and
    the second finds it working; a quote reaches the sleeve only once the venue has settled
    at its instant, so a stop sent on one quote has landed by the next, and only a second
    ask in the same handler meets it. Under a latency the first ask is owed until the
    cancel lands, and the second owes nothing more."""
    assert STOP_ASKED_TWICE != STOP_OVER_TAKE_PROFIT
    run = _run(request_for, latency_ms, STOP_ASKED_TWICE)
    flat_within_the_session(run)
    assert [(fill.side, fill.qty) for fill in run.fills] == [("BUY", 100.0), ("SELL", 100.0)]


@pytest.mark.parametrize("cancel", ["cancel_all_orders", "cancel_orders", "cancel_order"])
def test_an_owed_exit_at_a_price_is_forgotten_once_the_sleeve_cancels_its_exits_again(
    request_for, cancel: str
) -> None:
    """A sleeve that cancels on the closing side after asking for an exit at a price has
    taken it back, as the cancel would have taken back the limit order itself, so the exit
    its cancel in flight held back is never sent and the position is kept."""
    run = _run(request_for, 20.0, taken_back(cancel, priced=True))
    assert [(fill.side, fill.qty) for fill in run.fills] == [("BUY", 100.0)]


@pytest.mark.parametrize("cancel", ["cancel_all_orders", "cancel_orders", "cancel_order"])
def test_an_owed_exit_at_market_outlives_the_sleeve_s_later_cancels(
    request_for, cancel: str
) -> None:
    """A market order is taken by the venue before any cancel that follows it, so the
    exit at market a cancel in flight held back is not taken back by a cancel the sleeve
    sends after it: it goes once the first cancel has landed, and the sleeve ends flat.
    This guards the owed exit, not origin/main: a later cancel that threw it away would
    hold the 100 shares overnight, where the market order sent at once would have sold
    them. origin/main passes, because it owed nothing and sent the exit whole."""
    flat_within_the_session(_run(request_for, 20.0, taken_back(cancel)))


@pytest.mark.parametrize("latency_ms", [0.0, 20.0, 1500.0])
def test_housekeeping_cancels_do_not_strand_an_exit_at_market(
    request_for, latency_ms: float
) -> None:
    """The sleeve cancels its take-profit and exits at market once, then cancels every order
    it has on every quote after. This guards the owed exit, not origin/main: were the first
    of those cancels to throw it away, the position would be held overnight at every latency
    but none. origin/main passes, because it owed nothing and sent the exit whole."""
    source = HOUSEKEEPING.replace(
        b"        elif self.seen >= 30:\n            self.cancel_all_orders(instrument_id)\n",
        b"        elif self.seen >= 30:\n            self.cancel_all_orders(instrument_id)\n"
        b"            if self.seen == 30:\n                self.submit_exit(instrument_id)\n",
    )
    assert source != HOUSEKEEPING
    flat_within_the_session(_run(request_for, latency_ms, source))


@pytest.mark.parametrize("latency_ms", [0.0, 20.0, 1500.0])
@pytest.mark.parametrize("gap", [0, 1])
def test_a_stop_sent_behind_a_take_profit_in_flight_still_closes(
    request_for, gap: int, latency_ms: float
) -> None:
    """A take-profit far above the market still in flight to the venue is not cancelled and
    counts, so the stop sent behind it is cut to nothing; what it cuts is owed, and once the
    venue holds the take-profit open the owed stop cancels it and takes the position.
    This guards the sizing, not origin/main: an exit that counted the take-profit in
    flight and owed nothing would never send the stop again and would hold the 100 shares
    overnight. origin/main passes, because it did not count working orders at all."""
    run = _run(request_for, latency_ms, tp_in_flight_then_stop(gap))
    flat_within_the_session(run)
    assert [(fill.side, fill.qty) for fill in run.fills] == [("BUY", 100.0), ("SELL", 100.0)]


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_an_exit_rule_that_says_so_once_closes_whatever_its_host_cancels(
    request_for, latency_ms: float
) -> None:
    """The rule says exit once, on the thirtieth quote, while the host's take-profit is
    waiting on the cancel the host sent; the host goes on cancelling everything on every
    quote. The exit the cancel in flight held back is the rule's, so the host's cancels do
    not take it back, and it goes once the cancel has landed. This guards the sizing, not
    origin/main: a rule's exit sized to nothing and never owed would hold the position
    overnight. origin/main passes, because it did not count working exits at all."""
    quotes = [point for point in points()[0] if type(point).__name__ == "QuoteTick"]
    rule = (("exit", exit_once_from(quotes[29].ts_event), {}),)
    flat_within_the_session(_run(request_for, latency_ms, HOUSEKEEPING, rule))


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
@pytest.mark.parametrize(
    "cancel",
    [
        "self.cancel_order(order)",
        "self.cancel_orders([order])",
        "self.cancel_all_orders(instrument_id)",
    ],
)
def test_an_exit_cancelled_in_flight_still_counts_until_its_cancel_lands(
    request_for, cancel: str, latency_ms: float
) -> None:
    """The venue takes an order before the cancel that follows it, so a marketable exit
    cancelled while still in flight fills, whatever the latency. The market exit sent
    beside it is held back and owed; the old exit's fill leaves the position flat, the owed
    exit is dropped there, and the short the sleeve opens later is its own. Measured on
    origin/main: at 0 ms and at 20 ms, with the cancelled order read as spent, both exits
    filled and the sleeve was short 100 before it ever asked to be."""
    run = _run(request_for, latency_ms, CANCELLED_IN_FLIGHT.replace(b"CANCEL", cancel.encode()))
    assert [(fill.side, fill.qty) for fill in run.fills] == [
        ("BUY", 100.0),
        ("SELL", 100.0),
        ("SELL", 100.0),
    ]
    assert never_short(run.fills[:2]) == (100.0, 100.0, 0.0)


MODIFIED = (
    "from nautilus_trader.model.objects import Price; "
    "self.modify_order(order, price=Price(round(float(tick.bid_price) - 0.04, 2), 2))"
)
"""A modify of the marketable exit, still marketable, sent while it is in flight."""

CANCELLED_TWICE = [
    "self.cancel_order(order); self.cancel_order(order)",
    "self.cancel_order(order); self.cancel_all_orders(instrument_id)",
    "self.cancel_orders([order]); self.cancel_order(order)",
    MODIFIED + "; self.cancel_order(order)",
]
"""An exit still in flight cancelled twice in the handler that sent it, or modified and
then cancelled: the first command leaves it pending cancel or pending update, which the
engine reports as open although the venue never took it."""


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
@pytest.mark.parametrize("cancels", CANCELLED_TWICE)
def test_an_exit_cancelled_twice_in_flight_still_counts_until_its_cancel_lands(
    request_for, cancels: str, latency_ms: float
) -> None:
    """A second cancel, or a modify before the cancel, does not make an order the venue has
    not taken yet into one it held open, so the marketable exit still counts, the market
    exit beside it is cut and owed, and the sleeve is never short before it asks to be.
    Measured on the round before this test: at 0 ms the second cancel read the
    pending-cancel order as held open and spent, the market exit went at full size, both
    filled and the sleeve was short 100; and on the round after it, the cancel read the
    pending-update order the same way, with the same result."""
    run = _run(request_for, latency_ms, CANCELLED_IN_FLIGHT.replace(b"CANCEL", cancels.encode()))
    assert [(fill.side, fill.qty) for fill in run.fills] == [
        ("BUY", 100.0),
        ("SELL", 100.0),
        ("SELL", 100.0),
    ]
    assert never_short(run.fills[:2]) == (100.0, 100.0, 0.0)


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_an_exit_modified_in_flight_is_not_cancelled_by_an_exit_at_market(
    request_for, latency_ms: float
) -> None:
    """A marketable exit modified in the handler that sent it is pending update, which the
    engine reports as open although the venue never took it; the exit at market sent
    beside it must not read it as resting, cancel it and count it spent. It counts, the
    market exit is cut and owed, and the sleeve is never short before it asks to be.
    Measured on the round before this test: at 0 ms the market exit cancelled the modified
    one, sent itself whole, both filled and the backtest sold 100 more than it held, where
    the node, whose order was still unsent, did not."""
    run = _run(request_for, latency_ms, CANCELLED_IN_FLIGHT.replace(b"CANCEL", MODIFIED.encode()))
    assert [(fill.side, fill.qty) for fill in run.fills] == [
        ("BUY", 100.0),
        ("SELL", 100.0),
        ("SELL", 100.0),
    ]
    assert never_short(run.fills[:2]) == (100.0, 100.0, 0.0)


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_an_exit_at_market_cancels_a_stop_the_order_emulator_holds(
    request_for, latency_ms: float
) -> None:
    """A stop held by the engine's order emulator has not reached the venue, and the engine
    reports it as not open; an exit at market cancels it with the sleeve's resting orders,
    and the emulator takes it out at once, so the exit goes whole and the sleeve is flat.
    Measured on the round before this test: the stop counted as working and was never
    cancelled, so it cut the exit to nothing on every point and the 100 shares were held to
    the end of the window."""
    run = _run(request_for, latency_ms, EMULATED_STOP)
    assert [(fill.side, fill.qty) for fill in run.fills] == [("BUY", 100.0), ("SELL", 100.0)]


@pytest.mark.parametrize("latency_ms", [0.0, 20.0, 2500.0])
def test_an_exit_at_market_leaves_an_order_whose_modify_is_in_flight_to_it(
    request_for, latency_ms: float
) -> None:
    """An exit the venue holds open, modified to a marketable price in the handler that then
    exits at market, is pending update until the venue answers the modify; the exit at
    market must not cancel it, which on a node would overtake the modify, nor count it
    spent. It counts, the market exit is cut to nothing and owed, the modify fills it, and
    the sleeve is flat. At 2500 ms, longer than the second between points, the venue has not
    taken the exit yet when it is modified; the cancel goes out as it takes it, on the next
    point, with the modify still unanswered, and the modify, stamped first, still lands
    first. Measured on the round before this test: at 0 ms the exit at market cancelled the
    order it read as held open and counted it spent, went whole, and the modify filled as
    well, leaving the backtest short 100; and on origin/main at 2500 ms the run went short
    100."""
    run = _run(request_for, latency_ms, MODIFIED_OPEN)
    assert never_short(run.fills) == (100.0, 100.0, 0.0), run.fills


@pytest.mark.parametrize("latency_ms", [0.0, 20.0, 2500.0])
def test_an_exit_at_market_is_paid_behind_a_modify_the_sleeve_sends_on_every_point(
    request_for, latency_ms: float
) -> None:
    """A sleeve that modifies its resting exit on every quote leaves it pending update at
    the end of every handler, and the owed exit is asked for again only then. The exit at
    market holds its cancel back — at 0 ms until the venue answers the modify, under a
    latency until the next quote, before the handler, answered or not (at 20 ms and 2500 ms
    alike it is not) — so it lands behind the modify; the order is cancelled, the owed exit
    is paid, and the sleeve is flat within the session.
    Measured on the round before this test: the exit at market neither cancelled the order
    nor counted it spent, so it was cut to nothing and owed at every quote, never paid, and
    the position was held to the end of the window."""
    run = _run(request_for, latency_ms, MODIFIED_EVERY_QUOTE, quotes=True)
    assert [(fill.side, fill.qty) for fill in run.fills] == [("BUY", 100.0), ("SELL", 100.0)]
    flat_within_the_session(run)


def seen_at_send(answered: bool) -> bytes:
    """What MODIFIED_EVERY_QUOTE adds to fail the run unless, whenever a cancel held behind
    its modify is sent, the order reads `ACCEPTED` with the latest modify answered or not as
    `answered` says, and unless one was sent at all."""
    return b"""
    def modify_order(self, order, quantity=None, price=None, *args, **kwargs):
        self.asked = price
        super().modify_order(order, quantity, price, *args, **kwargs)

    def _send_behind_modify(self):
        for held in self._behind_modify.values():
            current = self._current(held)
            seen = (current.status_string(), current.price == self.asked)
            assert seen == ("ACCEPTED", %s), seen
            self.checked = True
        super()._send_behind_modify()

    def on_stop(self):
        assert getattr(self, "checked", False), "no cancel was held behind a modify"
""" % str(answered).encode()


@pytest.mark.parametrize("last", ["session", "window"])
@pytest.mark.parametrize("unsettled", sorted(UNSETTLED))
def test_an_exit_at_market_asked_for_on_a_last_point_is_paid_on_it(
    request_for, unsettled: str, last: str
) -> None:
    """With no latency stated, an exit at market cut by an order the venue has not settled —
    one whose modify it has not answered, or one still on its way to it — is paid as the
    venue answers that order, in the instant it was asked for. Asked for on a session's last
    quote the sleeve is not carried overnight, and on the window's last it is not held to
    the end. Measured on the round before this test: the owed exit was paid on the next
    point, the next session's first quote, and at the window's last quote never."""
    quote, instant = last_quotes()[last]
    run = _run(request_for, 0.0, exiting_on(UNSETTLED[unsettled], quote), quotes=True)
    assert [(fill.side, fill.qty) for fill in run.fills] == [("BUY", 100.0), ("SELL", 100.0)]
    assert run.fills[1].ts_ns == instant


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
@pytest.mark.parametrize("bracket", sorted(BRACKETS))
def test_an_exit_at_market_does_not_count_the_exits_of_a_bracket_whose_entry_has_not_filled(
    request_for, bracket: str, latency_ms: float
) -> None:
    """The stop-loss and take-profit of a bracket can fill only once its entry has, and then
    close what the entry opened, so while the entry has filled nothing they leave the position
    as it is and do not count. The exit at market takes the whole position. Measured on the
    round before this test: both legs counted and were never cancelled, since neither the
    venue nor the emulator had them open, so the exit sold 80 of 100 and the 20 it owed were
    held to the end of the window."""
    source = BRACKET_UNFILLED.replace(b"EMULATION", BRACKETS[bracket])
    run = _run(request_for, latency_ms, source)
    assert [(fill.side, fill.qty) for fill in run.fills] == [("BUY", 100.0), ("SELL", 100.0)]


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_a_batch_of_cancels_with_an_emulated_order_in_it_cancels_every_one(
    request_for, latency_ms: float
) -> None:
    """The engine's own `cancel_orders` sends nothing for a batch with an emulated order
    after its first, having already marked the first pending cancel; kanso cancels an
    emulated order on its own, through the emulator, and batches the rest, so the resting
    exit is cancelled and the market exit beside it never goes past flat. Measured on the
    round before this test: at 0 ms the resting exit read as cancelled, the market exit
    went at full size, both filled and the sleeve was short 100."""
    run = _run(request_for, latency_ms, BATCH_WITH_EMULATED)
    assert never_short(run.fills) == (100.0, 100.0, 0.0), run.fills


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_an_exit_owed_to_a_sleeve_that_holds_only_the_book_is_still_paid(
    request_for, latency_ms: float
) -> None:
    """A book change reaches the author's `on_order_book_deltas` and no other handler, so
    the owed exit must be asked for again after that handler too. Under a latency the
    market exit asked for once after the cancel is sized to nothing and owed; it goes out
    on a later book change and the sleeve is flat within the session. This guards the owed
    exit on book points: paid only after bars, quotes, trades and custom data, it was never
    sent, and the 100 shares were held to the end of the window. origin/main passes,
    because it did not count working exits at all."""
    document = hypothesis().model_dump(mode="json")
    document.update(resolution="tick", data_requirements=["book"])
    document["costs"] = chasing_costs(latency_ms)
    hyp = Hypothesis.model_validate(document)
    day = RESEARCH[0]
    request = request_for(RESEARCH, source=BOOK_ONLY, hypothesis_=hyp)
    result = execute(request, [instrument()], [tuple(book(day))])
    assert not result.crashed, result.traceback_tail
    fills = result.run.fills
    assert [(fill.side, fill.qty) for fill in fills] == [("BUY", 100.0), ("SELL", 100.0)]
    base = midnight_ns(day) + BOOK_OPEN_NS
    assert all(fill.ts_ns - base < 80 * 1_000_000_000 for fill in fills)


CANCELS = [
    "self.cancel_order(order)",
    "self.cancel_orders([order])",
    "self.cancel_all_orders(instrument_id)",
]
"""The three ways a sleeve cancels one of its own orders."""


@pytest.mark.parametrize("cancel", CANCELS)
def test_an_order_cancelled_in_the_handler_that_sent_it_never_rests(
    request_for, cancel: str
) -> None:
    """With no latency stated the backtest's venue takes the order and then the cancel that
    followed it, before it matches anything further, so an order that does not fill when it
    is taken is cancelled there and never rests through a point: the sleeve buys and sells
    nothing else. Measured on the round that held such a cancel back to the next point:
    each one-share exit rested through a quote, and the sleeve sold all 100 shares in 100
    fills that no venue would have given it."""
    run = _run(request_for, 0.0, SENT_AND_CANCELLED.replace(b"CANCEL", cancel.encode()))
    assert [(fill.side, fill.qty) for fill in run.fills] == [("BUY", 100.0)]


OTHER = "OTHR"
"""A second name, for a sleeve owed an exit on each of two."""

TWO_NAMES_IN_FLIGHT = (
    HEAD
    + b'''
from nautilus_trader.model.enums import OrderType


class Strategy(KansoStrategy):
    """Buys each of its two names once, on that name's first quote; on the first name's
    thirtieth quote sends, for each name in turn, an exit a dollar above the ask and then an
    exit at market, which the first, still on its way to the venue, cuts to nothing. It
    notes, for every exit asked for again as the venue answers an order, whether that name's
    own orders on the closing side had all closed by then, and fails the run at the end
    unless each name's was asked for once, and only once they had."""

    config_cls = Config

    def on_start(self):
        self.seen = 0
        self.bought = set()
        self.answering = None
        self.paid = []

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        if instrument_id not in self.bought:
            self.bought.add(instrument_id)
            self.submit_entry(instrument_id, "BUY", qty=100)
        if instrument_id != self.universe[0]:
            return
        self.seen += 1
        if self.seen == 30:
            for name in self.universe:
                ask = float(self.cache.quote_tick(name).ask_price)
                self.submit_exit(name, price=round(ask + 1.0, 2))
                self.submit_exit(name)

    def _pay_owed(self, only=None):
        answering, self.answering = self.answering, only
        try:
            super()._pay_owed(only)
        finally:
            self.answering = answering

    def submit_exit(self, instrument_id, **kwargs):
        if self.answering is not None:
            waiting = [
                order
                for order in self.cache.orders(strategy_id=self.id)
                if str(order.instrument_id) == str(instrument_id)
                and order.order_type != OrderType.MARKET
                and not order.is_closed
            ]
            self.paid.append((str(instrument_id), not waiting))
        return super().submit_exit(instrument_id, **kwargs)

    def on_stop(self):
        names = sorted(str(name) for name in self.universe)
        assert sorted(self.paid) == [(name, True) for name in names], self.paid
'''
)
"""A sleeve owed an exit at market on each of two names at once, each behind an exit of its
own still in flight."""


def test_an_exit_owed_on_one_name_waits_for_that_name_s_own_answer(request_for) -> None:
    """With no latency stated, an exit at market owed behind an order is asked for again as
    the venue answers that order. Owed on two names at once, the answer that closes the
    first name's order asks for the first name's exit alone: the second's still waits on an
    order the venue has taken but not yet cancelled, and is asked for as that order closes.
    Both are paid in the instant they were asked for, and the sleeve is flat in each name."""
    quotes = quotes_only(points(names=(SYMBOL, OTHER)))[0]
    asked = int(quotes[29].ts_init)
    run = _run(request_for, 0.0, TWO_NAMES_IN_FLIGHT, quotes=True, names=(SYMBOL, OTHER))
    sells = sorted(
        (fill.instrument_id, fill.qty, fill.ts_ns) for fill in run.fills if fill.side == "SELL"
    )
    assert sells == [(f"{name}.{VENUE}", 100.0, asked) for name in sorted((SYMBOL, OTHER))]
    assert sorted((fill.instrument_id, fill.qty) for fill in run.fills if fill.side == "BUY") == [
        (f"{name}.{VENUE}", 100.0) for name in sorted((SYMBOL, OTHER))
    ]


TAKEN_BACK_AT_A_PRICE = (
    HEAD
    + b'''

class Strategy(KansoStrategy):
    """Buys once; on its thirtieth quote sends an exit a dollar above the ask, then an exit at
    market, which the first, still on its way to the venue, cuts to nothing, and then takes
    that back with an exit two dollars above the ask, which the first cuts to nothing too.
    On its thirty-first it fails the run unless the first still rests, uncancelled, and
    nothing waits on the venue's answer to it."""

    config_cls = Config

    def on_start(self):
        self.seen = 0
        self.resting = None
        self.checked = False

    def on_quote_tick(self, tick):
        instrument_id = tick.instrument_id
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(instrument_id, "BUY", qty=100)
        elif self.seen == 30:
            ask = float(tick.ask_price)
            self.resting = self.submit_exit(instrument_id, price=round(ask + 1.0, 2))
            self.submit_exit(instrument_id)
            self.submit_exit(instrument_id, price=round(ask + 2.0, 2))
        elif self.seen == 31:
            order = self.cache.order(self.resting.client_order_id)
            assert order.status_string() == "ACCEPTED", order
            assert not self._awaiting, self._awaiting
            self.checked = True

    def on_stop(self):
        assert self.checked, "the thirty-first quote never came"
'''
)
"""A sleeve that takes back an exit at market owed behind its own exit in flight, with an
exit at a price, before the venue answers."""


def test_an_order_is_not_cancelled_for_an_exit_at_market_taken_back_before_its_answer(
    request_for,
) -> None:
    """An exit at market owed behind an order in flight cancels that order once the venue
    has taken it. When the sleeve has taken the exit at market back by the time the venue
    answers — here with an exit at a price, which owes nothing — the answer leaves the order
    resting where the sleeve put it, stops waiting on it, and nothing is sold."""
    run = _run(request_for, 0.0, TAKEN_BACK_AT_A_PRICE, quotes=True)
    assert [(fill.side, fill.qty) for fill in run.fills] == [("BUY", 100.0)]
