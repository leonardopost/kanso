"""An account's order rate, held on the data clock, on both code paths alike.

A sleeve under a venue model that states `costs.orders_per_minute` takes a token for every
command it sends from a bucket of that many, refilled at that many a minute of the data
clock. With the bucket empty an entry, a modify and a list are refused, an exit is owed and a
cancel deferred. Each scenario here runs on the research path (`backtest.execute`) and on
the live one (`session.run_node`) and asserts both the behaviour and that the two agree on
every intent, fill and count; and with the key unset nothing a run does moves.

The quotes are a second apart, so a rate of `n` a minute refills a token every `60 / n`
quotes, and every instant asserted below is arithmetic on that: the bucket keeps its level
in token-nanoseconds, so the first quote that finds a whole token is exact.
"""

from __future__ import annotations

import pickle
from dataclasses import replace
from typing import Any

import pytest
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.nautilus import backtest, session
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


def steady(count: int, *, rise_at: int | None = None) -> list[QuoteTick]:
    """`count` quotes a second apart at 9.99/10.01, 1,000 a side; from the `rise_at`th on,
    10.05/10.07."""
    made: list[QuoteTick] = []
    for seen in range(1, count + 1):
        risen = rise_at is not None and seen >= rise_at
        bid, ask = (10.05, 10.07) if risen else (9.99, 10.01)
        made.append(
            QuoteTick(
                InstrumentId(Symbol(SYMBOL), Venue(VENUE)),
                Price(bid, 2),
                Price(ask, 2),
                Quantity.from_int(1_000),
                Quantity.from_int(1_000),
                ts_event=at(seen),
                ts_init=at(seen),
            )
        )
    return made


HYP = hypothesis(
    resolution="tick",
    horizon="1d",
    data_requirements=["quote", "trade"],
    risk_limits={"max_position_pct": 100, "max_drawdown_pct": 40, "max_leverage": 1},
)


def rated(source: bytes, per_minute: int | None, *, latency_ms: float = 0.0) -> RunRequest:
    """The request both paths run, its venue model stating `per_minute` when one is given."""
    request = request_for(source=source, hyp=HYP)
    model = dict(request.venue_model)
    costs: dict[str, Any] = {**dict(model["costs"]), "latency_ms": latency_ms}  # type: ignore[call-overload]
    if per_minute is not None:
        costs["orders_per_minute"] = per_minute
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
from nautilus_trader.model.enums import OrderSide
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
            self.resting = self.submit_exit(name, price=PRICE)
"""


def sleeve(body: str, *, price: float = 20.0, extra: str = "") -> bytes:
    """Buys 100 at market on its first quote and rests an exit of it at `price` on its second;
    `body` is what it does on later quotes, `extra` any further method."""
    return (
        HEAD.replace(b"PRICE", repr(price).encode())
        + body.encode()
        + (b"\n" + extra.encode() if extra else b"")
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


HAND_BUILT_STOP = sleeve(
    """        elif self.seen == 3:
            self.cancel_order(self.resting)
        elif self.seen == 4:
            stop = self.order_factory.stop_market(
                name, OrderSide.SELL, Quantity.from_int(100), trigger_price=Price(9.0, 2)
            )
            self.submit_order(stop)
"""
)
"""The same, protecting it with a stop it built itself, which kanso cannot send again as it was."""


def test_an_exit_kanso_cannot_send_again_as_it_was_asked_is_refused() -> None:
    refusal = refused(rated(HAND_BUILT_STOP, 3), [tuple(steady(10))])

    assert (refusal.rule, refusal.asked, refusal.ts_event) == (ORDER_RATE, "SELL", at(4))
    assert refusal.why.startswith("an exit kanso cannot send again as it was asked needed 1")


def deferring(call: str) -> bytes:
    """Cancels its resting exit twice on its third quote by `call`, and exits at market once
    the venue has cancelled it."""
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


def test_a_cancel_behind_a_modify_past_the_rate_waits_behind_it_again_on_both_paths() -> None:
    """At two a minute and 20 ms the modify takes the last whole token on the sixtieth quote,
    so the exit at market's cancel of the order being modified is deferred, still behind the
    modify, and the exit is owed; on the sixty-first a token has come back, the modify is
    not answered yet, and the cancel goes behind it. The exit goes once the cancel has landed
    and a token has come back again, thirty seconds on."""
    held = both(rated(BEHIND_A_MODIFY, 2, latency_ms=20.0), [tuple(steady(100))])

    assert stamps(held) == [
        (at(1), "BUY", "MARKET"),
        (at(2), "SELL", "LIMIT"),
        (at(91), "SELL", "MARKET"),
    ]
    assert held.commands == Commands(submits=3, modifies=1, cancels=1, peak_60s=3, p99_60s=3)


def test_an_entry_past_the_rate_is_refused_on_both_paths() -> None:
    twice = sleeve(
        """        elif self.seen == 3:
            self.submit_entry(name, "BUY", qty=100)
"""
    )

    refusal = refused(rated(twice, 2), [tuple(steady(10))])

    assert (refusal.rule, refusal.asked, refusal.ts_event) == (ORDER_RATE, "BUY", at(3))
    assert refusal.held == {f"{SYMBOL}.{VENUE}": 100.0}
    assert "the bucket held 0 after 2 in the 60 s before it" in refusal.why


def test_a_modify_past_the_rate_is_refused_whichever_order_it_moves() -> None:
    moving = sleeve(
        """        elif self.seen == 3:
            self.modify_order(self.resting, price=Price(19.0, 2))
"""
    )

    refusal = refused(rated(moving, 2), [tuple(steady(10))])

    assert (refusal.rule, refusal.asked, refusal.ts_event) == (ORDER_RATE, "MODIFY", at(3))


def test_a_list_takes_a_token_for_each_of_its_orders_and_is_refused_short_of_them() -> None:
    bracketed = HEAD.replace(
        b"""        if self.seen == 1:
            self.submit_entry(name, "BUY", qty=100)""",
        b"""        if self.seen == 1:
            self.submit_order_list(
                self.order_factory.bracket(
                    name,
                    OrderSide.BUY,
                    Quantity.from_int(100),
                    sl_trigger_price=Price(9.0, 2),
                    tp_price=Price(11.0, 2),
                )
            )""",
    ).replace(b"PRICE", b"20.0")

    refusal = refused(rated(bracketed, 2), [tuple(steady(5))])

    assert (refusal.rule, refusal.ts_event) == (ORDER_RATE, at(1))
    assert refusal.why.startswith("a list of 3 orders needed 3 of the 2 order commands")


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
