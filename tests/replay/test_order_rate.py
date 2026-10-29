"""An account's order rate, held on the data clock, on both code paths alike.

A sleeve under a venue model that states `costs.orders_per_minute` takes a token for every
command it sends from a bucket of that many, refilled at that many a minute of the data
clock — a cancel as many as `requests_per_cancel` says. With no token available to it an
entry, an entry's list and a modify are dropped unsent and counted as refused, and the run
goes on; an exit `submit_exit` sized is owed and a cancel deferred, no entry or modify taking
a token while a cancel waits, where an exit takes one the bucket holds; an exit the strategy
built itself is refused. The second leg of a flip is an entry there — `submit_entry`'s in every
form, and one built by hand behind an exit at market the rate owes, for want of a token or
behind a cancel it deferred, funded on what that exit frees — a deferred cancel whose order has
closed waits for nothing, and a dropped order is nothing to cancel or to move.
Each scenario here runs on the research path (`backtest.execute`) and on the live one
(`session.run_node`) and asserts both the behaviour and that the two agree on every intent,
fill and count, the refused ones included. With neither key stated each path does what it did
before the keys existed, a cancel of an order the engine was never handed included, where the
two paths part as they did, and that is pinned on each.

The quotes are a second apart, so a rate of `n` a minute refills a token every `60 / n`
quotes, and every instant asserted below is arithmetic on that: the bucket keeps its level
in token-nanoseconds, so the first quote that finds a whole token is exact.
"""

from __future__ import annotations

import asyncio
import contextlib
import pickle
from dataclasses import replace
from typing import Any

import pytest
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.nautilus import backtest, node, session
from kanso.nautilus.backtest import RunRequest, RunResult
from kanso.nautilus.sizing import ORDER_RATE, Refusal, SizingError
from kanso.schemas import Commands
from tests.replay.conftest import (
    FORWARD,
    SECOND_NS,
    SYMBOL,
    VENUE,
    hypothesis,
    instrument,
    request_for,
)

T0 = midnight_ns(FORWARD[0]) + 14 * 3_600 * SECOND_NS
"""The first quote's instant; the kth quote a sleeve sees is `k - 1` seconds after it."""


def at(seen: int) -> int:
    """The instant of the quote a sleeve sees `seen`th."""
    return T0 + (seen - 1) * SECOND_NS


NAME = InstrumentId(Symbol(SYMBOL), Venue(VENUE))


def quoted(
    count: int,
    levels: dict[int, tuple[float, float]] | None = None,
    *,
    first: int = T0,
    gap: int = SECOND_NS,
    name: InstrumentId = NAME,
) -> list[QuoteTick]:
    """`count` quotes of `name`, `gap` apart from `first` — a second unless given — 1,000 a
    side, at 9.99/10.01 and, from each `levels` key on, at its bid and ask."""
    made: list[QuoteTick] = []
    bid, ask = 9.99, 10.01
    for seen in range(1, count + 1):
        bid, ask = (levels or {}).get(seen, (bid, ask))
        instant = first + (seen - 1) * gap
        made.append(
            QuoteTick(
                name,
                Price(bid, 2),
                Price(ask, 2),
                Quantity.from_int(1_000),
                Quantity.from_int(1_000),
                ts_event=instant,
                ts_init=instant,
            )
        )
    return made


def steady(count: int, *, rise_at: int | None = None) -> list[QuoteTick]:
    """`count` quotes a second apart at 9.99/10.01, 1,000 a side; from the `rise_at`th on,
    10.05/10.07."""
    return quoted(count, None if rise_at is None else {rise_at: (10.05, 10.07)})


def printed(seen: range, price: float = 10.0) -> list[TradeTick]:
    """A print of 100 at `price` at each instant a sleeve sees `seen`."""
    return [
        TradeTick(
            NAME,
            Price(price, 2),
            Quantity.from_int(100),
            AggressorSide.BUYER,
            TradeId(str(k)),
            ts_event=at(k),
            ts_init=at(k),
        )
        for k in seen
    ]


HYP = hypothesis(
    resolution="tick",
    horizon="1d",
    data_requirements=["quote", "trade"],
    risk_limits={"max_position_pct": 100, "max_drawdown_pct": 40, "max_leverage": 1},
)


def rated(
    source: bytes,
    per_minute: int | None,
    *,
    latency_ms: float = 0.0,
    per_cancel: int | None = None,
    modifiers: tuple[tuple[str, bytes, dict[str, Any]], ...] = (),
) -> RunRequest:
    """The request both paths run, its venue model stating `per_minute` and `per_cancel` when
    each is given, with any attached `modifiers`."""
    request = request_for(source=source, hyp=HYP, modifiers=modifiers)
    model = dict(request.venue_model)
    costs: dict[str, Any] = {**dict(model["costs"]), "latency_ms": latency_ms}  # type: ignore[call-overload]
    if per_minute is not None:
        costs["orders_per_minute"] = per_minute
    if per_cancel is not None:
        costs["requests_per_cancel"] = per_cancel
    model["costs"] = costs
    return replace(request, venue_model=model)


def both(
    request: RunRequest, groups: list[tuple[object, ...]], symbols: tuple[str, ...] = (SYMBOL,)
) -> RunResult:
    """The request on both code paths, over the instruments `symbols` names, asserted to agree
    on every intent, fill and count."""
    instruments = [instrument(symbol) for symbol in symbols]
    engine = backtest.execute(request, instruments, groups)
    node = session.run_node(request, instruments, groups).result
    assert not node.crashed, node.traceback_tail
    assert node.intents == engine.intents
    assert node.run.fills == engine.run.fills
    assert node.commands == engine.commands
    return engine


def refused(request: RunRequest, groups: list[tuple[object, ...]]) -> Refusal:
    """The refusal the research path raises; the live path stops on the same one."""
    with pytest.raises(SizingError) as raised:
        backtest.execute(request, [instrument()], groups)
    node = session.run_node(request, [instrument()], groups).result
    assert node.crashed
    assert f"sizing: {ORDER_RATE} at {raised.value.refusal.instrument_id}" in str(
        node.traceback_tail
    )
    return raised.value.refusal


def stamps(result: RunResult) -> list[tuple[int, str, str]]:
    """When each intent was sent, its side and its order type."""
    return [(ts, side, kind) for ts, _, side, _, kind, _ in result.intents]


HEAD = b"""
from datetime import UTC, datetime

from nautilus_trader.model.enums import OrderSide, TimeInForce, TriggerType
from nautilus_trader.model.objects import Price, Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0
        self.resting = None

    def on_quote_tick(self, tick):
        self.seen += 1
        name = tick.instrument_id
        if self.seen == 1:
            self.submit_entry(name, "BUY", qty=100)
        elif self.seen == 2:
            SECOND
"""


def sleeve(body: str, *, price: float = 20.0, extra: str = "", second: str | None = None) -> bytes:
    """Buys 100 at market on its first quote and rests an exit of it at `price` on its second,
    or does `second` there instead; `body` is what it does on later quotes, `extra` any
    further method."""
    rest = f"self.resting = self.submit_exit(name, price={price!r})" if second is None else second
    return (
        HEAD.replace(b"SECOND", rest.encode())
        + body.encode()
        + (b"\n" + extra.encode() if extra else b"")
    )


def resting_buy(price: float) -> str:
    """A buy of 50 resting at `price`, built by hand."""
    return (
        "self.submit_order(self.order_factory.limit("
        f"name, OrderSide.BUY, Quantity.from_int(50), Price({price!r}, 2)))"
    )


OWED_AT_MARKET = sleeve(
    """        elif self.seen == 3:
            self.cancel_order(self.resting)
        elif self.seen == 4:
            self.submit_exit(name)
"""
)
"""Three commands on its first three quotes, then an exit at market once, on the fourth."""


def test_an_exit_past_the_rate_is_owed_until_the_bucket_refills_on_both_paths() -> None:
    """At three a minute the three commands empty the bucket to a tenth of a token, and it
    refills a twentieth a second: the exit asked for once on the fourth quote is owed and
    goes out on the first quote that finds a whole token, the twenty-first, twenty seconds of
    data clock after the third command."""
    unrated = both(rated(OWED_AT_MARKET, None), [tuple(steady(40))])
    held = both(rated(OWED_AT_MARKET, 3), [tuple(steady(40))])

    assert stamps(unrated)[-1] == (at(4), "SELL", "MARKET")
    assert stamps(held) == [
        (at(1), "BUY", "MARKET"),
        (at(2), "SELL", "LIMIT"),
        (at(21), "SELL", "MARKET"),
    ]
    assert [(fill.side, fill.qty) for fill in held.run.fills] == [("BUY", 100.0), ("SELL", 100.0)]
    assert held.commands == Commands(submits=3, cancels=1, peak_60s=4, p99_60s=4)


HAND_BUILT = {
    "market": "self.order_factory.market(name, OrderSide.SELL, Quantity.from_int(100))",
    "limit": "self.order_factory.limit(name, OrderSide.SELL, Quantity.from_int(100), "
    "Price(10.03, 2))",
    "day": "self.order_factory.limit(name, OrderSide.SELL, Quantity.from_int(100), "
    "Price(10.03, 2), time_in_force=TimeInForce.DAY)",
    "market_immediate_or_cancel": "self.order_factory.market(name, OrderSide.SELL, "
    "Quantity.from_int(100), time_in_force=TimeInForce.IOC)",
    "past_flat": "self.order_factory.market(name, OrderSide.SELL, Quantity.from_int(150))",
    "stop": "self.order_factory.stop_market(name, OrderSide.SELL, Quantity.from_int(100), "
    "trigger_price=Price(9.0, 2))",
    "immediate_or_cancel": "self.order_factory.limit(name, OrderSide.SELL, "
    "Quantity.from_int(100), Price(10.03, 2), time_in_force=TimeInForce.IOC)",
    "fill_or_kill": "self.order_factory.limit(name, OrderSide.SELL, Quantity.from_int(100), "
    "Price(10.03, 2), time_in_force=TimeInForce.FOK)",
    "good_till_date": "self.order_factory.limit(name, OrderSide.SELL, Quantity.from_int(100), "
    "Price(10.03, 2), time_in_force=TimeInForce.GTD, "
    "expire_time=datetime(2024, 3, 2, tzinfo=UTC))",
    "post_only": "self.order_factory.limit(name, OrderSide.SELL, Quantity.from_int(100), "
    "Price(9.99, 2), post_only=True)",
    "display_qty": "self.order_factory.limit(name, OrderSide.SELL, Quantity.from_int(100), "
    "Price(10.03, 2), display_qty=Quantity.from_int(10))",
    "quote_quantity": "self.order_factory.market(name, OrderSide.SELL, "
    "Quantity.from_int(100), quote_quantity=True)",
    "emulated": "self.order_factory.limit(name, OrderSide.SELL, Quantity.from_int(100), "
    "Price(10.03, 2), emulation_trigger=TriggerType.DEFAULT)",
    "a_leg_of_a_list": "self.order_factory.bracket(name, OrderSide.SELL, "
    "Quantity.from_int(100), sl_trigger_price=Price(11.0, 2), tp_price=Price(9.0, 2)).orders[0]",
}
"""Exits a strategy built itself: at market, at a good-till-cancelled limit, at a limit for the
day, at market immediate-or-cancel, at market past flat; a stop; a limit that would not rest,
would rest only as a maker, would expire or would show part of itself; a size in the quote's
currency; an emulated order; an order of a list."""


