"""The sleeve, in a real backtest: its clock, its record of intents, and its sizing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Quantity

from kanso.errors import ValidationError
from kanso.nautilus.strategy import ENTRY, EXIT_ORDER, KansoConfig, KansoStrategy

from .conftest import DEMO, HEDGE, bar, every_grain, quote, saw_tooth

FREE = {"costs": {"commission_bps": 0.0, "slippage_bps": 0.0, "spread": "quotes"}}
"""A cost model that charges nothing, so a sizing assertion is exact arithmetic."""


def config(cls: type[KansoConfig] = KansoConfig, **overrides: object) -> KansoConfig:
    fields: dict[str, object] = {
        "hyp_id": "demo_mr",
        "universe": ("DEMO.XNAS",),
        "resolution": "1m",
        "data_requirements": ("bar",),
        "capital": 100_000.0,
        "max_position_pct": 20.0,
        "max_drawdown_pct": 15.0,
        "max_leverage": 1.0,
        "venue_model": FREE,
    }
    fields.update(overrides)
    return cls(**fields)


class Trader(KansoStrategy):
    """Enters on the third bar and exits on the eighth."""

    def on_start(self) -> None:
        self.bars = 0
        self.engine_clock: list[int] = []
        self.data_times: list[int] = []

    def on_bar(self, bar_: object) -> None:
        self.bars += 1
        self.engine_clock.append(self.clock.timestamp_ns())
        self.data_times.append(self.data_time)
        if self.bars == 3:
            self.submit_entry(DEMO, "BUY")
        if self.bars == 8:
            self.submit_exit(DEMO)


# --- a sleeve trades ---------------------------------------------------------


def test_a_sleeve_opens_and_closes_a_position(backtest) -> None:
    run = backtest(Trader(config()))

    (position,) = run.engine.cache.positions()
    assert position.is_closed
    assert [intent.side for intent in run.strategy.intents] == ["BUY", "SELL"]


# --- data time is the only clock --------------------------------------------


def test_data_time_is_the_ts_event_of_the_last_bar_not_the_engine_clock(backtest) -> None:
    run = backtest(Trader(config()))
    strategy = run.strategy

    bars = saw_tooth(DEMO)
    assert strategy.data_times == [b.ts_event for b in bars]
    # The engine's own clock is advanced to ts_init, which is when the bar became
    # available, not the minute it describes.
    assert strategy.engine_clock == [b.ts_init for b in bars]
    assert strategy.data_time == bars[-1].ts_event


def test_data_time_is_set_before_the_author_s_handler_runs(backtest) -> None:
    class First(KansoStrategy):
        def on_start(self) -> None:
            self.seen: list[int] = []

        def on_bar(self, bar_: object) -> None:
            self.seen.append(self.data_time)

    run = backtest(First(config()))

    assert run.strategy.seen[0] == saw_tooth(DEMO)[0].ts_event


def test_an_intent_is_stamped_with_data_time(backtest) -> None:
    run = backtest(Trader(config()))
    third, eighth = saw_tooth(DEMO)[2], saw_tooth(DEMO)[7]

    assert [intent.ts_event for intent in run.strategy.intents] == [
        third.ts_event,
        eighth.ts_event,
    ]
    assert third.ts_init not in [intent.ts_event for intent in run.strategy.intents]


def test_a_historical_bar_does_not_move_the_stream_clock(backtest) -> None:
    class Catchup(KansoStrategy):
        def on_start(self) -> None:
            self.after: int | None = None

        def on_bar(self, bar_: object) -> None:
            if self.after is None:
                self.handle_bar(bar(DEMO, 900, 10.0), historical=True)
                self.after = self.data_time

    run = backtest(Catchup(config()))

    assert run.strategy.after == saw_tooth(DEMO)[0].ts_event


def test_every_grain_moves_the_clock_and_is_remembered(backtest) -> None:
    class Grains(KansoStrategy):
        def on_start(self) -> None:
            self.quotes = 0
            self.trades = 0

        def on_quote_tick(self, tick: object) -> None:
            self.quotes += 1

        def on_trade_tick(self, tick: object) -> None:
            self.trades += 1

    strategy = Grains(config(data_requirements=("bar", "quote", "trade", "corporate_action")))
    run = backtest(strategy, data=every_grain(DEMO))

    assert run.strategy.quotes == 6
    assert run.strategy.trades == 6
    assert run.strategy.last_bar(DEMO) is not None
    assert run.strategy.last_quote(DEMO) is not None
    assert run.strategy.last_trade(DEMO) is not None
    assert run.strategy.last_price(DEMO) == pytest.approx(11.0)
    assert run.strategy.last_price(HEDGE) is None


def test_generic_data_moves_the_clock_with_or_without_an_instrument(backtest) -> None:
    class Generic(KansoStrategy):
        def on_start(self) -> None:
            self.done = False
            self.after: list[int] = []

        def on_bar(self, bar_: object) -> None:
            if self.done:
                return
            self.done = True
            self.handle_data(quote(DEMO, 500, 10.0))
            self.after.append(self.data_time)
            self.handle_data(bar(DEMO, 700, 10.0))
            self.after.append(self.data_time)

    run = backtest(Generic(config()))

    assert run.strategy.after == [quote(DEMO, 500).ts_event, bar(DEMO, 700, 10.0).ts_event]


def test_data_handled_after_the_run_does_not_consult_the_exit_rules(backtest) -> None:
    run = backtest(Trader(config()))
    before = len(run.strategy.intents)

    run.strategy.handle_bar(bar(DEMO, 999, 10.0))

    assert run.strategy.data_time == bar(DEMO, 999, 10.0).ts_event
    assert len(run.strategy.intents) == before


# --- every order is recorded -------------------------------------------------


def test_an_order_that_bypasses_the_helpers_is_still_recorded(backtest) -> None:
    class ByHand(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                order = self.order_factory.market(DEMO, OrderSide.BUY, Quantity.from_int(7))
                self.submit_order(order)
            if self.bars == 6:
                self.close_all_positions(DEMO)

    run = backtest(ByHand(config()))

    assert [(i.side, i.qty, i.order_type) for i in run.strategy.intents] == [
        ("BUY", 7.0, "MARKET"),
        ("SELL", 7.0, "MARKET"),
    ]


def test_a_limit_order_records_its_price(backtest) -> None:
    class Passive(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.submit_entry(DEMO, OrderSide.BUY, qty=5, price=10.75)

    run = backtest(Passive(config()))
    (intent,) = run.strategy.intents

    assert (intent.order_type, intent.price, intent.qty) == ("LIMIT", 10.75, 5.0)


def test_an_order_list_is_recorded_leg_by_leg(backtest) -> None:
    class Bracketed(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                orders = [
                    self.order_factory.market(DEMO, OrderSide.BUY, Quantity.from_int(4)),
                    self.order_factory.limit(
                        DEMO, OrderSide.SELL, Quantity.from_int(4), bar_.close
                    ),
                ]
                self.submit_order_list(self.order_factory.create_list(orders))

    run = backtest(Bracketed(config()))

    assert [(i.side, i.order_type) for i in run.strategy.intents] == [
        ("BUY", "MARKET"),
        ("SELL", "LIMIT"),
    ]


# --- entry and exit are decided by the effect on the net position -----------


def test_an_order_is_an_entry_or_an_exit_by_its_effect_on_the_position(backtest) -> None:
    class Classifier(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0
            self.kinds: list[str] = []

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.kinds.append(self.classify(self._market(OrderSide.BUY, 10)))
                self.submit_entry(DEMO, "BUY", qty=10)
            if self.bars == 5:
                self.kinds.append(self.classify(self._market(OrderSide.BUY, 5)))
                self.kinds.append(self.classify(self._market(OrderSide.SELL, 4)))
                self.kinds.append(self.classify(self._market(OrderSide.SELL, 25)))

        def _market(self, side: OrderSide, qty: int) -> object:
            return self.order_factory.market(DEMO, side, Quantity.from_int(qty))

    run = backtest(Classifier(config()))

    # flat -> entry; growing a long -> entry; shrinking it -> exit;
    # flipping past flat into a larger short -> entry.
    assert run.strategy.kinds == [ENTRY, ENTRY, EXIT_ORDER, ENTRY]


# --- sizing and exposure -----------------------------------------------------


def test_an_entry_is_capped_by_max_position_pct(backtest) -> None:
    run = backtest(Trader(config()))
    entry = run.strategy.intents[0]

    # 20% of 100_000 at the third bar's close of 11.00, floored onto the lot size.
    assert entry.qty == 1818.0


def test_an_entry_is_capped_by_gross_leverage(backtest) -> None:
    run = backtest(Trader(config(max_position_pct=100.0, max_leverage=0.5)))

    assert run.strategy.intents[0].qty == 4545.0


def test_the_cost_model_is_reserved_out_of_the_size(backtest) -> None:
    costed = {"costs": {"commission_bps": 5.0, "slippage_bps": 5.0, "spread": "quotes"}}
    run = backtest(Trader(config(venue_model=costed)))

    # 20_000 / (1 + 2 x 10bps) / 11.00
    assert run.strategy.intents[0].qty == 1814.0


def test_a_fixed_spread_counts_towards_the_reserve(backtest) -> None:
    fixed = {
        "costs": {
            "commission_bps": 0.0,
            "slippage_bps": 0.0,
            "spread": "fixed_bps",
            "fixed_bps": 10.0,
        }
    }
    run = backtest(Trader(config(venue_model=fixed)))

    assert run.strategy.intents[0].qty == 1814.0


def test_a_venue_model_without_costs_reserves_nothing(backtest) -> None:
    run = backtest(Trader(config(venue_model={})))

    assert run.strategy.cost_rate == 0.0
    assert run.strategy.intents[0].qty == 1818.0


def test_a_second_entry_only_takes_the_room_that_is_left(backtest) -> None:
    class Twice(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars in (3, 6):
                self.submit_entry(DEMO, "BUY", notional=15_000)

    run = backtest(Twice(config()))
    first, second = run.strategy.intents

    # 15_000 at the third bar's 11.00 is 1363 shares; by the sixth bar, at 11.50, they
    # are worth 15_674.50 of the 20_000 cap, and the 4_325.50 left buys 376 more.
    assert first.qty == 1363.0
    assert second.qty == 376.0


def test_an_entry_with_no_room_left_is_not_placed(backtest) -> None:
    class Greedy(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0
            self.refused: list[object] = []

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars in (3, 4):
                self.refused.append(self.submit_entry(DEMO, "BUY"))

    run = backtest(Greedy(config()))

    assert run.strategy.refused[0] is not None
    assert run.strategy.refused[1] is None
    assert len(run.strategy.intents) == 1


def test_an_entry_before_any_price_is_seen_is_not_placed(backtest) -> None:
    class Early(KansoStrategy):
        def on_start(self) -> None:
            self.at_start = self.submit_entry(DEMO, "BUY")

    run = backtest(Early(config()))

    assert run.strategy.at_start is None
    assert run.strategy.intents == ()


def test_a_quantity_that_floors_to_nothing_is_not_placed(backtest) -> None:
    class Crumb(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0
            self.placed: object = "unset"

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.placed = self.submit_entry(DEMO, "BUY", notional=1.0)

    run = backtest(Crumb(config()))

    assert run.strategy.placed is None


def test_an_explicit_quantity_is_still_capped_by_the_limits(backtest) -> None:
    class Ambitious(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.submit_entry(DEMO, "BUY", qty=1_000_000)

    run = backtest(Ambitious(config()))

    assert run.strategy.intents[0].qty == 1818.0


def test_an_entry_may_be_sold_short(backtest) -> None:
    class Short(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.submit_entry(DEMO, "SELL")

    run = backtest(Short(config()))

    assert run.strategy.intents[0].side == "SELL"


def test_an_unknown_instrument_cannot_be_sized(backtest) -> None:
    class Elsewhere(KansoStrategy):
        def on_start(self) -> None:
            self.error: str | None = None

        def on_bar(self, bar_: object) -> None:
            if self.error is None:
                try:
                    self.submit_entry("NOPE.XNAS", "BUY")
                except ValidationError as exc:
                    self.error = exc.message

    run = backtest(Elsewhere(config()))

    assert "NOPE.XNAS" in run.strategy.error


def test_an_unknown_instrument_cannot_be_exited(backtest) -> None:
    class Elsewhere(KansoStrategy):
        def on_start(self) -> None:
            self.error: str | None = None

        def on_bar(self, bar_: object) -> None:
            if self.error is None:
                try:
                    self.submit_exit("NOPE.XNAS")
                except ValidationError as exc:
                    self.error = exc.message

    run = backtest(Elsewhere(config()))

    assert "NOPE.XNAS" in run.strategy.error


def test_a_side_that_is_neither_buy_nor_sell_is_refused() -> None:
    with pytest.raises(ValidationError, match="neither BUY nor SELL"):
        KansoStrategy._side("hold")


def test_a_quantity_is_floored_onto_the_lot_size() -> None:
    lots_of_five = SimpleNamespace(lot_size=5, size_increment=1, size_precision=0)
    assert KansoStrategy._quantise(lots_of_five, 12.0) == Quantity.from_int(10)
    assert KansoStrategy._quantise(lots_of_five, 4.9) is None
    assert KansoStrategy._quantise(lots_of_five, 0.0) is None
    assert KansoStrategy._quantise(SimpleNamespace(lot_size=0, size_increment=0), 5.0) is None
    fractional = SimpleNamespace(lot_size=None, size_increment="0.01", size_precision=2)
    assert KansoStrategy._quantise(fractional, 1.239) == Quantity(1.23, 2)


# --- exits -------------------------------------------------------------------


def test_an_exit_never_goes_past_flat(backtest) -> None:
    class Overshoot(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.submit_entry(DEMO, "BUY", qty=10)
            if self.bars == 6:
                self.submit_exit(DEMO, qty=1_000)

    run = backtest(Overshoot(config()))

    assert run.strategy.intents[1].qty == 10.0


def test_a_partial_exit_leaves_the_rest(backtest) -> None:
    class Half(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.submit_entry(DEMO, "BUY", qty=10)
            if self.bars == 6:
                self.submit_exit(DEMO, qty=4, price=11.0)

    run = backtest(Half(config()))
    exit_intent = run.strategy.intents[1]

    assert (exit_intent.qty, exit_intent.order_type, exit_intent.price) == (4.0, "LIMIT", 11.0)


def test_exiting_a_flat_instrument_places_nothing(backtest) -> None:
    class Nothing(KansoStrategy):
        def on_start(self) -> None:
            self.result: object = "unset"

        def on_bar(self, bar_: object) -> None:
            self.result = self.submit_exit(DEMO)

    run = backtest(Nothing(config()))

    assert run.strategy.result is None
    assert run.strategy.intents == ()


def test_a_short_is_exited_by_buying_back(backtest) -> None:
    class Cover(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.submit_entry(DEMO, "SELL", qty=10)
            if self.bars == 6:
                self.submit_exit(DEMO)

    run = backtest(Cover(config()))

    assert [i.side for i in run.strategy.intents] == ["SELL", "BUY"]


def test_an_exit_that_floors_to_nothing_places_nothing(backtest) -> None:
    class Sliver(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0
            self.result: object = "unset"

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.submit_entry(DEMO, "BUY", qty=10)
            if self.bars == 6:
                self.result = self.submit_exit(DEMO, qty=0.4)

    run = backtest(Sliver(config()))

    assert run.strategy.result is None


# --- the names a modifier may attach by --------------------------------------


def test_a_sleeve_answers_to_its_id_and_to_its_class_name(backtest) -> None:
    run = backtest(Trader(config()))

    assert run.strategy.host_names == ("Trader-000", "Trader")
