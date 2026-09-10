"""On a coincident grain, on_bar sizes off the bar close, not a same-instant quote."""

from __future__ import annotations

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.objects import Money

from kanso.nautilus import actions
from kanso.nautilus.backtest import CLIENT_ID, _load_stream
from kanso.nautilus.cross_section import KansoCrossSection, arm, with_cross_section
from kanso.nautilus.hooks import OVERLAY
from kanso.nautilus.strategy import (
    Decision,
    HookContext,
    KansoModifier,
    KansoModifierConfig,
    KansoStrategy,
)

from .conftest import DEMO, HEDGE, VENUE, bar, equity, quote, second_bar, trade
from .test_sleeve import config as sleeve_config


def _run(
    strategy: KansoStrategy,
    points: tuple[object, ...],
    *,
    hold: bool,
    modifiers: tuple[object, ...] = (),
) -> None:
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        venue=VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(100_000, USD)],
        modules=actions.modules(VENUE.value),
    )
    engine.add_instrument(equity(DEMO))
    engine.add_instrument(equity(HEDGE))
    _load_stream(engine, points)
    if hold:
        arm(strategy, points)
    for modifier in modifiers:
        engine.add_actor(modifier)
    engine.add_strategy(strategy)
    try:
        engine.run()
    finally:
        engine.dispose()


def test_on_bar_last_price_is_the_bar_close_not_a_same_instant_quote() -> None:
    """Bars of the instant flush before quotes of that instant are observed as last_price."""

    class Probe(KansoStrategy):
        def on_start(self) -> None:
            self.seen: list[tuple[str, float | None]] = []

        def on_bar(self, bar_: object) -> None:
            key = str(bar_.bar_type.instrument_id)  # type: ignore[attr-defined]
            self.seen.append((key, self.last_price(key)))

    strategy = Probe(
        sleeve_config(universe=("DEMO.XNAS", "HEDGE.XNAS"), data_requirements=("bar", "quote"))
    )
    closes = [bar(DEMO, 0, 10.0), bar(HEDGE, 0, 11.0)]
    mids = [quote(DEMO, 0, 99.0), quote(HEDGE, 0, 99.0)]
    _run(strategy, with_cross_section((*closes, *mids)), hold=True)

    assert strategy.seen == [("DEMO.XNAS", 10.0), ("HEDGE.XNAS", 11.0)]


def test_a_flush_marker_is_not_author_data() -> None:
    """The marker is a control signal; it must not become `on_data`."""

    class Probe(KansoStrategy):
        def on_start(self) -> None:
            self.data: list[object] = []

        def on_data(self, data: object) -> None:
            self.data.append(data)

        def on_bar(self, bar_: object) -> None:
            return

    strategy = Probe(sleeve_config(universe=("DEMO.XNAS", "HEDGE.XNAS")))
    points = with_cross_section((bar(DEMO, 0, 10.0), bar(HEDGE, 0, 11.0)))
    _run(strategy, points, hold=True)

    assert strategy.data == []


def test_a_bare_cross_section_does_not_reach_handle_data() -> None:
    """The engine drops a Data subclass that is not wrapped in CustomData."""

    class Probe(KansoStrategy):
        def on_start(self) -> None:
            self.data: list[object] = []

        def handle_data(self, data: object) -> None:
            self.data.append(data)
            super().handle_data(data)

        def on_bar(self, bar_: object) -> None:
            return

    first = bar(DEMO, 0, 10.0)
    bare = KansoCrossSection(n=1, ts_event=int(first.ts_event), ts_init=int(first.ts_init))
    strategy = Probe(sleeve_config())
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        venue=VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(100_000, USD)],
        modules=actions.modules(VENUE.value),
    )
    engine.add_instrument(equity(DEMO))
    engine.add_data([first])
    engine.add_data([bare], client_id=ClientId(CLIENT_ID))
    engine.add_strategy(strategy)
    try:
        engine.run()
    finally:
        engine.dispose()

    assert strategy.data == []


def test_a_held_trade_flushes_after_the_cohort_is_in_the_book() -> None:
    class Probe(KansoStrategy):
        def on_start(self) -> None:
            self.seen: list[str] = []

        def on_trade_tick(self, tick: object) -> None:
            self.seen.append(str(tick.instrument_id))  # type: ignore[attr-defined]

    strategy = Probe(
        sleeve_config(universe=("DEMO.XNAS", "HEDGE.XNAS"), data_requirements=("trade",))
    )
    points = with_cross_section((trade(DEMO, 0), trade(HEDGE, 0)))
    _run(strategy, points, hold=True)

    assert strategy.seen == ["DEMO.XNAS", "HEDGE.XNAS"]


def test_held_author_data_flushes_through_handle_data() -> None:
    from nautilus_trader.core.data import Data
    from nautilus_trader.model.custom import customdataclass

    note_cls = customdataclass(type("_KansoNote", (Data,), {"__annotations__": {"n": int}}))

    class Probe(KansoStrategy):
        def on_start(self) -> None:
            self.generic = 0

        def on_bar(self, bar_: object) -> None:
            if str(bar_.bar_type.instrument_id) == "DEMO.XNAS":  # type: ignore[attr-defined]
                self.handle_data(note_cls(n=1, ts_event=1, ts_init=1))

        def on_data(self, data: object) -> None:
            self.generic += 1

    strategy = Probe(sleeve_config(universe=("DEMO.XNAS", "HEDGE.XNAS")))
    points = with_cross_section((bar(DEMO, 0, 10.0), bar(HEDGE, 0, 11.0)))
    _run(strategy, points, hold=True)

    assert strategy.generic == 1


