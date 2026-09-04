"""The strategy API: the sleeve class, the attached-construct class and their configs.

A hypothesis becomes a `strategy.py` holding a `Config` and a `Strategy`. `KansoStrategy`
is the base that turns one into something the framework can run identically in a backtest,
a sandbox and a live node, and it does four things the author never writes:

**It is the only clock.** `data_time` is the `ts_event` of the last data event handled —
the economic reference time of the information the strategy is acting on. It exists
because the engine's own clock is a different object in each environment: a test clock
advanced by the data stream in a backtest, a wall clock in a live node. Reading the engine
clock therefore makes a strategy behave differently in replay than it did in research,
which is exactly what the parity gate refuses. `data_time` is held by overriding the
engine's data handlers, so it is correct before the author's `on_bar` runs. The engine
delivers by `ts_init` (availability) and never by `ts_event`, so a late-published point can
carry an earlier reference time than its predecessor; `data_time` reports what arrived,
because that is the reference time of the information the strategy actually has.

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

**It consults the constructs attached to it.** A filter, an overlay or an exit rule is a
`KansoModifier` — an engine actor, because a strategy config cannot configure an actor and
an actor config cannot configure a strategy; the two are sibling types and the engine
raises `TypeError` on the wrong pairing. Modifiers register against their host and the
sleeve reads them synchronously through `before_entry`, `size`, `before_exit` and `hedges`.
Each order is classified as an entry or an exit by its effect on the net position: an order
that shrinks the absolute net position is an exit, anything else is an entry.

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

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import ClassVar, Final

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
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from kanso.errors import ValidationError
from kanso.nautilus.hooks import (
    EXIT,
    FILTER,
    MODIFIER_CONSTRUCTS,
    OVERLAY,
    Decision,
    Hedge,
    HookContext,
    deregister_modifier,
    modifiers_for,
    register_modifier,
)
from kanso.schemas.duration import is_duration

__all__ = [
    "BAR",
    "ENTRY",
    "EXIT_ORDER",
    "QUOTE",
    "TRADE",
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
    data_requirements: tuple[str, ...] = (BAR,)
    capital: float = 0.0
    max_position_pct: float = PERCENT
    max_drawdown_pct: float = PERCENT
    max_leverage: float = 1.0
    venue_model: dict[str, object] = {}


class KansoModifierConfig(ActorConfig, frozen=True):
    """What an attached construct is configured with.

    It derives from `ActorConfig` and not from `KansoConfig` because a modifier is an
    engine actor: the engine's two config bases are siblings and it refuses to configure an
    actor with a strategy config.
    """

    host_strategy_id: str = ""
    """The sleeve this construct attaches to: its `StrategyId`, or its class name."""

    hyp_id: str = ""


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
        self._hedging = False
        self._exiting = False

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
        """The most one instrument may hold: `max_position_pct` of capital."""
        return self.capital * self._cfg.max_position_pct / PERCENT

    @property
    def gross_limit(self) -> float:
        """The most the sleeve may hold across every instrument: `max_leverage` x capital."""
        return self.capital * self._cfg.max_leverage

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

    # --- lifecycle -----------------------------------------------------------

    def _start(self) -> None:
        self.subscribe_universe()
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
                elif requirement == QUOTE:
                    self.subscribe_quote_ticks(instrument_id)
                elif requirement == TRADE:
                    self.subscribe_trade_ticks(instrument_id)
                else:
                    self.log.warning(
                        f"data requirement {requirement!r} is a custom type; "
                        "subscribe to it from on_start with its registered class"
                    )

    # --- data handlers: the clock, the last observations, the exit rules -----

    def handle_bar(self, bar: Bar, historical: bool = False) -> None:
        if not historical:
            key = bar.bar_type.instrument_id.value
            self._last_bar[key] = bar
            self._observe(key, int(bar.ts_event), float(bar.close))
        super().handle_bar(bar, historical)
        if not historical:
            self._consult_exit(bar.bar_type.instrument_id)

    def handle_quote_tick(self, tick: QuoteTick, historical: bool = False) -> None:
        if not historical:
            key = tick.instrument_id.value
            self._last_quote[key] = tick
            mid = (float(tick.bid_price) + float(tick.ask_price)) / 2.0
            self._observe(key, int(tick.ts_event), mid)
        super().handle_quote_tick(tick, historical)
        if not historical:
            self._consult_exit(tick.instrument_id)

    def handle_trade_tick(self, tick: TradeTick, historical: bool = False) -> None:
        if not historical:
            key = tick.instrument_id.value
            self._last_trade[key] = tick
            self._observe(key, int(tick.ts_event), float(tick.price))
        super().handle_trade_tick(tick, historical)
        if not historical:
            self._consult_exit(tick.instrument_id)

    def handle_data(self, data: object) -> None:
        instrument_id = getattr(data, "instrument_id", None)
        key = None if instrument_id is None else str(instrument_id)
        self._observe(key, int(getattr(data, "ts_event", self._data_time)), None)
        super().handle_data(data)
        if instrument_id is not None:
            self._consult_exit(instrument_id)

    def _observe(self, key: str | None, ts_event: int, price: float | None) -> None:
        self._data_time = ts_event
        if key is not None and price is not None:
            self._last_price[key] = price

    # --- hooks: what an attached construct changes ---------------------------

    def before_entry(self, ctx: HookContext) -> bool:
        """Whether an entry may be placed. Every attached filter must allow it."""
        return all(decision.allow is not False for decision in self._decisions(FILTER, ctx))

    def size(self, ctx: HookContext, qty: float) -> float:
        """The quantity after every attached overlay has scaled it, multiplicatively."""
        scale = 1.0
        for decision in self._decisions(OVERLAY, ctx):
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
        """The hedge legs the attached overlays ask for, as orders ready to submit."""
        orders: list[object] = []
        for decision in self._decisions(OVERLAY, ctx):
            for leg in decision.hedges or ():
                order = self._hedge_order(leg)
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
        kind = self.classify(order)
        ctx = self._context_for(order)
        if kind == ENTRY and not self._hedging and not self.before_entry(ctx):
            return
        self._intents.append(self._intent(order))
        super().submit_order(order, position_id, client_id, params)
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
        kind = self.classify(orders[0])
        ctx = self._context_for(orders[0])
        if kind == ENTRY and not self._hedging and not self.before_entry(ctx):
            return
        for order in orders:
            self._intents.append(self._intent(order))
        super().submit_order_list(order_list, position_id, client_id, params)
        if kind == ENTRY and not self._hedging:
            self._place_hedges(ctx)

    def classify(self, order: object) -> str:
        """`entry` or `exit`, by the order's effect on the net position.

        An order that shrinks the absolute net position is an exit; one that opens a
        position, grows it, or flips past flat into a larger opposite one is an entry.
        """
        net = float(self.portfolio.net_position(order.instrument_id))  # type: ignore[attr-defined]
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
                self.submit_order(leg)
        finally:
            self._hedging = False

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
        order: object = self.order_factory.market(instrument_id, side, quantity)
        return order

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
        `max_position_pct` of capital in this instrument, `max_leverage` x capital gross,
        both reduced by the round-trip cost the runner will charge. Attached overlays then
        scale it. Returns the submitted order, or `None` when nothing was submitted —
        no room, no price to size against, or an attached filter refused the entry.
        """
        resolved_id = self._instrument_id(instrument_id)
        resolved_side = self._side(side)
        instrument = self.cache.instrument(resolved_id)
        if instrument is None:
            raise ValidationError(
                f"instrument: {resolved_id} is not in the cache; "
                "it is not part of this run's resolved universe"
            )
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
        quantity = self._quantise(instrument, self.size(ctx, raw))
        if quantity is None:
            return None
        order = self._order(instrument, resolved_side, quantity, price)
        return self._submitted(order)

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

    def _submitted(self, order: object) -> object | None:
        placed = len(self._intents)
        self.submit_order(order)
        return order if len(self._intents) > placed else None

    def _headroom(self, instrument_id: InstrumentId, reference: float) -> float:
        held = abs(float(self.portfolio.net_position(instrument_id))) * reference
        room = min(self.max_notional - held, self.gross_limit - self._gross_exposure())
        return room / (1.0 + ROUND_TRIP * self.cost_rate)

    def _gross_exposure(self) -> float:
        total = 0.0
        for position in self.cache.positions_open(strategy_id=self.id):
            price = self._last_price.get(position.instrument_id.value)
            if price is None:
                price = float(position.avg_px_open)
            total += abs(float(position.signed_qty)) * price
        return total

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
            position_qty=float(self.portfolio.net_position(instrument_id)),
            capital=self.capital,
            last_bar=self._last_bar.get(key),
            last_quote=self._last_quote.get(key),
            last_trade=self._last_trade.get(key),
            host_strategy_id=self.id.value,
            cache=self.cache,
        )

    def _market_order_in_flight(self, instrument_id: InstrumentId, side: OrderSide) -> bool:
        """Whether a market order on this side has been sent and not yet filled.

        An exit rule is consulted on every data event, and a market order is not filled
        the instant it is submitted — it is in flight to the venue, and in a live node
        that is a network round trip. Without this the rule would place a second exit on
        the next tick, and a third on the one after, until the first filled, and the
        position would flip to the other side.

        Both engine states count: `orders_inflight` holds an order the venue has not
        acknowledged, `orders_open` one it has. A resting limit order is deliberately not
        counted — a take-profit at an unreachable price must not be able to block a stop.
        """
        where = {"instrument_id": instrument_id, "strategy_id": self.id, "side": side}
        sent = [*self.cache.orders_inflight(**where), *self.cache.orders_open(**where)]
        return any(order.order_type == OrderType.MARKET for order in sent)

    def _consult_exit(self, instrument_id: InstrumentId) -> None:
        if self._exiting or not self.is_running:
            return
        net = float(self.portfolio.net_position(instrument_id))
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
        """
        return Decision.neutral(self.construct)
