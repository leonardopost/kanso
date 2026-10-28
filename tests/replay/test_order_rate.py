"""An account's order rate, held on the data clock, on both code paths alike.

A sleeve under a venue model that states `costs.orders_per_minute` takes a token for every
command it sends from a bucket of that many, refilled at that many a minute of the data
clock — a cancel as many as `requests_per_cancel` says. With the bucket empty an entry, an
entry's list and a modify are dropped unsent and counted as refused, and the run goes on; any
other exit is owed and a cancel deferred, and what waited goes first once a token comes back;
an exit kanso cannot send again as it was asked is refused. Each scenario here runs on the
research path (`backtest.execute`) and on the live one (`session.run_node`) and asserts both
the behaviour and that the two agree on every intent, fill and count, the refused ones
included; and with the key unset nothing a run does moves.

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
    count: int, levels: dict[int, tuple[float, float]] | None = None, *, first: int = T0
) -> list[QuoteTick]:
    """`count` quotes a second apart from `first`, 1,000 a side, at 9.99/10.01 and, from each
    `levels` key on, at its bid and ask."""
    made: list[QuoteTick] = []
    bid, ask = 9.99, 10.01
    for seen in range(1, count + 1):
        bid, ask = (levels or {}).get(seen, (bid, ask))
        instant = first + (seen - 1) * SECOND_NS
        made.append(
            QuoteTick(
                NAME,
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


def both(request: RunRequest, groups: list[tuple[object, ...]]) -> RunResult:
    """The request on both code paths, asserted to agree on every intent, fill and count."""
    engine = backtest.execute(request, [instrument()], groups)
    node = session.run_node(request, [instrument()], groups).result
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


HAND_BUILT_CLOSE = sleeve(
    """        elif self.seen == 3:
            self.cancel_order(self.resting)
        elif self.seen == 4:
            self.close_position(self.cache.positions_open(strategy_id=self.id)[0])
"""
)
"""The same, closing by the engine's own `close_position`, a market order it builds."""


def test_an_exit_the_strategy_built_at_market_is_owed_as_submit_exit_would_send_it() -> None:
    held = both(rated(HAND_BUILT_CLOSE, 3), [tuple(steady(40))])

    assert stamps(held)[-1] == (at(21), "SELL", "MARKET")
    assert held.intents[-1][3] == 100.0


HAND_BUILT = {
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
"""Exits a strategy built itself that `submit_exit` cannot send again as they were asked: a
stop; a limit that would not rest, would rest only as a maker, would expire or would show part
of itself; a size in the quote's currency; an emulated order; an order of a list."""


def hand_built(order: str) -> bytes:
    """Empties the bucket on its first three quotes, then closes by `order`, built itself."""
    return sleeve(
        f"""        elif self.seen == 3:
            self.cancel_order(self.resting)
        elif self.seen == 4:
            self.submit_order({order})
"""
    )


@pytest.mark.parametrize("built", sorted(HAND_BUILT))
def test_an_exit_kanso_cannot_send_again_as_it_was_asked_is_refused(built: str) -> None:
    """Owed and sent again through `submit_exit`, each would go as a plain order at market or
    at a good-till-cancelled limit: an immediate-or-cancel exit would rest and fill later, a
    post-only one take, a stop fill at once. So past the rate each is refused like an entry."""
    refusal = refused(rated(hand_built(HAND_BUILT[built]), 3), [tuple(steady(10))])

    assert (refusal.rule, refusal.asked, refusal.ts_event) == (ORDER_RATE, "SELL", at(4))
    assert refusal.why.startswith("an exit kanso cannot send again as it was asked needed 1")


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


def test_a_list_that_closes_a_position_past_the_rate_is_refused() -> None:
    """An order of a list is an exit kanso cannot send again as it was asked, so a list whose
    first order closes the position is refused past the rate, as each of its orders would be,
    rather than dropped with the position left open."""
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
    assert "an exit kanso cannot send again as it was asked is refused" in refusal.why


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


def test_an_exit_the_rate_owed_takes_the_token_before_the_handler_can() -> None:
    """At two a minute the cancel the exit calls for waits for the thirty-first quote, and the
    exit, owed behind it, finds the bucket empty again and is owed for want of a token. On the
    sixty-first a token comes back: the exit, asked for thirty seconds before, takes it before
    the handler runs, as an account working its requests in turn would send it, and the entry
    the handler sends finds the bucket empty and is dropped."""
    unrated = both(rated(OWED_BEFORE_AN_ENTRY, None), [tuple(steady(70))])
    held = both(rated(OWED_BEFORE_AN_ENTRY, 2), [tuple(steady(70))])

    assert stamps(unrated)[2:] == [(at(3), "SELL", "MARKET"), (at(61), "BUY", "LIMIT")]
    assert stamps(held)[2:] == [(at(61), "SELL", "MARKET")], "the exit went first"
    assert [(fill.side, fill.qty) for fill in held.run.fills] == [("BUY", 100.0), ("SELL", 100.0)]
    assert held.commands == Commands(submits=3, cancels=1, peak_60s=3, p99_60s=3, refused_submits=1)


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


def test_what_waits_for_a_token_goes_on_the_first_point_after_it_once_the_venue_matched_it() -> (
    None
):
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
