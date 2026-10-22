"""A sleeve keeps its position reads until something they read changes, and a kept read is
always the read it stands for.

`held`, `_own_filled` and `_own_intended` are kept by name and forgotten at every change of
what they read (`KansoStrategy._moved`). The property here drives a real engine through
random orders — entries at market and at a limit, exits, hand-built orders, modifies,
cancels of one order and of a name, the engine's own close — with and without a latency,
and with clips an overlay holds, and at every bar and every order and position event the
sleeve's reads are compared with the reads made from scratch the way they were made before
anything was kept. The suite's own check (`tests/conftest.py`) is off here, so what is
answered is what a card is answered.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import LatencyModel
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Money, Price, Quantity

from kanso.nautilus import actions
from kanso.nautilus.hooks import OVERLAY, Clip
from kanso.nautilus.strategy import (
    Decision,
    HookContext,
    KansoModifier,
    KansoModifierConfig,
    KansoStrategy,
    _name_of,
    _same,
)

from .conftest import DEMO, HEDGE, VENUE, bar, equity, saw_tooth
from .test_sizing import sized, two_names
from .test_sleeve import FREE, config

NAMES = ("DEMO.XNAS", "HEDGE.XNAS")
BARS = 24


@pytest.fixture(autouse=True)
def _kept_reads_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kept reads are answered as kept, as in a card."""
    monkeypatch.setattr(KansoStrategy, "_checking", False)


def from_scratch(sleeve: KansoStrategy, key: str) -> tuple[float, float]:
    """`(_own_filled, _own_intended)` of a name, read as the harness read them before it kept
    anything."""
    instrument_id = InstrumentId.from_str(key)
    position = sum(
        (
            held.signed_decimal_qty()
            for held in sleeve.cache.positions_open(
                instrument_id=instrument_id, strategy_id=sleeve.id
            )
        ),
        Decimal(0),
    )
    clips = sleeve._clip_orders.get(key, ())
    filled = float(position) - sum((_signed(order, filled=True) for order in clips), 0.0)
    flying = sum(
        (
            _signed(order, filled=False)
            for order in sleeve._sent
            if not order.is_closed  # type: ignore[attr-defined]
            and order.instrument_id.value == key  # type: ignore[attr-defined]
            and not any(order is clip for clip in clips)
        ),
        0.0,
    )
    return filled, filled + flying


def _signed(order: object, *, filled: bool) -> float:
    qty = float(order.filled_qty if filled else order.leaves_qty)  # type: ignore[attr-defined]
    return float(-qty if order.side == OrderSide.SELL else qty)  # type: ignore[attr-defined]


def compare(sleeve: KansoStrategy) -> int:
    """Every name's kept reads against the reads from scratch, bit for bit; how many."""
    for instrument_id in sleeve.universe:
        key = instrument_id.value
        filled, intended = from_scratch(sleeve, key)
        for read in (sleeve.held(instrument_id), sleeve.held(key), sleeve._own_intended(key)):
            assert read.hex() == intended.hex(), (key, read, intended)
        assert sleeve._own_filled(key).hex() == filled.hex(), (key, filled)
    return 1


Step = tuple[object, ...]

steps = st.one_of(
    st.tuples(
        st.just("entry"),
        st.sampled_from(NAMES),
        st.sampled_from(("BUY", "SELL")),
        st.integers(min_value=1, max_value=400),
        st.none() | st.sampled_from((-1.0, -0.6, -0.1, 0.1, 0.6, 1.0)),
    ),
    st.tuples(
        st.just("exit"),
        st.sampled_from(NAMES),
        st.none() | st.integers(min_value=1, max_value=400),
        st.none() | st.sampled_from((-0.6, 0.0, 0.6)),
    ),
    st.tuples(
        st.just("hand"),
        st.sampled_from(NAMES),
        st.sampled_from(("BUY", "SELL")),
        st.integers(min_value=1, max_value=100),
    ),
    st.tuples(st.just("cancel_name"), st.sampled_from(NAMES)),
    st.tuples(st.just("cancel_one")),
    st.tuples(st.just("modify"), st.sampled_from((-0.3, 0.3))),
    st.tuples(st.just("close"), st.sampled_from(NAMES)),
)
scripts = st.dictionaries(
    st.integers(min_value=2, max_value=BARS - 2),
    st.lists(steps, min_size=1, max_size=4),
    min_size=4,
    max_size=BARS - 3,
)