def hand_built(order: str) -> bytes:
    """Empties the bucket on its first three quotes, then closes by `order`, built itself."""
    return sleeve(
        f"""        elif self.seen == 3:
            self.cancel_order(self.resting)
        elif self.seen == 4:
            self.submit_order({order})
"""
    )


HAND_BUILT_CLOSE = sleeve(
    """        elif self.seen == 3:
            self.cancel_order(self.resting)
        elif self.seen == 4:
            self.close_position(self.cache.positions_open(strategy_id=self.id)[0])
"""
)
"""The same, closing by the engine's own `close_position`, a market order it builds."""


@pytest.mark.parametrize("built", [*sorted(HAND_BUILT), "close_position"])
def test_an_exit_the_strategy_built_itself_is_refused_past_the_rate(built: str) -> None:
    """kanso owes only an exit `submit_exit` sized. Owed and sent again as `submit_exit` sends
    one, an exit the strategy built would go as an order kanso built in its place: one the
    strategy holds no handle to, so a cancel or a modify of its own order would reach nothing,
    and one cut at flat, so a sale past it would lose what it opened; an immediate-or-cancel
    exit would rest and fill later, a post-only one take, a stop fill at once. So past the
    rate each is refused, whatever its type, its time in force or its size, the engine's own
    `close_position` among them, and the refusal names `submit_exit`, which is owed."""
    source = HAND_BUILT_CLOSE if built == "close_position" else hand_built(HAND_BUILT[built])
    refusal = refused(rated(source, 3), [tuple(steady(10))])

    assert (refusal.rule, refusal.asked, refusal.ts_event) == (ORDER_RATE, "SELL", at(4))
    assert refusal.why.startswith("an exit the strategy built itself needed 1 of the 3 tokens")
    assert "close with submit_exit, which is owed rather than refused" in refusal.why


def deferring(call: str, *, price: float = 20.0) -> bytes:
    """Rests its exit at `price`, cancels it twice on its third quote by `call`, and exits at
    market once the venue has cancelled it."""
    cancel = {
        "cancel_order": "self.cancel_order(self.resting)",
        "cancel_orders": "self.cancel_orders([self.resting])",
        "cancel_all_orders": "self.cancel_all_orders(name)",
    }[call]
    return sleeve(
        f"""        elif self.seen == 3:
            {cancel}
            {cancel}
""",
        price=price,
        extra="""    def on_order_canceled(self, event):
        self.submit_exit(event.instrument_id)
""",
    )


@pytest.mark.parametrize("call", ["cancel_order", "cancel_orders", "cancel_all_orders"])
def test_a_cancel_past_the_rate_is_deferred_until_the_bucket_refills_on_both_paths(
    call: str,
) -> None:
    """At two a minute the entry and the resting exit leave a thirtieth of a token, and it
    refills a thirtieth a second: the cancel asked for twice on the third quote is one cancel,
    deferred and sent on the thirty-first, before that quote's handler. The exit the
    cancellation calls for is owed then, the bucket being empty again, and goes on the
    sixty-first."""
    unrated = both(rated(deferring(call), None), [tuple(steady(70))])
    held = both(rated(deferring(call), 2), [tuple(steady(70))])

    assert stamps(unrated)[-1] == (at(3), "SELL", "MARKET")
    assert stamps(held) == [
        (at(1), "BUY", "MARKET"),
        (at(2), "SELL", "LIMIT"),
        (at(61), "SELL", "MARKET"),
    ]
    assert held.commands == Commands(submits=3, cancels=1, peak_60s=3, p99_60s=3)


LIFTED = sleeve(
    """        elif self.seen == 3:
            self.cancel_order(self.resting)
        elif self.seen == 5:
            self._lift_rate()
""",
    extra="""    def on_order_canceled(self, event):
        self.submit_exit(event.instrument_id)
""",
)
"""The deferring sleeve, lifting the rate on its fifth quote as a stage's flatten does."""


def test_lifting_the_rate_sends_what_was_deferred_and_holds_back_nothing_more() -> None:
    held = both(rated(LIFTED, 2), [tuple(steady(10))])

    assert stamps(held)[-1] == (at(5), "SELL", "MARKET")


def test_a_deferred_cancel_of_an_order_that_filled_meanwhile_is_dropped_for_nothing() -> None:
    """The resting exit at 10.05 fills when the bid reaches it on the tenth quote, while its
    cancel waits for a token; the cancel is dropped then, and pays none."""
    rested = sleeve(
        """        elif self.seen == 3:
            self.cancel_order(self.resting)
""",
        price=10.05,
    )
    held = both(rated(rested, 2), [tuple(steady(40, rise_at=10))])

    assert [(fill.side, fill.qty) for fill in held.run.fills] == [("BUY", 100.0), ("SELL", 100.0)]
    assert held.commands == Commands(submits=2, peak_60s=2, p99_60s=2)


SECOND_BUY = (
    "self.other = self.order_factory.limit(name, OrderSide.BUY, Quantity.from_int(50), "
    "Price(8.0, 2))\n            self.submit_order(self.other)"
)
"""A second buy resting, at 8.00, for a modify to move."""


def filled_while_its_cancel_waits(then: str) -> bytes:
    """Buys 100 at market, rests its exit at 10.05 and a buy of 50 at 8.00 on its second quote,
    and cancels the exit on its third — at four a minute, two requests a cancel, a cancel that
    waits for its tokens. The bid reaches the exit on the tenth quote, and the venue fills it
    there while its cancel still waits; on that fill the sleeve does `then`."""
    return sleeve(
        """        elif self.seen == 3:
            self.cancel_order(self.resting)
""",
        second=f"self.resting = self.submit_exit(name, price=10.05)\n            {SECOND_BUY}",
        extra=f"""    refilled = False

    def on_order_filled(self, event):
        if event.order_side == OrderSide.SELL and not self.refilled:
            self.refilled = True
            {then}
""",
    )


ON_THE_FILL = {
    "entry": (
        'self.submit_entry(event.instrument_id, "BUY", qty=100)',
        (at(9), "BUY", "MARKET"),
        Commands(submits=4, peak_60s=4, p99_60s=4),
    ),
    "modify": (
        "self.modify_order(self.other, price=Price(8.5, 2))",
        (at(2), "BUY", "LIMIT"),
        Commands(submits=3, modifies=1, peak_60s=4, p99_60s=4),
    ),
}
"""What the sleeve sends from the handler of its exit's fill, the last order it then sent —
an intent is stamped with the last point the sleeve saw, the ninth, since the venue matches
the tenth before any handler of it runs — and the commands counted."""


@pytest.mark.parametrize("then", sorted(ON_THE_FILL))
def test_a_deferred_cancel_whose_order_filled_holds_nothing_back_in_the_handler_of_the_fill(
    then: str,
) -> None:
    """At four a minute the entry and the two resting orders leave the bucket more than one
    token and less than two on the third quote, where the exit's cancel waits for two. On the
    tenth the venue fills the exit, and the handler of that fill finds the bucket holding a
    token and a half: the cancel still queued is of an order that has filled, so it would send
    nothing and waits for nothing, and the entry or the modify the handler sends takes a token
    and goes, neither dropped nor counted as refused."""
    order, last, counted = ON_THE_FILL[then]
    held = both(
        rated(filled_while_its_cancel_waits(order), 4, per_cancel=2),
        [tuple(steady(40, rise_at=10))],
    )

    assert stamps(held)[-1] == last
    assert held.commands == counted


CLOSED_BY_HAND_ON_THE_FILL = (
    HEAD.replace(
        b'self.submit_entry(name, "BUY", qty=100)', b'self.submit_entry(name, "BUY", qty=200)'
    ).replace(b"SECOND", b"self.resting = self.submit_exit(name, qty=100, price=10.05)")
    + b"""        elif self.seen == 3:
            self.cancel_order(self.resting)

    def on_order_filled(self, event):
        if event.order_side == OrderSide.SELL and not getattr(self, "closed", False):
            self.closed = True
            self.submit_order(
                self.order_factory.market(
                    event.instrument_id, OrderSide.SELL, Quantity.from_int(100)
                )
            )
"""
)
"""Buys 200, rests an exit of 100 at 10.05 and cancels it on its third quote; when the bid
reaches it on the tenth and it fills, closes the other 100 with a sale it built itself."""


def test_an_exit_refused_after_its_waiting_cancel_s_order_filled_names_no_cancel_waiting() -> None:
    """At two a minute, two requests a cancel, the entry and the resting exit empty the bucket
    and the exit's cancel waits on the third quote. The exit fills on the tenth while the cancel
    is still queued, and the sale the sleeve builds on that fill finds a quarter of a token: it
    is refused, and the refusal names no cancel waiting, since the one queued is of an order that
    filled, which waits for nothing."""
    refusal = refused(
        rated(CLOSED_BY_HAND_ON_THE_FILL, 2, per_cancel=2), [tuple(steady(40, rise_at=10))]
    )

    assert (refusal.rule, refusal.asked, refusal.ts_event) == (ORDER_RATE, "SELL", at(9))
    assert refusal.why.startswith(
        "an exit the strategy built itself needed 1 of the 2 tokens a minute "
        "costs.orders_per_minute allows, and the bucket held 0 after 2 command(s) in the 60 s "
        "before it."
    )


BEHIND_A_MODIFY = sleeve(
    """        elif self.seen == 60:
            self.modify_order(self.resting, price=Price(19.0, 2))
            self.submit_exit(name)
"""
)
"""Moves its resting exit on its sixtieth quote and exits at market in the same handler."""


BEHIND_A_SLOW_MODIFY = sleeve(
    """        elif self.seen == 5:
            self.resting = self.submit_exit(name, price=20.0)
        elif self.seen == 60:
            self.modify_order(self.resting, price=Price(19.0, 2))
            self.submit_exit(name)
""",
    second="pass",
)
"""The same at a latency past the gap between quotes: it rests its exit on its fifth quote,
once its entry has landed and filled."""


@pytest.mark.parametrize(
    ("source", "latency_ms", "rested_at"),
    [(BEHIND_A_MODIFY, 20.0, 2), (BEHIND_A_SLOW_MODIFY, 2_500.0, 5)],
    ids=["answered", "unanswered"],
)
def test_a_cancel_behind_a_modify_past_the_rate_lands_behind_it_on_both_paths(
    source: bytes, latency_ms: float, rested_at: int
) -> None:
    """At two a minute the modify takes the last whole token on the sixtieth quote, so the
    exit at market's cancel of the order being modified is deferred and the exit is owed. On
    the sixty-first a token has come back and the cancel goes, stamped a point after the
    modify, so it lands behind it on both paths: at 20 ms the venue has answered the modify by
    then, and at 2,500 ms it has not. The exit goes once the cancel has landed and a token has
    come back again, thirty seconds on."""
    held = both(rated(source, 2, latency_ms=latency_ms), [tuple(steady(100))])

    assert stamps(held) == [
        (at(1), "BUY", "MARKET"),
        (at(rested_at), "SELL", "LIMIT"),
        (at(91), "SELL", "MARKET"),
    ]
    assert held.commands == Commands(submits=3, modifies=1, cancels=1, peak_60s=3, p99_60s=3)


