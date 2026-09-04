"""Attached constructs, in a real backtest: what each one does to an unchanged host.

The host below is one sleeve, written once and never edited. Every test attaches a
modifier to it and asserts the difference, because that is the whole claim of an attached
construct: the host's `strategy.py` does not know it exists.
"""

from __future__ import annotations

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Quantity

from kanso.errors import ValidationError
from kanso.nautilus.hooks import EXIT, FILTER, OVERLAY
from kanso.nautilus.strategy import (
    Decision,
    Hedge,
    HookContext,
    KansoConfig,
    KansoModifier,
    KansoModifierConfig,
    KansoStrategy,
)

from .conftest import DEMO, HEDGE, flat, saw_tooth


class Host(KansoStrategy):
    """Buys on the third bar, sells on the twelfth. Knows nothing of any construct."""

    def on_start(self) -> None:
        self.bars = 0

    def on_bar(self, bar_: object) -> None:
        self.bars += 1
        if self.bars == 3:
            self.submit_entry(DEMO, "BUY", qty=100)
        if self.bars == 12:
            self.submit_exit(DEMO)


class Attached(KansoModifierConfig):
    pass


class Careful(Host):
    """A host that records, rather than propagates, a construct's own failure."""

    def on_start(self) -> None:
        self.bars = 0
        self.error: str | None = None

    def on_bar(self, bar_: object) -> None:
        self.bars += 1
        if self.bars == 3:
            try:
                self.submit_entry(DEMO, "BUY", qty=10)
            except ValidationError as exc:
                self.error = exc.message


def host(**overrides: object) -> Host:
    fields: dict[str, object] = {
        "hyp_id": "demo_mr",
        "universe": ("DEMO.XNAS",),
        "resolution": "1m",
        "data_requirements": ("bar",),
        "capital": 100_000.0,
        "max_position_pct": 20.0,
        "max_drawdown_pct": 15.0,
        "max_leverage": 1.0,
        "venue_model": {"costs": {"commission_bps": 0.0, "slippage_bps": 0.0}},
    }
    fields.update(overrides)
    return Host(KansoConfig(**fields))


def attached(cls: type[KansoModifier], **overrides: object) -> KansoModifier:
    return cls(Attached(host_strategy_id="Host", hyp_id="attached", **overrides))


def attached_for(cls: type[KansoModifier], host_name: str) -> KansoModifier:
    return cls(Attached(host_strategy_id=host_name))


# --- the neutral construct changes nothing -----------------------------------


class Neutral(KansoModifier):
    """The shipped stub: `evaluate` returns the construct's identity."""

    construct = FILTER
    config_cls = Attached


def test_a_neutral_decision_leaves_the_host_exactly_as_it_was(backtest) -> None:
    alone = backtest(host())
    with_construct = backtest(host(), [attached(Neutral)])

    assert with_construct.strategy.intents == alone.strategy.intents


def test_a_neutral_decision_of_every_kind_leaves_the_host_alone(backtest) -> None:
    class NeutralOverlay(Neutral):
        construct = OVERLAY

    class NeutralExit(Neutral):
        construct = EXIT

    alone = backtest(host())
    modified = backtest(
        host(),
        [attached(Neutral), attached(NeutralOverlay), attached(NeutralExit)],
    )

    assert modified.strategy.intents == alone.strategy.intents


# --- filter: Decision.allow via before_entry ---------------------------------


class Closed(KansoModifier):
    construct = FILTER
    config_cls = Attached

    def evaluate(self, ctx: HookContext) -> Decision:
        return Decision(allow=False)


def test_a_filter_that_refuses_stops_the_host_entering(backtest) -> None:
    run = backtest(host(), [attached(Closed)])

    assert run.strategy.intents == ()
    assert run.engine.cache.positions() == []


