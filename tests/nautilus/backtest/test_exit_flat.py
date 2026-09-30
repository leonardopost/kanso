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

from kanso.criteria.gates import max_hold
from kanso.data.loaders.synthetic import SyntheticLoader
from kanso.nautilus.backtest import execute
from kanso.schemas import Hypothesis
from tests.criteria.builders import context
from tests.nautilus.backtest.conftest import RESEARCH, SYMBOL, VENUE, hypothesis, instrument

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


def points(sessions: tuple[date, date] = SESSIONS) -> list[tuple[object, ...]]:
    """Quotes and prints a second apart over ten minutes of each session, from the generator."""
    loader = SyntheticLoader()
    spec = {
        "seed": 7,
        "instruments": [SYMBOL],
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


def _run(request_for, latency_ms: float, source: bytes = CHASING, modifiers=()):
    document = hypothesis().model_dump(mode="json")
    document.update(resolution="tick", data_requirements=["quote", "trade"])
    document["costs"] = chasing_costs(latency_ms)
    hyp = Hypothesis.model_validate(document)
    request = request_for(RESEARCH, source=source, hypothesis_=hyp, modifiers=modifiers)
    result = execute(request, [instrument()], points())
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
    held back is owed and goes at market once the cancel has landed. Measured before the
    exit was owed: at 20 ms the sleeve never sold, and held the 100 shares overnight."""
    flat_within_the_session(_run(request_for, latency_ms, MARKET_AFTER_CANCEL))


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_a_stop_at_market_cancels_the_take_profit_it_would_otherwise_wait_on(
    request_for, latency_ms: float
) -> None:
    """A market exit the sleeve's own resting take-profit would cut is not held back by it:
    the take-profit is cancelled and the stop takes the position. Measured before: with the
    take-profit counted, at 0 ms the stop was sized to nothing and the sleeve held overnight;
    before exits counted the ones working at all, at 20 ms each stop in flight was joined by
    another and the sleeve sold 200 of the 100 it had bought."""
    run = _run(request_for, latency_ms, STOP_OVER_TAKE_PROFIT)
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
    Measured before: every later cancel threw the owed exit away and the 100 shares were
    held overnight, where the market order sent at once would have sold them."""
    flat_within_the_session(_run(request_for, 20.0, taken_back(cancel)))


@pytest.mark.parametrize("latency_ms", [0.0, 20.0, 1500.0])
def test_housekeeping_cancels_do_not_strand_an_exit_at_market(
    request_for, latency_ms: float
) -> None:
    """The sleeve cancels its take-profit and exits at market once, then cancels every order
    it has on every quote after. Measured before: the first of those cancels threw the owed
    exit away, and the position was held overnight at every latency but none."""
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
    Measured before: nothing was owed, the stop was never sent again, and the 100 shares
    were held overnight."""
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
    not take it back, and it goes once the cancel has landed. Measured before the exit was
    owed: at 20 ms the rule's exit was sized to nothing and the position held overnight."""
    quotes = [point for point in points()[0] if type(point).__name__ == "QuoteTick"]
    rule = (("exit", exit_once_from(quotes[29].ts_event), {}),)
    flat_within_the_session(_run(request_for, latency_ms, HOUSEKEEPING, rule))


@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
@pytest.mark.parametrize(
    "cancel", ["self.cancel_order(order)", "self.cancel_all_orders(instrument_id)"]
)
def test_an_exit_cancelled_in_flight_still_counts_until_its_cancel_lands(
    request_for, cancel: str, latency_ms: float
) -> None:
    """The venue takes an order before the cancel that follows it, so a marketable exit
    cancelled while still in flight fills, whatever the latency. The market exit sent
    beside it is held back and owed; the old exit's fill leaves the position flat, the owed
    exit is dropped there, and the short the sleeve opens later is its own. Measured before:
    at 0 ms, with the cancelled order read as spent, both exits filled and the sleeve was
    short 100 before it ever asked to be."""
    run = _run(request_for, latency_ms, CANCELLED_IN_FLIGHT.replace(b"CANCEL", cancel.encode()))
    assert [(fill.side, fill.qty) for fill in run.fills] == [
        ("BUY", 100.0),
        ("SELL", 100.0),
        ("SELL", 100.0),
    ]
    assert never_short(run.fills[:2]) == (100.0, 100.0, 0.0)