AGAIN_UNTIL_SENT = sleeve(
    """        elif self.seen >= 3 and not self.again:
            self.again = self.submit_entry(name, "BUY", qty=100) is not None
""",
    extra="""    again = False
""",
)
"""Asks for a second entry on every quote from its third until one is sent."""


def test_an_entry_past_the_rate_is_dropped_and_counted_and_the_run_goes_on() -> None:
    """At two a minute the entry and the resting exit leave a thirtieth of a token on the
    second quote, and it refills a thirtieth a second. The second entry, asked for on every
    quote from the third, is dropped unsent on each of the twenty-eight before the
    thirty-first and goes on that one; `submit_entry` answers `None` for each it dropped, the
    run goes on past them on both paths, and the card counts them as refused."""
    unrated = both(rated(AGAIN_UNTIL_SENT, None), [tuple(steady(40))])
    held = both(rated(AGAIN_UNTIL_SENT, 2), [tuple(steady(40))])

    assert stamps(unrated)[-1] == (at(3), "BUY", "MARKET")
    assert stamps(held) == [
        (at(1), "BUY", "MARKET"),
        (at(2), "SELL", "LIMIT"),
        (at(31), "BUY", "MARKET"),
    ]
    assert [(fill.side, fill.qty) for fill in held.run.fills] == [("BUY", 100.0), ("BUY", 100.0)]
    assert held.commands == Commands(submits=3, peak_60s=3, p99_60s=3, refused_submits=28)
    assert unrated.commands is not None and unrated.commands.refused_submits == 0


MOVING = sleeve(
    """        elif self.seen == 3:
            self.modify_order(self.resting, price=Price(10.05, 2))
"""
)
"""Moves its resting exit from 20.00 to 10.05 on its third quote; the bid reaches 10.05 on
the fifth."""


def test_a_modify_past_the_rate_is_dropped_and_the_order_stays_as_it_was() -> None:
    """At two a minute the entry and the resting exit empty the bucket, so the modify on the
    third quote is dropped unsent and counted, whichever order it moves: the exit stays at
    20.00, working, and does not fill when the bid reaches 10.05, where the modified one did."""
    unrated = both(rated(MOVING, None), [tuple(steady(10, rise_at=5))])
    held = both(rated(MOVING, 2), [tuple(steady(10, rise_at=5))])

    assert [(fill.side, fill.px) for fill in unrated.run.fills] == [
        ("BUY", 10.01),
        ("SELL", 10.05),
    ]
    assert [(fill.side, fill.px) for fill in held.run.fills] == [("BUY", 10.01)]
    assert held.commands == Commands(submits=2, peak_60s=2, p99_60s=2, refused_modifies=1)


BRACKET = """self.submit_order_list(
                self.order_factory.bracket(
                    name,
                    OrderSide.{side},
                    Quantity.from_int(100),
                    sl_trigger_price=Price({stop}, 2),
                    tp_price=Price({target}, 2),
                )
            )"""
"""A bracket of 100 on `side`: its first order at market, its stop and its target."""


@pytest.mark.parametrize(("per_minute", "sent"), [(2, 0), (3, 3)])
def test_an_entry_s_list_takes_a_token_for_each_of_its_orders_or_is_dropped_whole(
    per_minute: int, sent: int
) -> None:
    """A bracket of three is three submits: at three a minute it goes whole, and at two it
    finds the bucket a token short, is dropped whole and counted as three refused, and the
    run goes on with nothing held."""
    bracketed = HEAD.replace(
        b"""        if self.seen == 1:
            self.submit_entry(name, "BUY", qty=100)""",
        (
            "        if self.seen == 1:\n            "
            + BRACKET.format(side="BUY", stop=9.0, target=11.0)
        ).encode(),
    ).replace(b"SECOND", b"self.resting = self.submit_exit(name, price=20.0)")

    held = both(rated(bracketed, per_minute), [tuple(steady(5))])

    assert len(held.intents) == sent
    assert held.commands is not None
    assert (held.commands.submits, held.commands.refused_submits) == (sent, 3 - sent)


BRACKET_THEN_AN_ENTRY = HEAD.replace(
    b"""        if self.seen == 1:
            self.submit_entry(name, "BUY", qty=100)""",
    (
        "        if self.seen == 1:\n            "
        + BRACKET.format(side="BUY", stop=9.0, target=11.0)
        + '\n            self.submit_entry(name, "BUY", qty=10)'
    ).encode(),
).replace(b"SECOND", b"pass")
"""Sends a bracket of three on its first quote, and then an entry of 10 in the same handler."""


def test_a_list_is_refused_whole_where_the_bucket_holds_some_but_fewer_than_it_costs() -> None:
    """A command finds no token available to it when the bucket holds fewer than it costs, a
    list one for each of its orders, all or none. At two a minute the bucket is full on the
    first quote and holds two, and no cancel waits: the bracket of three is dropped whole and
    its three orders counted as refused, though the bucket is not empty, and the entry of 10
    the sleeve sends after it in the same handler takes one of the two tokens and goes."""
    held = both(rated(BRACKET_THEN_AN_ENTRY, 2), [tuple(steady(5))])

    assert stamps(held) == [(at(1), "BUY", "MARKET")]
    assert held.intents[0][3] == 10.0
    assert held.commands == Commands(submits=1, peak_60s=1, p99_60s=1, refused_submits=3)


def test_a_list_that_closes_a_position_past_the_rate_is_refused() -> None:
    """kanso owes only an exit `submit_exit` sized, and an order of a list is not one, so a list
    whose first order closes the position is refused past the rate, as each of its orders would
    be, rather than dropped with the position left open."""
    closing = sleeve(
        "",
        second="pass\n        elif self.seen == 3:\n            "
        + BRACKET.format(side="SELL", stop=11.0, target=9.0),
    )

    refusal = refused(rated(closing, 1), [tuple(steady(5))])

    assert (refusal.rule, refusal.asked, refusal.ts_event) == (ORDER_RATE, "SELL", at(3))
    assert refusal.why.startswith(
        "a list of 3 orders closing a position needed 3 of the 1 tokens a minute"
    )
    assert "kanso owes only an exit submit_exit sized" in refusal.why


FIRST_LEGS = {"market": "self.submit_exit(name)", "priced": "self.submit_exit(name, price=10.05)"}
"""The first leg of a flip: the whole position at market, or at a limit the bid reaches on the
sixty-fifth quote."""

FLIPS = {
    "entry": 'self.submit_entry(name, "SELL", qty=100)',
    "room": 'self.submit_entry(name, "SELL")',
    "list": BRACKET.format(side="SELL", stop=11.0, target=9.0),
}
"""The second leg of a flip: an entry on the other side of a quantity given, or of what the
room leaves, or a bracket of three whose first order is one."""

ROOM_CAPITAL = 1_001.0
"""A capital the long of 100 at 10.01 fills the room of, so the leg sized by the room finds
some only where the first leg reads as gone."""


@pytest.mark.parametrize(
    ("first", "leg", "unheld", "dropped"),
    [
        ("market", "entry", 3, 1),
        ("market", "room", 3, 1),
        ("market", "list", 5, 3),
        ("priced", "entry", 3, 1),
        ("priced", "room", 2, 0),
    ],
)
@pytest.mark.parametrize(("per_minute", "exited_at"), [(2, 2), (1, 61)], ids=["sent", "owed"])
def test_the_second_leg_of_a_flip_past_the_rate_is_an_entry_dropped_and_counted(
    first: str, leg: str, unheld: int, dropped: int, per_minute: int, exited_at: int
) -> None:
    """A flip is `submit_exit` and then an entry on the other side, in one handler. At two a
    minute the exit takes the last token on the second quote, and at one it is owed, going on
    the sixty-first; either way the second leg finds no token and is the entry it is, dropped
    and counted — sized by a quantity or by the room, behind an exit at market or at a price,
    every order of a bracket — never owed as an exit and forgotten once the exit has left the
    sleeve flat, and never refused. An exit owed at market frees the room as the market order
    it holds back would, so the leg sized by the room is sized and dropped whether the exit
    went or was owed; one at a price is a limit resting or owed, which frees no room, so the
    leg finds none there unheld or held, and the rate counts nothing it would have sent. Unheld,
    every leg that found room is sent, and the sleeve ends the other side."""
    flipping = sleeve("", second=f"{FIRST_LEGS[first]}\n            {FLIPS[leg]}")
    groups: list[tuple[object, ...]] = [tuple(steady(70, rise_at=65))]
    capital = {"capital": ROOM_CAPITAL} if leg == "room" else {}
    unrated = both(replace(rated(flipping, None), **capital), groups)
    held = both(replace(rated(flipping, per_minute), **capital), groups)

    exited = "MARKET" if first == "market" else "LIMIT"
    assert len(unrated.intents) == unheld
    assert stamps(held) == [(at(1), "BUY", "MARKET"), (at(exited_at), "SELL", exited)]
    assert [(fill.side, fill.qty) for fill in held.run.fills] == [("BUY", 100.0), ("SELL", 100.0)]
    assert held.commands is not None
    assert held.commands.refused_submits == dropped


BEHIND_AN_OWED_EXIT = {
    "at_a_price": (
        FIRST_LEGS["priced"],
        BRACKET.format(side="SELL", stop=11.0, target=9.0),
        "a list of 3 orders closing a position needed 3",
    ),
    "of_half": (
        "self.submit_exit(name, qty=50)",
        "self.submit_order(self.order_factory.market(name, OrderSide.SELL, Quantity.from_int(50)))",
        "an exit the strategy built itself needed 1",
    ),
}
"""An exit `submit_exit` owes, and an order built by hand on the closing side behind it."""


@pytest.mark.parametrize("behind", sorted(BEHIND_AN_OWED_EXIT))
def test_an_order_built_by_hand_reads_an_owed_exit_as_gone_only_for_what_it_takes_at_market(
    behind: str,
) -> None:
    """At one a minute the exit asked for on the second quote is owed, and what the sleeve
    builds by hand behind it in the same handler finds no token. An exit owed at a price stands
    for a limit, which may never fill, and an exit owed for half the position takes only half:
    so a bracket on the other side behind the first, and a sale of the other half behind the
    second, each closes what the owed exit leaves, and is an exit the strategy built itself,
    refused — not the second leg of a flip, dropped."""
    first, then, why = BEHIND_AN_OWED_EXIT[behind]
    refusal = refused(
        rated(sleeve("", second=f"{first}\n            {then}"), 1), [tuple(steady(10))]
    )

    assert (refusal.rule, refusal.asked, refusal.ts_event) == (ORDER_RATE, "SELL", at(2))
    assert refusal.why.startswith(why)


RESTING_AT_20 = "self.resting = self.submit_exit(name, price=20.0)"
"""The sleeve's exit of the whole position resting at 20.00."""

STOP_LIMIT_AT_5 = (
    "self.resting = self.order_factory.stop_limit(name, OrderSide.SELL, Quantity.from_int(100), "
    "price=Price(5.0, 2), trigger_price=Price(9.0, 2))\n            self.submit_order(self.resting)"
)
"""A stop-limit sale of the whole position, triggered at 9.00 and limited at 5.00: the room
holds back what it would add at its limit, half the notional the long holds."""