def test_a_filter_gates_entries_and_not_exits(backtest) -> None:
    class AfterFive(KansoModifier):
        construct = FILTER
        config_cls = Attached

        def evaluate(self, ctx: HookContext) -> Decision:
            return Decision(allow=ctx.ts_event >= saw_tooth(DEMO)[4].ts_event)

    class Late(Host):
        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars in (3, 8):
                self.submit_entry(DEMO, "BUY", qty=100)
            if self.bars == 12:
                self.submit_exit(DEMO)

    run = backtest(Late(host().kanso_config), [attached_for(AfterFive, "Late")])

    # The third bar is refused, the eighth is allowed, and the exit is never filtered.
    assert [(i.side, i.ts_event) for i in run.strategy.intents] == [
        ("BUY", saw_tooth(DEMO)[7].ts_event),
        ("SELL", saw_tooth(DEMO)[11].ts_event),
    ]


def test_a_filter_sees_the_order_it_is_gating(backtest) -> None:
    class Nosy(KansoModifier):
        construct = FILTER
        config_cls = Attached

        def __init__(self, config: Attached) -> None:
            super().__init__(config)
            self.seen: list[HookContext] = []

        def evaluate(self, ctx: HookContext) -> Decision:
            self.seen.append(ctx)
            return Decision(allow=True)

    modifier = attached(Nosy)
    run = backtest(host(), [modifier])
    (ctx,) = modifier.seen

    assert ctx.instrument_id == "DEMO.XNAS"
    assert ctx.ts_event == saw_tooth(DEMO)[2].ts_event
    assert (ctx.side, ctx.qty, ctx.order_type) == ("BUY", 100.0, "MARKET")
    assert ctx.position_qty == 0.0
    assert ctx.capital == 100_000.0
    assert ctx.host_strategy_id == "Host-000"
    assert ctx.last_bar is not None
    assert ctx.cache is run.engine.cache


# --- overlay: Decision.scale via size ----------------------------------------


class Half(KansoModifier):
    construct = OVERLAY
    config_cls = Attached

    def evaluate(self, ctx: HookContext) -> Decision:
        return Decision(scale=0.5)


def test_an_overlay_scales_the_host_s_size(backtest) -> None:
    alone = backtest(host())
    scaled = backtest(host(), [attached(Half)])

    assert alone.strategy.intents[0].qty == 100.0
    assert scaled.strategy.intents[0].qty == 50.0


def test_two_overlays_compose_multiplicatively(backtest) -> None:
    class Fifth(KansoModifier):
        construct = OVERLAY
        config_cls = Attached

        def evaluate(self, ctx: HookContext) -> Decision:
            return Decision(scale=0.2)

    run = backtest(host(), [attached(Half), attached(Fifth)])

    assert run.strategy.intents[0].qty == 10.0


def test_an_overlay_that_scales_to_nothing_places_no_entry(backtest) -> None:
    class Off(KansoModifier):
        construct = OVERLAY
        config_cls = Attached

        def evaluate(self, ctx: HookContext) -> Decision:
            return Decision(scale=0.0)

    run = backtest(host(), [attached(Off)])

    assert run.strategy.intents == ()


def test_an_overlay_does_not_scale_an_exit(backtest) -> None:
    run = backtest(host(), [attached(Half)])

    assert [i.qty for i in run.strategy.intents] == [50.0, 50.0]


# --- overlay: Decision.hedges via hedges -------------------------------------


class Hedged(KansoModifier):
    construct = OVERLAY
    config_cls = Attached

    def evaluate(self, ctx: HookContext) -> Decision:
        return Decision(scale=1.0, hedges=(Hedge("HEDGE.XNAS", -40.0),))


def test_an_overlay_hedge_leg_is_placed_beside_the_entry(backtest) -> None:
    run = backtest(host(), [attached(Hedged)], instruments=(DEMO, HEDGE))

    assert [(i.instrument_id, i.side, i.qty) for i in run.strategy.intents] == [
        ("DEMO.XNAS", "BUY", 100.0),
        ("HEDGE.XNAS", "SELL", 40.0),
        ("DEMO.XNAS", "SELL", 100.0),
    ]