class Shuffles(KansoStrategy):
    """Plays its script on DEMO's bars and compares its reads on every bar and every event."""

    script: dict[int, list[Step]] = {}

    def on_start(self) -> None:
        self.bars = 0
        self.compared = 0

    def on_bar(self, bar_: object) -> None:
        self.compared += compare(self)
        if str(bar_.bar_type.instrument_id) != "DEMO.XNAS":  # type: ignore[attr-defined]
            return
        self.bars += 1
        for step in self.script.get(self.bars, ()):
            self.play(step)
            self.compared += compare(self)

    def on_event(self, event: object) -> None:
        self.compared += compare(self)

    def play(self, step: Step) -> None:
        kind = step[0]
        if kind == "entry":
            _, name, side, qty, offset = step
            price = None if offset is None else self._near(str(name), float(offset))  # type: ignore[arg-type]
            self.submit_entry(str(name), str(side), qty=float(qty), price=price)  # type: ignore[arg-type]
        elif kind == "exit":
            _, name, qty, offset = step
            price = None if offset is None else self._near(str(name), float(offset))  # type: ignore[arg-type]
            self.submit_exit(
                str(name),
                qty=None if qty is None else float(qty),  # type: ignore[arg-type]
                price=price,
            )
        elif kind == "hand":
            _, name, side, qty = step
            order = self.order_factory.market(
                InstrumentId.from_str(str(name)),
                OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                Quantity.from_int(int(qty)),  # type: ignore[call-overload]
            )
            self.submit_order(order)
        elif kind == "cancel_name":
            self.cancel_all_orders(InstrumentId.from_str(str(step[1])))
        elif kind == "cancel_one":
            working = self.cache.orders_open(strategy_id=self.id)
            if working:
                self.cancel_order(working[0])
        elif kind == "modify":
            for order in self.cache.orders_open(strategy_id=self.id):
                if getattr(order, "price", None) is not None and not order.is_pending_update:
                    moved = round(float(order.price) + float(step[1]), 2)  # type: ignore[arg-type]
                    self.modify_order(order, price=Price(max(moved, 0.01), 2))
                    break
        else:
            self.close_all_positions(InstrumentId.from_str(str(step[1])))

    def _near(self, name: str, offset: float) -> float | None:
        last = self.last_price(name)
        return None if last is None else round(last + offset, 2)


def run(sleeve: KansoStrategy, modifiers: Sequence[object], latency_ms: float, data: list[object]):
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(1_000_000, USD)],
            modules=actions.modules(VENUE.value),
            latency_model=(
                LatencyModel(base_latency_nanos=int(latency_ms * 1_000_000)) if latency_ms else None
            ),
        )
        engine.add_instrument(equity(DEMO))
        engine.add_instrument(equity(HEDGE))
        engine.add_data(data)
        for modifier in modifiers:
            engine.add_actor(modifier)
        engine.add_strategy(sleeve)
        engine.run()
        compare(sleeve)
    finally:
        engine.dispose()


@settings(max_examples=60, deadline=None)
@given(script=scripts, latency_ms=st.sampled_from((0.0, 20.0)))
def test_every_read_a_sleeve_keeps_is_the_read_from_scratch(
    script: dict[int, list[Step]], latency_ms: float
) -> None:
    """Whatever orders a free sleeve sends, modifies and cancels in two names, at no latency
    or at 20 ms, every read it is answered — on a bar, after each order, on every order and
    position event — is the one read from scratch."""
    costs = {**FREE["costs"], "latency_ms": latency_ms}
    sleeve = Shuffles(
        config(
            universe=NAMES,
            max_position_pct=100.0,
            max_leverage=4.0,
            capital=1_000_000.0,
            venue_model={"costs": costs},
        )
    )
    sleeve.script = script
    run(
        sleeve,
        (),
        latency_ms,
        [*saw_tooth(DEMO, BARS), *(bar(HEDGE, i, 21.0 - 0.25 * (i % 5)) for i in range(BARS))],
    )

    assert sleeve.compared >= BARS