BEHIND_A_DEFERRED_CANCEL = {
    "built_by_hand": (
        RESTING_AT_20,
        "self.submit_order(self.order_factory.market(name, OrderSide.SELL, "
        "Quantity.from_int(100)))",
        4,
        1,
    ),
    "a_list_built_by_hand": (
        RESTING_AT_20,
        BRACKET.format(side="SELL", stop=11.0, target=9.0),
        6,
        3,
    ),
    "entry": (RESTING_AT_20, 'self.submit_entry(name, "SELL", qty=100)', 4, 1),
    "room": (STOP_LIMIT_AT_5, 'self.submit_entry(name, "SELL")', 4, 1),
}
"""What rests on the closing side, the second leg of a flip sent behind `submit_exit` at market
— an order or a bracket the sleeve builds itself, an entry of a quantity given, an entry of
what the room leaves — what the flip sends unheld, and what the rate drops of the leg."""


@pytest.mark.parametrize(
    ("per_minute", "per_cancel", "exited_at"),
    [(2, 1, 61), (3, 2, 41)],
    ids=["one_a_cancel", "two_a_cancel"],
)
@pytest.mark.parametrize("leg", sorted(BEHIND_A_DEFERRED_CANCEL))
def test_a_flip_s_second_leg_behind_an_exit_owed_behind_a_deferred_cancel_is_dropped_and_counted(
    leg: str, per_minute: int, per_cancel: int, exited_at: int
) -> None:
    """The exit at market on the third quote must cancel the sleeve's own sale resting on the
    closing side first, and the bucket holds too few tokens for the cancel: it waits, the order
    it would cancel is working until it goes, and the exit is owed behind it, to go once the
    cancel has — on the sixty-first quote at two a minute, one request a cancel, and on the
    forty-first at three, two. The owed exit stands for the market order the rate held back,
    and no entry goes while the cancel waits, so the second leg of the flip behind it reads it
    as gone: it opens the other side, and is the entry it is, dropped and counted — an
    order or a list built by hand, an entry of a quantity given, an entry sized by the room the
    owed exit frees — and never sent as the order that closes the position in the owed exit's
    place, nor refused as an exit the strategy built itself. Unheld, the flip goes whole."""
    rest, then, unheld, dropped = BEHIND_A_DEFERRED_CANCEL[leg]
    flipping = sleeve(
        f"""        elif self.seen == 3:
            self.submit_exit(name)
            {then}
""",
        second=rest,
    )
    capital = {"capital": ROOM_CAPITAL} if leg == "room" else {}
    groups: list[tuple[object, ...]] = [tuple(steady(130))]
    unrated = both(replace(rated(flipping, None), **capital), groups)
    held = both(replace(rated(flipping, per_minute, per_cancel=per_cancel), **capital), groups)

    rested = "STOP_LIMIT" if leg == "room" else "LIMIT"
    assert len(unrated.intents) == unheld
    assert stamps(held) == [
        (at(1), "BUY", "MARKET"),
        (at(2), "SELL", rested),
        (at(exited_at), "SELL", "MARKET"),
    ]
    assert [(fill.side, fill.qty) for fill in held.run.fills] == [("BUY", 100.0), ("SELL", 100.0)]
    assert held.commands is not None
    assert (held.commands.cancels, held.commands.refused_submits) == (1, dropped)


BEHIND_A_CANCEL_IN_FLIGHT = sleeve(
    """        elif self.seen == 3:
            self.submit_exit(name)
            self.submit_order(
                self.order_factory.market(name, OrderSide.SELL, Quantity.from_int(100))
            )
"""
)
"""Exits at market on its third quote, behind its own sale resting at 20.00, and sells the long
at market by hand in the same handler."""


def test_a_leg_built_by_hand_behind_an_exit_owed_behind_a_cancel_in_flight_is_its_own_exit() -> (
    None
):
    """At three a minute, one request a cancel and 20 ms, the exit at market on the third quote
    cancels the sleeve's resting sale with the last whole token, and the cancel is in flight:
    the sale can fill until it lands, so the exit is owed behind it, as it would be with no rate
    stated, and is owed for no token and behind no cancel the rate deferred. `held` applies no
    exit owed so, and nor does the rate: the sale the sleeve builds by hand behind it closes the
    long it reads, and with no token left for it is an exit the strategy built itself, refused —
    where behind a cancel the rate deferred it is an entry, dropped and counted (above)."""
    refusal = refused(
        rated(BEHIND_A_CANCEL_IN_FLIGHT, 3, per_cancel=1, latency_ms=20.0), [tuple(steady(10))]
    )

    assert (refusal.rule, refusal.asked, refusal.ts_event) == (ORDER_RATE, "SELL", at(3))
    assert refusal.why.startswith(
        "an exit the strategy built itself needed 1 of the 3 tokens a minute "
        "costs.orders_per_minute allows, and the bucket held 0 after 3 command(s) in the 60 s "
        "before it."
    )


def behind_a_cancel(then: str, *, also: str = "") -> bytes:
    """Buys 100 at market, rests a buy of 50 at 9.00 on its second quote, and any order `also`
    says, cancels the resting buy on its third — at two requests a cancel, a cancel that waits
    for its tokens — and does `then` in the same handler."""
    rest = (
        "self.buy = self.order_factory.limit(name, OrderSide.BUY, Quantity.from_int(50), "
        "Price(9.0, 2))\n            self.submit_order(self.buy)"
    )
    return sleeve(
        f"""        elif self.seen == 3:
            self.cancel_order(self.buy)
            {then}
""",
        second=rest + (f"\n            {also}" if also else ""),
    )


WHILE_A_CANCEL_WAITS = {
    "stop": (
        "self.submit_order(self.order_factory.stop_market(name, OrderSide.SELL, "
        "Quantity.from_int(100), trigger_price=Price(9.0, 2)))",
        (at(3), "SELL", "STOP_MARKET"),
        Commands(submits=4, peak_60s=4, p99_60s=4),
    ),
    "exit": (
        "self.submit_exit(name)",
        (at(3), "SELL", "MARKET"),
        Commands(submits=4, peak_60s=4, p99_60s=4),
    ),
    "entry": (
        'self.submit_entry(name, "BUY", qty=10)',
        (at(2), "BUY", "LIMIT"),
        Commands(submits=3, peak_60s=3, p99_60s=3, refused_submits=1),
    ),
    "modify": (
        "self.modify_order(self.other, price=Price(8.5, 2))",
        (at(2), "BUY", "LIMIT"),
        Commands(submits=3, peak_60s=3, p99_60s=3, refused_modifies=1),
    ),
}
"""What the sleeve sends behind its waiting cancel, the last order it then sent, and the
commands counted before the cancel goes."""


@pytest.mark.parametrize("then", sorted(WHILE_A_CANCEL_WAITS))
def test_while_a_cancel_waits_an_exit_takes_a_token_the_bucket_holds_and_an_entry_does_not(
    then: str,
) -> None:
    """At four a minute, two requests a cancel, the entry and the two resting buys leave the
    bucket more than one token and less than two on the third quote, where the cancel of the
    first buy waits for its two. An exit sent behind it — a stop built by hand, `submit_exit`
    at market — takes the token the bucket holds and goes: it is one order the account must
    take, and the cancel goes once the bucket holds two again. An entry or a modify takes none
    while the cancel waits, and is dropped and counted, so entries and modifies do not starve a
    cancel that costs two."""
    order, last, counted = WHILE_A_CANCEL_WAITS[then]
    held = both(
        rated(behind_a_cancel(order, also=SECOND_BUY), 4, per_cancel=2), [tuple(steady(10))]
    )

    assert stamps(held)[-1] == last
    assert held.commands == counted


OWED_BEHIND_A_CANCEL = behind_a_cancel("self.submit_exit(name)")
"""At two a minute: the exit at market the sleeve asks for behind its waiting cancel finds no
token, and is owed."""


def test_an_exit_owed_for_want_of_a_token_takes_one_that_comes_back_while_a_cancel_waits() -> None:
    """At two a minute, two requests a cancel, the entry and the resting buy leave a thirtieth
    of a token on the third quote: the cancel waits for two and the exit at market is owed. On
    the thirty-first a whole token has come back, and the owed exit takes it before the
    handler, though the cancel still waits for its second; the cancel goes once two have come
    back again, on the ninety-first. Were the exit held behind the cancel, the cancel would go
    on the sixty-first and the position stay open until the ninety-first."""
    held = both(rated(OWED_BEHIND_A_CANCEL, 2, per_cancel=2), [tuple(steady(100))])

    assert stamps(held)[2:] == [(at(31), "SELL", "MARKET")]
    assert [(fill.side, fill.qty) for fill in held.run.fills] == [("BUY", 100.0), ("SELL", 100.0)]
    assert held.commands is not None and held.commands.cancels == 1


def test_an_exit_built_by_hand_with_no_token_behind_a_cancel_is_refused_saying_what_waits() -> None:
    """At two a minute, two requests a cancel, the bucket holds no whole token on the third
    quote: the stop the sleeve builds behind its waiting cancel is refused, and the refusal says
    what the bucket held and that a cancel waits for its two, ahead of every entry."""
    stop = (
        "self.submit_order(self.order_factory.stop_market(name, OrderSide.SELL, "
        "Quantity.from_int(100), trigger_price=Price(9.0, 2)))"
    )
    refusal = refused(rated(behind_a_cancel(stop), 2, per_cancel=2), [tuple(steady(10))])

    assert (refusal.rule, refusal.asked, refusal.ts_event) == (ORDER_RATE, "SELL", at(3))
    assert refusal.why.startswith(
        "an exit the strategy built itself needed 1 of the 2 tokens a minute "
        "costs.orders_per_minute allows, and the bucket held 0, a cancel of 1 order(s) "
        "waiting for 2 token(s) each, after 2 command(s) in the 60 s before it."
    )


SIZED_FLIP = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, tick):
        if tick.instrument_id.symbol.value != "DEMO":
            return
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(tick.instrument_id, "BUY")
        elif self.seen == 2:
            self.submit_exit(tick.instrument_id)
            self.submit_entry(INTO, "SIDE")
