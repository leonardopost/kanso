"""The warmup gate: a sleeve fed its prefix sees everything and places nothing.

Every test runs the real engine, because the claim is about what reaches the venue: an
order dropped by the gate never becomes a fill, and one placed after the open does.
"""

from __future__ import annotations

from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Quantity

from kanso.nautilus.cross_section import warm
from kanso.nautilus.hooks import OVERLAY
from kanso.nautilus.strategy import (
    Decision,
    Hedge,
    HookContext,
    KansoModifier,
    KansoStrategy,
)

from .conftest import DEEP, DEMO, HEDGE, LATENCY_NS, MINUTE_NS, bar, flat, saw_tooth
from .test_modifiers import Attached, attached, host
from .test_sleeve import config

OPEN_INDEX = 5
"""The saw-tooth bar the window opens on; the four before it are the prefix."""


def opens_ns() -> int:
    """The availability instant of the bar the window opens on."""
    return int(saw_tooth(DEMO)[OPEN_INDEX].ts_init)


class Eager(KansoStrategy):
    """Tries to enter on every bar and records what each attempt returned."""

    def on_start(self) -> None:
        self.answers: list[object] = []
        self.seen: list[int] = []

    def on_bar(self, bar_: Bar) -> None:
        self.seen.append(int(bar_.ts_init))
        self.answers.append(self.submit_entry(DEMO, "BUY", qty=10))


def warmed(strategy: KansoStrategy) -> KansoStrategy:
    warm(strategy, opens_ns())
    return strategy


# --- what the gate drops -----------------------------------------------------


def test_every_handler_runs_over_the_prefix_and_no_order_leaves_it(backtest) -> None:
    run = backtest(warmed(Eager(config())))
    strategy = run.strategy

    assert strategy.seen == [int(point.ts_init) for point in saw_tooth(DEMO)]
    assert strategy.answers[:OPEN_INDEX] == [None] * OPEN_INDEX
    assert strategy.answers[OPEN_INDEX] is not None
    assert run.strategy.intents[0].ts_event == saw_tooth(DEMO)[OPEN_INDEX].ts_event
    assert all(
        int(fill.ts_event) >= opens_ns() for fill in run.engine.cache.orders_closed()[0].events
    )


def test_a_sleeve_not_warmed_trades_from_its_first_bar(backtest) -> None:
    """No prefix, no gate: today's behaviour, byte for byte."""
    run = backtest(Eager(config()))

    assert run.strategy.answers[0] is not None
    assert run.strategy.intents[0].ts_event == saw_tooth(DEMO)[0].ts_event


def test_a_hand_built_order_in_the_prefix_is_dropped_before_any_check(backtest) -> None:
    """Under a sizing rule a hand-built order is a refusal; in the prefix it is nothing."""

    class ByHand(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0

        def on_bar(self, bar_: Bar) -> None:
            self.bars += 1
            if self.bars <= OPEN_INDEX:
                order = self.order_factory.market(DEMO, OrderSide.BUY, Quantity.from_int(7))
                self.submit_order(order)

    run = backtest(warmed(ByHand(config(sizing_budget=50_000.0))))

    assert run.strategy.intents == ()
    assert run.engine.cache.positions() == []


def test_an_order_list_in_the_prefix_is_dropped_whole(backtest) -> None:
    class Bracketed(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0

        def on_bar(self, bar_: Bar) -> None:
            self.bars += 1
            if self.bars == 2:
                self.submit_order_list(
                    self.order_factory.bracket(
                        DEMO,
                        OrderSide.BUY,
                        Quantity.from_int(10),
                        sl_trigger_price=self.cache.instrument(DEMO).make_price(9.0),
                        tp_price=self.cache.instrument(DEMO).make_price(12.0),
                    )
                )

    run = backtest(warmed(Bracketed(config())))

    assert run.strategy.intents == ()


def test_the_gate_is_keyed_on_availability_and_not_on_the_reference_time(backtest) -> None:
    """A bar closing before the open but published after it is in the window."""
    early = saw_tooth(DEMO)[:OPEN_INDEX]
    late = bar(DEMO, OPEN_INDEX, 10.0, DEEP)
    straddling = Bar(
        late.bar_type,
        late.open,
        late.high,
        late.low,
        late.close,
        late.volume,
        ts_event=int(late.ts_event) - MINUTE_NS // 2,
        ts_init=opens_ns(),
    )
    after = [bar(DEMO, index, 10.0, DEEP) for index in range(OPEN_INDEX + 1, 12)]

    run = backtest(warmed(Eager(config())), data=[*early, straddling, *after])

    assert run.strategy.answers[:OPEN_INDEX] == [None] * OPEN_INDEX
    assert run.strategy.answers[OPEN_INDEX] is not None
    assert run.strategy.intents[0].ts_event == int(straddling.ts_event)
    assert int(straddling.ts_event) < opens_ns() - LATENCY_NS


# --- what the gate leaves alone -----------------------------------------------


class Counting(KansoModifier):
    """An overlay whose data clock counts its consultations and always asks for a clip."""

    construct = OVERLAY
    config_cls = Attached
    asked: list[int] = []

    def on_data(self, ctx: HookContext) -> Decision:
        Counting.asked.append(ctx.ts_event)
        if ctx.book.get("HEDGE.XNAS", 0.0) == 0.0:
            return Decision(hedges=(Hedge("HEDGE.XNAS", -10.0),))
        return Decision.neutral(self.construct)


def test_an_overlay_s_clock_is_consulted_over_the_prefix_and_its_answer_dropped(
    backtest,
) -> None:
    Counting.asked = []
    points = [*saw_tooth(DEMO), *flat(HEDGE, close=20.0, volume=DEEP)]
    strategy = host(universe=("DEMO.XNAS", "HEDGE.XNAS"))
    warm(strategy, opens_ns())

    run = backtest(strategy, [attached(Counting)], data=points, instruments=(DEMO, HEDGE))
    hedges = [i for i in run.strategy.intents if i.instrument_id == "HEDGE.XNAS"]

    assert len(Counting.asked) >= len(saw_tooth(DEMO)), "asked on every bar, prefix included"
    assert hedges, "and its clip is placed once the window opens"
    assert hedges[0].ts_event >= saw_tooth(DEMO)[OPEN_INDEX].ts_event