def test_author_data_inside_a_held_handler_does_not_consume_a_marker() -> None:
    """Data the author raises mid-flush dispatches at once; every cohort point still flushes."""
    from nautilus_trader.core.data import Data
    from nautilus_trader.model.custom import customdataclass

    note_cls = customdataclass(type("_KansoNote2", (Data,), {"__annotations__": {"n": int}}))

    class Probe(KansoStrategy):
        def on_start(self) -> None:
            self.bars = 0
            self.generic = 0

        def on_bar(self, bar_: object) -> None:
            self.bars += 1
            if str(bar_.bar_type.instrument_id) == "DEMO.XNAS":  # type: ignore[attr-defined]
                self.handle_data(note_cls(n=1, ts_event=self.data_time, ts_init=self.data_time))

        def on_data(self, data: object) -> None:
            self.generic += 1

    strategy = Probe(sleeve_config(universe=("DEMO.XNAS", "HEDGE.XNAS")))
    cohorts = [(bar(DEMO, i, 10.0 + i), bar(HEDGE, i, 11.0 + i)) for i in range(4)]
    points = with_cross_section(tuple(point for cohort in cohorts for point in cohort))
    _run(strategy, points, hold=True)

    assert (strategy.bars, strategy.generic, len(strategy._pending)) == (8, 4, 0)


def test_an_overlay_is_consulted_once_per_cohort_not_per_name() -> None:
    """Two names at one instant are one book, and the overlay's clock ticks once for it."""

    class Probe(KansoStrategy):
        def on_bar(self, bar_: object) -> None:
            return

    class Counting(KansoModifier):
        construct = OVERLAY
        config_cls = KansoModifierConfig

        def on_start(self) -> None:
            self.asked: list[tuple[int, str]] = []

        def on_data(self, ctx: HookContext) -> Decision:
            self.asked.append((ctx.ts_event, ctx.instrument_id))
            return Decision.neutral(self.construct)

    strategy = Probe(sleeve_config(universe=("DEMO.XNAS", "HEDGE.XNAS")))
    overlay = Counting(KansoModifierConfig(host_strategy_id="Probe", hyp_id="attached"))
    cohorts = [(bar(DEMO, i, 10.0), bar(HEDGE, i, 11.0)) for i in range(3)]
    points = with_cross_section(tuple(point for cohort in cohorts for point in cohort))
    _run(strategy, points, hold=True, modifiers=(overlay,))

    assert [name for _, name in overlay.asked] == ["HEDGE.XNAS"] * 3
    assert len({ts for ts, _ in overlay.asked}) == 3


def test_the_data_clock_prices_are_the_finest_grain_prints() -> None:
    """On an extra grain the overlay sees the print the venue fills on, for every name."""

    class Probe(KansoStrategy):
        def on_bar(self, bar_: object) -> None:
            return

    class Reading(KansoModifier):
        construct = OVERLAY
        config_cls = KansoModifierConfig

        def on_start(self) -> None:
            self.seen: list[tuple[float | None, float | None, float | None]] = []

        def on_data(self, ctx: HookContext) -> Decision:
            self.seen.append((ctx.price, ctx.prices.get("DEMO.XNAS"), ctx.prices.get("HEDGE.XNAS")))
            return Decision.neutral(self.construct)

    strategy = Probe(sleeve_config(universe=("DEMO.XNAS", "HEDGE.XNAS"), extra_resolutions=("1s",)))
    overlay = Reading(KansoModifierConfig(host_strategy_id="Probe", hyp_id="attached"))
    minute = (bar(DEMO, 0, 10.0), bar(HEDGE, 0, 20.0))
    origin = int(minute[0].ts_init)
    seconds = (
        second_bar(DEMO, 0, 12.0, origin_ns=origin),
        second_bar(HEDGE, 0, 22.0, origin_ns=origin),
        second_bar(DEMO, 1, 13.0, origin_ns=origin),
        second_bar(HEDGE, 1, 23.0, origin_ns=origin),
    )
    _run(strategy, with_cross_section((*minute, *seconds)), hold=True, modifiers=(overlay,))

    assert overlay.seen == [(22.0, 12.0, 22.0), (23.0, 13.0, 23.0)]
    assert strategy.last_price("DEMO.XNAS") == 10.0


def test_engine_delivered_custom_data_is_held_and_flushed_like_any_point() -> None:
    """A custom point the feed delivers waits for its marker; one the author raises does not."""
    from nautilus_trader.core.data import Data
    from nautilus_trader.model.custom import customdataclass
    from nautilus_trader.model.data import CustomData, DataType

    note_cls = customdataclass(type("_KansoNote3", (Data,), {"__annotations__": {"n": int}}))

    class Probe(KansoStrategy):
        def on_start(self) -> None:
            self.generic = 0
            self.subscribe_data(DataType(note_cls), client_id=ClientId(CLIENT_ID))

        def on_bar(self, bar_: object) -> None:
            return

        def on_data(self, data: object) -> None:
            self.generic += 1

    strategy = Probe(sleeve_config(universe=("DEMO.XNAS", "HEDGE.XNAS")))
    first, second = bar(DEMO, 0, 10.0), bar(HEDGE, 0, 11.0)
    ts = int(first.ts_init)
    notes = (
        CustomData(DataType(note_cls), note_cls(n=1, ts_event=ts, ts_init=ts)),
        CustomData(DataType(note_cls), note_cls(n=2, ts_event=ts, ts_init=ts)),
    )
    _run(strategy, with_cross_section((first, second, *notes)), hold=True)

    assert strategy.generic == 2
    assert not strategy._pending