def test_a_hedge_leg_is_not_itself_hedged_or_filtered(backtest) -> None:
    class Both(KansoModifier):
        construct = OVERLAY
        config_cls = Attached

        def evaluate(self, ctx: HookContext) -> Decision:
            return Decision(scale=1.0, hedges=(Hedge("HEDGE.XNAS", 10.0),))

    run = backtest(host(), [attached(Both), attached(Closed)], instruments=(DEMO, HEDGE))

    # The filter refused the entry, so no hedge was asked for either.
    assert run.strategy.intents == ()


def test_a_long_hedge_leg_buys(backtest) -> None:
    class Long(KansoModifier):
        construct = OVERLAY
        config_cls = Attached

        def evaluate(self, ctx: HookContext) -> Decision:
            return Decision(hedges=(Hedge("HEDGE.XNAS", 12.0),))

    run = backtest(host(), [attached(Long)], instruments=(DEMO, HEDGE))

    assert ("HEDGE.XNAS", "BUY", 12.0) in [
        (i.instrument_id, i.side, i.qty) for i in run.strategy.intents
    ]


def test_a_hedge_position_counts_against_gross_exposure(backtest) -> None:
    class Big(KansoModifier):
        construct = OVERLAY
        config_cls = Attached

        def evaluate(self, ctx: HookContext) -> Decision:
            return Decision(hedges=(Hedge("HEDGE.XNAS", 100.0),))

    class Twice(Host):
        """Takes 100 shares, then whatever gross exposure is left."""

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.submit_entry(DEMO, "BUY", qty=100)
            if self.bars == 6:
                self.submit_entry(DEMO, "BUY")

    config = host(max_position_pct=100.0, max_leverage=0.05).kanso_config
    data = flat(DEMO) + flat(HEDGE)
    alone = backtest(Twice(config), data=data, instruments=(DEMO, HEDGE))
    hedged = backtest(
        Twice(config),
        [attached_for(Big, "Twice")],
        data=data,
        instruments=(DEMO, HEDGE),
    )
    second = [i for i in hedged.strategy.intents if i.instrument_id == "DEMO.XNAS"][1]

    # 5% of 100_000 is 5_000 of gross exposure. 100 shares at 10.00 leave 4_000 of it;
    # the hedge's own 100 shares of an instrument the sleeve never sees a price for are
    # marked at what they were opened at, and leave 3_000.
    assert alone.strategy.intents[1].qty == 400.0
    assert second.qty == 300.0


def test_a_hedge_leg_that_floors_to_nothing_is_dropped(backtest) -> None:
    class Crumb(KansoModifier):
        construct = OVERLAY
        config_cls = Attached

        def evaluate(self, ctx: HookContext) -> Decision:
            return Decision(hedges=(Hedge("HEDGE.XNAS", 0.4),))

    run = backtest(host(), [attached(Crumb)], instruments=(DEMO, HEDGE))

    assert [i.instrument_id for i in run.strategy.intents] == ["DEMO.XNAS", "DEMO.XNAS"]


def test_a_hedge_into_an_unknown_instrument_is_refused(backtest) -> None:
    class Elsewhere(KansoModifier):
        construct = OVERLAY
        config_cls = Attached

        def evaluate(self, ctx: HookContext) -> Decision:
            return Decision(hedges=(Hedge("NOPE.XNAS", 1.0),))

    run = backtest(Careful(host().kanso_config), [attached_for(Elsewhere, "Careful")])

    assert "NOPE.XNAS" in run.strategy.error


# --- exit: Decision.exit via before_exit -------------------------------------


class Immediate(KansoModifier):
    construct = EXIT
    config_cls = Attached

    def evaluate(self, ctx: HookContext) -> Decision:
        return Decision(exit=True)


def test_an_exit_rule_closes_the_host_s_position_without_the_host_asking(backtest) -> None:
    alone = backtest(host())
    with_exit = backtest(host(), [attached(Immediate)])

    # The host would have held to the twelfth bar; the construct closes on the fourth.
    assert alone.strategy.intents[1].ts_event == saw_tooth(DEMO)[11].ts_event
    assert with_exit.strategy.intents[1].ts_event == saw_tooth(DEMO)[3].ts_event