"""
"""Under a sizing rule, buys the whole budget on its first quote and on its second exits it and
enters `INTO` on `SIDE`: the other side of the same name, a flip, or another name, a rotation."""

OTHER = "OTHR"
"""The name a sized rotation moves into, quoted at the same prices a nanosecond before each of
`DEMO`'s quotes, so the bucket refills on `DEMO`'s alone."""

TWO_NAMES = hypothesis(
    resolution="tick",
    horizon="1d",
    data_requirements=["quote", "trade"],
    universe=[f"{SYMBOL}.{VENUE}", f"{OTHER}.{VENUE}"],
    risk_limits={"max_position_pct": 100, "max_drawdown_pct": 40, "max_leverage": 1},
)


@pytest.mark.parametrize(
    ("into", "side"),
    [("tick.instrument_id", "SELL"), ("'OTHR.XNAS'", "BUY")],
    ids=["flip", "rotation"],
)
@pytest.mark.parametrize(("per_minute", "exited_at"), [(2, 2), (1, 61)], ids=["sent", "owed"])
def test_a_sized_flip_or_rotation_past_the_rate_drops_its_entry_and_runs_on(
    into: str, side: str, per_minute: int, exited_at: int
) -> None:
    """Under a sizing rule the exit a flip or a rotation sends first is in flight when the entry
    sizes, so the entry is legal; one owed for want of a token is in flight too, as the market
    order the rate held back. At two a minute the exit takes the last
    token and at one it is owed, going on the sixty-first quote; either way the entry behind it
    finds no token and is dropped and counted, never refused as `one_position` nor owed as an
    exit `submit_exit` would refuse to send, and both paths run to the end."""
    source = SIZED_FLIP.replace(b"INTO", into.encode()).replace(b"SIDE", side.encode())
    request = replace(rated(source, per_minute), hyp=TWO_NAMES, sleeve_budget=10_000.0)
    other = quoted(70, first=T0 - 1, name=InstrumentId(Symbol(OTHER), Venue(VENUE)))
    groups: list[tuple[object, ...]] = [tuple(steady(70)), tuple(other)]

    unrated = both(
        replace(request, venue_model=rated(source, None).venue_model), groups, (SYMBOL, OTHER)
    )
    held = both(request, groups, (SYMBOL, OTHER))

    assert len(unrated.intents) == 3
    assert [(ts, name, sold) for ts, name, sold, *_ in held.intents] == [
        (at(1), f"{SYMBOL}.{VENUE}", "BUY"),
        (at(exited_at), f"{SYMBOL}.{VENUE}", "SELL"),
    ]
    assert [fill.side for fill in held.run.fills] == ["BUY", "SELL"]
    assert held.commands is not None and held.commands.refused_submits == 1


EXITED_THEN = b"""
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, tick):
        if tick.instrument_id.symbol.value != "DEMO":
            return
        self.seen += 1
        name = tick.instrument_id
        other = InstrumentId.from_str("OTHR.XNAS")
        if self.seen == 1:
            self.submit_entry(name, "BUY", qty=100)
        elif self.seen == 2:
            REST
        elif self.seen == 3:
            self.submit_exit(name)
            THEN
"""
"""Buys 100 of `DEMO` on its first quote of it and does `REST` on its second; on its third
exits at market and does `THEN` in the same handler. It acts on no quote of `OTHR`."""

BUILT_BEHIND_AN_EXIT = {
    "requote": "self.submit_order(self.order_factory.limit(name, OrderSide.BUY, "
    "Quantity.from_int(50), Price(9.0, 2)))",
    "rotation": "self.submit_order(self.order_factory.market(other, OrderSide.BUY, "
    "Quantity.from_int(10)))",
    "bracket": "self.submit_order_list(self.order_factory.bracket(other, OrderSide.BUY, "
    "Quantity.from_int(10), sl_trigger_price=Price(9.0, 2), tp_price=Price(11.0, 2)))",
}
"""What a sleeve builds by hand behind its exit: a buy of 50 resting at 9.00 in the name it
left, a buy of 10 `OTHR` at market, and a bracket of 10 `OTHR` — each an entry, and the
bracket three orders."""


@pytest.mark.parametrize(
    ("per_minute", "per_cancel", "rest", "exited_at"),
    [(1, None, "pass", 61), (2, 1, STOP_LIMIT_AT_5, 61), (3, 2, STOP_LIMIT_AT_5, 41)],
    ids=["for_want_of_a_token", "behind_a_cancel_of_one", "behind_a_cancel_of_two"],
)
@pytest.mark.parametrize("built", sorted(BUILT_BEHIND_AN_EXIT))
def test_an_order_built_by_hand_behind_an_owed_exit_is_funded_on_what_it_frees_and_dropped(
    built: str, per_minute: int, per_cancel: int | None, rest: str, exited_at: int
) -> None:
    """On a capital of 1,001 the long of 100 at 10.01 fills the book, so an entry the sleeve
    builds by hand behind its exit at market fits only where that exit reads as gone. At one a
    minute the exit finds no token and is owed for want of one, going on the sixty-first quote;
    at two a minute, one request a cancel, and at three, two, it must first cancel the
    stop-limit sale resting on the closing side, the bucket holds too few tokens for that
    cancel, and the exit is owed behind it, going on the sixty-first and the forty-first. Owed
    either way, the exit stands for the market order the rate held back: the order or list
    built behind it — a re-quote in the name it left, a rotation into another, a bracket there
    — is funded on the room it frees, as behind an exit that went, and is the entry it is,
    dropped and counted, every order of the bracket, the card running on; never refused as
    `unfunded_order`. Unheld, each is sent."""
    source = EXITED_THEN.replace(b"REST", rest.encode()).replace(
        b"THEN", BUILT_BEHIND_AN_EXIT[built].encode()
    )
    request = replace(
        rated(source, per_minute, per_cancel=per_cancel), hyp=TWO_NAMES, capital=ROOM_CAPITAL
    )
    other = quoted(130, first=T0 - 1, name=InstrumentId(Symbol(OTHER), Venue(VENUE)))
    groups: list[tuple[object, ...]] = [tuple(steady(130)), tuple(other)]
    unrated = both(
        replace(request, venue_model=rated(source, None).venue_model), groups, (SYMBOL, OTHER)
    )
    held = both(request, groups, (SYMBOL, OTHER))

    demo = f"{SYMBOL}.{VENUE}"
    rested = [] if rest == "pass" else [(at(2), demo, "SELL", "STOP_LIMIT")]
    dropped = 3 if built == "bracket" else 1
    assert len(unrated.intents) == 2 + len(rested) + dropped
    assert [(ts, name, side, kind) for ts, name, side, _, kind, _ in held.intents] == [
        (at(1), demo, "BUY", "MARKET"),
        *rested,
        (at(exited_at), demo, "SELL", "MARKET"),
    ]
    assert [(fill.side, fill.qty) for fill in held.run.fills] == [("BUY", 100.0), ("SELL", 100.0)]
    assert held.commands is not None
    assert (held.commands.cancels, held.commands.refused_submits) == (len(rested), dropped)


DROPPED_THEN = {
    "cancel_order": "self.cancel_order(self.dropped)",
    "cancel_orders": "self.cancel_orders([self.dropped])",
    "modify_order": "self.modify_order(self.dropped, price=Price(6.0, 2))",
}
"""What a strategy may still do with an entry the rate dropped, which it holds all the same."""


@pytest.mark.parametrize("call", sorted(DROPPED_THEN))
def test_a_cancel_or_a_modify_of_an_entry_the_rate_dropped_is_no_command(call: str) -> None:
    """At two a minute the entry and the resting exit leave a thirtieth of a token, and the buy
    the sleeve builds on its third quote is dropped unsent, though the sleeve still holds it. By
    the forty-fifth a token has come back: cancelling or moving the dropped buy sends the venue
    nothing, so it takes no token and is counted as nothing, and the buy the sleeve rests after
    it in the same handler takes the token."""
    source = sleeve(
        f"""        elif self.seen == 3:
            self.dropped = self.order_factory.limit(
                name, OrderSide.BUY, Quantity.from_int(50), Price(5.0, 2)
            )
            self.submit_order(self.dropped)
        elif self.seen == 45:
            {DROPPED_THEN[call]}
            {resting_buy(5.0)}
"""
    )
    held = both(rated(source, 2), [tuple(steady(50))])

    assert stamps(held)[2:] == [(at(45), "BUY", "LIMIT")]
    assert held.commands == Commands(submits=3, peak_60s=3, p99_60s=3, refused_submits=1)


REPOSTING = b'''
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Price, Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Rests a buy of 10 far under the market and, on every quote, cancels it and rests
    another: every buy it rested, or, under `CHECKS`, only one the engine was handed."""

    config_cls = Config

    def on_start(self):
        self.resting = None

    def on_quote_tick(self, tick):
        last = self.resting
        if last is not None and (not CHECKS or self.cache.order(last.client_order_id)):
            self.cancel_order(last)
        self.resting = self.order_factory.limit(
            tick.instrument_id, OrderSide.BUY, Quantity.from_int(10), Price(5.0, 2)
        )
        self.submit_order(self.resting)
