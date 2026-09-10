"""Under a sizing rule the harness sizes every order, and what it will not size it refuses."""

from __future__ import annotations

import pytest
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Quantity

from kanso.nautilus.hooks import OVERLAY
from kanso.nautilus.sizing import (
    BUDGET_BELOW_LOT,
    HAND_BUILT_ORDER,
    ONE_POSITION,
    SCALE_UNDER_SIZING,
    SIZE_ARGUMENT,
    SizingError,
    full_book_quantity,
)
from kanso.nautilus.strategy import (
    Decision,
    HookContext,
    KansoModifier,
    KansoModifierConfig,
    KansoStrategy,
)

from .conftest import DEMO, HEDGE, flat
from .test_sleeve import config

BUDGET = 30_000.0
DEMO_SHARES = 2_997.0
"""floor(30,000 / 10.01): the budget over the price with one increment reserved, no costs."""
HEDGE_SHARES = 1_499.0
"""floor(30,000 / 20.01)."""


def sized(**overrides: object):
    fields: dict[str, object] = {
        "universe": ("DEMO.XNAS", "HEDGE.XNAS"),
        "sizing_budget": BUDGET,
        "max_position_pct": 100.0,
    }
    fields.update(overrides)
    return config(**fields)


def two_names() -> list[object]:
    return [*flat(DEMO), *flat(HEDGE, close=20.0)]


class Enters(KansoStrategy):
    """Enters DEMO on the third bar; subclasses add what happens after."""

    def on_start(self) -> None:
        self.bars = 0
        self.seen: list[object] = []

    def on_bar(self, bar_: object) -> None:
        if str(bar_.bar_type.instrument_id) != "DEMO.XNAS":  # type: ignore[attr-defined]
            return
        self.bars += 1
        if self.bars == 3:
            self.seen.append(self.submit_entry(DEMO, "BUY"))
        self.after(self.bars)

    def after(self, bars: int) -> None:
        return


def intents(run) -> list[tuple[str, str, float]]:
    return [(i.instrument_id, i.side, i.qty) for i in run.strategy.intents]


def test_the_arithmetic_reserves_the_round_trip_and_one_increment() -> None:
    assert full_book_quantity(30_000.0, 10.0, 0.01, 0.0) == pytest.approx(30_000.0 / 10.01)
    assert full_book_quantity(30_000.0, 10.0, 0.0, 0.001) == pytest.approx(30_000.0 / 10.02)


def test_a_full_book_entry_is_the_budget_over_the_price_in_whole_lots(backtest) -> None:
    run = backtest(Enters(sized()), data=two_names(), instruments=(DEMO, HEDGE))

    assert intents(run) == [("DEMO.XNAS", "BUY", DEMO_SHARES)]
    assert float(run.engine.cache.positions_open()[0].signed_qty) == DEMO_SHARES


def test_a_full_book_entry_while_holding_the_same_name_is_a_no_op(backtest) -> None:
    class Again(Enters):
        def after(self, bars: int) -> None:
            if bars == 5:
                self.seen.append(self.submit_entry(DEMO, "BUY"))

    run = backtest(Again(sized()), data=two_names(), instruments=(DEMO, HEDGE))

    assert intents(run) == [("DEMO.XNAS", "BUY", DEMO_SHARES)]
    assert run.strategy.seen[1] is None


def test_a_second_instrument_while_one_is_held_is_refused_and_named(backtest) -> None:
    class Both(Enters):
        def after(self, bars: int) -> None:
            if bars == 5:
                self.submit_entry(HEDGE, "BUY")

    with pytest.raises(SizingError) as failure:
        backtest(Both(sized()), data=two_names(), instruments=(DEMO, HEDGE))

    refused = failure.value.refusal
    assert refused.rule == ONE_POSITION
    assert (refused.instrument_id, refused.asked) == ("HEDGE.XNAS", "BUY")
    assert refused.held == {"DEMO.XNAS": DEMO_SHARES, "HEDGE.XNAS": 0.0}
    assert "submit_exit('DEMO.XNAS') first, in the same handler" in refused.why
    assert "sizing: one_position at HEDGE.XNAS" in failure.value.message


