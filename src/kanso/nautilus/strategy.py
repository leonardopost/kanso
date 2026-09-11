"""The strategy API: the sleeve class, the attached-construct class and their configs.

A hypothesis becomes a `strategy.py` holding a `Config` and a `Strategy`. `KansoStrategy`
is the base that turns one into something the framework can run identically in a backtest,
a sandbox and a live node, and it does four things the author never writes:

**It is the only clock.** `data_time` is the `ts_event` of the last data event whose
author handler is running — the economic reference time of the information the strategy
is acting on. It exists because the engine's own clock is a different object in each
environment: a test clock advanced by the data stream in a backtest, a wall clock in a
live node. Reading the engine clock therefore makes a strategy behave differently in
replay than it did in research, which is exactly what the parity gate refuses. `data_time`
is held by overriding the engine's data handlers, so it is correct before the author's
`on_bar` runs. The engine delivers by `ts_init` (availability) and never by `ts_event`, so
a late-published point can carry an earlier reference time than its predecessor;
`data_time` reports the event being handled, because that is the reference time of the
information the strategy is acting on. On a multi-instrument instant the last prices of
every name that printed are current before any handler of that grain runs; `data_time`
still belongs to the event whose handler is running, not to the last arrival of the
instant.

**It records every order intent.** `(ts_event, instrument, side, qty, order_type, price?)`
per submitted order, stamped with `data_time` rather than a wall clock, captured by
overriding `submit_order` and `submit_order_list`. Every path that reaches the venue goes
through one of the two — the cost-aware helpers below, an order the author built by hand,
and the engine's own `close_position` / `close_all_positions`, which construct a market
order and submit it through `submit_order`. An order that bypasses the helpers is still on
the record.

**It sizes.** The cost-aware helpers `submit_entry` and `submit_exit` choose a quantity
from the capital and the risk limits injected from the hypothesis, leaving room for the
round-trip cost the runner will apply. This is where per-strategy exposure is enforced,
because the engine has nowhere else to enforce it: `RiskEngineConfig` offers exactly one
limit, `max_notional_per_order` keyed by instrument, which is a per-order backstop and
knows nothing of a position, a strategy or a book.

**It does not apply corporate actions, and must not.** A split is applied by the venue,
one call before the ex-date's first point is matched (`kanso.nautilus.actions`), because a
sleeve handles a point only after the exchange has already matched against it — measured, a
take-profit resting at fifty was filled for 1,005 shares on the ex-date bar of a one-for-ten
reverse split that restated the price to a hundred, and the card reported the bookkeeping
change as a 40% return. By the time `on_bar` runs, the positions this sleeve holds are
already in post-split shares and its resting orders in that instrument are already cancelled.

**It consults the constructs attached to it.** A filter, an overlay or an exit rule is a
`KansoModifier` — an engine actor, because a strategy config cannot configure an actor and
an actor config cannot configure a strategy; the two are sibling types and the engine
raises `TypeError` on the wrong pairing. Modifiers register against their host and the
sleeve reads them synchronously through `before_entry`, `size`, `before_exit` and `hedges`.
An overlay is consulted once more per cohort of its own grain, through `on_data`, after
every handler of that cohort and never while a market order of the sleeve is unfilled, so
a clipper can add and take off legs while the host is already holding and does not wait
for the next host buy. Every attached overlay is asked once per host entry. Each order
is classified as an entry or an exit by its effect on the net position: an order that
shrinks the absolute net position is an exit, anything else is an entry.

Engine facts this module relies on (nautilus_trader 1.231.0): `Strategy` and `Actor`
methods are `cpdef`, so a Python subclass's `handle_bar`, `handle_quote_tick`,
`handle_trade_tick`, `handle_data`, `submit_order`, `submit_order_list`, `_start` and
`_stop` all take precedence when the engine calls them; `Trader` starts actors before
strategies, so a modifier is registered before its host runs; `close_position` and
`close_all_positions` route through `submit_order`; `portfolio.net_position(instrument_id)`
returns a signed `Decimal`; `StrategyConfig` and `ActorConfig` are frozen msgspec structs
whose subclasses inherit the freeze, and the engine defines no `config_cls` — `config_cls`
here is kanso's own attribute, honoured by kanso's loader alone.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any, ClassVar, Final, NoReturn

from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig, StrategyConfig
from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    AggregationSource,
    BarAggregation,
    OrderSide,
    OrderType,
    PriceType,
    order_side_to_str,
    order_type_to_str,
)
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from kanso.errors import ValidationError
from kanso.nautilus import splits
from kanso.nautilus.costs import fixed_half_spread, quote_half_spread, side_rate
from kanso.nautilus.cross_section import MARKER_TYPE, KansoCrossSection
from kanso.nautilus.hooks import (
    BUY,
    EXIT,
    FILTER,
    FLAT,
    MODIFIER_CONSTRUCTS,
    OVERLAY,
    Clip,
    Decision,
    Hedge,
    HookContext,
    deregister_modifier,
    modifiers_for,
    register_modifier,
)
from kanso.nautilus.sizing import (
    BUDGET_BELOW_LOT,
    CLIP_DIRECTION,
    CLIP_WITHOUT_BUDGET,
    HAND_BUILT_ORDER,
    HEDGE_UNDER_SIZING,
    ONE_CLIP,
    ONE_POSITION,
    SCALE_UNDER_SIZING,
    SIZE_ARGUMENT,
    UNFUNDED_ORDER,
    Refusal,
    SizingError,
    full_book_quantity,
)
from kanso.schemas.duration import is_duration

__all__ = [
    "BAR",
    "ENTRY",
    "EXIT_ORDER",
    "QUOTE",
    "TRADE",
    "Clip",
    "Decision",
    "Hedge",
    "HookContext",
    "KansoConfig",
    "KansoModifier",
    "KansoModifierConfig",
    "KansoStrategy",
    "OrderIntent",
    "tunable_fields",
]

BAR: Final = "bar"
QUOTE: Final = "quote"
TRADE: Final = "trade"

ENTRY: Final = "entry"
"""An order that opens or grows the net position."""

EXIT_ORDER: Final = "exit"
"""An order that shrinks the absolute net position."""

BASIS_POINT: Final = 10_000.0
PERCENT: Final = 100.0
ROUND_TRIP: Final = 2

_AGGREGATION: Final[dict[str, object]] = {
    "s": BarAggregation.SECOND,
    "m": BarAggregation.MINUTE,
    "h": BarAggregation.HOUR,
    "d": BarAggregation.DAY,
    "w": BarAggregation.WEEK,
}
_UNIT: Final[dict[object, str]] = {value: key for key, value in _AGGREGATION.items()}


@dataclass(frozen=True)
class OrderIntent:
    """One order the sleeve submitted, stamped with data time rather than wall time."""

    ts_event: int
    instrument_id: str
    side: str
    qty: float
    order_type: str
    price: float | None = None


class KansoConfig(StrategyConfig, frozen=True):
    """What the hypothesis injects into a sleeve, and the base an author extends.

    Every field here is set by kanso from `hypothesis.yaml` and the resolved venue model;
    the numeric fields an author adds in their own subclass are the strategy's parameters,
    and are what the `param_plateau` gate perturbs — `tunable_fields` is that set.
    """

    hyp_id: str = ""
    universe: tuple[str, ...] = ()
    resolution: str = "1d"
    extra_resolutions: tuple[str, ...] = ()
    """Bar grains subscribed in addition to `resolution`, so an overlay can have its own
    clock without the host's `on_bar` running on that grain."""

    data_requirements: tuple[str, ...] = (BAR,)
    capital: float = 0.0
    max_position_pct: float = PERCENT
    max_drawdown_pct: float = PERCENT
    max_leverage: float = 1.0
    venue_model: dict[str, object] = {}
    sizing_budget: float = 0.0
    """The budget every order is sized to when the hypothesis declares `sizing`; zero
    is free sizing, where the author chooses a size within the risk limits."""