'''


def reposting(checks: bool) -> bytes:
    """The re-posting sleeve, cancelling only what was sent when `checks`."""
    return REPOSTING.replace(b"CHECKS", b"True" if checks else b"False")


@pytest.mark.parametrize("per_cancel", [None, 2], ids=["one", "two"])
def test_a_sleeve_that_cancels_its_last_order_sent_or_not_sends_what_one_that_checks_does(
    per_cancel: int | None,
) -> None:
    """At twenty a minute a sleeve that cancels its resting buy and rests another on every quote
    finds most of its buys dropped. A cancel of one dropped sends nothing and spends no token, so
    the sleeve that cancels its last buy whatever became of it sends exactly what one that
    cancels only a buy the engine was handed does: every buy it sent but the last, cancelled."""
    groups: list[tuple[object, ...]] = [tuple(steady(300))]
    blind = both(rated(reposting(False), 20, per_cancel=per_cancel), groups)
    checking = both(rated(reposting(True), 20, per_cancel=per_cancel), groups)

    assert blind.intents == checking.intents
    assert blind.commands == checking.commands
    assert blind.commands is not None and blind.commands.refused_submits > 0
    assert blind.commands.cancels == blind.commands.submits - 1


def test_a_cancel_that_costs_two_is_not_starved_by_entries_taking_the_single_tokens_back() -> None:
    """At the 190 a minute an Alpaca account declares, two requests a cancel, the re-posting
    sleeve on quotes a quarter second apart wants three tokens a quote and gets under one. The
    cancel that waits for two keeps its place: no buy takes a single token that comes back
    before it has its two, as a throttled account working its requests in turn sends them, so
    every buy sent but the last is cancelled. Were the single tokens the handler's, each would
    go to the next buy and the cancels would wait while the buys piled up resting."""
    groups: list[tuple[object, ...]] = [tuple(quoted(480, gap=SECOND_NS // 4))]
    held = both(rated(reposting(True), 190, per_cancel=2), groups)

    assert held.commands is not None and held.commands.refused_submits > 0
    assert held.commands.cancels == held.commands.submits - 1


def scaling_out(every_quote: bool) -> bytes:
    """Buys 100 at market, rests a buy of 50 at 9.00 on its second quote and cancels it on its
    third; with `every_quote`, from the third quote on it asks `submit_exit` for 10 of the
    position on every quote."""
    stream = "\n        if self.seen >= 3:\n            self.submit_exit(name, qty=10)"
    return sleeve(
        f"""        elif self.seen == 3:
            self.cancel_order(self.buy)
{stream if every_quote else ""}
""",
        second="self.buy = self.order_factory.limit(name, OrderSide.BUY, Quantity.from_int(50), "
        "Price(9.0, 2))\n            self.submit_order(self.buy)",
    )


def test_an_exit_of_part_of_a_position_asked_for_on_every_point_keeps_a_cancel_waiting() -> None:
    """An exit takes a token the bucket holds while a cancel waits, so exits can keep one
    waiting. At two a minute, two requests a cancel, the entry and the resting buy empty the
    bucket, and the buy's cancel on the third quote waits for two tokens. With no exit asked
    for, the cancel goes on the sixty-first, once two have come back, and the buy never fills.
    A `submit_exit` of 10 of the position, asked for on every quote, is owed each time the
    bucket is empty and takes each token that comes back, thirty seconds apart, for as long as
    the position lasts: the cancel never has its two, and the buy it would have taken out works
    on and fills when the ask reaches it on the two-hundredth quote."""
    groups: list[tuple[object, ...]] = [tuple(quoted(300, {200: (8.98, 9.0), 201: (9.99, 10.01)}))]
    once = both(rated(scaling_out(False), 2, per_cancel=2), groups)
    scaling = both(rated(scaling_out(True), 2, per_cancel=2), groups)

    assert once.commands is not None and once.commands.cancels == 1
    assert [(fill.side, fill.qty) for fill in once.run.fills] == [("BUY", 100.0)]
    assert stamps(scaling)[2:] == [(at(seen), "SELL", "MARKET") for seen in range(31, 300, 30)]
    assert scaling.commands is not None and scaling.commands.cancels == 0
    assert ("BUY", 50.0, 9.0) in [(fill.side, fill.qty, fill.px) for fill in scaling.run.fills]


def dipping(count: int) -> list[QuoteTick]:
    """`count` quotes a second apart at 9.99/10.01, 1,000 a side, but for every thirtieth from
    the fortieth, which asks 9.00 for 5."""
    made: list[QuoteTick] = []
    for seen in range(1, count + 1):
        dips = seen >= 40 and (seen - 40) % 30 == 0
        made.append(
            QuoteTick(
                NAME,
                Price(8.98 if dips else 9.99, 2),
                Price(9.0 if dips else 10.01, 2),
                Quantity.from_int(1_000),
                Quantity.from_int(5 if dips else 1_000),
                ts_event=at(seen),
                ts_init=at(seen),
            )
        )
    return made


def exiting_whole(every_quote: bool) -> bytes:
    """Buys 100 at market, rests a buy of 50 at 9.00 on its second quote and cancels it on its
    third, asking `submit_exit` for the whole position there — and, with `every_quote`, on every
    quote after it."""
    asks = "self.seen >= 3" if every_quote else "self.seen == 3"
    return sleeve(
        f"""        elif self.seen == 3:
            self.cancel_order(self.buy)
        if {asks}:
            self.submit_exit(name)
""",
        second="self.buy = self.order_factory.limit(name, OrderSide.BUY, Quantity.from_int(50), "
        "Price(9.0, 2))\n            self.submit_order(self.buy)",
    )


def test_a_whole_exit_asked_on_every_point_keeps_a_cancel_waiting_while_its_order_fills() -> None:
    """The cancel that waits is of an order on the opening side, a buy of 50 resting at 9.00,
    which works on until the cancel goes. At two a minute, two requests a cancel, a `submit_exit`
    of the whole position asked for once, on the third quote, is owed for want of a token and
    goes on the thirty-first, and the cancel once two have come back. Asked for on every quote
    it closes the long on the thirty-first as well; but from the fortieth every thirtieth quote
    asks 9.00 for 5, each fill of the buy leaves a position of 5, and the exit asked again closes
    each with the single token that comes back: the cancel never has its two, and the buy fills
    45 of its 50."""
    groups: list[tuple[object, ...]] = [tuple(dipping(300))]
    once = both(rated(exiting_whole(False), 2, per_cancel=2), groups)
    streaming = both(rated(exiting_whole(True), 2, per_cancel=2), groups)

    assert once.commands is not None and once.commands.cancels == 1
    assert stamps(streaming)[2:] == [
        (at(seen), "SELL", "MARKET") for seen in (31, *range(61, 300, 30))
    ]
    assert streaming.commands is not None and streaming.commands.cancels == 0
    bought = [fill.qty for fill in streaming.run.fills if (fill.side, fill.px) == ("BUY", 9.0)]
    assert sum(bought) == 45.0


def chase_groups() -> list[tuple[object, ...]]:
    """Two sessions of quotes and prints a second apart, the ask moving on most of them."""
    from tests.nautilus.backtest.test_exit_flat import points

    return points((FORWARD[0], FORWARD[0]))


FOLLOWING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Buys once at market, then rests its exit a dollar over the ask and follows the ask with
    it: on every quote that moves it, cancel the resting exit and send another."""

    config_cls = Config

    def on_start(self):
        self.bought = False
        self.asked = None

    def on_quote_tick(self, tick):
        name = tick.instrument_id
        if not self.bought:
            self.bought = self.submit_entry(name, "BUY", qty=100) is not None
            return
        ask = float(tick.ask_price)
        if ask == self.asked:
            return
        self.cancel_all_orders(name)
        placed = self.submit_exit(name, price=round(ask + 1.0, 2))
        self.asked = ask if placed is not None else None
'''


def chasing(per_minute: int | None, latency_ms: float) -> RunRequest:
    """A sleeve that follows the ask with an exit that never fills, cancelling and re-posting
    it on every quote that moves the ask, under a rate of `per_minute`."""
    return rated(FOLLOWING, per_minute, latency_ms=latency_ms)


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_a_sleeve_that_chases_the_ask_past_the_rate_agrees_on_both_paths(
    latency_ms: float,
) -> None:
    """A re-posting exit sends a cancel and an order on most quotes, past what twenty a minute
    allows: its cancels are deferred and its exits owed, never refused, and both paths agree
    on every one; it sends far fewer than it would unheld, no more in any 60 s than a burst
    of twenty and twenty refilled, and still keeps an exit resting most of the time."""
    unrated = both(chasing(None, latency_ms), chase_groups())
    held = both(chasing(20, latency_ms), chase_groups())

    assert unrated.commands is not None and held.commands is not None
    assert unrated.commands.peak_60s > 40, "unheld, the sleeve sends past what twenty allows"
    assert held.commands.sent < unrated.commands.sent / 2
    assert held.commands.peak_60s <= 40, "a burst of twenty and twenty a minute after it"
    assert held.commands.modifies == 0
    assert abs(held.commands.submits - held.commands.cancels) <= 2
    assert [(fill.side, fill.qty) for fill in held.run.fills] == [("BUY", 100.0)]


def runs_alike(left: RunResult, right: RunResult) -> None:
    """Two runs that did the same thing, byte for byte, but for the venue model each records."""
    assert pickle.dumps(replace(left.run, venue_model={})) == pickle.dumps(
        replace(right.run, venue_model={})
    )
    assert left.intents == right.intents
    assert left.commands == right.commands


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_an_unstated_rate_holds_nothing_back_and_moves_no_result(latency_ms: float) -> None:
    """With the key absent, stated as nothing, or stated higher than the sleeve ever sends, a
    run is the same run: every fill, trade, return and intent, and the same commands counted.
    The model a run records without the key carries no `orders_per_minute` at all."""
    groups = chase_groups()
    absent = backtest.execute(chasing(None, latency_ms), [instrument()], groups)
    request = chasing(None, latency_ms)
    model = dict(request.venue_model)
    model["costs"] = {**dict(model["costs"]), "orders_per_minute": None}  # type: ignore[call-overload]
    nothing = backtest.execute(replace(request, venue_model=model), [instrument()], groups)
    unbinding = backtest.execute(chasing(10**6, latency_ms), [instrument()], groups)

    runs_alike(absent, nothing)
    runs_alike(absent, unbinding)
    assert "orders_per_minute" not in dict(absent.run.venue_model["costs"])  # type: ignore[call-overload]
    assert absent.commands is not None and absent.commands.sent > 0


UNSENT = (
    "self.unsent = self.order_factory.limit(name, OrderSide.SELL, Quantity.from_int({qty}), "
    "Price({price}, 2))\n            self.cancel_order(self.unsent)"
)
"""An exit of `qty` at `price`, built on the second quote and cancelled before it was ever
sent."""

NEVER_SENT = {
    "sent_later": (
        sleeve(
            """        elif self.seen == 3:
            self.submit_order(self.unsent)
""",
            second=UNSENT.format(qty=100, price=10.04),
        ),
        {5: (10.05, 10.07)},
    ),
    "an_exit_at_market_behind_it": (
        sleeve(
            """        elif self.seen == 3:
            self.submit_order(self.unsent)
            self.submit_exit(name)
""",
            second=UNSENT.format(qty=100, price=20.0),
        ),
        {4: (9.80, 9.82), 5: (10.05, 10.07)},
    ),
    "an_exit_at_a_price_behind_it": (
        sleeve(
            """        elif self.seen == 3:
            self.submit_order(self.unsent)
        elif self.seen == 4:
            self.submit_exit(name, price=10.5)
        elif self.seen == 6:
            self.modify_order(self.unsent, quantity=Quantity.from_int(10))
""",
            second=UNSENT.format(qty=50, price=20.0),
        ),
        {20: (10.5, 10.52)},
    ),
}
"""Sleeves that cancel an order before it was ever sent and send that same object on their
third quote: alone, with an exit at market behind it in the same handler, or with an exit at
10.50 behind it on the fourth and the order shrunk to 10 on the sixth; and the quotes each
runs on, at 9.99/10.01 but from each key on."""


def sent(result: RunResult) -> list[tuple[int, str, float, str, float | None]]:
    """When each intent was sent, its side, its quantity, its order type and any price."""
    return [(ts, side, qty, kind, price) for ts, _, side, qty, kind, price in result.intents]


def filled(result: RunResult) -> list[tuple[str, float, float]]:
    """Each fill's side, quantity and price."""
    return [(fill.side, fill.qty, fill.px) for fill in result.run.fills]


ENTERED = (at(1), "BUY", 100.0, "MARKET", None)
"""The entry every `NEVER_SENT` sleeve sends on its first quote."""

ON_MAIN = {
    "sent_later": (
        [ENTERED, (at(3), "SELL", 100.0, "LIMIT", 10.04)],
        [("BUY", 100.0, 10.01), ("SELL", 100.0, 10.04)],
        [ENTERED, (at(3), "SELL", 100.0, "LIMIT", 10.04)],
        [("BUY", 100.0, 10.01)],
    ),
    "an_exit_at_market_behind_it": (
        [ENTERED, (at(3), "SELL", 100.0, "LIMIT", 20.0), (at(4), "SELL", 100.0, "MARKET", None)],
        [("BUY", 100.0, 10.01), ("SELL", 100.0, 9.8)],
        [ENTERED, (at(3), "SELL", 100.0, "LIMIT", 20.0), (at(3), "SELL", 100.0, "MARKET", None)],
        [("BUY", 100.0, 10.01), ("SELL", 100.0, 9.99)],
    ),
    "an_exit_at_a_price_behind_it": (
        [
            ENTERED,
            (at(3), "SELL", 50.0, "LIMIT", 20.0),
            (at(4), "SELL", 50.0, "LIMIT", 10.5),
            (at(7), "SELL", 40.0, "LIMIT", 10.5),
        ],
        [("BUY", 100.0, 10.01), ("SELL", 50.0, 10.5), ("SELL", 40.0, 10.5)],
        [ENTERED, (at(3), "SELL", 50.0, "LIMIT", 20.0), (at(4), "SELL", 100.0, "LIMIT", 10.5)],
        [("BUY", 100.0, 10.01), ("SELL", 100.0, 10.5)],
    ),
}
"""What each sleeve sent and filled with no rate stated at no latency, on the research path and
then on the live one, as kanso did before the keys existed."""