def test_a_flip_is_an_exit_then_an_entry_in_one_handler_at_leverage_one(backtest) -> None:
    class Flips(Enters):
        def after(self, bars: int) -> None:
            if bars == 6:
                self.submit_exit(DEMO)
                self.seen.append(self.held(DEMO))
                self.submit_entry(HEDGE, "BUY")

    run = backtest(Flips(sized(max_leverage=1.0)), data=two_names(), instruments=(DEMO, HEDGE))

    assert intents(run) == [
        ("DEMO.XNAS", "BUY", DEMO_SHARES),
        ("DEMO.XNAS", "SELL", DEMO_SHARES),
        ("HEDGE.XNAS", "BUY", HEDGE_SHARES),
    ]
    assert run.strategy.seen[1] == 0.0, "the exit in flight nets the old leg to nothing"
    held = {str(p.instrument_id): float(p.signed_qty) for p in run.engine.cache.positions_open()}
    assert held == {"HEDGE.XNAS": HEDGE_SHARES}
    assert len({i.ts_event for i in run.strategy.intents[1:]}) == 1


def test_the_other_side_of_a_held_name_is_refused_until_it_is_exited(backtest) -> None:
    class Reverses(Enters):
        def after(self, bars: int) -> None:
            if bars == 5:
                self.submit_entry(DEMO, "SELL")

    with pytest.raises(SizingError) as failure:
        backtest(Reverses(sized()), data=two_names(), instruments=(DEMO, HEDGE))

    assert failure.value.refusal.rule == ONE_POSITION
    assert "on the other side" in failure.value.refusal.why


@pytest.mark.parametrize(
    ("kwargs", "named"),
    [
        ({"notional": 5_000.0}, "notional=5000.0"),
        ({"qty": 10.0}, "qty=10.0"),
        ({"price": 9.5}, "price=9.5"),
    ],
)
def test_a_size_keyword_under_full_book_is_refused_not_capped(backtest, kwargs, named) -> None:
    class Knob(KansoStrategy):
        def on_bar(self, bar_: object) -> None:
            self.submit_entry(DEMO, "BUY", **kwargs)

    with pytest.raises(SizingError) as failure:
        backtest(Knob(sized()), data=two_names(), instruments=(DEMO, HEDGE))

    assert failure.value.refusal.rule == SIZE_ARGUMENT
    assert named in failure.value.refusal.why
    assert "call submit_entry(id, side)" in failure.value.refusal.why


def test_a_full_book_exit_is_the_whole_position_and_takes_no_qty(backtest) -> None:
    class Exits(Enters):
        def after(self, bars: int) -> None:
            if bars == 6:
                self.seen.append(self.submit_exit(DEMO))
            if bars == 8:
                self.seen.append(self.submit_exit(DEMO))

    run = backtest(Exits(sized()), data=two_names(), instruments=(DEMO, HEDGE))

    assert intents(run) == [("DEMO.XNAS", "BUY", DEMO_SHARES), ("DEMO.XNAS", "SELL", DEMO_SHARES)]
    assert run.strategy.seen[2] is None, "flat is None, not an order"

    class Partial(Enters):
        def after(self, bars: int) -> None:
            if bars == 6:
                self.submit_exit(DEMO, qty=10.0)

    with pytest.raises(SizingError) as failure:
        backtest(Partial(sized()), data=two_names(), instruments=(DEMO, HEDGE))

    assert failure.value.refusal.rule == SIZE_ARGUMENT
    assert "qty=10.0" in failure.value.refusal.why


def test_a_hand_built_order_under_full_book_is_refused(backtest) -> None:
    class Builder(KansoStrategy):
        def on_bar(self, bar_: object) -> None:
            self.submit_order(self.order_factory.market(DEMO, OrderSide.BUY, Quantity.from_int(5)))

    with pytest.raises(SizingError) as failure:
        backtest(Builder(sized()), data=two_names(), instruments=(DEMO, HEDGE))

    assert failure.value.refusal.rule == HAND_BUILT_ORDER
    assert failure.value.refusal.asked == "BUY"
    assert len(failure.value.refusal.held) == 2


