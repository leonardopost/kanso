"""The warmup gate: a sleeve fed its prefix sees everything and places nothing.

Every test runs the real engine, because the claim is about what reaches the venue: an
order dropped by the gate never becomes a fill, and one placed after the open does.
"""

from __future__ import annotations

from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Quantity

from kanso.nautilus.cross_section import deliver_from, warm
from kanso.nautilus.hooks import OVERLAY, Clip
from kanso.nautilus.strategy import (
    Decision,
    Hedge,
    HookContext,
    KansoModifier,
    KansoModifierConfig,
    KansoStrategy,
)

from .conftest import (
    DEEP,
    DEMO,
    HEDGE,
    LATENCY_NS,
    MINUTE_NS,
    bar,
    flat,
    quote,
    saw_tooth,
    trade,
)
from .test_clips import HEDGE_CLIP, clipper
from .test_modifiers import Attached, attached, host
from .test_sizing import Enters, sized
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


class Clipping(KansoModifier):
    """A sized overlay that asks for the same clip on every consult, prefix included."""

    construct = OVERLAY
    config_cls = KansoModifierConfig

    def on_data(self, ctx: HookContext) -> Decision:
        return Decision(clips=(Clip("HEDGE.XNAS", "SELL"),))


class Ledgered(Enters):
    """A sized host that records, on each of its own bars, how many clips are on the ledger."""

    def on_start(self) -> None:
        super().on_start()
        self.ledger: dict[int, int] = {}

    def after(self, bars: int) -> None:
        self.ledger[bars] = sum(len(held) for held in self._clip_orders.values())


def test_a_sized_overlay_s_prefix_clip_is_dropped_before_it_reaches_the_ledger(
    backtest,
) -> None:
    """A clip built and then refused would sit on the ledger and count as the one clip
    for the rest of the run, so the answer is dropped before any leg is built."""
    points = [*saw_tooth(DEMO), *flat(HEDGE, close=20.0, volume=DEEP)]
    strategy = Ledgered(sized())
    warm(strategy, opens_ns())
    overlay = clipper(Clipping, host="Ledgered")

    run = backtest(strategy, [overlay], data=points, instruments=(DEMO, HEDGE))
    clips = [i for i in run.strategy.intents if i.instrument_id == "HEDGE.XNAS"]

    assert strategy.ledger[OPEN_INDEX + 1] == 0, "nothing on the ledger at the first window bar"
    assert clips, "the first in-window clip is placed: the prefix's did not count as the one clip"
    assert clips[0].ts_event == saw_tooth(DEMO)[OPEN_INDEX].ts_event
    assert clips[0].qty == HEDGE_CLIP, "sized at the last print, which the prefix supplied"
    assert all(clip.ts_event >= saw_tooth(DEMO)[OPEN_INDEX].ts_event for clip in clips)


# --- a shared feed: what precedes the strategy's own delivery is not handled --------


class Listening(KansoStrategy):
    """Records the availability instant of every point each of its handlers is given."""

    def on_start(self) -> None:
        self.seen: list[tuple[str, int]] = []

    def on_bar(self, bar_: Bar) -> None:
        self.seen.append(("bar", int(bar_.ts_init)))
        if len(self.seen) == 1:
            self.handle_data(quote(DEMO, 0))
            self.handle_data(quote(DEMO, 30))

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self.seen.append(("quote", int(tick.ts_init)))

    def on_trade_tick(self, tick: TradeTick) -> None:
        self.seen.append(("trade", int(tick.ts_init)))

    def on_data(self, data: object) -> None:
        self.seen.append(("data", int(data.ts_init)))  # type: ignore[attr-defined]


def test_a_strategy_on_a_shared_feed_handles_nothing_before_its_own_delivery(backtest) -> None:
    """A stage feeds one series once, cut at the deepest warmup on it; a version handles
    only the span its own request delivers, so its state is what a run alone would build."""
    points = [
        *saw_tooth(DEMO),
        *(quote(DEMO, i) for i in range(20)),
        *(trade(DEMO, i) for i in range(20)),
    ]
    strategy = Listening(config(data_requirements=("bar", "quote", "trade")))
    deliver_from(strategy, opens_ns())

    run = backtest(strategy, data=points)

    seen = run.strategy.seen
    assert min(ts for _, ts in seen) == opens_ns()
    assert [kind for kind, _ in seen].count("bar") == len(saw_tooth(DEMO)) - OPEN_INDEX
    assert [kind for kind, _ in seen].count("quote") == 20 - OPEN_INDEX
    assert [kind for kind, _ in seen].count("trade") == 20 - OPEN_INDEX
    assert [ts for kind, ts in seen if kind == "data"] == [int(quote(DEMO, 30).ts_init)], (
        "a point raised by the author's own handler is gated the same way"
    )
    assert int(run.strategy.last_bar(DEMO).ts_init) == int(saw_tooth(DEMO)[-1].ts_init)


def test_a_strategy_fed_its_own_span_handles_all_of_it(backtest) -> None:
    """Nothing set: today's behaviour, byte for byte, on the runs whose feed is their own."""
    run = backtest(Listening(config()))

    assert [ts for kind, ts in run.strategy.seen if kind == "bar"] == [
        int(point.ts_init) for point in saw_tooth(DEMO)
    ]