class KansoModifierConfig(ActorConfig, frozen=True):
    """What an attached construct is configured with.

    It derives from `ActorConfig` and not from `KansoConfig` because a modifier is an
    engine actor: the engine's two config bases are siblings and it refuses to configure an
    actor with a strategy config.
    """

    host_strategy_id: str = ""
    """The sleeve this construct attaches to: its `StrategyId`, or its class name."""

    hyp_id: str = ""

    sizing_budget: float = 0.0
    """The budget a sized overlay's clips are sized to; zero means it hedges with
    quantities of its own and places no clip."""


def tunable_fields(config: StrategyConfig) -> tuple[str, ...]:
    """The numeric parameters of a strategy config: its own fields, not the injected ones.

    A perturbation gate needs the author's parameters and must not touch the capital, the
    risk limits or anything else the framework injected, so the base's field set is
    subtracted. Booleans are not parameters.
    """
    injected = set(KansoConfig.__struct_fields__)
    names: list[str] = []
    for name in type(config).__struct_fields__:
        if name in injected:
            continue
        value = getattr(config, name, None)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        names.append(name)
    return tuple(names)


def _bar_type(instrument_id: InstrumentId, resolution: str) -> BarType:
    """The external bar type a duration resolution names for one instrument."""
    if not is_duration(resolution):
        raise ValidationError(
            f"resolution: {resolution!r} is not a bar size, so no bar type exists for it; "
            "subscribe to quotes or trades instead"
        )
    step, unit = int(resolution[:-1]), resolution[-1]
    return BarType(
        instrument_id,
        BarSpecification(step, _AGGREGATION[unit], PriceType.LAST),
        AggregationSource.EXTERNAL,
    )


def _resolution_of_bar(bar: Bar) -> str:
    """The duration string (`1s`, `1d`, …) a bar's specification names."""
    spec = bar.bar_type.spec
    unit = _UNIT.get(spec.aggregation)
    if unit is None:
        raise ValidationError(
            f"bar: aggregation {spec.aggregation!r} is not a duration kanso subscribes"
        )
    return f"{int(spec.step)}{unit}"


def _signed(order: object, *, filled: bool) -> float:
    """An order's filled quantity, or what is left of it, signed by its side."""
    qty = float(order.filled_qty if filled else order.leaves_qty)  # type: ignore[attr-defined]
    return float(-qty if order.side == OrderSide.SELL else qty)  # type: ignore[attr-defined]


def _budget_of(modifier: object) -> float:
    """The budget a modifier's clips are sized to; zero for one without a config or a rule."""
    config = getattr(modifier, "modifier_config", None)
    return float(getattr(config, "sizing_budget", 0.0) or 0.0)


def _charges(config: KansoConfig) -> Mapping[str, Any]:
    """The venue model's cost rates as the config carries them; none when it states none."""
    costs = config.venue_model.get("costs")
    return costs if isinstance(costs, Mapping) else {}


def _order_price(order: object) -> float | None:
    price = getattr(order, "price", None)
    return None if price is None else float(price)