def test_the_engine_s_own_close_is_a_hand_built_order_too(backtest) -> None:
    class Closer(Enters):
        def after(self, bars: int) -> None:
            if bars == 6:
                self.close_all_positions(DEMO)

    with pytest.raises(SizingError) as failure:
        backtest(Closer(sized()), data=two_names(), instruments=(DEMO, HEDGE))

    assert failure.value.refusal.rule == HAND_BUILT_ORDER


def test_an_overlay_scale_under_full_book_is_refused(backtest) -> None:
    class Halving(KansoModifier):
        construct = OVERLAY
        config_cls = KansoModifierConfig

        def evaluate(self, ctx: HookContext) -> Decision:
            return Decision(scale=0.5)

    overlay = Halving(KansoModifierConfig(host_strategy_id="Enters", hyp_id="attached"))
    with pytest.raises(SizingError) as failure:
        backtest(Enters(sized()), [overlay], data=two_names(), instruments=(DEMO, HEDGE))

    assert failure.value.refusal.rule == SCALE_UNDER_SIZING
    assert "scaled the entry by 0.5" in failure.value.refusal.why


def test_a_neutral_overlay_leaves_a_sized_entry_alone(backtest) -> None:
    class Silent(KansoModifier):
        construct = OVERLAY
        config_cls = KansoModifierConfig

    overlay = Silent(KansoModifierConfig(host_strategy_id="Enters", hyp_id="attached"))
    run = backtest(Enters(sized()), [overlay], data=two_names(), instruments=(DEMO, HEDGE))

    assert intents(run) == [("DEMO.XNAS", "BUY", DEMO_SHARES)]


def test_a_budget_below_one_lot_is_refused(backtest) -> None:
    with pytest.raises(SizingError) as failure:
        backtest(Enters(sized(sizing_budget=5.0)), data=two_names(), instruments=(DEMO, HEDGE))

    assert failure.value.refusal.rule == BUDGET_BELOW_LOT
    assert "5 at 10 floors to no whole lot" in failure.value.refusal.why


def test_an_entry_before_any_print_is_not_placed(backtest) -> None:
    class Early(KansoStrategy):
        def on_start(self) -> None:
            self.seen: list[object] = []

        def on_bar(self, bar_: object) -> None:
            self.seen.append(self.submit_entry(HEDGE, "BUY"))

    run = backtest(Early(sized()), data=list(flat(DEMO)), instruments=(DEMO, HEDGE))

    assert run.strategy.seen and all(placed is None for placed in run.strategy.seen)
    assert run.strategy.intents == ()


def test_held_reads_the_own_position_with_orders_in_flight(backtest) -> None:
    class Reads(Enters):
        def after(self, bars: int) -> None:
            if bars == 3:
                self.seen.append(self.held(DEMO))
            if bars == 4:
                self.seen.append(self.held("DEMO.XNAS"))

    run = backtest(Reads(sized()), data=two_names(), instruments=(DEMO, HEDGE))

    assert run.strategy.seen[1:] == [DEMO_SHARES, DEMO_SHARES]


def test_without_a_rule_held_is_the_venue_s_net_position(backtest) -> None:
    class Reads(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0
            self.seen: list[float] = []

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.submit_entry(DEMO, "BUY", qty=100)
            if self.bars == 5:
                self.seen.append(self.held(DEMO))

    run = backtest(Reads(config()))

    assert run.strategy.seen == [100.0]
    assert not run.strategy.sized


def test_an_exit_while_the_entry_is_still_in_flight_places_nothing(backtest) -> None:
    class Hasty(Enters):
        def after(self, bars: int) -> None:
            if bars == 3:
                self.seen.append(self.submit_exit(DEMO))

    run = backtest(Hasty(sized()), data=two_names(), instruments=(DEMO, HEDGE))

    assert run.strategy.seen[1] is None
    assert intents(run) == [("DEMO.XNAS", "BUY", DEMO_SHARES)]