def test_an_exit_rule_is_consulted_only_while_a_position_is_open(backtest) -> None:
    class Counting(KansoModifier):
        construct = EXIT
        config_cls = Attached

        def __init__(self, config: Attached) -> None:
            super().__init__(config)
            self.seen: list[int] = []

        def evaluate(self, ctx: HookContext) -> Decision:
            self.seen.append(ctx.ts_event)
            return Decision(exit=False)

    modifier = attached(Counting)
    backtest(host(), [modifier])

    # The third bar's entry fills on the fourth. The rule is asked on every bar the
    # position is open and nothing is already on its way out: not before the fill, and
    # not on the twelfth, where the host's own exit is already in flight.
    assert modifier.seen == [b.ts_event for b in saw_tooth(DEMO)[3:11]]


def test_an_exit_rule_sees_the_position_it_would_close(backtest) -> None:
    class Nosy(KansoModifier):
        construct = EXIT
        config_cls = Attached

        def __init__(self, config: Attached) -> None:
            super().__init__(config)
            self.first: HookContext | None = None

        def evaluate(self, ctx: HookContext) -> Decision:
            if self.first is None:
                self.first = ctx
            return Decision(exit=False)

    modifier = attached(Nosy)
    backtest(host(), [modifier])
    ctx = modifier.first

    assert (ctx.side, ctx.qty, ctx.position_qty) == ("SELL", 100.0, 100.0)


def test_an_exit_rule_on_a_short_buys_it_back(backtest) -> None:
    class Sells(Host):
        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if self.bars == 3:
                self.submit_entry(DEMO, "SELL", qty=100)

    strategy = Sells(host().kanso_config)
    run = backtest(strategy, [attached_for(Immediate, "Sells")])

    assert [i.side for i in run.strategy.intents] == ["SELL", "BUY"]


# --- attachment --------------------------------------------------------------


def test_a_modifier_attaches_by_the_host_s_engine_assigned_id(backtest) -> None:
    run = backtest(host(), [attached_for(Closed, "Host-000")])

    assert run.strategy.intents == ()


def test_a_modifier_naming_another_host_does_not_reach_this_one(backtest) -> None:
    alone = backtest(host())
    run = backtest(host(), [attached_for(Closed, "SomeOtherSleeve")])

    assert run.strategy.intents == alone.strategy.intents


def test_a_modifier_answering_outside_its_construct_is_refused(backtest) -> None:
    class Confused(KansoModifier):
        construct = FILTER
        config_cls = Attached

        def evaluate(self, ctx: HookContext) -> Decision:
            return Decision(allow=True, exit=True)

    run = backtest(Careful(host().kanso_config), [attached_for(Confused, "Careful")])

    assert "filter construct answers allow" in run.strategy.error


def test_a_modifier_stops_answering_once_the_engine_stops_it(backtest) -> None:
    modifier = attached(Closed)
    run = backtest(host(), [modifier])
    order = run.strategy.order_factory.market(DEMO, OrderSide.BUY, Quantity.from_int(1))

    assert run.strategy.intents == ()
    assert run.strategy._decisions(FILTER, run.strategy._context_for(order)) == ()


def test_a_filter_refuses_a_whole_order_list(backtest) -> None:
    class Bracketed(Host):
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

    run = backtest(Bracketed(host().kanso_config), [attached_for(Closed, "Bracketed")])

    assert run.strategy.intents == ()
    assert run.engine.cache.orders() == []


def test_an_exit_rule_does_not_pile_on_an_exit_already_in_flight(backtest) -> None:
    class FromTheTwelfth(KansoModifier):
        construct = EXIT
        config_cls = Attached

        def evaluate(self, ctx: HookContext) -> Decision:
            return Decision(exit=ctx.ts_event >= saw_tooth(DEMO)[11].ts_event)

    # The host submits its own market exit on the twelfth bar; the rule is consulted on
    # that same bar, while that order is still working, and must not send a second one.
    run = backtest(host(), [attached(FromTheTwelfth)])

    assert [i.side for i in run.strategy.intents] == ["BUY", "SELL"]
    (position,) = run.engine.cache.positions()
    assert position.is_closed