AT_TWENTY_MS = {
    "an_exit_at_market_behind_it": (
        [ENTERED, (at(3), "SELL", 100.0, "LIMIT", 20.0), (at(5), "SELL", 100.0, "MARKET", None)],
        [("BUY", 100.0, 10.01), ("SELL", 100.0, 10.05)],
        [ENTERED, (at(3), "SELL", 100.0, "LIMIT", 20.0), (at(4), "SELL", 100.0, "MARKET", None)],
        [("BUY", 100.0, 10.01), ("SELL", 100.0, 10.05)],
    ),
}
"""Where 20 ms moves any of it: the exit at market waits a quote longer on each path."""


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
@pytest.mark.parametrize("built", sorted(NEVER_SENT))
def test_with_no_rate_a_cancel_of_an_order_never_sent_is_held_and_each_path_does_as_before(
    built: str, latency_ms: float
) -> None:
    """With neither key stated each path does what it did before the keys existed, here too:
    a cancel of an order the engine was never handed is held back, as one of an order not yet
    handed to the venue is, and sent once the strategy submits that same object. The research
    engine answers it as not found, so on a card the order rests and fills, and an exit behind
    it is cut by it, and owed what it cut; a node obeys it and cancels the order, so the exit
    behind it goes whole at once. The two paths part there, as they did before — pinned as
    they are, since a rate that is not stated changes nothing (`docs/backlog.md` row 233) —
    where under a stated rate the cancel is neither sent nor held and both agree (below)."""
    source, levels = NEVER_SENT[built]
    groups: list[tuple[object, ...]] = [tuple(quoted(40, levels))]
    request = rated(source, None, latency_ms=latency_ms)
    engine = backtest.execute(request, [instrument()], groups)
    node = session.run_node(request, [instrument()], groups).result
    assert not node.crashed, node.traceback_tail
    did = AT_TWENTY_MS.get(built, ON_MAIN[built]) if latency_ms else ON_MAIN[built]

    assert (sent(engine), filled(engine), sent(node), filled(node)) == did


UNDER_A_RATE = {
    "sent_later": (
        [ENTERED, (at(3), "SELL", 100.0, "LIMIT", 10.04)],
        [("BUY", 100.0, 10.01), ("SELL", 100.0, 10.04)],
        Commands(submits=2, peak_60s=2, p99_60s=2),
    ),
    "an_exit_at_market_behind_it": (
        [ENTERED, (at(3), "SELL", 100.0, "LIMIT", 20.0), (at(3), "SELL", 100.0, "MARKET", None)],
        [("BUY", 100.0, 10.01), ("SELL", 100.0, 9.99)],
        Commands(submits=3, cancels=1, peak_60s=4, p99_60s=4),
    ),
    "an_exit_at_a_price_behind_it": (
        [ENTERED, (at(3), "SELL", 50.0, "LIMIT", 20.0), (at(4), "SELL", 50.0, "LIMIT", 10.5)],
        [("BUY", 100.0, 10.01), ("SELL", 50.0, 10.5)],
        Commands(submits=3, modifies=1, peak_60s=4, p99_60s=4),
    ),
}
"""What each sleeve sends, fills and counts on both paths at no latency under a rate it never
reaches."""


@pytest.mark.parametrize("built", sorted(NEVER_SENT))
def test_under_a_rate_a_cancel_of_an_order_never_sent_is_neither_sent_nor_held(built: str) -> None:
    """Under a stated rate, here one of a thousand a minute that nothing reaches, a cancel of
    an order the engine was never handed is no command: the venue holds nothing of it, so it
    is neither sent, held nor counted, and a strategy that cancels its last order on every
    point, sent or not, spends no token on one the rate dropped. The order the sleeve sends a
    quote later is its own then: it rests and fills on both paths, the exit at market behind it
    cancels it and goes whole in the same instant, and the exit at a price behind it sells what
    the order leaves and owes nothing, since no cancel of the order was ever sent."""
    source, levels = NEVER_SENT[built]
    intents, fills, counted = UNDER_A_RATE[built]
    held = both(rated(source, 1_000), [tuple(quoted(40, levels))])

    assert (sent(held), filled(held), held.commands) == (intents, fills, counted)


@pytest.mark.parametrize("per_cancel", [None, 2], ids=["one", "two"])
def test_a_cancel_takes_the_requests_the_venue_model_says_it_costs(per_cancel: int | None) -> None:
    """At three a minute the entry and the resting exit leave a token and a tenth on the
    third quote, and the bucket refills one every twenty seconds. A cancel that costs one goes
    then, and the exit the cancellation calls for is owed until the twenty-first; one that
    costs two waits until the twenty-first, and the exit until the forty-first. Either way it
    is counted as one cancel."""
    held = both(rated(deferring("cancel_order"), 3, per_cancel=per_cancel), [tuple(steady(50))])

    assert stamps(held)[-1] == (at(21 if per_cancel is None else 41), "SELL", "MARKET")
    assert held.commands is not None and held.commands.cancels == 1


NO_OP_MODIFY = {
    "unchanged": sleeve(
        """        elif self.seen == 3:
            self.modify_order(self.resting, price=Price(20.0, 2))
"""
    ),
    "filled": sleeve(
        """        elif self.seen == 6:
            self.modify_order(self.resting, price=Price(10.10, 2))
""",
        price=10.05,
    ),
}
"""A modify the engine sends nothing for: to the price the order already has, and of an order
the venue filled on the fourth quote."""


@pytest.mark.parametrize("modify", sorted(NO_OP_MODIFY))
def test_a_modify_the_engine_sends_nothing_for_takes_no_token_and_is_not_counted(
    modify: str,
) -> None:
    """At two a minute the entry and the resting exit empty the bucket, and a modify that
    would go then is dropped and counted as refused; one the engine drops, logging why, is no
    command at all, refused or sent."""
    unrated = both(rated(NO_OP_MODIFY[modify], None), [tuple(steady(10, rise_at=4))])
    held = both(rated(NO_OP_MODIFY[modify], 2), [tuple(steady(10, rise_at=4))])

    assert unrated.commands is not None and unrated.commands.modifies == 0
    assert held.commands == unrated.commands


NOT_AN_ORDER = {
    "none": ("self.modify_order(None, price=Price(10.05, 2))", "'order' argument was `None`"),
    "a_quote": (
        "self.modify_order(tick, price=Price(10.05, 2))",
        "Argument 'order' has incorrect type",
    ),
}
"""A modify of what `submit_exit` answers when it sent nothing, and of a value that is no order
at all, and how the engine's own `modify_order` refuses each."""


@pytest.mark.parametrize("per_minute", [None, 2], ids=["unset", "stated"])
@pytest.mark.parametrize("handed", sorted(NOT_AN_ORDER))
def test_a_modify_of_what_is_not_an_order_raises_the_engine_s_own_error_on_both_paths(
    handed: str, per_minute: int | None
) -> None:
    """`submit_exit` answers `None` when it sends nothing, and a strategy that moves what it was
    answered without looking hands `modify_order` no order. kanso hands that, and a value of any
    other type, to the engine's own `modify_order` as it is, the rate reading nothing of it, so
    the engine raises its own error on both paths, as it did before the keys existed — with
    neither key stated, and under a rate alike."""
    call, raised = NOT_AN_ORDER[handed]
    request = rated(sleeve(f"        elif self.seen == 3:\n            {call}\n"), per_minute)
    groups: list[tuple[object, ...]] = [tuple(steady(5))]

    with pytest.raises(TypeError, match=raised):
        backtest.execute(request, [instrument()], groups)
    node = session.run_node(request, [instrument()], groups).result
    assert node.crashed
    assert f'TypeError("{raised}' in str(node.traceback_tail)


OWED_BEFORE_AN_ENTRY = sleeve(
    """        elif self.seen == 3:
            self.submit_exit(name)
        elif self.seen == 61:
            self.submit_order(
                self.order_factory.limit(name, OrderSide.BUY, Quantity.from_int(1), Price(5.0, 2))
            )
"""
)
"""Exits at market on its third quote, which cancels its resting exit first, and sends an
entry of one on its sixty-first."""


def test_an_exit_owed_for_want_of_a_token_takes_one_before_the_handler_can() -> None:
    """At two a minute the cancel the exit calls for waits for the thirty-first quote, and the
    exit, owed behind it, finds the bucket empty again and is owed for want of a token. On the
    sixty-first a token comes back: the exit, asked for thirty seconds before, takes it before
    the handler runs, and the entry the handler sends finds the bucket empty and is dropped."""
    unrated = both(rated(OWED_BEFORE_AN_ENTRY, None), [tuple(steady(70))])
    held = both(rated(OWED_BEFORE_AN_ENTRY, 2), [tuple(steady(70))])

    assert stamps(unrated)[2:] == [(at(3), "SELL", "MARKET"), (at(61), "BUY", "LIMIT")]
    assert stamps(held)[2:] == [(at(61), "SELL", "MARKET")], "the exit went first"
    assert [(fill.side, fill.qty) for fill in held.run.fills] == [("BUY", 100.0), ("SELL", 100.0)]
    assert held.commands == Commands(submits=3, cancels=1, peak_60s=3, p99_60s=3, refused_submits=1)


OWED_BEFORE_A_MODIFY = sleeve(
    """        elif self.seen == 3:
            self.submit_exit(name)
        elif self.seen == 31:
            self.modify_order(self.buy, price=Price(5.5, 2))
""",
    second="self.buy = self.order_factory.limit(name, OrderSide.BUY, Quantity.from_int(50), "
    "Price(5.0, 2))\n            self.submit_order(self.buy)",
)
"""Buys 100 at market, rests a buy of 50 at 5.00, asks for the whole position at market on its
third quote and moves the buy to 5.50 on its thirty-first."""


def test_an_exit_owed_for_want_of_a_token_goes_before_a_modify_asked_after_it() -> None:
    """At two a minute the entry and the resting buy empty the bucket, and the exit at market
    asked for on the third quote — the buy is on the opening side, so there is nothing for it to
    cancel — finds no token and is owed for want of one. On the thirty-first a token is back: the
    exit takes it before the handler runs, and the modify the handler sends finds none and is
    dropped and counted, the buy staying at 5.00. Unheld, the exit went on the third quote and
    the modify on the thirty-first."""
    unrated = both(rated(OWED_BEFORE_A_MODIFY, None), [tuple(steady(40))])
    held = both(rated(OWED_BEFORE_A_MODIFY, 2), [tuple(steady(40))])

    assert unrated.commands is not None and unrated.commands.modifies == 1
    assert stamps(held)[2:] == [(at(31), "SELL", "MARKET")]
    assert held.commands == Commands(submits=3, peak_60s=3, p99_60s=3, refused_modifies=1)


OWED_THEN_CANCELLED = sleeve(
    """        elif self.seen == 3:
            self.submit_exit(name)
        elif self.seen == 4:
            self.cancel_order(self.buy)
""",
    second="self.buy = self.order_factory.limit(name, OrderSide.BUY, Quantity.from_int(50), "
    "Price(9.0, 2))\n            self.submit_order(self.buy)",
)
"""Buys 100 at market, rests a buy of 50 at 9.00, asks for the whole position at market on its
third quote and cancels the buy on its fourth."""


@pytest.mark.parametrize(
    ("gap", "exited_at"),
    [(1, at(31)), (300, at(4) + 330 * SECOND_NS)],
    ids=["a_second", "five_minutes"],
)
def test_the_deferred_cancels_take_the_tokens_that_come_back_before_an_owed_exit(
    gap: int, exited_at: int
) -> None:
    """At two a minute, two requests a cancel, the entry and the resting buy empty the bucket:
    the exit at market asked for on the third quote finds no token and is owed for want of one,
    and the cancel of the buy asked for on the fourth is deferred. Before each handler the
    deferred cancels the bucket holds tokens for are sent first, and then the exits owed for
    want of a token are asked for again. With the fifth quote a second after the fourth, a
    token is back on the thirty-first and the exit takes it, the cancel needing two; with it
    five minutes after, the bucket is full again there, the cancel, asked for after the exit,
    takes both, and the exit goes thirty seconds later."""
    fifth = at(4) + gap * SECOND_NS
    groups: list[tuple[object, ...]] = [(*quoted(4), *quoted(196, first=fifth))]
    held = both(rated(OWED_THEN_CANCELLED, 2, per_cancel=2), groups)

    assert stamps(held)[2:] == [(exited_at, "SELL", "MARKET")]
    assert held.commands is not None and held.commands.cancels == 1