class KansoStrategy(Strategy):  # type: ignore[misc]
    """The sleeve: a whole strategy, with its universe and its limits injected."""

    config_cls: ClassVar[type[KansoConfig]] = KansoConfig

    def __init__(self, config: KansoConfig | None = None) -> None:
        resolved = self.config_cls() if config is None else config
        if not isinstance(resolved, KansoConfig):
            raise ValidationError(
                f"config: {type(resolved).__name__} configures a sleeve but does not subclass "
                "KansoConfig; a sleeve is an engine strategy and needs a strategy config"
            )
        super().__init__(resolved)
        self._cfg: KansoConfig = resolved
        self._data_time = 0
        self._intents: list[OrderIntent] = []
        self._last_bar: dict[str, Bar] = {}
        self._last_quote: dict[str, QuoteTick] = {}
        self._last_trade: dict[str, TradeTick] = {}
        self._last_price: dict[str, float] = {}
        self._last_print: dict[str, float] = {}
        self._sent: list[object] = []
        self._hedging = False
        self._exiting = False
        self._hold_until_cross_section = False
        self._flushing = False
        self._pending: deque[object] = deque()
        self._overlay_due: tuple[InstrumentId, Bar | None, float | None] | None = None
        self._entry_answers: tuple[tuple[object, Decision], ...] | None = None
        self._clip_orders: dict[str, list[object]] = {}
        self._sizing_call = False
        self._built = False
        self._charges = _charges(resolved)
        self._cash = resolved.capital
        self._ledger: list[list[Any]] = []
        self._print_ns: dict[str, int] = {}
        self._quoted: dict[str, tuple[list[int], list[float]]] = {}

    # --- what the hypothesis injected ---------------------------------------

    @property
    def kanso_config(self) -> KansoConfig:
        """The injected configuration, typed."""
        return self._cfg

    @property
    def universe(self) -> tuple[InstrumentId, ...]:
        """The instruments this hypothesis trades, as the engine identifies them."""
        return tuple(InstrumentId.from_str(value) for value in self._cfg.universe)

    @property
    def capital(self) -> float:
        """The starting balance the hypothesis is sized against."""
        return self._cfg.capital

    @property
    def sized(self) -> bool:
        """Whether the hypothesis declared a sizing rule, so the harness sizes every order."""
        return self._cfg.sizing_budget > 0

    @property
    def budget(self) -> float:
        """The budget every entry is sized to under the sizing rule; zero when free."""
        return self._cfg.sizing_budget

    @property
    def venue_model(self) -> Mapping[str, object]:
        """The resolved venue model: broker, account, currency and cost model."""
        return self._cfg.venue_model

    @property
    def cost_rate(self) -> float:
        """One-way cost as a fraction of notional, from the resolved venue model.

        The runner applies costs once, in its extraction, and never inside the simulated
        venue; this is the same number, used to leave room when sizing so a position is
        not opened at exactly the limit and then pushed through it by its own costs.
        """
        costs = self._cfg.venue_model.get("costs")
        if not isinstance(costs, Mapping):
            return 0.0
        bps = float(costs.get("commission_bps") or 0.0) + float(costs.get("slippage_bps") or 0.0)
        if costs.get("spread") == "fixed_bps":
            bps += float(costs.get("fixed_bps") or 0.0)
        return bps / BASIS_POINT

    @property
    def max_notional(self) -> float:
        """The most one instrument may hold: `max_position_pct` of the smaller of the capital
        and `balance`, so a sleeve that has lost money cannot borrow to keep its size."""
        return min(self.capital, self.balance) * self._cfg.max_position_pct / PERCENT

    @property
    def gross_limit(self) -> float:
        """The most the sleeve may hold across every instrument: `max_leverage` x the smaller
        of the capital and `balance`."""
        return min(self.capital, self.balance) * self._cfg.max_leverage

    # --- the only clock ------------------------------------------------------

    @property
    def data_time(self) -> int:
        """The `ts_event` of the last data event handled; zero before the first.

        The only time source a strategy may read.
        """
        return self._data_time

    @property
    def intents(self) -> tuple[OrderIntent, ...]:
        """Every order this sleeve submitted, in order."""
        return tuple(self._intents)

    def last_bar(self, instrument_id: InstrumentId | str) -> Bar | None:
        """The last bar seen for an instrument."""
        return self._last_bar.get(str(instrument_id))

    def last_quote(self, instrument_id: InstrumentId | str) -> QuoteTick | None:
        """The last quote seen for an instrument."""
        return self._last_quote.get(str(instrument_id))

    def last_trade(self, instrument_id: InstrumentId | str) -> TradeTick | None:
        """The last trade seen for an instrument."""
        return self._last_trade.get(str(instrument_id))

    def last_price(self, instrument_id: InstrumentId | str) -> float | None:
        """The last price seen for an instrument: a bar close, a quote mid or a trade."""
        return self._last_price.get(str(instrument_id))

    def held(self, instrument_id: InstrumentId | str) -> float:
        """This sleeve's own signed position in an instrument, unfilled orders applied.

        The reader a strategy sizes and flips by, on both paths: the venue's net position
        less what the attached overlays hold as clips in the same name, plus what the
        sleeve's own market orders in flight will add. An exit submitted in this handler
        already reads as gone, which is what lets the entry that follows it size to the
        room the exit frees.
        """
        return self._own_intended(str(instrument_id))

    @property
    def balance(self) -> float:
        """What this sleeve's account is worth now: the runner's equity, kept as it runs.

        The capital, less what every fill paid for what it bought and the commission,
        slippage and half-spread the runner charges it, plus every open position marked at
        its last print. It is the runner's own arithmetic (`kanso.nautilus.costs`) on the
        sleeve's own fills, so at every period end it is the equity the card records, and
        between period ends it moves with every fill and every print. It is kept from the
        fills and from the share counts the venue restated at a split, never from the
        engine's account, whose balance a corporate action leaves quoted in shares no
        position holds. A position the sleeve has seen no price for is marked at its own
        split-aware cost, where the runner marks it at the last price its stream holds.

        Entries and an overlay's hedge legs are cut to what the smaller of this and the
        capital can fund, and a strategy may size from it.
        """
        if self.cache is None:  # not registered with an engine: nothing booked, nothing held
            return self._cash
        self._settle()
        worth = sum(
            (self._worth(position) for position in self.cache.positions_open(strategy_id=self.id)),
            0.0,
        )
        return self._cash + worth

    # --- lifecycle -----------------------------------------------------------

    def _start(self) -> None:
        self.subscribe_universe()
        self._ledger.extend([order, 0] for order in self.cache.orders(strategy_id=self.id))
        super()._start()

    def subscribe_universe(self) -> None:
        """Subscribe every instrument of the universe to every data requirement.

        Called before `on_start`, so an author's `on_start` need not call anything. `bar`,
        `quote` and `trade` are subscribed here; a requirement naming a registered custom
        type is left to the author, who knows the class, and is logged as unsubscribed.
        """
        for instrument_id in self.universe:
            for requirement in self._cfg.data_requirements:
                if requirement == BAR:
                    self.subscribe_bars(_bar_type(instrument_id, self._cfg.resolution))
                    for extra in self._cfg.extra_resolutions:
                        if extra != self._cfg.resolution:
                            self.subscribe_bars(_bar_type(instrument_id, extra))
                elif requirement == QUOTE:
                    self.subscribe_quote_ticks(instrument_id)
                elif requirement == TRADE:
                    self.subscribe_trade_ticks(instrument_id)
                else:
                    self.log.warning(
                        f"data requirement {requirement!r} is a custom type; "
                        "subscribe to it from on_start with its registered class"
                    )
        if self._hold_until_cross_section:
            from nautilus_trader.model.identifiers import ClientId

            from kanso.nautilus.backtest import CLIENT_ID

            self.subscribe_data(MARKER_TYPE, client_id=ClientId(CLIENT_ID))

    # --- data handlers: the clock, the last observations, the exit rules -----

    def handle_bar(self, bar: Bar, historical: bool = False) -> None:
        if historical:
            super().handle_bar(bar, historical)
            return
        key = bar.bar_type.instrument_id.value
        self._printed(key, float(bar.close), int(bar.ts_event))
        if not self._is_extra_bar(bar):
            self._last_bar[key] = bar
            self._observe_price(key, float(bar.close))
        if self._held():
            self._pending.append(bar)
            return
        self._dispatch_bar(bar)
        self._consult_due()

    def handle_quote_tick(self, tick: QuoteTick, historical: bool = False) -> None:
        if historical:
            super().handle_quote_tick(tick, historical)
            return
        key = tick.instrument_id.value
        self._last_quote[key] = tick
        mid = (float(tick.bid_price) + float(tick.ask_price)) / 2.0
        self._printed(key, mid, int(tick.ts_event))
        self._observe_price(key, mid)
        if self._charges.get("spread") == "quotes":
            times, values = self._quoted.setdefault(key, ([], []))
            times.append(int(tick.ts_init))
            values.append(quote_half_spread(float(tick.bid_price), float(tick.ask_price)))
            self._settle()
        if self._held():
            self._pending.append(tick)
            return
        self._dispatch_quote(tick)
        self._consult_due()

    def handle_trade_tick(self, tick: TradeTick, historical: bool = False) -> None:
        if historical:
            super().handle_trade_tick(tick, historical)
            return
        key = tick.instrument_id.value
        self._last_trade[key] = tick
        self._printed(key, float(tick.price), int(tick.ts_event))
        self._observe_price(key, float(tick.price))
        if self._held():
            self._pending.append(tick)
            return
        self._dispatch_trade(tick)
        self._consult_due()

    def handle_data(self, data: object) -> None:
        if isinstance(data, KansoCrossSection):
            if self._pending:
                self._flush_one()
            if not self._pending:
                self._consult_due()
            return
        if self._held():
            self._pending.append(data)
            return
        self._dispatch_data(data)
        self._consult_due()

    def _held(self) -> bool:
        """Whether a point arriving now waits for a flush marker.

        Only what the engine delivers is held. Anything that arrives while a flush is
        already running was raised by the author's own handler — the engine delivers
        nothing re-entrantly — and is dispatched at once, or it would take the marker
        meant for a point of the cohort and leave that point in the buffer.
        """
        return self._hold_until_cross_section and not self._flushing

    def _flush_one(self) -> None:
        event = self._pending.popleft()
        self._flushing = True
        try:
            if isinstance(event, Bar):
                self._dispatch_bar(event)
            elif isinstance(event, QuoteTick):
                self._dispatch_quote(event)
            elif isinstance(event, TradeTick):
                self._dispatch_trade(event)
            else:
                self._dispatch_data(event)
        finally:
            self._flushing = False

    def _consult_due(self) -> None:
        """Consult the attached overlays once for the cohort just handled.

        Every handler of a cohort marks the overlay clock due; it fires once, after the
        last of them, so an overlay sees the fills of the whole instant rather than
        answering after each name with the next name's handler still to run. On the
        unmarked path a cohort is one point, so this runs after every handler. While a
        cohort is still flushing, author-raised data inside a handler leaves the mark for
        the cohort's end rather than consulting mid-instant.
        """
        if self._hold_until_cross_section and (self._pending or self._flushing):
            return
        due = self._overlay_due
        self._overlay_due = None
        if due is not None:
            instrument_id, last_bar, price = due
            self._consult_overlay(instrument_id, last_bar=last_bar, price=price)

    def _dispatch_bar(self, bar: Bar) -> None:
        self._data_time = int(bar.ts_event)
        instrument_id = bar.bar_type.instrument_id
        if self._is_extra_bar(bar):
            self._overlay_due = (instrument_id, bar, float(bar.close))
            return
        super().handle_bar(bar, False)
        self._consult_exit(instrument_id)
        if not self._cfg.extra_resolutions:
            self._overlay_due = (instrument_id, None, None)

    def _dispatch_quote(self, tick: QuoteTick) -> None:
        self._data_time = int(tick.ts_event)
        super().handle_quote_tick(tick, False)
        self._consult_exit(tick.instrument_id)
        if not self._cfg.extra_resolutions:
            self._overlay_due = (tick.instrument_id, None, None)

    def _dispatch_trade(self, tick: TradeTick) -> None:
        self._data_time = int(tick.ts_event)
        super().handle_trade_tick(tick, False)
        self._consult_exit(tick.instrument_id)
        if not self._cfg.extra_resolutions:
            self._overlay_due = (tick.instrument_id, None, None)

    def _dispatch_data(self, data: object) -> None:
        instrument_id = getattr(data, "instrument_id", None)
        key = None if instrument_id is None else str(instrument_id)
        self._observe(key, int(getattr(data, "ts_event", self._data_time)), None)
        super().handle_data(data)
        if instrument_id is not None:
            self._consult_exit(instrument_id)
            if not self._cfg.extra_resolutions:
                self._overlay_due = (instrument_id, None, None)

    def _printed(self, key: str, price: float, ts_event: int) -> None:
        """Keep the last price an instrument printed at, and when, for marking what it holds."""
        self._last_print[key] = price
        self._print_ns[key] = ts_event

    def _observe_price(self, key: str | None, price: float | None) -> None:
        if key is not None and price is not None:
            self._last_price[key] = price

    def _observe(self, key: str | None, ts_event: int, price: float | None) -> None:
        self._data_time = ts_event
        self._observe_price(key, price)

    # --- hooks: what an attached construct changes ---------------------------

    def before_entry(self, ctx: HookContext) -> bool:
        """Whether an entry may be placed. Every attached filter must allow it."""
        return all(decision.allow is not False for decision in self._decisions(FILTER, ctx))

    def size(self, ctx: HookContext, qty: float) -> float:
        """The quantity after every attached overlay has scaled it, multiplicatively."""
        scale = 1.0
        for _, decision in self._ask_overlays(ctx):
            if decision.scale is not None:
                scale *= decision.scale
        return qty * scale

    def before_exit(self, ctx: HookContext) -> bool:
        """Whether an attached exit rule requires the open position closed now.

        Consulted after every data event on which the sleeve holds a position, and after
        the author's own handler has run, so an exit construct has the last word over the
        host's own logic without the host being written to expect one.
        """
        return any(decision.exit for decision in self._decisions(EXIT, ctx))

    def hedges(self, ctx: HookContext) -> list[object]:
        """The legs the attached overlays ask for beside this entry, as orders to submit."""
        orders: list[object] = []
        for modifier, decision in self._ask_overlays(ctx):
            orders.extend(self._legs_of(modifier, decision))
        return orders

    def _ask_overlays(self, ctx: HookContext) -> tuple[tuple[object, Decision], ...]:
        """Every attached overlay's answer for this entry, each beside the overlay that gave it.

        `submit_entry` asks once and stashes the answers, so an overlay is consulted once
        per host entry rather than once to scale and again to hedge. An order the author
        built by hand reaches `submit_order` with no stash and is asked for there.
        """
        if self._entry_answers is not None:
            return self._entry_answers
        return tuple(
            (modifier, modifier.evaluate(ctx).check(OVERLAY))
            for modifier in modifiers_for(self.msgbus, self.host_names, OVERLAY)
        )

    def _legs_of(self, modifier: object, decision: Decision) -> list[object]:
        """The orders one overlay's answer asks for: hedges with a quantity, or sized clips.

        An overlay with a budget names clips and the harness sizes them; one without names
        hedges and their quantities. Each vocabulary is refused on the other kind, so a
        quantity never arrives from a construct the hypothesis sized — but the neutral
        answer, empty on both, is the identity for either.
        """
        budget = _budget_of(modifier)
        orders: list[object] = []
        if decision.hedges:
            if budget > 0:
                self._refuse(
                    HEDGE_UNDER_SIZING,
                    decision.hedges[0].instrument,
                    "hedge",
                    why="a sized overlay names clips and the harness sizes them; return "
                    "Decision(clips=(Clip(instrument, side),)) rather than hedges",
                )
            for leg in decision.hedges:
                order = self._hedge_order(leg)
                if order is not None:
                    orders.append(order)
        if decision.clips:
            if budget <= 0:
                self._refuse(
                    CLIP_WITHOUT_BUDGET,
                    decision.clips[0].instrument,
                    decision.clips[0].side,
                    why="an overlay with no `sizing` budget has nothing to size a clip to; "
                    "declare `sizing` on its hypothesis, or return hedges with a quantity",
                )
            for clip in decision.clips:
                order = self._clip_order(clip, budget)
                if order is not None:
                    orders.append(order)
        return orders

    @property
    def host_names(self) -> tuple[str, ...]:
        """The names a modifier may attach to this sleeve by: its id, and its class name.

        The engine rewrites a strategy's `StrategyId` when the trader adopts it unless the
        config pins an `order_id_tag`, so a composed modifier that names the sleeve's class
        finds it whatever tag the trader assigned, and one that names the final id finds it
        when several instances of the same class share a node.
        """
        return (self.id.value, type(self).__name__)

    def _decisions(self, construct: str, ctx: HookContext) -> tuple[Decision, ...]:
        attached = modifiers_for(self.msgbus, self.host_names, construct)
        return tuple(modifier.evaluate(ctx).check(construct) for modifier in attached)

    # --- order capture and construct consultation ---------------------------

    def submit_order(
        self,
        order: object,
        position_id: object = None,
        client_id: object = None,
        params: dict[str, object] | None = None,
    ) -> None:
        """Record the intent, consult the attached filters, submit, then hedge."""
        self._check_hand_built(order)
        kind = self.classify(order)
        if kind == ENTRY and not (self.sized or self._built or self._sizing_call):
            self._check_funded(order)
        ctx = self._context_for(order)
        if kind == ENTRY and not self._hedging and not self.before_entry(ctx):
            return
        self._intents.append(self._intent(order))
        super().submit_order(order, position_id, client_id, params)
        self._track(order)
        if kind == ENTRY and not self._hedging:
            self._place_hedges(ctx)

    def submit_order_list(
        self,
        order_list: object,
        position_id: object = None,
        client_id: object = None,
        params: dict[str, object] | None = None,
    ) -> None:
        """As `submit_order`, classifying and filtering the list by its first order."""
        orders = list(order_list.orders)  # type: ignore[attr-defined]
        self._check_hand_built(orders[0])
        kind = self.classify(orders[0])
        if kind == ENTRY and not (self.sized or self._built or self._sizing_call):
            self._check_funded(orders[0])
        ctx = self._context_for(orders[0])
        if kind == ENTRY and not self._hedging and not self.before_entry(ctx):
            return
        for order in orders:
            self._intents.append(self._intent(order))
        super().submit_order_list(order_list, position_id, client_id, params)
        for order in orders:
            self._track(order)
        if kind == ENTRY and not self._hedging:
            self._place_hedges(ctx)

    def classify(self, order: object) -> str:
        """`entry` or `exit`, by the order's effect on the net position.

        An order that shrinks the absolute net position is an exit; one that opens a
        position, grows it, or flips past flat into a larger opposite one is an entry.
        """
        key = order.instrument_id.value  # type: ignore[attr-defined]
        if not self.sized:
            net = float(self.portfolio.net_position(order.instrument_id))  # type: ignore[attr-defined]
        elif self._is_clip(order):
            net = self._clips_filled(key)
        else:
            net = self._own_filled(key)
        signed = float(order.quantity)  # type: ignore[attr-defined]
        if order.side == OrderSide.SELL:  # type: ignore[attr-defined]
            signed = -signed
        return EXIT_ORDER if abs(net + signed) < abs(net) else ENTRY

    def _intent(self, order: object) -> OrderIntent:
        return OrderIntent(
            ts_event=self._data_time,
            instrument_id=order.instrument_id.value,  # type: ignore[attr-defined]
            side=order_side_to_str(order.side),  # type: ignore[attr-defined]
            qty=float(order.quantity),  # type: ignore[attr-defined]
            order_type=order_type_to_str(order.order_type),  # type: ignore[attr-defined]
            price=_order_price(order),
        )

    def _place_hedges(self, ctx: HookContext) -> None:
        legs = self.hedges(ctx)
        if not legs:
            return
        self._hedging = True
        try:
            for leg in legs:
                self._submit_own(leg)
        finally:
            self._hedging = False

    def _submit_own(self, order: object) -> None:
        """Submit an order the harness built, marked as its own for the sizing rule."""
        self._sizing_call = True
        try:
            self.submit_order(order)
        finally:
            self._sizing_call = False

    def _check_hand_built(self, order: object) -> None:
        if self.sized and not self._sizing_call:
            self._refuse(
                HAND_BUILT_ORDER,
                order.instrument_id.value,  # type: ignore[attr-defined]
                order_side_to_str(order.side),  # type: ignore[attr-defined]
                why="under a sizing rule every order is the harness's; call "
                "submit_entry(instrument_id, side) or submit_exit(instrument_id)",
            )

    def _hedge_order(self, leg: Hedge) -> object | None:
        instrument_id = InstrumentId.from_str(leg.instrument)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            raise ValidationError(
                f"hedge: {leg.instrument} is not in the cache, so the overlay's hedge leg "
                "cannot be placed; add it to the universe or to the instruments file"
            )
        quantity = self._quantise(instrument, abs(leg.qty))
        if quantity is None:
            return None
        side = OrderSide.BUY if leg.qty > 0 else OrderSide.SELL
        price = self._last_print.get(instrument_id.value)
        if not self.sized and price:
            # A leg that opens or grows exposure is funded like an entry, from the same room.
            quantity = self._quantise(
                instrument, self._funded(instrument_id, side, float(quantity), price)
            )
            if quantity is None:
                return None
        order: object = self.order_factory.market(instrument_id, side, quantity)
        return order

    def _clip_order(self, clip: Clip, budget: float) -> object | None:
        """One clip as an order: `FLAT` takes the held clip off, a side opens the budget.

        One clip at a time, in one instrument, on one side: a second instrument while a
        clip is held or in flight is refused, and so is the other side of a held clip —
        `FLAT` first, in the same answer, is how a clip switches. The order is recorded
        as a clip before it is submitted, so the ledger attributes its fills to the
        overlay and never to the host.
        """
        instrument_id = InstrumentId.from_str(clip.instrument)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            raise ValidationError(
                f"clip: {clip.instrument} is not in the cache, so the overlay's clip "
                "cannot be placed; add it to the universe or to the instruments file"
            )
        key = clip.instrument
        intended = self._clips_intended(key)
        if clip.side == FLAT:
            filled = self._clips_filled(key)
            if intended == 0 or filled == 0:
                return None
            side = OrderSide.SELL if filled > 0 else OrderSide.BUY
            quantity = self._quantise(instrument, abs(filled))
        else:
            signed = 1.0 if clip.side == BUY else -1.0
            if intended != 0:
                if (intended > 0) == (signed > 0):
                    return None
                self._refuse(
                    CLIP_DIRECTION,
                    key,
                    clip.side,
                    why=f"a clip of {intended:g} is held in {key}; take it off with "
                    f"Clip({key!r}, 'FLAT') before opening the other side",
                )
            elsewhere = {
                name: qty for name, qty in self._clips().items() if name != key and qty != 0
            }
            if elsewhere:
                (name, qty), *_ = elsewhere.items()
                self._refuse(
                    ONE_CLIP,
                    key,
                    clip.side,
                    why=f"a clip of {qty:g} is held in {name}; take it off with "
                    f"Clip({name!r}, 'FLAT') in the same answer before opening {key}",
                )
            price = self._last_print.get(key)
            if price is None or price <= 0:
                return None
            raw = full_book_quantity(
                budget, price, float(instrument.price_increment), self.cost_rate
            )
            quantity = self._quantise(instrument, raw)
            if quantity is None:
                self._refuse(
                    BUDGET_BELOW_LOT,
                    key,
                    clip.side,
                    why=f"a budget of {budget:g} at {price:g} floors to no whole lot",
                )
            side = OrderSide.BUY if signed > 0 else OrderSide.SELL
        order: object = self.order_factory.market(instrument_id, side, quantity)
        self._clip_orders.setdefault(key, []).append(order)
        return order

    def _is_clip(self, order: object) -> bool:
        key = order.instrument_id.value  # type: ignore[attr-defined]
        return any(order is held for held in self._clip_orders.get(key, ()))

    def _clips_filled(self, key: str) -> float:
        return sum((_signed(order, filled=True) for order in self._clip_orders.get(key, ())), 0.0)

    def _clips_intended(self, key: str) -> float:
        return self._clips_filled(key) + sum(
            (
                _signed(order, filled=False)
                for order in self._clip_orders.get(key, ())
                if not order.is_closed  # type: ignore[attr-defined]
            ),
            0.0,
        )

    def _own_filled(self, key: str) -> float:
        net = float(self.portfolio.net_position(InstrumentId.from_str(key)))
        return net - self._clips_filled(key)

    def _own_intended(self, key: str) -> float:
        return self._own_filled(key) + sum(
            (
                _signed(order, filled=False)
                for order in self._open_markets()
                if order.instrument_id.value == key and not self._is_clip(order)  # type: ignore[attr-defined]
            ),
            0.0,
        )

    def _holdings(self) -> dict[str, float]:
        """The sleeve's own intended position in every name it holds or has an order in."""
        names = {instrument_id.value for instrument_id in self.universe}
        names.update(
            position.instrument_id.value
            for position in self.cache.positions_open(strategy_id=self.id)
        )
        names.update(order.instrument_id.value for order in self._open_markets())  # type: ignore[attr-defined]
        return {name: self._own_intended(name) for name in sorted(names)}

    def _clips(self) -> dict[str, float]:
        return {
            key: self._clips_intended(key)
            for key in sorted(set(self._clip_orders) | {i.value for i in self.universe})
        }

    def _refuse(
        self,
        rule: str,
        instrument_id: str,
        asked: str,
        *,
        why: str,
        held: Mapping[str, float] | None = None,
    ) -> NoReturn:
        raise SizingError(
            Refusal(
                rule=rule,
                ts_event=self._data_time,
                instrument_id=instrument_id,
                asked=asked,
                held=dict(self._holdings() if held is None else held),
                why=why,
            )
        )

    # --- cost-aware helpers: where exposure is enforced ---------------------

    def submit_entry(
        self,
        instrument_id: InstrumentId | str,
        side: OrderSide | str,
        *,
        notional: float | None = None,
        qty: float | None = None,
        price: float | None = None,
    ) -> object | None:
        """Open or grow a position, sized to the risk limits and the cost model.

        The quantity is the smallest of what was asked for and what the limits leave:
        `max_position_pct` of the book in this instrument and `max_leverage` x the book
        gross, the book being the smaller of the capital and `balance`, so a sleeve that
        has lost money cannot borrow to keep its size. Both are reduced by the round-trip
        cost the runner will charge and read with the sleeve's own unfilled market orders
        applied — so a flip is `submit_exit(old)` then
        `submit_entry(new, side)` in one handler at leverage one, the exit in flight
        freeing the room the entry takes, and the venue settles both at one price with the
        exit first. Attached overlays then scale it. Returns the submitted order, or `None`
        when nothing was submitted — no room, no price to size against, or an attached
        filter refused the entry.
        """
        resolved_id = self._instrument_id(instrument_id)
        resolved_side = self._side(side)
        instrument = self.cache.instrument(resolved_id)
        if instrument is None:
            raise ValidationError(
                f"instrument: {resolved_id} is not in the cache; "
                "it is not part of this run's resolved universe"
            )
        if self.sized:
            return self._sized_entry(instrument, resolved_id, resolved_side, notional, qty, price)
        reference = price if price is not None else self.last_price(resolved_id)
        if reference is None or reference <= 0:
            return None
        room = self._headroom(resolved_id, reference)
        if room <= 0:
            return None
        wanted = room if qty is None else abs(qty) * reference
        if notional is not None:
            wanted = min(wanted, abs(notional))
        raw = min(wanted, room) / reference
        ctx = self._context(
            resolved_id, resolved_side, raw, price, "LIMIT" if price is not None else "MARKET"
        )
        self._entry_answers = self._ask_overlays(ctx)
        try:
            quantity = self._quantise(instrument, self.size(ctx, raw))
            if quantity is None:
                return None
            order = self._order(instrument, resolved_side, quantity, price)
            return self._submitted(order)
        finally:
            self._entry_answers = None

    def submit_exit(
        self,
        instrument_id: InstrumentId | str,
        *,
        qty: float | None = None,
        price: float | None = None,
    ) -> object | None:
        """Reduce a position, never past flat. Returns the order, or `None` when flat.

        Exits are neither filtered nor scaled: a construct that shrinks an exit would leave
        exposure behind that nothing in the hypothesis accounts for.
        """
        resolved_id = self._instrument_id(instrument_id)
        instrument = self.cache.instrument(resolved_id)
        if instrument is None:
            raise ValidationError(
                f"instrument: {resolved_id} is not in the cache; "
                "it is not part of this run's resolved universe"
            )
        if self.sized:
            return self._sized_exit(instrument, resolved_id, qty, price)
        net = float(self.portfolio.net_position(resolved_id))
        if net == 0.0:
            return None
        target = abs(net) if qty is None else min(abs(qty), abs(net))
        quantity = self._quantise(instrument, target)
        if quantity is None:
            return None
        side = OrderSide.SELL if net > 0 else OrderSide.BUY
        order = self._order(instrument, side, quantity, price)
        return self._submitted(order)

    def _sized_entry(
        self,
        instrument: object,
        instrument_id: InstrumentId,
        side: OrderSide,
        notional: float | None,
        qty: float | None,
        price: float | None,
    ) -> object | None:
        """The whole budget in one instrument, at market, or a named refusal.

        Already holding the budget on this side is the ordinary bar and answers `None`;
        the other side of a held instrument, or any other instrument held with no exit
        in flight, is refused. The exit an author submitted first in the same handler
        counts: its market order nets the old leg to nothing before this entry sizes, so
        a flip is legal at leverage one. The price is the last print of the finest grain
        loaded — what the venue's book holds — never the host grain's own close.
        """
        key = instrument_id.value
        asked = order_side_to_str(side)
        if notional is not None or qty is not None or price is not None:
            given = ", ".join(
                f"{name}={value!r}"
                for name, value in (("notional", notional), ("qty", qty), ("price", price))
                if value is not None
            )
            self._refuse(
                SIZE_ARGUMENT,
                key,
                asked,
                why=f"{given} was passed, and under a sizing rule the harness sizes every "
                "entry to the budget and places it at market; call submit_entry(id, side)",
            )
        own = self._own_intended(key)
        signed = 1.0 if side == OrderSide.BUY else -1.0
        if own != 0:
            if (own > 0) == (signed > 0):
                return None
            self._refuse(
                ONE_POSITION,
                key,
                asked,
                why=f"{key} is held ({own:g}) on the other side and no exit of it is in "
                f"flight; submit_exit({key!r}) first, in the same handler",
            )
        elsewhere = {name: held for name, held in self._holdings().items() if held != 0}
        if elsewhere:
            (name, held), *_ = elsewhere.items()
            self._refuse(
                ONE_POSITION,
                key,
                asked,
                why=f"{name} is held ({held:g}) and no exit of it is in flight; "
                f"submit_exit({name!r}) first, in the same handler",
            )
        reference = self._last_print.get(key)
        if reference is None or reference <= 0:
            return None
        raw = full_book_quantity(
            self.budget,
            reference,
            float(instrument.price_increment),  # type: ignore[attr-defined]
            self.cost_rate,
        )
        ctx = self._context(instrument_id, side, raw, None, "MARKET")
        self._entry_answers = self._ask_overlays(ctx)
        try:
            for _, decision in self._entry_answers:
                if decision.scale is not None and decision.scale != 1.0:
                    self._refuse(
                        SCALE_UNDER_SIZING,
                        key,
                        asked,
                        why=f"an overlay scaled the entry by {decision.scale:g}, and under a "
                        "sizing rule an entry is the whole budget or nothing",
                    )
            quantity = self._quantise(instrument, raw)
            if quantity is None:
                self._refuse(
                    BUDGET_BELOW_LOT,
                    key,
                    asked,
                    why=f"a budget of {self.budget:g} at {reference:g} floors to no whole lot",
                )
            order = self._order(instrument, side, quantity, None)
            self._sizing_call = True
            try:
                return self._submitted(order)
            finally:
                self._sizing_call = False
        finally:
            self._entry_answers = None

    def _sized_exit(
        self,
        instrument: object,
        instrument_id: InstrumentId,
        qty: float | None,
        price: float | None,
    ) -> object | None:
        """The whole of this sleeve's own position, at market, leaving any clip alone."""
        key = instrument_id.value
        if qty is not None or price is not None:
            given = ", ".join(
                f"{name}={value!r}"
                for name, value in (("qty", qty), ("price", price))
                if value is not None
            )
            self._refuse(
                SIZE_ARGUMENT,
                key,
                "EXIT",
                why=f"{given} was passed, and under a sizing rule an exit is the whole "
                "position at market; call submit_exit(id)",
            )
        if self._own_intended(key) == 0:
            return None
        filled = self._own_filled(key)
        quantity = self._quantise(instrument, abs(filled))
        if quantity is None:
            return None
        side = OrderSide.SELL if filled > 0 else OrderSide.BUY
        order = self._order(instrument, side, quantity, None)
        self._sizing_call = True
        try:
            return self._submitted(order)
        finally:
            self._sizing_call = False

    def _submitted(self, order: object) -> object | None:
        placed = len(self._intents)
        built, self._built = self._built, True
        try:
            self.submit_order(order)
        finally:
            self._built = built
        return order if len(self._intents) > placed else None

    def _headroom(self, instrument_id: InstrumentId, reference: float) -> float:
        """What the limits leave for an entry in this name, on what the book can fund.

        `max_notional` and `gross_limit` are shares of the smaller of the capital and
        `balance`: an account that has lost money cannot borrow
        to keep its size, and one that has made money does not grow past its capital.
        Measured on a sleeve run from 2022 with the room read on the capital alone, it kept
        buying full-size positions with its balance below zero, which no account that does
        not borrow can do.

        Read on what the sleeve will hold once its orders in flight fill, not on what the
        venue holds now: an exit submitted in the same handler counts as gone, so a flip
        fits at leverage one exactly as it does under a sizing rule, where the same rule
        was written first. Read on the venue alone, the old leg still filled the book and
        every flip was refused for a bar.
        """
        intended = self._holdings()
        held = abs(intended.get(instrument_id.value, 0.0)) * reference
        room = min(self.max_notional - held, self.gross_limit - self._gross_intended(intended))
        return room / (1.0 + ROUND_TRIP * self.cost_rate)

    def _gross_intended(self, intended: Mapping[str, float]) -> float:
        """What this sleeve will hold, marked at the last price seen and at cost where none was.

        The fallback is the position's own split-aware cost basis rather than
        `Position.avg_px_open`, which a corporate action leaves quoted in shares the
        position no longer holds and which would understate the exposure by the ratio.
        """
        positions = {
            position.instrument_id.value: position
            for position in self.cache.positions_open(strategy_id=self.id)
        }
        total = 0.0
        for name, quantity in intended.items():
            if quantity == 0.0:
                continue
            price = self._last_price.get(name)
            if price is None:
                position = positions.get(name)
                price = 0.0 if position is None else splits.ledger(splits.moves_of(position)).basis
            total += abs(quantity) * price
        return total

    def _settle(self) -> None:
        """Fold every fill not booked yet into the sleeve's cash, as the runner charges it.

        Each order the sleeve sent is kept with how many of its fills are booked, and
        leaves once the venue has closed it and every fill is in. A quoted spread's series
        is then cut back to each instrument's last quote: every fill before now is booked,
        and a fill still to come is charged at a quote no older than that one.
        """
        waiting: list[list[Any]] = []
        for entry in self._ledger:
            order, booked = entry
            fills = [event for event in order.events if isinstance(event, OrderFilled)]
            for event in fills[booked:]:
                self._cash -= self._paid(event)
            entry[1] = len(fills)
            if not order.is_closed:
                waiting.append(entry)
        self._ledger = waiting
        for times, values in self._quoted.values():
            del times[:-1]
            del values[:-1]

    def _paid(self, event: Any) -> float:
        """What one fill took out of cash: the value it bought, or gave back what it sold
        for, and the commission, slippage and half-spread the runner charges it."""
        instrument = self.cache.instrument(event.instrument_id)
        multiplier = 1.0 if instrument is None else float(instrument.multiplier)
        qty, px = float(event.last_qty), float(event.last_px)
        signed = qty if event.order_side == OrderSide.BUY else -qty
        rate = side_rate(
            float(self._charges.get("commission_bps") or 0.0),
            float(self._charges.get("slippage_bps") or 0.0),
            self._half_spread_at(event.instrument_id.value, int(event.ts_event)),
        )
        return signed * px * multiplier + qty * px * multiplier * rate

    def _half_spread_at(self, key: str, ts_ns: int) -> float:
        """Half the spread one fill pays: the stated width, or the last quote before it."""
        if self._charges.get("spread") != "quotes":
            return fixed_half_spread(self._charges.get("fixed_bps"))
        times, values = self._quoted.get(key, ([], []))
        index = bisect_right(times, ts_ns)
        return 0.0 if index == 0 else values[index - 1]

    def _worth(self, position: Any) -> float:
        """One open position's market value, in the share count its last print was quoted in.

        The venue restates every holder of a split at the first point past its ex-date,
        whichever instrument printed it, so on a pair's ex-date the other leg's handler can
        see the new share count beside a price still quoted in the old one — a one-for-ten
        reverse split read as a ninety per cent loss. So the adjustments later than the
        instrument's last print are taken back out until it prints again. A position the
        sleeve has seen no price for is marked at its own split-aware cost.
        """
        key = position.instrument_id.value
        instrument = self.cache.instrument(position.instrument_id)
        multiplier = 1.0 if instrument is None else float(instrument.multiplier)
        price = self._last_print.get(key)
        if price is None:
            basis = splits.ledger(splits.moves_of(position)).basis
            return float(position.signed_qty) * basis * multiplier
        printed = self._print_ns.get(key, 0)
        unpriced = sum(
            (
                float(event.quantity_change)
                for event in position.adjustments
                if event.quantity_change is not None and int(event.ts_event) > printed
            ),
            0.0,
        )
        return (float(position.signed_qty) - unpriced) * price * multiplier

    def _opening(
        self, instrument_id: InstrumentId, side: OrderSide, quantity: float, price: float
    ) -> tuple[float, float, float]:
        """What an order closes, what it opens or grows, and the room for the second part.

        The closed part frees its own room before the opened part takes any, so a leg that
        crosses zero is funded like an exit followed by an entry. The room is the reserved
        one `submit_entry` sizes to, in notional.
        """
        now = self.held(instrument_id)
        signed = quantity if side == OrderSide.BUY else -quantity
        closing = min(quantity, abs(now)) if now * signed < 0 else 0.0
        room = self._headroom(instrument_id, price)
        room += closing * price / (1.0 + ROUND_TRIP * self.cost_rate)
        return closing, quantity - closing, room

    def _funded(
        self, instrument_id: InstrumentId, side: OrderSide, quantity: float, price: float
    ) -> float:
        """How much of a hedge leg the book can fund: all of what it closes, and of what it
        opens or grows, what the room leaves once the closed part has freed its own."""
        closing, opening, room = self._opening(instrument_id, side, quantity, price)
        if opening <= 0.0:
            return quantity
        return closing + min(opening, max(0.0, room) / price)

    def _check_funded(self, order: Any) -> None:
        """Refuse an entry built by hand that the book cannot fund.

        kanso cuts the orders it builds — `submit_entry` and an overlay's hedge legs — to the
        room the smaller of the capital and `balance` leaves. An order a strategy built
        itself is not rebuilt at another size, so one whose opening part exceeds that room
        (its cost reserve aside) is refused inside the handler that placed it, as a sizing
        rule refuses a hand-built order, and the card is discarded with the refusal rather
        than measured on money the account never had. With no price seen for the name
        there is nothing to show it fits, and it is refused for that.
        """
        key = order.instrument_id.value
        side = order_side_to_str(order.side)
        price = _order_price(order) or self._last_print.get(key)
        if price is None:
            self._refuse(
                UNFUNDED_ORDER,
                key,
                side,
                why="an entry built by hand in a name with no price seen yet cannot be shown "
                "to fit the book; place it with submit_entry, which places nothing until a "
                "price has been seen",
            )
        _, opening, room = self._opening(
            order.instrument_id, order.side, float(order.quantity), price
        )
        limit = max(0.0, room) * (1.0 + ROUND_TRIP * self.cost_rate)
        if opening * price <= limit + 1e-6:
            return
        self._refuse(
            UNFUNDED_ORDER,
            key,
            side,
            why=f"an entry built by hand opens {opening * price:,.2f} of exposure and the book "
            f"can fund {limit:,.2f}: max_position_pct and max_leverage of the smaller of the "
            "capital and the balance. kanso does not rebuild an order it did not build; place "
            "entries with submit_entry, which cuts them to the room, or size them from "
            "self.balance",
        )

    def _order(
        self, instrument: object, side: OrderSide, quantity: Quantity, price: float | None
    ) -> object:
        if price is None:
            return self.order_factory.market(instrument.id, side, quantity)  # type: ignore[attr-defined]
        return self.order_factory.limit(
            instrument.id,  # type: ignore[attr-defined]
            side,
            quantity,
            Price(price, instrument.price_precision),  # type: ignore[attr-defined]
        )

    @staticmethod
    def _quantise(instrument: object, raw: float) -> Quantity | None:
        """Floor a quantity onto the instrument's lot size; `None` when nothing is left."""
        if raw <= 0:
            return None
        lot = instrument.lot_size or instrument.size_increment  # type: ignore[attr-defined]
        step = Decimal(str(lot))
        if step <= 0:
            return None
        units = (Decimal(repr(raw)) / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if units <= 0:
            return None
        return Quantity(float(units), instrument.size_precision)  # type: ignore[attr-defined]

    # --- context -------------------------------------------------------------

    @staticmethod
    def _instrument_id(instrument_id: InstrumentId | str) -> InstrumentId:
        if isinstance(instrument_id, str):
            return InstrumentId.from_str(instrument_id)
        return instrument_id

    @staticmethod
    def _side(side: OrderSide | str) -> OrderSide:
        if isinstance(side, str):
            resolved = {"BUY": OrderSide.BUY, "SELL": OrderSide.SELL}.get(side.upper())
            if resolved is None:
                raise ValidationError(f"side: {side!r} is neither BUY nor SELL")
            return resolved
        return side

    def _context_for(self, order: object) -> HookContext:
        return self._context(
            order.instrument_id,  # type: ignore[attr-defined]
            order.side,  # type: ignore[attr-defined]
            float(order.quantity),  # type: ignore[attr-defined]
            _order_price(order),
            order_type_to_str(order.order_type),  # type: ignore[attr-defined]
        )

    def _context(
        self,
        instrument_id: InstrumentId,
        side: OrderSide,
        qty: float,
        price: float | None,
        order_type: str,
    ) -> HookContext:
        key = instrument_id.value
        return HookContext(
            instrument_id=key,
            ts_event=self._data_time,
            side=order_side_to_str(side),
            qty=qty,
            price=price,
            order_type=order_type,
            position_qty=self.held(instrument_id),
            capital=self.capital,
            last_bar=self._last_bar.get(key),
            last_quote=self._last_quote.get(key),
            last_trade=self._last_trade.get(key),
            host_strategy_id=self.id.value,
            cache=self.cache,
            book=self._book(),
            prices=dict(self._last_print),
            clips=self._clips(),
            balance=self.balance,
        )

    def _track(self, order: object) -> None:
        """Keep every order this sleeve sent for the balance to book its fills from, and a
        market order until the venue closes it."""
        self._ledger.append([order, 0])
        if order.order_type == OrderType.MARKET:  # type: ignore[attr-defined]
            self._sent.append(order)

    def _open_markets(self) -> list[object]:
        """This sleeve's market orders the venue has not closed yet, on either path.

        Read from the orders themselves rather than from the cache's status indexes: a
        market order sits at `SUBMITTED` in a backtest and at `INITIALIZED` in a node,
        where the live risk engine queues it, and the two indexes answer differently for
        the same instant. `Order.is_closed` — filled, cancelled, rejected, denied or
        expired — is the one reading both paths agree on, and it is what keeps the two
        code paths submitting the same orders.
        """
        self._sent = [order for order in self._sent if not order.is_closed]  # type: ignore[attr-defined]
        return self._sent

    def _market_order_in_flight(self, instrument_id: InstrumentId, side: OrderSide) -> bool:
        """Whether a market order on this side has been sent and not yet filled.

        An exit rule is consulted on every data event, and a market order is not filled
        the instant it is submitted — it is in flight to the venue, and in a live node
        that is a network round trip. Without this the rule would place a second exit on
        the next tick, and a third on the one after, until the first filled, and the
        position would flip to the other side. A resting limit order is deliberately not
        counted — a take-profit at an unreachable price must not be able to block a stop.
        """
        return any(
            order.instrument_id == instrument_id and order.side == side  # type: ignore[attr-defined]
            for order in self._open_markets()
        )

    def _consult_exit(self, instrument_id: InstrumentId) -> None:
        if self._exiting or not self.is_running:
            return
        net = self._own_filled(instrument_id.value)  # what is open, not what is in flight
        if net == 0.0:
            return
        side = OrderSide.SELL if net > 0 else OrderSide.BUY
        if self._market_order_in_flight(instrument_id, side):
            return
        ctx = self._context(instrument_id, side, abs(net), None, "MARKET")
        self._exiting = True
        try:
            if self.before_exit(ctx):
                self.submit_exit(instrument_id)
        finally:
            self._exiting = False

    def _consult_overlay(
        self,
        instrument_id: InstrumentId,
        *,
        last_bar: Bar | None = None,
        price: float | None = None,
    ) -> None:
        """Place the hedge legs attached overlays ask for on this data event.

        Consulted once per cohort of the overlay's grain, after every handler of that
        cohort — the host grain's when they match, an extra grain's instead of the host's
        handler — so a clipper has a clock that is not the host's next buy. `scale` is
        ignored: resizing the host is an entry-time question. A market order already in
        flight is left to fill first, or the same clip would stack on every event until
        the first fill landed. A sleeve with no overlay attached pays nothing here: the
        registry is asked before any scan or context is built.
        """
        if self._hedging or not self.is_running:
            return
        clocks = [
            (modifier, clock)
            for modifier in modifiers_for(self.msgbus, self.host_names, OVERLAY)
            if callable(clock := getattr(modifier, "on_data", None))
        ]
        if not clocks or self._any_market_in_flight():
            return
        ctx = self._clock_context(instrument_id, last_bar=last_bar, price=price)
        self._hedging = True
        try:
            for modifier, clock in clocks:
                decision = clock(ctx).check(OVERLAY)
                for order in self._legs_of(modifier, decision):
                    self._submit_own(order)
        finally:
            self._hedging = False

    def _clock_context(
        self,
        instrument_id: InstrumentId,
        *,
        last_bar: Bar | None = None,
        price: float | None = None,
    ) -> HookContext:
        key = instrument_id.value
        bar = last_bar if last_bar is not None else self._last_bar.get(key)
        px = price if price is not None else self._last_print.get(key)
        return HookContext(
            instrument_id=key,
            ts_event=self._data_time,
            side="",
            qty=0.0,
            price=px,
            order_type="",
            position_qty=self.held(instrument_id),
            capital=self.capital,
            last_bar=bar,
            last_quote=self._last_quote.get(key),
            last_trade=self._last_trade.get(key),
            host_strategy_id=self.id.value,
            cache=self.cache,
            book=self._book(),
            prices=dict(self._last_print),
            clips=self._clips(),
            balance=self.balance,
        )

    def _book(self) -> dict[str, float]:
        if self.sized:
            return {
                instrument_id.value: self._own_intended(instrument_id.value)
                for instrument_id in self.universe
            }
        return {
            instrument_id.value: float(self.portfolio.net_position(instrument_id))
            for instrument_id in self.universe
        }

    def _is_extra_bar(self, bar: Bar) -> bool:
        extra = self._cfg.extra_resolutions
        return bool(extra) and _resolution_of_bar(bar) in extra

    def _any_market_in_flight(self) -> bool:
        return bool(self._open_markets())


class KansoModifier(Actor):  # type: ignore[misc]
    """An attached construct: a filter, an overlay or an exit rule on one host sleeve.

    It is an engine actor, registered against its host so the sleeve can consult it
    synchronously inside the call that is about to place an order. `construct` names which
    part of a `Decision` it owns; answering outside that part is refused.
    """

    construct: ClassVar[str] = ""
    config_cls: ClassVar[type[KansoModifierConfig]] = KansoModifierConfig

    def __init__(self, config: KansoModifierConfig | None = None) -> None:
        resolved = self.config_cls() if config is None else config
        if not isinstance(resolved, KansoModifierConfig):
            raise ValidationError(
                f"config: {type(resolved).__name__} configures a modifier but does not subclass "
                "KansoModifierConfig; a modifier is an engine actor, and an actor config and a "
                "strategy config are separate types the engine refuses to swap"
            )
        if self.construct not in MODIFIER_CONSTRUCTS:
            raise ValidationError(
                f"construct: {self.construct!r} is not an attachable construct; "
                f"one of {', '.join(MODIFIER_CONSTRUCTS)} was expected"
            )
        if not resolved.host_strategy_id:
            raise ValidationError(
                "config.host_strategy_id: a modifier must name the sleeve it attaches to"
            )
        super().__init__(resolved)
        self._cfg: KansoModifierConfig = resolved

    @property
    def modifier_config(self) -> KansoModifierConfig:
        """The injected configuration, typed."""
        return self._cfg

    @property
    def host_strategy_id(self) -> str:
        """The `StrategyId` of the sleeve this construct is attached to."""
        return self._cfg.host_strategy_id

    def _start(self) -> None:
        register_modifier(self.msgbus, self._cfg.host_strategy_id, self)
        super()._start()

    def _stop(self) -> None:
        deregister_modifier(self.msgbus, self._cfg.host_strategy_id, self)
        super()._stop()

    def evaluate(self, ctx: HookContext) -> Decision:
        """This construct's decision for the moment described by `ctx`.

        The default is the identity: whatever the host would have done, unchanged.
        An overlay is asked this on a host entry (`scale` and entry-time `hedges`).
        """
        return Decision.neutral(self.construct)

    def on_data(self, ctx: HookContext) -> Decision:
        """This overlay's hedge legs for a data event that is not a host entry.

        The default is silence. Overlays that only scale or hedge at host entry leave
        this alone. `scale` is ignored; only `hedges` are placed. `ctx.qty` is 0,
        `ctx.side` is empty, and `ctx.book` is the host's net per name.
        """
        return Decision.neutral(self.construct)