class Clipping(KansoModifier):
    """Opens and takes off clips from a script keyed by the host's bar count."""

    construct = OVERLAY
    config_cls = KansoModifierConfig
    script: dict[int, tuple[Clip, ...]] = {}

    def on_start(self) -> None:
        self.ticks = 0

    def on_data(self, ctx: HookContext) -> Decision:
        self.ticks += 1
        clips = self.script.get(self.ticks)
        return Decision.neutral(self.construct) if clips is None else Decision(clips=clips)


class Holds(Shuffles):
    """A sized host: enters DEMO on the third bar and exits it on the sixteenth."""

    def play(self, step: Step) -> None:
        return

    def on_bar(self, bar_: object) -> None:
        super().on_bar(bar_)
        if str(bar_.bar_type.instrument_id) != "DEMO.XNAS":  # type: ignore[attr-defined]
            return
        if self.bars == 3:
            self.submit_entry(DEMO, "BUY")
        if self.bars == 16:
            self.submit_exit(DEMO)
        self.compared += compare(self)


episodes = st.lists(
    st.tuples(st.sampled_from(NAMES), st.sampled_from(("BUY", "SELL"))), min_size=1, max_size=3
)


@settings(max_examples=20, deadline=None)
@given(episodes=episodes, latency_ms=st.sampled_from((0.0, 20.0)))
def test_a_sized_host_s_kept_reads_take_its_overlay_s_clips_out(
    episodes: list[tuple[str, str]], latency_ms: float
) -> None:
    """Clips an overlay opens and takes off in either name, at no latency or at 20 ms: every
    read the host is answered is the one read from scratch, clips taken out."""
    script: dict[int, tuple[Clip, ...]] = {}
    for index, (name, side) in enumerate(episodes):
        script[4 + 6 * index] = (Clip(name, side),)
        script[7 + 6 * index] = (Clip(name, "FLAT"),)

    class Scripted(Clipping):
        pass

    Scripted.script = script
    costs = {**FREE["costs"], "latency_ms": latency_ms}
    host = Holds(sized(venue_model={"costs": costs}))
    overlay = Scripted(
        KansoModifierConfig(host_strategy_id="Holds", hyp_id="attached", sizing_budget=20_000.0)
    )
    run(host, (overlay,), latency_ms, two_names())

    assert host._clip_orders, "the overlay held clips"
    assert host.compared >= BARS


def test_a_kept_read_is_answered_until_something_it_reads_changes(backtest) -> None:
    """A kept read is answered as kept — here one planted by hand — until `_moved`."""
    ran = backtest(Shuffles(config()))
    sleeve = ran.strategy
    assert sleeve.held(DEMO) == 0.0

    sleeve._intended_at["DEMO.XNAS"] = 7.0
    sleeve._filled_at["DEMO.XNAS"] = 5.0

    assert (sleeve.held(DEMO), sleeve._own_filled("DEMO.XNAS")) == (7.0, 5.0)
    sleeve._moved()
    assert (sleeve.held(DEMO), sleeve._own_filled("DEMO.XNAS")) == (0.0, 0.0)


def test_under_checking_a_kept_read_unlike_the_fresh_one_raises(
    backtest, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = backtest(Shuffles(config()))
    sleeve = ran.strategy
    monkeypatch.setattr(KansoStrategy, "_checking", True)
    sleeve._intended_at["DEMO.XNAS"] = 7.0

    with pytest.raises(AssertionError, match=r"_own_intended\('DEMO.XNAS'\) kept 7.0"):
        sleeve.held(DEMO)


def test_kept_and_read_are_compared_bit_for_bit() -> None:
    assert _same(None, 2.0, "held", "DEMO.XNAS") == 2.0
    assert _same(2.0, 2.0, "held", "DEMO.XNAS") == 2.0
    with pytest.raises(AssertionError):
        _same(0.0, -0.0, "held", "DEMO.XNAS")


def test_an_id_s_name_is_formatted_once_and_kept() -> None:
    assert _name_of(DEMO) == "DEMO.XNAS"
    assert _name_of(InstrumentId.from_str("DEMO.XNAS")) is _name_of(DEMO)