OWED_BEHIND_A_CANCEL_OVERNIGHT = sleeve(
    """        elif self.seen == 3:
            self.submit_exit(name)
        elif self.seen == 4:
            THEN
"""
)
"""Buys 100 at market, rests an exit of it at 20.00, asks for the whole position at market on its
third quote and does `THEN` on its fourth, the next session's first."""


@pytest.mark.parametrize(
    ("then", "sold", "after"),
    [("pass", 100.0, 0), ('self.submit_entry(name, "BUY", qty=10)', 110.0, 20)],
    ids=["alone", "behind_an_entry"],
)
def test_an_exit_owed_behind_a_deferred_cancel_is_asked_for_once_the_cancel_has_landed(
    then: str, sold: float, after: int
) -> None:
    """At three a minute, two requests a cancel, the entry and the resting exit leave a token and
    a tenth on the third quote, too few for the cancel the exit at market calls for: the cancel
    is deferred, and the exit owed behind it. The next session opens a day later with the bucket
    full, and the cancel goes before the handler of its first quote, leaving one token. The exit
    owed behind it is asked for again once the cancel has landed, after that handler: alone it
    takes the token there, and behind the entry that handler sends, which takes the token first,
    it goes twenty seconds later, once the next one is back, selling the 10 bought as well."""
    day_two = T0 + 24 * 3_600 * SECOND_NS
    groups: list[tuple[object, ...]] = [(*steady(3), *quoted(30, first=day_two))]
    source = OWED_BEHIND_A_CANCEL_OVERNIGHT.replace(b"THEN", then.encode())
    held = both(rated(source, 3, per_cancel=2), groups)

    entered = [(day_two, "BUY", 10.0, "MARKET")] if after else []
    assert [(ts, side, qty, kind) for ts, _, side, qty, kind, _ in held.intents][2:] == [
        *entered,
        (day_two + after * SECOND_NS, "SELL", sold, "MARKET"),
    ]
    assert held.commands is not None
    assert (held.commands.cancels, held.commands.refused_submits) == (1, 0)


GROWN = sleeve(
    """        elif self.seen == 3:
            self.submit_exit(name)
""",
    second=resting_buy(9.9),
)
"""Buys 100 at market, rests a buy of 50 at 9.90, and asks for the whole position at market on
its third quote."""


def test_an_exit_owed_for_the_rate_is_asked_for_as_it_was_asked() -> None:
    """At two a minute the exit at market of the whole position finds the bucket empty and is
    owed as asked, the whole position, not the 100 it would have sold: the resting buy fills
    when the ask reaches it on the tenth quote, and on the thirty-first the exit sells 150."""
    held = both(rated(GROWN, 2), [tuple(quoted(40, {10: (9.88, 9.90)}))])

    assert held.intents[-1][2:5] == ("SELL", 150.0, "MARKET")
    assert stamps(held)[-1] == (at(31), "SELL", "MARKET")


def rule_says_exit_from(ts_ns: int) -> bytes:
    """An exit rule that says so once, at the first point at or after `ts_ns`."""
    from tests.nautilus.backtest.test_exit_flat import exit_once_from

    return exit_once_from(ts_ns)


RULED = sleeve(
    """        elif self.seen == 4:
            self.submit_exit(name, price=19.0)
""",
    second=resting_buy(9.0),
)
"""Buys 100 at market and rests a buy of 50 far below, then asks for an exit at 19.00 on its
fourth quote, a quote after its exit rule closed at market."""


def test_an_exit_rule_s_owed_exit_is_not_replaced_by_its_host_s() -> None:
    """At two a minute the exit rule's close on the third quote is owed for want of a token,
    and so is the host's exit at a price on the fourth; the rule's stays owed as the rule's,
    and on the thirty-first it goes at market."""
    rule = (("exit", rule_says_exit_from(at(3)), {}),)
    held = both(rated(RULED, 2, modifiers=rule), [tuple(steady(40))])

    assert stamps(held)[-1] == (at(31), "SELL", "MARKET")


PRICED_BEHIND_A_DEFERRED_CANCEL = sleeve(
    """        elif self.seen == 3:
            self.cancel_order(self.resting)
        elif self.seen == 4:
            self.submit_exit(name, price=19.0)
"""
)
"""Cancels its resting exit on its third quote and asks for one at 19.00 on its fourth."""


def test_an_exit_at_a_price_behind_a_deferred_cancel_is_owed_until_it_lands() -> None:
    """At two a minute the cancel waits for the thirty-first quote, and the order it cancels
    is working until then, so the exit at 19.00 is owed behind it, as behind a cancel in
    flight; once the cancel has gone the exit finds the bucket empty again, and goes on the
    sixty-first."""
    held = both(rated(PRICED_BEHIND_A_DEFERRED_CANCEL, 2), [tuple(steady(70))])

    assert stamps(held)[-1] == (at(61), "SELL", "LIMIT")
    assert held.intents[-1][5] == 19.0


def test_a_cancel_deferred_before_a_run_of_prints_goes_on_the_print_a_token_comes_back_at() -> None:
    """Three quotes and then only prints: the cancel deferred on the third quote goes on the
    thirty-first point, a print, and the exit its cancellation calls for on the sixty-first."""
    groups: list[tuple[object, ...]] = [tuple(steady(3)), tuple(printed(range(4, 71)))]
    held = both(rated(deferring("cancel_order"), 2), groups)

    assert stamps(held)[-1] == (at(61), "SELL", "MARKET")


TWICE_AT_ONCE = sleeve(
    """        elif self.seen == 3:
            order = self.order_factory.limit(
                name, OrderSide.SELL, Quantity.from_int(100), Price(21.0, 2)
            )
            self.submit_order(order)
            self.cancel_order(order)
            self.cancel_order(order)
"""
)
"""Rests a second exit on its third quote and cancels it twice in the same handler, while a
node has not handed it to the venue yet."""

MODIFIED_TWICE = sleeve(
    """        elif self.seen == 60:
            self.modify_order(self.resting, price=Price(19.0, 2))
            self.submit_exit(name)
            self.submit_exit(name)
"""
)
"""Moves its resting exit and exits at market twice in one handler, each exit cancelling the
order being modified behind the modify."""

EMULATED_TWICE = sleeve(
    """        elif self.seen == 3:
            self.cancel_order(self.emulated)
            self.cancel_order(self.emulated)
""",
    second="self.emulated = self.order_factory.limit(name, OrderSide.SELL, "
    "Quantity.from_int(100), Price(20.0, 2), emulation_trigger=TriggerType.DEFAULT)\n"
    "            self.submit_order(self.emulated)",
)
"""Rests an emulated exit, and cancels it twice in one handler."""


@pytest.mark.parametrize(
    ("source", "latency_ms"),
    [(TWICE_AT_ONCE, 0.0), (MODIFIED_TWICE, 20.0), (EMULATED_TWICE, 0.0)],
    ids=["held_until_the_venue_has_it", "behind_a_modify", "emulated"],
)
def test_a_cancel_of_an_order_whose_cancel_is_on_its_way_is_no_command(
    source: bytes, latency_ms: float
) -> None:
    """A second cancel of an order whose cancel kanso holds until the venue has the order, or
    holds behind a modify, or whose cancel the emulator already took, sends nothing, so it is
    counted on neither path and takes no token on either."""
    unrated = both(rated(source, None, latency_ms=latency_ms), [tuple(steady(70))])
    held = both(rated(source, 1_000, latency_ms=latency_ms), [tuple(steady(70))])

    assert unrated.commands is not None and unrated.commands.cancels == 1
    assert held.commands == unrated.commands


ACROSS_A_GAP = deferring("cancel_order", price=10.5)
"""The deferring sleeve, its resting exit at 10.50."""


def test_an_order_whose_cancel_waits_over_a_session_s_close_can_fill_at_the_next_open() -> None:
    """A sleeve acts only on points, and the venue matches a point before any handler of it
    runs. The cancel deferred on a session's last quote waits for the next session's first,
    where the market has opened above the resting exit: the venue fills it there, before the
    cancel goes. Unheld, the cancel went at once and the exit at market with it."""
    day_two = quoted(5, {1: (10.6, 10.62)}, first=T0 + 24 * 3_600 * SECOND_NS)
    groups: list[tuple[object, ...]] = [(*steady(3), *day_two)]

    unrated = both(rated(ACROSS_A_GAP, None), groups)
    held = both(rated(ACROSS_A_GAP, 2), groups)

    assert [(fill.side, fill.px) for fill in unrated.run.fills] == [
        ("BUY", 10.01),
        ("SELL", 9.99),
    ]
    assert [(fill.side, fill.px) for fill in held.run.fills] == [
        ("BUY", 10.01),
        ("SELL", 10.5),
    ]


def flattened(
    request: RunRequest, groups: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch
) -> tuple[list[tuple[str, float]], int, RunResult]:
    """The request on the node path, closed after the window's last point by
    `kanso.nautilus.node._closed`, the way a stage's `_drive` closes it; with the positions
    and the number of orders still open once the close has landed."""
    seen: dict[str, Any] = {}
    venues_of = session._venues

    def venues(*args: Any) -> Any:
        seen["venues"] = venues_of(*args)
        return seen["venues"]

    async def drive(built: Any, client: Any, strategy: Any, halt: Any, chunks: Any) -> None:
        runner = asyncio.create_task(built.run_async())
        await session._started(built, client, strategy)
        await client.settle()
        for _ in chunks:
            await client.settle()
            await client.replay()
        await node._closed(client, [strategy], seen["venues"])
        cache = built.kernel.cache
        seen["open"] = [
            (str(position.instrument_id), float(position.signed_qty))
            for position in cache.positions_open()
        ]
        seen["orders"] = len(cache.orders_open())
        await built.stop_async()
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        await session._cleared()

    monkeypatch.setattr(session, "_venues", venues)
    monkeypatch.setattr(session, "_drive", drive)
    staged = session.run_node(request, [instrument()], groups).result
    return seen["open"], seen["orders"], staged


DEFERRED_AT_THE_END = sleeve(
    """        elif self.seen == 3:
            self.cancel_order(self.resting)
        elif self.seen == 4:
            self.submit_exit(name)
"""
)
"""Cancels its resting exit on its third quote and exits at market on its fourth: at two a
minute both wait for a token that ten quotes do not bring back."""


def test_a_stage_s_flatten_lifts_the_rate_and_sends_what_the_bucket_held_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the window's end the cancel is still deferred and the exit owed. The flatten is the
    stage's: it lifts the rate, sends the deferred cancel and closes the position at market,
    and the stage stops flat with no order open."""
    held, working, staged = flattened(
        rated(DEFERRED_AT_THE_END, 2), [tuple(steady(10))], monkeypatch
    )

    assert (held, working) == ([], 0)
    assert [(fill.side, fill.qty) for fill in staged.run.fills] == [
        ("BUY", 100.0),
        ("SELL", 100.0),
    ]
