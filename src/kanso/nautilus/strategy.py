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
`on_bar` runs — and before `on_order_book_deltas`, which a change to the book stamps with
its own `ts_event`; it used to leave the last print's there, which for a sleeve that holds
only the book is no instant at all. The engine delivers by `ts_init` (availability) and
never by `ts_event`, so
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
knows nothing of a position, a strategy or a book. An entry the author built is refused at
`submit_order` when the book cannot fund it, and `cancel_order`, `cancel_orders` and
`cancel_all_orders` are kanso's own so that an exit owed or a cancel held back is kept.
`modify_order` is not: it is the engine's, and a modify that grows an entry's quantity, or
moves a resting entry to a price at which it opens more, is held neither to the room nor to
what the book funds — the room reads the order at its new size and price from its next read
on. A sizing rule denies a researched strategy `modify_order` (`kanso.criteria.integrity`);
an unsized one may call it (`docs/backlog.md` row 123).

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

**It shows a `depth` sleeve the book its account would see.** Under the hypothesis's `depth`
every change of a level-two book still reaches both venues, but the author is handed only
the top levels as they stood on a grid, at the first point after each grid instant, and
level one on every instant the top moved, from a copy of the book the harness keeps itself
by the engine's level-two rules (`kanso.nautilus.facts` checks them). Without the key the
author is handed every change, as the venue is.

Engine facts this module relies on (nautilus_trader 1.231.0): `Strategy` and `Actor`
methods are `cpdef`, so a Python subclass's `handle_bar`, `handle_quote_tick`,
`handle_trade_tick`, `handle_order_book_deltas`, `handle_data`, `submit_order`,
`submit_order_list`, `cancel_order`, `cancel_orders`, `cancel_all_orders`, `_start` and
`_stop` all take precedence when the engine calls them; `Trader` starts actors before
strategies, so a modifier is registered before its host runs; `close_position` and
`close_all_positions` route through `submit_order`; `portfolio.net_position(instrument_id)`
returns a signed `Decimal`; an order whose cancel was sent is not `is_closed` and can
still fill until the cancel lands — under a latency model after the next point is matched,
with none before it (`kanso.nautilus.facts` measures both on the backtest engine, and the
replay tests hold the node to the same fills); `cancel_all_orders` marks every order open at
the venue `PENDING_CANCEL` at once and cancels one in flight to it as well
(`kanso.nautilus.facts` measures it on the backtest engine), and skips one still
`INITIALIZED` (read in `trading/strategy.pyx`), which on the backtest engine an order handed
to `submit_order` never is when the call returns; a cancel sent for an
order a node has not yet handed to the venue reaches the venue before the order, where it is
lost and the order rests (read in `live/risk_engine.py`, `execution/manager.pyx` and
`trading/strategy.pyx`), which is why kanso holds such a cancel back until the node has
handed the order over (`KansoStrategy._hold_cancel`); `Strategy.cancel_orders` sends a
`BatchCancelOrders` and refuses one whose orders are in more than one instrument, or that
holds an emulated order after its first, after marking the orders before it
`PENDING_CANCEL` (read in `trading/strategy.pyx`); the order emulator takes an emulated
order out and marks it pending cancel locally before `cancel_order` returns (read in
`execution/emulator.pyx`, measured on both paths by the exit and replay tests);
`Order.events` hands back a new list of every event the order holds, where `event_count`
and `last_event` read its length and its last element without one, and an order's events
grow only at its end, each handed to the strategy's `handle_event` once the order has taken
it (`kanso.nautilus.facts` measures it on the backtest engine);
`StrategyConfig` and `ActorConfig` are frozen msgspec structs
whose subclasses inherit the freeze, and the engine defines no `config_cls` — `config_cls`
here is kanso's own attribute, honoured by kanso's loader alone.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from math import fsum, prod
from typing import Any, ClassVar, Final, NoReturn

from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig, StrategyConfig
from nautilus_trader.model.data import (
    Bar,
    BarSpecification,
    BarType,
    BookOrder,
    DataType,
    OrderBookDelta,
    OrderBookDeltas,
    QuoteTick,
    TradeTick,
)
from nautilus_trader.model.enums import (
    AggregationSource,
    BarAggregation,
    BookAction,
    BookType,
    LiquiditySide,
    OrderSide,
    OrderStatus,
    OrderType,
    PriceType,
    RecordFlag,
    order_side_to_str,
    order_type_to_str,
)
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from kanso.criteria.run import day_of
from kanso.errors import ValidationError
from kanso.nautilus import actions, splits
from kanso.nautilus.costs import (
    BookPolicy,
    carry,
    fill_cost,
    fixed_half_spread,
    funding_payment,
    month_turned,
    quote_half_spread,
    reset,
)
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
    "BOOK",
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
BOOK: Final = "book"

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
    data_by_instrument: tuple[tuple[str, tuple[str, ...]], ...] = ()
    session_scope: tuple[str, str, tuple[str, ...]] | None = None
    capital: float = 0.0
    max_position_pct: float = PERCENT
    max_drawdown_pct: float = PERCENT
    max_leverage: float = 1.0
    venue_model: dict[str, object] = {}
    sizing_budget: float = 0.0
    """The budget every order is sized to when the hypothesis declares `sizing`; zero
    is free sizing, where the author chooses a size within the risk limits."""
    books_funding: bool = False
    """Whether the balance books each funding settlement it is handed, as the runner's
    extraction does. The runner and a stage node on a simulated venue set it, because the
    simulated account settles no funding; an account a broker keeps settles its own, and a
    sleeve on one leaves it off. Either way the strategy's `on_data` is handed the point."""
    depth: tuple[int, int] | None = None
    """What the author is shown of a level-two book under the hypothesis's `depth`: the grid
    in nanoseconds and the levels of each side. None hands the author every change, as the
    venue is handed them."""
    fixed_params: tuple[str, ...] = ()
    """The numeric fields of an author's own configuration that are not knobs — a selector
    among rules, a clock constant, a size the hypothesis sets — so the `param_plateau` gate
    leaves them where they are. Each must name a numeric field of the subclass."""

    def __post_init__(self) -> None:
        own = {
            name
            for name in type(self).__struct_fields__
            if name not in KansoConfig.__struct_fields__ and _is_number(getattr(self, name, None))
        }
        unknown = [name for name in self.fixed_params if name not in own]
        if unknown:
            raise ValueError(
                f"fixed_params: {', '.join(unknown)} is not a numeric field of "
                f"{type(self).__name__}, so there is nothing to hold fixed"
            )


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
    subtracted, and so are the fields the author named in `fixed_params`: a selector among
    rules or a clock constant is a number the strategy reads, not a knob it was tuned on,
    and moving one tests a different strategy rather than the same one nearby. Booleans are
    not parameters.
    """
    injected = set(KansoConfig.__struct_fields__)
    fixed = set(getattr(config, "fixed_params", ()))
    names: list[str] = []
    for name in type(config).__struct_fields__:
        if name in injected or name in fixed:
            continue
        if not _is_number(getattr(config, name, None)):
            continue
        names.append(name)
    return tuple(names)


def _is_number(value: object) -> bool:
    """A parameter a perturbation can move: an int or a float, and not a bool."""
    return isinstance(value, int | float) and not isinstance(value, bool)


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


def _maker_bps(charges: Mapping[str, Any]) -> float | None:
    """What a maker's fill pays of its notional, or `None` when the venue model states no
    such rate — kept apart from zero, which is a maker paying nothing."""
    stated = charges.get("maker_bps")
    return None if stated is None else float(stated)


def _maker_per_share(charges: Mapping[str, Any]) -> float | None:
    """What a maker's fill pays on each share, or `None` when the venue model states no such
    charge — kept apart from zero, which states a maker's schedule that charges nothing."""
    stated = charges.get("maker_per_share")
    return None if stated is None else float(stated)


def _leaves(orders: Sequence[Any]) -> Decimal:
    """What a set of orders can still fill between them."""
    return sum((order.leaves_qty.as_decimal() for order in orders), Decimal(0))


_SETTLED = frozenset((OrderStatus.ACCEPTED, OrderStatus.TRIGGERED, OrderStatus.PARTIALLY_FILLED))
"""The statuses of an order the venue holds open with nothing of the sleeve's to answer."""


def _held_open(order: Any) -> bool:
    """Whether the venue holds an order open and settled: it has taken the order, not closed
    it, and has no cancel or modify of it still to answer.

    In nautilus_trader 1.231.0 `Order.is_open` does not say so. It counts `PENDING_UPDATE`
    and `PENDING_CANCEL`, which a modify or a cancel applies to an order the venue has not
    taken yet (from `SUBMITTED` on the backtest engine), and it is false for an order with
    an emulation trigger (`Order.is_open_c` in `model/orders/base.pyx`). So kanso reads
    whether the venue has taken it from `venue_order_id`, which `Order.apply` sets only from
    the venue's own acceptance or a fill (an update only replaces one already set), and never
    on submission, a modify or a cancel, on both paths; and whether it is settled from its
    status, `ACCEPTED`, `TRIGGERED` or `PARTIALLY_FILLED`. An order the venue took whose
    modify is still in flight (`PENDING_UPDATE`) is not held open: on a node
    `Strategy.modify_order` goes through the live risk engine's queue and `cancel_order`
    straight to the live execution engine's (`trading/strategy.pyx`), so a cancel sent
    behind the modify would overtake it, where the backtest engine lands the modify first
    and fills it if it is marketable; an exit at market cancels it later instead, with no
    latency stated as the venue answers the modify, and under one on the next point, before
    the sleeve's handler, whether or not the modify has been answered by then — the modify
    was stamped first, so the cancel still lands behind it (`_modifying`,
    `KansoStrategy._cancel_behind_modify`, `KansoStrategy._send_behind_modify`,
    `KansoStrategy._answered`). Measured on both paths by
    the exit and replay tests, an order modified in flight, cancelled or not, and an order
    the venue holds modified in the handler that exits at market, among them.
    """
    return bool(order.venue_order_id is not None and order.status in _SETTLED)


def _modifying(order: Any) -> bool:
    """Whether the venue holds an order whose modify it has not answered yet: it has taken
    the order (`venue_order_id`, as `_held_open` reads it) and the order is
    `PENDING_UPDATE`."""
    return bool(order.venue_order_id is not None and order.status == OrderStatus.PENDING_UPDATE)


def _order_price(order: object) -> float | None:
    price = getattr(order, "price", None)
    return None if price is None else float(price)


class _Ladder:
    """One side of a level-two book by price level, the best price last.

    The harness keeps its own copy of each book a `depth` sleeve holds, because the cache's
    is already past the change being handled when a handler runs, and because the engine's
    `OrderBook.bids()` builds every level it holds — which, on a book loaded to its top few
    levels, includes every price a move left behind — where this hands the top few alone.
    A change follows the engine's L2 rules (nautilus_trader 1.231.0, `model/book.pyx`): an
    add or an update sets the level's size whether or not it is held, a delete of a price
    not held does nothing, and a clear empties both sides.
    """

    __slots__ = ("keys", "levels", "sign")

    def __init__(self, sign: int) -> None:
        self.sign = sign
        self.keys: list[int] = []
        self.levels: dict[int, tuple[Price, Quantity]] = {}

    def put(self, price: Price, size: Quantity) -> None:
        key = price.raw * self.sign
        if key not in self.levels:
            insort(self.keys, key)
        self.levels[key] = (price, size)

    def drop(self, price: Price) -> None:
        key = price.raw * self.sign
        if self.levels.pop(key, None) is not None:
            del self.keys[bisect_left(self.keys, key)]

    def clear(self) -> None:
        self.keys.clear()
        self.levels.clear()

    def top(self, count: int) -> dict[Price, Quantity]:
        """The best `count` levels, best first."""
        return dict(self.levels[key] for key in reversed(self.keys[-count:]))

    def best(self) -> tuple[Price, Quantity] | None:
        return self.levels[self.keys[-1]] if self.keys else None


def _apply(book: tuple[_Ladder, _Ladder], deltas: Any) -> None:
    """Apply one batch of changes to a harness book of (bids, asks)."""
    bids, asks = book
    for delta in deltas.deltas:
        action = delta.action
        if action == BookAction.CLEAR:
            bids.clear()
            asks.clear()
            continue
        order = delta.order
        side = bids if order.side == OrderSide.BUY else asks
        if action == BookAction.DELETE:
            side.drop(order.price)
        else:
            side.put(order.price, order.size)


def _changes(
    instrument_id: InstrumentId,
    shown: dict[Price, Quantity],
    now: dict[Price, Quantity],
    side: OrderSide,
    ts_ns: int,
) -> list[OrderBookDelta]:
    """What turns the levels an author was shown of one side into the levels it has now."""
    made = [
        OrderBookDelta(
            instrument_id,
            BookAction.DELETE,
            BookOrder(side, price, Quantity(0, size.precision), 0),
            0,
            0,
            ts_ns,
            ts_ns,
        )
        for price, size in shown.items()
        if price not in now
    ]
    for price, size in now.items():
        was = shown.get(price)
        if was is None or was != size:
            action = BookAction.ADD if was is None else BookAction.UPDATE
            made.append(
                OrderBookDelta(
                    instrument_id, action, BookOrder(side, price, size, 0), 0, 0, ts_ns, ts_ns
                )
            )
    return made


@dataclass(eq=False, slots=True)
class _Tracked:
    """One order the sleeve sent, as far as its balance has read it (`KansoStrategy._settle`)."""

    order: Any
    read: int = 0
    """How many of its events have been folded in."""
    unread: list[Any] | None = None
    """The events it has gained since, as `handle_event` was handed them, while they are known
    to be exactly those (`KansoStrategy._arrived`); `None` when they are not."""
    booked: set[object] = field(default_factory=set)
    """The ids of its fills booked so far."""


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
        self._markers_bound = False
        self._trading_from_ns = 0
        self._delivered_ns = 0
        self._fed_from_ns = 0
        self._flushing = False
        self._pending: deque[object] = deque()
        self._overlay_due: tuple[InstrumentId, Bar | None, float | None] | None = None
        self._entry_answers: tuple[tuple[object, Decision], ...] | None = None
        self._clip_orders: dict[str, list[object]] = {}
        self._sizing_call = False
        self._built = False
        self._owed: dict[tuple[str, OrderSide], tuple[float | None, float | None, bool]] = {}
        self._cancels: dict[object, bool] = {}
        self._unsent: dict[object, tuple[Any, Any, dict[str, object] | None]] = {}
        self._behind_modify: dict[object, Any] = {}
        self._awaiting: set[object] = set()
        self._charges = _charges(resolved)
        self._cash = resolved.capital
        self._ledger: dict[object, _Tracked] = {}
        self._print_ns: dict[str, int] = {}
        self._scope_days: dict[str, set[date]] = {}
        self._scope_cls: type | None = None
        self._price_ns: dict[str, int] = {}
        self._restated: dict[str, list[splits.Split]] = {}
        self._quoted: dict[str, tuple[list[int], list[float]]] = {}
        self._policy: BookPolicy | None = None
        self._anchor_ns = 0
        self._period_ns = 1
        self._cushion = 0.0
        self._settled_ns: int | None = None
        self._carried_from_ns = 0
        self._period_index: int | None = None
        self._period_last_ns = 0
        self._mark_at: dict[str, tuple[int, float]] = {}
        self._settling: list[tuple[int, str, float, float]] = []
        self._filled: list[tuple[int, str, float]] = []
        self._depth_books: dict[str, tuple[_Ladder, _Ladder]] = {}
        self._depth_shown: dict[str, tuple[dict[Price, Quantity], dict[Price, Quantity]]] = {}
        self._depth_seen_ns = -1
        self._top_shown: dict[str, tuple[Price, Quantity, Price, Quantity]] = {}

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
        """The starting balance: the most the room is ever read on (see `balance`)."""
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
        not opened at exactly the limit and then pushed through it by its own costs. An order
        does not know whether it will rest, so where the model states a maker's rate the
        larger of the two is the one reserved; a rebate reserves nothing of its own. Half of
        the sell-side fee in basis points is reserved on top, because a round trip pays it
        once and the reserve is struck per side.
        """
        costs = self._cfg.venue_model.get("costs")
        if not isinstance(costs, Mapping):
            return 0.0
        bps = float(costs.get("commission_bps") or 0.0) + float(costs.get("slippage_bps") or 0.0)
        if costs.get("spread") == "fixed_bps":
            bps += float(costs.get("fixed_bps") or 0.0)
        maker = _maker_bps(costs)
        if maker is not None:
            bps = max(bps, maker)
        bps += float(costs.get("sell_fee_bps") or 0.0) / 2.0
        return bps / BASIS_POINT

    def cost_rate_at(self, price: float, multiplier: float = 1.0) -> float:
        """`cost_rate` at a price: the per-share commission, where the model states one, and
        half the per-share sell fee are fractions of notional only once the price is known,
        and a dearer share pays less. An order does not know whether it will rest, so where
        the model states a maker's charge per share the larger of it and the commission is
        the one reserved, as the larger rate is in `cost_rate`; a rebate reserves nothing of
        its own. On a multiplied instrument per share means per contract, and one contract's
        notional is `price x multiplier`, so the fraction is the charge over that."""
        per_share = float(self._charges.get("commission_per_share") or 0.0)
        maker = _maker_per_share(self._charges)
        if maker is not None:
            per_share = max(per_share, maker)
        per_share += float(self._charges.get("sell_fee_per_share") or 0.0) / 2.0
        if per_share <= 0.0 or price <= 0.0:
            return self.cost_rate
        return self.cost_rate + per_share / (price * multiplier)

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

    def _warming(self) -> bool:
        """Whether the point being handled precedes the window this run is measured on.

        The runner's `warm` sets the instant the window opens; the handlers record the
        availability instant of every point they are handed. While the latter is short of
        the former the strategy is being fed its prefix: it sees everything, and places
        nothing. Keyed on `ts_init` and not on `data_time`, because the window is an
        availability span and a point of it may carry a reference time before midnight.
        """
        return self._delivered_ns < self._trading_from_ns

    def _undelivered(self, ts_init: int) -> bool:
        """Whether a point of a shared feed precedes the span this strategy is delivered.

        The node's `deliver_from` sets that instant; every handler returns on such a point
        before recording or dispatching anything, so the strategy's state is what its own
        span alone would have built. A run over the request's own span sets nothing here.
        """
        return int(ts_init) < self._fed_from_ns

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
        """This sleeve's own signed position in an instrument, its market orders in flight
        applied.

        The reader a strategy sizes and flips by, on both paths: the venue's net position
        less what the attached overlays hold as clips in the same name, plus what the
        sleeve's own market orders not yet filled will add. An exit at market submitted in
        this handler already reads as gone, which is what lets the entry that follows it
        size to the room the exit frees. A limit or stop order is not applied, whether it
        rests at the venue or is still in flight to it: a sleeve whose exit rests at the ask
        reads the whole position until the exit fills. `submit_exit` is what counts the
        exits still working, so an exit sized from this reader never closes more than they
        leave.
        """
        return self._own_intended(str(instrument_id))

    @property
    def balance(self) -> float:
        """What this sleeve's account is worth now: the runner's equity, kept as it runs.

        The capital, less what every fill paid for what it bought and the commission,
        slippage and half-spread the runner charges it, plus every open position at its last
        print. It is the runner's own arithmetic (`kanso.nautilus.costs`) on the sleeve's
        own fills, so at every period end it is the equity the card records, and between
        period ends it moves with every fill and every print. It is kept from the fills,
        from the share counts the venue restated at a split and from the cash the split paid
        in lieu of the fraction it left (`_on_restated`), never from the engine's account,
        whose balance a corporate action leaves quoted in shares no position holds.
        Two differences are deliberate: a price printed before a split the venue has since
        applied is restated by the split's ratio, where the card marks it as printed until
        the name prints again; and a position the sleeve has seen no price for is marked at
        its own split-aware cost, where the card marks it at the last price its stream holds.

        Entries and an overlay's hedge legs are cut to what the smaller of this and the
        capital can fund, and a strategy may size from it.

        A read costs what the sleeve's orders gained since the last one, not what they hold:
        each event of each order is folded in once (`_settle`), so a sleeve may read it on
        every bar while it moves a resting order on every bar.

        Under a `book` policy it is the book the policy leaves. The runner settles each
        period at its last point — the carry on what the book held above its equity, then
        the reset's transfer at the turn of a month — and the harness settles the same
        period from the same functions when the first point of the next arrives, before
        anything else is done with it (`_turn`). So a balance read at a period's last point
        is the equity struck there before that end's carry and transfer, and one read at
        any later point has them.

        With `books_funding` it is also net of every funding settlement it has been handed
        (`_fund`), booked when the point is delivered, before `on_data` sees it, on what the
        runner counts as held there: every fill stamped before the instant and none stamped
        at it, so an order placed in answer to the settlement changes nothing it settled.
        One thing the runner uses at that instant can only be known after the point: a print
        of the instant delivered after it, which moves the mark. The first point of a later
        instant settles the difference before anything else is done with it, so from then on
        the two agree.
        """
        if self.cache is None:  # not registered with an engine: nothing booked, nothing held
            return self._cash
        self._settle()
        worth = sum(
            (self._worth(position) for position in self.cache.positions_open(strategy_id=self.id)),
            0.0,
        )
        return self._cash + worth

    # --- the book policy: the runner's period ends, mirrored -------------------

    def _turn(self, ts_ns: int) -> None:
        """Settle the period that ended, when a point of a later one is delivered.

        Called by every handler with the availability instant of the point it was handed,
        before that point moves a price, is buffered or reaches an author: at that moment
        the harness's marks are still the last period's, and the only fills in the cache
        after its end are the ones the venue just matched against this point. Periods are
        cut as the runner cuts them — from `_anchor_ns`, `_period_ns` long, ending at the
        last point inside — and nothing turns over a warmup prefix, which the runner
        measures in no period. A funding settlement booked at an earlier instant is settled
        first (`_refund`), so the period closes on the book the runner closes it on.
        """
        if self._settling and ts_ns > self._settling[0][0]:
            self._refund()
        if self._filled:
            self._filled = [filled for filled in self._filled if filled[0] >= ts_ns]
        if self._policy is None or ts_ns < self._anchor_ns or ts_ns < self._trading_from_ns:
            return
        index = (ts_ns - self._anchor_ns) // self._period_ns
        if self._period_index is not None and index != self._period_index:
            self._close_period(self._policy, self._period_last_ns)
        self._period_index = index
        self._period_last_ns = ts_ns

    def _on_restated(self, restated: Any) -> None:
        """Take in a split the venue has just applied, as `kanso.nautilus.actions` announces it.

        The split is kept for restating prices printed before it, and what it paid in lieu
        of the fraction it left each of this sleeve's positions is folded into cash, as the
        runner's extraction books it. The venue announces each split once, while it applies
        it and before the point that carried the market past it reaches any sleeve, so the
        payment is in cash before this sleeve can act on that point. Only a split that has
        happened is ever held here: a schedule would name the certification window's too.
        """
        self._restated.setdefault(restated.instrument_id, []).append(restated.split)
        for event in restated.adjustments:
            if event.strategy_id == self.id and event.pnl_change is not None:
                self._cash += event.pnl_change.as_double()

    def _close_period(self, policy: BookPolicy, end: int) -> None:
        """Charge the carry and move the reset's transfer, as the runner does at `end`."""
        value, gross = self._book_at(end)
        previous = self._settled_ns
        since = self._carried_from_ns
        charged = carry(
            gross,
            value,
            policy.financing_rate_bps,
            end - (since if previous is None else max(previous, since)),
        )
        self._cash -= charged
        if policy.resets and month_turned(previous, end):
            moved, self._cushion = reset(value - charged, self.capital, self._cushion)
            self._cash += moved
        self._settled_ns = end

    def _fund(self, data: object) -> None:
        """Book one funding settlement into cash as the runner books it (`_equity`).

        The rate on what this sleeve held before the instant — every fill the venue matched
        at it so far is booked and taken back out (`_since`), as the runner leaves it out —
        marked at the last print at or before it, the greatest of an instant's, as the
        runner picks, and times the multiplier. A print of the instant that follows the point
        is settled by `_refund`.
        """
        from kanso.data.types import Funding

        if not isinstance(data, Funding):
            return
        key = data.instrument_id.value
        ts = int(data.ts_init)
        rate = float(data.rate)
        self._settle()
        paid = self._funding_due(key, rate, self._holding(key, self._since(ts)))
        self._cash -= paid
        self._settling.append((ts, key, rate, paid))

    def _refund(self) -> None:
        """Settle the instant's funding again on everything the instant held, now it is over.

        Called by the first point of a later instant before it moves a price: the holdings
        are taken back to what was held before the settlement instant — every fill stamped
        at it or after it is taken out, as `_book_at` takes out the fills after a period's
        end — and each payment is struck again at the instant's final mark and the difference
        booked, so what was booked is what the runner books.
        """
        instant = self._settling[0][0]
        later = [
            (event.instrument_id.value, self._signed(event))
            for event in self._settle(until_ns=instant - 1)
        ]
        taken = [*later, *self._since(instant)]
        for _ts, key, rate, booked in self._settling:
            self._cash -= self._funding_due(key, rate, self._holding(key, taken)) - booked
        self._settling.clear()

    def _since(self, instant: int) -> list[tuple[str, float]]:
        """The fills this sleeve has booked that the venue stamped at or after `instant`, as
        `(instrument, signed quantity)`: what a settlement there does not see. Kept from the
        booking (`_settle`) until a later instant is delivered, because a fill of the
        instant may have been booked — by a read of the balance — before its settlement
        point arrived."""
        return [(key, signed) for ts, key, signed in self._filled if ts >= instant]

    def _funding_due(self, key: str, rate: float, qty: float) -> float:
        """What a settlement at `rate` takes from `qty` held of `key`, at its last print."""
        mark = self._mark_at.get(key)
        return funding_payment(
            qty, 0.0 if mark is None else mark[1], self._multiplier_of(key), rate
        )

    def _holding(self, key: str, taken: Sequence[tuple[str, float]] = ()) -> float:
        """This sleeve's signed quantity of `key`, with the fills in `taken` —
        `(instrument, signed quantity)` — taken back out."""
        qty = fsum(
            float(position.signed_qty)
            for position in self.cache.positions_open(strategy_id=self.id)
            if position.instrument_id.value == key
        )
        for name, signed in taken:
            if name == key:
                qty -= signed
        return qty

    @staticmethod
    def _signed(event: Any) -> float:
        """A fill's quantity, negative for a sale."""
        filled = float(event.last_qty)
        return filled if event.order_side == OrderSide.BUY else -filled

    def _book_at(self, end: int) -> tuple[float, float]:
        """The book's equity and gross at `end`, as the runner strikes them.

        Every fill up to `end` is booked; a fill after it — matched against the point that
        is turning the period — is left for its own quote and taken back out of the
        holdings, so a position it opened is not counted and one it closed still is.
        """
        later = self._settle(until_ns=end)
        worth: dict[str, float] = {}
        for position in self.cache.positions_open(strategy_id=self.id):
            key = position.instrument_id.value
            worth[key] = worth.get(key, 0.0) + self._worth(position)
        for event in later:
            key = event.instrument_id.value
            multiplier = self._multiplier_of(event.instrument_id)
            qty = float(event.last_qty)
            signed = qty if event.order_side == OrderSide.BUY else -qty
            price = self._print_now(key)
            mark = float(event.last_px) if price is None else price
            worth[key] = worth.get(key, 0.0) - signed * mark * multiplier
        worths = [worth[key] for key in sorted(worth)]
        return self._cash + fsum(worths), fsum(abs(value) for value in worths)

    # --- lifecycle -----------------------------------------------------------

    def _start(self) -> None:
        self.subscribe_universe()
        self._ledger.update(
            (order.client_order_id, _Tracked(order))
            for order in self.cache.orders(strategy_id=self.id)
        )
        self.msgbus.subscribe(topic=actions.TOPIC, handler=self._on_restated)
        super()._start()

    def subscribe_universe(self) -> None:
        """Subscribe every instrument of the universe to every data requirement.

        Called before `on_start`, so an author's `on_start` need not call anything. `bar`,
        `quote`, `trade` and `book` are subscribed per instrument — the book as level-two
        deltas, which the venue keeps a book from and the author need not handle. A
        requirement naming a registered
        custom type is subscribed once, by its class, and its points — every instrument's
        and the market-wide ones the runner loaded — reach the author's `on_data` as that
        type, at the instant each became public: a `corporate_action` arrives as a
        `CorporateAction`, with `kind`, `ratio`, `cash`, `currency` and `ex_date_ns`, and a
        `funding` as a `Funding`, with the realised `rate` of the period that settled — which,
        under `books_funding`, the balance has already booked when `on_data` sees it. The
        harness subscribes because the author cannot: a researched `strategy.py` may not
        import `kanso.data`, and the class is the one thing a subscription needs.
        """
        from nautilus_trader.model.identifiers import ClientId

        from kanso.data.types import resolve_type
        from kanso.nautilus.backtest import CLIENT_ID

        own = dict(self._cfg.data_by_instrument)
        for instrument_id in self.universe:
            for requirement in own.get(str(instrument_id), self._cfg.data_requirements):
                if requirement == BAR:
                    self.subscribe_bars(_bar_type(instrument_id, self._cfg.resolution))
                    for extra in self._cfg.extra_resolutions:
                        if extra != self._cfg.resolution:
                            self.subscribe_bars(_bar_type(instrument_id, extra))
                elif requirement == QUOTE:
                    self.subscribe_quote_ticks(instrument_id)
                elif requirement == TRADE:
                    self.subscribe_trade_ticks(instrument_id)
                elif requirement == BOOK:
                    self.subscribe_order_book_deltas(instrument_id, book_type=BookType.L2_MBP)
        for requirement in dict.fromkeys(self._cfg.data_requirements):
            if requirement not in (BAR, QUOTE, TRADE, BOOK):
                custom = DataType(resolve_type(requirement))
                self.subscribe_data(custom, client_id=ClientId(CLIENT_ID))
        if self._hold_until_cross_section:
            self._bind_markers()

    def _bind_markers(self) -> None:
        """Subscribe the cross-section flush markers, once, whenever a feed first has them.

        A sleeve held for markers subscribes them when it starts; one the runner arms for a
        later chunk of the window, after it started unmarked, subscribes them then, or every
        point that chunk holds back would wait for a flush that never reaches it.
        """
        from nautilus_trader.model.identifiers import ClientId

        from kanso.nautilus.backtest import CLIENT_ID

        if not self._markers_bound:
            self._markers_bound = True
            self.subscribe_data(MARKER_TYPE, client_id=ClientId(CLIENT_ID))

    # --- data handlers: the clock, the last observations, the exit rules -----

    def _out_of_scope(self, key: str, ts_init: int) -> bool:
        """Whether a market point of `key` falls on a session its scope does not admit.

        Under a `session_scope`, a name outside `always` is admitted on a session only by a
        point of the scope series carrying the flag above zero, delivered before the
        session's market points; `_note_scope` records those. The runner loads nothing
        outside the scope, so in a backtest this drops nothing; a node delivers every
        subscription, so here it drops what the backtest never saw, and the two paths agree.
        """
        scope = self._cfg.session_scope
        if scope is None or key in scope[2]:
            return False
        return day_of(int(ts_init)) not in self._scope_days.get(key, ())

    def _note_scope(self, data: object) -> None:
        """Record a scope point: the session it stamps is admitted for its instrument."""
        scope = self._cfg.session_scope
        if scope is None:
            return
        if self._scope_cls is None:
            from kanso.data.types import resolve_type

            self._scope_cls = resolve_type(scope[0])
        if not isinstance(data, self._scope_cls):
            return
        instrument_id = getattr(data, "instrument_id", None)
        flag = getattr(data, scope[1], None)
        if instrument_id is None or flag is None or float(flag) <= 0:
            return
        self._scope_days.setdefault(str(instrument_id), set()).add(day_of(int(data.ts_init)))  # type: ignore[attr-defined]

    def handle_bar(self, bar: Bar, historical: bool = False) -> None:
        if historical:
            super().handle_bar(bar, historical)
            return
        if self._undelivered(bar.ts_init) or self._out_of_scope(
            bar.bar_type.instrument_id.value, bar.ts_init
        ):
            return
        self._show_book(int(bar.ts_init))
        self._turn(int(bar.ts_init))
        self._delivered_ns = int(bar.ts_init)
        key = bar.bar_type.instrument_id.value
        self._printed(key, float(bar.close), int(bar.ts_event), int(bar.ts_init))
        if not self._is_extra_bar(bar):
            self._last_bar[key] = bar
            self._observe_price(key, float(bar.close), int(bar.ts_event))
        if self._held():
            self._pending.append(bar)
            return
        self._dispatch_bar(bar)
        self._consult_due()

    def handle_quote_tick(self, tick: QuoteTick, historical: bool = False) -> None:
        if historical:
            super().handle_quote_tick(tick, historical)
            return
        if self._undelivered(tick.ts_init) or self._out_of_scope(
            tick.instrument_id.value, tick.ts_init
        ):
            return
        self._show_book(int(tick.ts_init))
        self._turn(int(tick.ts_init))
        self._delivered_ns = int(tick.ts_init)
        key = tick.instrument_id.value
        self._last_quote[key] = tick
        mid = (float(tick.bid_price) + float(tick.ask_price)) / 2.0
        self._printed(key, mid, int(tick.ts_event), int(tick.ts_init))
        self._observe_price(key, mid, int(tick.ts_event))
        if self._charges.get("spread") == "quotes":
            times, values = self._quoted.setdefault(key, ([], []))
            times.append(int(tick.ts_init))
            values.append(quote_half_spread(float(tick.bid_price), float(tick.ask_price)))
            self._settle(key)
        if self._held():
            self._pending.append(tick)
            return
        self._dispatch_quote(tick)
        self._consult_due()

    def handle_trade_tick(self, tick: TradeTick, historical: bool = False) -> None:
        if historical:
            super().handle_trade_tick(tick, historical)
            return
        if self._undelivered(tick.ts_init) or self._out_of_scope(
            tick.instrument_id.value, tick.ts_init
        ):
            return
        self._show_book(int(tick.ts_init))
        self._turn(int(tick.ts_init))
        self._delivered_ns = int(tick.ts_init)
        key = tick.instrument_id.value
        self._last_trade[key] = tick
        self._printed(key, float(tick.price), int(tick.ts_event), int(tick.ts_init))
        self._observe_price(key, float(tick.price), int(tick.ts_event))
        if self._held():
            self._pending.append(tick)
            return
        self._dispatch_trade(tick)
        self._consult_due()

    def handle_order_book_deltas(self, deltas: object, historical: bool = False) -> None:
        """Hand the book's changes of one instant to the author, then ask again for any exit
        still owed.

        The runner delivers every change one instrument's book made at one instant as one
        batch (`kanso.nautilus.cross_section.batched`), so the author is called once per
        instrument and instant, with the cache's book already holding all of it, and never
        with a book that has lost its best level and not yet been handed the next. The
        batch is the data event the author is acting on, so `data_time` is its
        `ts_event` before `on_order_book_deltas` runs, as a bar's is before `on_bar`: an
        order sent from there is stamped with the change, not with the last print. A book
        change reaches `on_order_book_deltas` and no other handler here, so a sleeve
        that holds only the book would otherwise never be asked again for an exit a cancel
        in flight held back, and would hold the position to the end of the window. In
        nautilus_trader 1.231.0 `Actor.handle_order_book_deltas` is `cpdef`, hands the
        deltas to `on_order_book_deltas` only while the component is running, and hands
        historical ones to `handle_historical_data` instead (read in `common/actor.pyx`; the
        replay tests measure the owed exit it pays on both paths, the backtest engine and
        the node).

        Under `depth` the change is the harness's and not the author's: it goes into the
        harness's own copy of the book and the author is handed what is due of it — the
        grid's view and level one (`_take_depth`) — while the owed exits are asked for on
        every change as before, stamped with the change.
        """
        if self._cfg.depth is not None and not historical:
            self._take_depth(deltas)
            return
        live = not historical and self.is_running and not self._warming()
        if not historical:
            self._data_time = int(deltas.ts_event)  # type: ignore[attr-defined]
        if live:
            self._send_behind_modify()
        super().handle_order_book_deltas(deltas, historical)
        if live:
            self._pay_owed()

    # --- depth: the book as the account sees it ------------------------------

    def _take_depth(self, deltas: Any) -> None:
        """Keep a change of the book under `depth`, showing the author only what is due.

        The grid's view that came due before this change is shown first, so it never holds
        this change or any later one. The change then goes into the harness's book, and
        level one is shown at once if the change moved it: the runner hands every change one
        instrument's book made at one instant as one batch on both paths
        (`kanso.nautilus.cross_section.batched`), so the batch is the whole instant and level
        one is never shown partway through one. A book batch is never held for a flush
        marker, and none follows it, so waiting for anything after the batch would hand
        level one at the next point of the feed rather than at the change.

        The exits owed are asked for last, with `data_time` the change's: the change is
        where the venue's state that makes them due — a cancel that has landed, a fill that
        has cut what is left — is known, whether or not the author was shown anything of it.
        `_show_view` asks for none, because the grid's view is stamped with an instant
        before the change it is shown on.
        """
        instrument_id = deltas.instrument_id
        key = instrument_id.value
        ts_event = int(deltas.ts_event)
        self._show_depth(int(deltas.ts_init))
        book = self._depth_books.get(key)
        if book is None:
            book = self._depth_books[key] = (_Ladder(1), _Ladder(-1))
        _apply(book, deltas)
        self._show_top(instrument_id, book, ts_event, int(deltas.ts_init))
        self._data_time = ts_event
        if self.is_running and not self._warming():
            self._send_behind_modify()
            self._pay_owed()

    def _show_book(self, ts_init: int) -> None:
        """Show the author the grid's view that came due before a point published at
        `ts_init`; the point's own dispatch asks for the exits owed."""
        if self._cfg.depth is not None:
            self._show_depth(ts_init)

    def _show_top(
        self,
        instrument_id: InstrumentId,
        book: tuple[_Ladder, _Ladder],
        ts_event: int,
        ts_init: int,
    ) -> None:
        """Hand level one of a book just changed, when both sides hold a level and the top
        is unlike the one last shown.

        The quote is stamped with the instant of the change and is a signal only: it moves
        no last price, no mark and no quoted spread, as the book it is read from moves none.
        """
        bids, asks = book
        bid, ask = bids.best(), asks.best()
        if bid is None or ask is None:
            return
        key = instrument_id.value
        top = (bid[0], bid[1], ask[0], ask[1])
        if top == self._top_shown.get(key):
            return
        self._top_shown[key] = top
        self._show_view(
            QuoteTick(instrument_id, bid[0], ask[0], bid[1], ask[1], ts_event, ts_init), ts_event
        )

    def _show_depth(self, ts_init: int) -> None:
        """Hand the book as of the last grid instant before `ts_init`, once per grid instant.

        Every change the harness holds was published at or before that instant: a change
        after it would have been handled by `_take_depth`, which shows the grid first. Each
        instrument whose top levels moved since it was last shown gets one batch of the
        differences, stamped with the grid instant and closed by `F_LAST`.
        """
        every, levels = self._cfg.depth  # type: ignore[misc]
        grid = (ts_init - 1) // every * every
        if grid <= self._depth_seen_ns:
            return
        self._depth_seen_ns = grid
        for key in sorted(self._depth_books):
            bids, asks = self._depth_books[key]
            shown = self._depth_shown.setdefault(key, ({}, {}))
            now = (bids.top(levels), asks.top(levels))
            instrument_id = InstrumentId.from_str(key)
            made = [
                *_changes(instrument_id, shown[0], now[0], OrderSide.BUY, grid),
                *_changes(instrument_id, shown[1], now[1], OrderSide.SELL, grid),
            ]
            if not made:
                continue
            self._depth_shown[key] = now
            last = made[-1]
            made[-1] = OrderBookDelta(
                instrument_id, last.action, last.order, RecordFlag.F_LAST, 0, grid, grid
            )
            self._show_view(OrderBookDeltas(instrument_id, made), grid)

    def _show_view(self, data: QuoteTick | OrderBookDeltas, data_time: int) -> None:
        """Hand the author a view of the book stamped `data_time`, the cancels held behind a
        modify sent before it as before any handler. The exits owed are not asked for here
        but by the point the view is shown on, stamped with that point (`_take_depth`)."""
        self._data_time = data_time
        if self.is_running and not self._warming():
            self._send_behind_modify()
        if isinstance(data, QuoteTick):
            super().handle_quote_tick(data, False)
        else:
            super().handle_order_book_deltas(data, False)

    def handle_data(self, data: object) -> None:
        if isinstance(data, KansoCrossSection):
            if self._pending:
                self._flush_one()
            if not self._pending:
                self._consult_due()
            return
        ts_init = getattr(data, "ts_init", None)
        if isinstance(ts_init, int):
            if self._undelivered(ts_init):
                return
            self._show_book(ts_init)
            self._turn(ts_init)
            self._delivered_ns = ts_init
        if self._cfg.books_funding:
            self._fund(data)
        self._note_scope(data)
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
        self._send_behind_modify()
        super().handle_bar(bar, False)
        self._pay_owed()
        self._consult_exit(instrument_id)
        if not self._cfg.extra_resolutions:
            self._overlay_due = (instrument_id, None, None)

    def _dispatch_quote(self, tick: QuoteTick) -> None:
        self._data_time = int(tick.ts_event)
        self._send_behind_modify()
        super().handle_quote_tick(tick, False)
        self._pay_owed()
        self._consult_exit(tick.instrument_id)
        if not self._cfg.extra_resolutions:
            self._overlay_due = (tick.instrument_id, None, None)

    def _dispatch_trade(self, tick: TradeTick) -> None:
        self._data_time = int(tick.ts_event)
        self._send_behind_modify()
        super().handle_trade_tick(tick, False)
        self._pay_owed()
        self._consult_exit(tick.instrument_id)
        if not self._cfg.extra_resolutions:
            self._overlay_due = (tick.instrument_id, None, None)

    def _dispatch_data(self, data: object) -> None:
        instrument_id = getattr(data, "instrument_id", None)
        key = None if instrument_id is None else str(instrument_id)
        self._observe(key, int(getattr(data, "ts_event", self._data_time)), None)
        self._send_behind_modify()
        super().handle_data(data)
        self._pay_owed()
        if instrument_id is not None:
            self._consult_exit(instrument_id)
            if not self._cfg.extra_resolutions:
                self._overlay_due = (instrument_id, None, None)

    def _printed(self, key: str, price: float, ts_event: int, ts_init: int) -> None:
        """Keep the last price an instrument printed at, and when, for marking what it holds;
        and the mark a funding settlement is struck at, chosen as the runner chooses it — the
        latest by availability, and of one instant's, the greatest."""
        self._last_print[key] = price
        self._print_ns[key] = ts_event
        held = self._mark_at.get(key)
        if held is None or ts_init > held[0] or (ts_init == held[0] and price > held[1]):
            self._mark_at[key] = (ts_init, price)

    def _observe_price(self, key: str | None, price: float | None, ts_event: int) -> None:
        if key is not None and price is not None:
            self._last_price[key] = price
            self._price_ns[key] = ts_event

    def _observe(self, key: str | None, ts_event: int, price: float | None) -> None:
        self._data_time = ts_event
        self._observe_price(key, price, ts_event)

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
        """Record the intent, consult the attached filters, submit, then hedge.

        Nothing at all while the strategy is warming: the order is neither checked nor
        recorded, so an author sees `None` from the helpers exactly as under a refused
        filter, and the two code paths hold identical intent lists.
        """
        if self._warming():
            return
        self._check_hand_built(order)
        kind = self.classify(order)
        if kind == ENTRY and not (self.sized or self._built or self._sizing_call):
            self._check_funded((order,))
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
        if self._warming():
            return
        orders = list(order_list.orders)  # type: ignore[attr-defined]
        self._check_hand_built(orders[0])
        kind = self.classify(orders[0])
        if not (self.sized or self._built or self._sizing_call):
            self._check_funded(orders)
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
                self._submit_leg(leg)
        finally:
            self._hedging = False

    def _submit_leg(self, order: Any) -> None:
        """Submit one leg an overlay asked for. On an unsized host a hedge leg that opens or
        grows exposure is cut first to the room the book can fund, with every leg already
        submitted counted as in flight, so two legs of one answer cannot each take it all."""
        if not self.sized and not self._is_clip(order):
            price = self._price_now(order.instrument_id.value)
            if price:
                instrument = self.cache.instrument(order.instrument_id)
                wanted = float(order.quantity)
                funded = self._quantise(
                    instrument, self._funded(order.instrument_id, order.side, wanted, price)
                )
                if funded is None:
                    return
                if float(funded) < wanted:
                    order = self.order_factory.market(order.instrument_id, order.side, funded)
        self._submit_own(order)

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
            multiplier = float(instrument.multiplier)
            raw = full_book_quantity(
                budget,
                price * multiplier,
                float(instrument.price_increment) * multiplier,
                self.cost_rate_at(price, multiplier),
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
        reference = price if price is not None else self._price_now(resolved_id.value)
        if reference is None or reference <= 0:
            return None
        room = self._headroom(resolved_id, reference)
        if room <= 0:
            return None
        unit = reference * float(instrument.multiplier)  # one contract's notional
        wanted = room if qty is None else abs(qty) * unit
        if notional is not None:
            wanted = min(wanted, abs(notional))
        raw = min(wanted, room) / unit
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
        """Reduce a position, never past flat, counting the exits still working.

        The quantity is the smaller of what was asked for and what is left to close: the
        net position less the unfilled quantity of every order of this sleeve's on the
        closing side that the venue has not closed — resting, in flight to it, or, under a
        stated latency, waiting on a cancel that has not landed (`_working`). So an exit that
        replaces one whose cancel is still in flight is sized to what the old one cannot also
        take, and the two cannot both fill past flat. Every such order counts in full, an
        exit or not: a stop that would reverse the position, or both legs of a bracket only
        one of which can fill, leave that much less to close. The stop-loss and take-profit
        of a bracket whose entry has filled nothing do not count, since they can fill only
        after it and close what it opens; once it has filled any of it they count in full
        (`_not_yet_live`).

        An exit at market cancels this sleeve's own limit or stop orders resting at the
        venue on the closing side when they would leave it less than asked. With no latency
        stated those cancels land before anything further is matched and the whole of what
        was asked goes at market. Under a stated latency the cancelled orders can still fill
        until their cancels land, so the exit is cut to what they leave, and the rest is
        owed. An order still in flight to the venue is not cancelled and counts, and what it
        cuts from an exit at market is owed as well; once the venue holds it open, the owed
        exit cancels it like any other. So is an order the sleeve itself cancelled while it
        was still on its way to the venue: the venue takes it before the cancel, on both
        paths (`_hold_cancel`). An order the venue holds whose modify it has not answered
        yet counts too, and is not cancelled in the handler that sent the modify, where on a
        node the cancel would overtake it (`_cancel_behind_modify`): with no latency stated
        it is cancelled as the venue answers the modify, and under one on the next point,
        before the sleeve's handler, whether or not the modify has been answered by then,
        the cancel landing behind the modify that was stamped before it
        (`_send_behind_modify`). What it cut is owed and paid as for any cancelled order.
        With no latency stated the venue answers an order in flight, and a modify, before
        the next point, and an exit at market owed behind either is paid in the instant it
        was asked for (`_answered`), so one asked for on a session's last point or the
        window's is not carried past it. A resting order whose cancel the venue refused is
        cancelled again. An order the engine's order emulator holds has not reached the
        venue: it counts until it is cancelled, an exit at market cancels it with the
        resting ones, and its cancel takes it out at once, at any latency (`_working`).

        What is owed is not dropped. When a cancel still in flight leaves less than asked,
        and whenever an exit at market is left less than asked, the exit is asked for again,
        with what is still owed at the price given, at every later point once the author's
        handler for it has run — each time sized to what is left then — until it goes out
        whole. It is forgotten when the position is flat or has changed sides and when this
        sleeve asks for another exit in the name. A cancel on that side takes back only an
        owed exit that has a price: an exit at market stands for an order the venue would
        have taken before any cancel that followed it. An exit an attached exit rule asked
        for is forgotten only when the position is flat, whatever its host sends or cancels.
        An owed exit with a price is asked for at that price on the next session's points
        too, if the position is still open there; it never goes past flat, but the price
        may be the last session's.

        Returns the order, or `None` when flat or when the orders still working already
        close what was asked, including when what is left is owed.

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
        net = Decimal(self.portfolio.net_position(resolved_id))
        return self._close(instrument, resolved_id, net, qty, price, clips=True)

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
        multiplier = float(instrument.multiplier)  # type: ignore[attr-defined]
        raw = full_book_quantity(
            self.budget,
            reference * multiplier,
            float(instrument.price_increment) * multiplier,  # type: ignore[attr-defined]
            self.cost_rate_at(reference, multiplier),
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
        """The whole of this sleeve's own position at market, leaving any clip alone, less
        what its own exits still working will close."""
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
        filled = Decimal(repr(self._own_filled(key)))
        self._sizing_call = True
        try:
            return self._close(instrument, instrument_id, filled, None, None, clips=False)
        finally:
            self._sizing_call = False

    def _close(
        self,
        instrument: object,
        instrument_id: InstrumentId,
        net: Decimal,
        qty: float | None,
        price: float | None,
        *,
        clips: bool,
    ) -> object | None:
        """Close up to `qty` of a signed position of `net`, less what this sleeve's orders
        still working on the closing side will close (`submit_exit` states the rule)."""
        key = instrument_id.value
        self._forget(key, OrderSide.NO_ORDER_SIDE, ruled=self._exiting)
        if net == 0:
            return None
        side = OrderSide.SELL if net > 0 else OrderSide.BUY
        asked = float(abs(net)) if qty is None else min(abs(qty), float(abs(net)))
        working = self._working(key, side, clips=clips)
        if price is None and float(abs(net) - _leaves(working)) < asked:
            for order in working:
                if order.order_type == OrderType.MARKET:
                    continue
                if _modifying(order):
                    self._cancel_behind_modify(order)
                elif _held_open(order) or order.is_emulated:
                    self.cancel_order(order)
            working = self._working(key, side, clips=clips)
        quantity = self._quantise(instrument, min(asked, float(abs(net) - _leaves(working))))
        sent = 0.0 if quantity is None else float(quantity)
        whole = self._quantise(instrument, asked)
        short = 0.0 if whole is None else float(whole) - sent
        if short > 0 and (
            price is None or any(order.client_order_id in self._cancels for order in working)
        ):
            owed = (None if qty is None else short, price, self._exiting)
            self._owed.setdefault((key, side), owed)
            if price is None and self._instant():
                self._awaiting.update(
                    order.client_order_id
                    for order in working
                    if order.order_type != OrderType.MARKET
                )
        if quantity is None:
            return None
        return self._submitted(self._order(instrument, side, quantity, price))

    def _cancel_behind_modify(self, order: Any) -> None:
        """Hold back an exit at market's cancel of an order the venue holds whose modify is
        still in flight, until the venue answers the modify with no latency stated
        (`_answered`), and until the next point under one (`_send_behind_modify`).

        On a node `Strategy.modify_order` goes through the live risk engine's queue and
        `cancel_order` straight to the live execution engine's (`trading/strategy.pyx` of
        nautilus_trader 1.231.0), so a cancel sent in the handler that sent the modify
        overtakes it, where the backtest engine lands the modify first. Both queues drain
        before the node's next point is released, so a cancel sent then reaches the venue
        behind the modify on both paths; under a latency it is stamped with that point's
        instant, after the modify's, and lands behind it there too.
        The order counts as working until the cancel lands (`_cancelling`, read then).
        """
        self._behind_modify[order.client_order_id] = order

    def _send_behind_modify(self) -> None:
        """Send the cancels `_cancel_behind_modify` held back, before the author's handler
        for this point, which could put the order back in flight with a modify of its own.

        Under a stated latency it is sent here whether or not the venue has answered the
        modify by then, not when it answers. When the latency is shorter than the gap
        between points the backtest engine answers the modify in the drain after the next
        point's handlers and the node in the drain before the point after that
        (`SimulatedVenue.on_data`), so a cancel sent on the answer would be stamped a point
        later on the node; when it is not shorter, later still. Either way neither path has
        answered the latest modify when the cancel is sent here. The order reads
        `PENDING_UPDATE` unless the answer to an earlier modify has landed since and
        returned it to `ACCEPTED`, as it has for a sleeve that modifies on every point. The
        cancel is stamped after every modify and delayed by the same latency, so it lands
        behind them on both paths. With no latency stated both answer the modify in the
        drain after the point's handlers, and `_answered` sends the cancel then; one it has
        not sent by the next point is sent here. Measured on both paths by the exit and
        replay tests, among them a sleeve that modifies its exit on every quote, at 20 and
        at 2500 ms with the status seen here, and a modify followed by an exit at market at
        20 ms. (At 2500 ms that sleeve modifies an order the venue has not taken yet, whose
        cancel goes out as it is taken, not from here.)
        """
        held, self._behind_modify = self._behind_modify, {}
        for order in held.values():
            current = self._current(order)
            if not current.is_closed and not current.is_pending_cancel:
                self._cancelling(current)
                super().cancel_order(current)

    def _pay_owed(self, only: tuple[str, OrderSide] | None = None) -> None:
        """Ask again for every exit still owed, on the side of the position it was owed on,
        or with `only` for the one owed on that side of that name.

        Run after the author's handler for a point and before the exit rules, so a sleeve
        that asks again on that point replaces what it was owed rather than adding to it,
        and an exit rule's own exit is asked for as the rule's, which its host cannot take
        back.
        """
        for (key, side), (qty, price, ruled) in list(self._owed.items()):
            if only is not None and (key, side) != only:
                continue
            net = (
                self._own_filled(key)
                if self.sized
                else float(self.portfolio.net_position(InstrumentId.from_str(key)))
            )
            if net == 0 or (OrderSide.SELL if net > 0 else OrderSide.BUY) != side:
                del self._owed[(key, side)]
                continue
            exiting, self._exiting = self._exiting, ruled
            try:
                self.submit_exit(key, qty=qty, price=price)
            finally:
                self._exiting = exiting

    def _forget(
        self, key: str, side: OrderSide, *, ruled: bool = False, priced_only: bool = False
    ) -> None:
        """Drop the exit owed on one side of a name, or on both for `NO_ORDER_SIDE`; one an
        exit rule is owed only when `ruled`, and one at market not when `priced_only`."""
        for owed in (OrderSide.BUY, OrderSide.SELL):
            held = self._owed.get((key, owed))
            if (
                side in (OrderSide.NO_ORDER_SIDE, owed)
                and held is not None
                and (ruled or not held[2])
                and not (priced_only and held[1] is None)
            ):
                del self._owed[(key, owed)]

    def cancel_order(
        self,
        order: Any,
        client_id: object = None,
        params: dict[str, object] | None = None,
    ) -> None:
        """Cancel an order, and forget the exit at a price owed on its side of the name: a
        sleeve that cancels an order on the closing side has taken back the limit it asked
        for. An exit at market owed there is kept, as the market order it stands for would
        have been taken by the venue before the cancel that followed it.

        A cancel for an order not yet handed to the venue is held back and sent once it has
        been (`_hold_cancel`)."""
        self._forget(order.instrument_id.value, order.side, priced_only=True)
        if not self._hold_cancel(order, client_id, params):
            self._cancelling(order)
            super().cancel_order(order, client_id, params)

    def cancel_orders(
        self,
        orders: list[Any],
        client_id: object = None,
        params: dict[str, object] | None = None,
    ) -> None:
        """Cancel orders, forgetting the exits at a price owed on their sides and holding
        back the cancel for any not yet handed to the venue, as `cancel_order` does.

        An order the engine's order emulator holds is cancelled on its own, through the
        engine's `cancel_order`, which routes it to the emulator; the rest go to the engine's
        own `cancel_orders` one batch per instrument. The engine refuses a batch that mixes
        instruments, or holds an emulated order after its first, after it has already marked
        the orders before it `PENDING_CANCEL`, and sends nothing, so those would rest while
        they read as cancelled (read in `trading/strategy.pyx` of nautilus_trader 1.231.0,
        which applies to both paths, the backtest engine and the node; the emulated case is
        measured on both by the exit and replay tests). An empty list is handed on as it is,
        for the engine to refuse.
        """
        batches: dict[object, list[Any]] = {}
        for order in orders:
            self._forget(order.instrument_id.value, order.side, priced_only=True)
            if self._hold_cancel(order, client_id, params):
                continue
            self._cancelling(order)
            if self._current(order).is_emulated:
                super().cancel_order(order, client_id, params)
            else:
                batches.setdefault(order.instrument_id, []).append(order)
        if not orders:
            super().cancel_orders(orders, client_id, params)
        for batch in batches.values():
            super().cancel_orders(batch, client_id, params)

    def cancel_all_orders(
        self,
        instrument_id: InstrumentId,
        order_side: OrderSide = OrderSide.NO_ORDER_SIDE,
        client_id: object = None,
        params: dict[str, object] | None = None,
    ) -> None:
        """Cancel this sleeve's orders in a name, forgetting the exits at a price owed on
        those sides as `cancel_order` does.

        When every one of them has been handed to the venue, the engine's own
        `cancel_all_orders` cancels them, those open at the venue and those in flight to it,
        marking each open one `PENDING_CANCEL` at once (`kanso.nautilus.facts` measures it on
        the backtest engine). When any has not been handed over yet, each is cancelled on its
        own through `cancel_order` instead, so the cancel for that one is held back as it is
        there: the engine's own skips an order still `INITIALIZED` (read in
        `trading/strategy.pyx` of nautilus_trader 1.231.0), which on the backtest engine an
        order handed to `submit_order` never is by the time the call returns (`_hold_cancel`
        names the exceptions).
        """
        orders = [
            order
            for order in (self._current(entry.order) for entry in self._ledger.values())
            if order.instrument_id == instrument_id
            and order_side in (OrderSide.NO_ORDER_SIDE, order.side)
            and not order.is_closed
        ]
        self._forget(instrument_id.value, order_side, priced_only=True)
        if any(order.status == OrderStatus.INITIALIZED for order in orders):
            for order in orders:
                self.cancel_order(order, client_id, params)
            return
        for order in orders:
            self._cancelling(order)
        super().cancel_all_orders(instrument_id, order_side, client_id, params)

    def _hold_cancel(self, order: Any, client_id: Any, params: dict[str, object] | None) -> bool:
        """Hold back the cancel for an order not yet handed to the venue, and say so.

        On the backtest engine `submit_order` hands the order to the venue before it returns
        (`SUBMITTED`), so a cancel sent after it reaches the venue behind the order: the
        venue takes the order, filling it if it is marketable, and then the cancel — with no
        latency stated before it matches anything further, under one both at the same
        instant, the order first. A node has not handed it over yet. The order is still
        `INITIALIZED` while its submit waits in the live risk engine's queue, and a cancel
        sent then goes straight to the live execution engine's, so it reaches the venue
        first, for an order it does not know, and the order then rests uncancelled (read in
        `live/risk_engine.py`, `execution/manager.pyx` and `trading/strategy.pyx` of
        nautilus_trader 1.231.0). So kanso holds such a cancel back, counts the order as
        working, and sends the cancel from `handle_event` when the node reports the order
        `SUBMITTED` — inside the call that hands it to the venue, before the venue has
        matched it, which reaches the venue behind the order as on the backtest engine. The
        replay tests measure it on both paths. An order the node closes first, by denying
        it, is never cancelled.

        On the backtest engine an order handed to `submit_order` is past `INITIALIZED` when
        the call returns, so a cancel is held back there only for an order the engine itself
        has not submitted yet: the child of an emulated one-triggers-other list, which the
        order emulator keeps back until its parent fills (`OrderEmulator
        ._handle_submit_order_list` in `execution/emulator.pyx`, read, not measured), or an
        order never submitted at all. Its cancel is sent once the order leaves `INITIALIZED`.
        """
        current = self._current(order)
        if current.status != OrderStatus.INITIALIZED:
            return False
        self._cancelling(current)
        self._unsent[current.client_order_id] = (current, client_id, params)
        return True

    def handle_event(self, event: Any) -> None:
        """Keep an event for the balance's next read (`_arrived`), hand it to the engine's own
        handling, then send a cancel held back for the order it is about once the node has
        handed that order to the venue (`_hold_cancel`). The event is kept first, so an
        author's handler for it that reads the balance reads it from what was kept.

        In nautilus_trader 1.231.0 the node's simulated venue reports an order `SUBMITTED`
        from inside the call that hands it to the exchange, before the exchange matches it,
        and the event reaches `Strategy.handle_event` in that same call, since kanso's relay
        delivers the venue's events to the execution engine's synchronous handler
        (`kanso.nautilus.sandbox`). A cancel sent here goes to the live execution engine's
        queue, which the replay feed drains before it releases the next point
        (`kanso.nautilus.replay_client`); under a latency it is stamped with the same
        instant as the order and lands right behind it (`SimulatedVenue._send`). Measured on
        both paths by the replay tests.
        """
        self._arrived(event)
        super().handle_event(event)
        client_order_id = getattr(event, "client_order_id", None)
        held = self._unsent.get(client_order_id)
        if held is not None and self._current(held[0]).status != OrderStatus.INITIALIZED:
            order, client_id, params = held
            current = self._current(order)
            del self._unsent[current.client_order_id]
            if not current.is_closed:
                super().cancel_order(current, client_id, params)
        if client_order_id in self._awaiting:
            self._answered(client_order_id)

    def _answered(self, client_order_id: object) -> None:
        """With no latency stated, move an exit at market owed behind an order on along as
        the venue answers that order, so the exit is paid in the instant it was asked for.

        An exit at market is cut by what an order the venue has not settled can still close:
        one still in flight to it, one it holds whose modify it has not answered yet, one the
        sleeve cancelled while it was in flight (`_close` notes each in `_awaiting`). With no
        latency stated the venue answers such an order before the next point, on both paths —
        the backtest engine in the drain after the point's handlers, which takes the commands
        sent from inside it too (`SimulatedExchange._drain_commands` in `backtest/engine.pyx`
        of nautilus_trader 1.231.0), and the node in the drain the replay feed waits on before
        it releases the next point (`kanso.nautilus.replay_client`). So each answer is acted on
        where it lands: once the venue has taken the order, or answered its modify, it is
        cancelled, the cancel sent behind the modify; once it is closed — cancelled, filled,
        or refused — the owed exit is asked for again, sized to what is left then. The exit is
        never paid on the answer itself, as the venue matches an order it has just taken, or
        just modified, right after it says so, and one it fills there closes what it fills.
        What the next point would pay instead is paid at the instant of the point it was
        asked on, so an exit asked for on a session's last point, or the window's, is not
        carried to the next one. Under a stated latency the answers land at later instants
        and the owed exit is paid on the next point, as before. Measured on both paths by
        the exit and replay tests.
        """
        order = self.cache.order(client_order_id)
        name = (order.instrument_id.value, order.side)
        if order.is_closed:
            self._awaiting.discard(client_order_id)
            self._behind_modify.pop(client_order_id, None)
            self._pay_owed(name)
            return
        if client_order_id in self._behind_modify:
            if not _modifying(order):
                del self._behind_modify[client_order_id]
                if not order.is_pending_cancel:
                    self._cancelling(order)
                    super().cancel_order(order)
            return
        owed = self._owed.get(name)
        if owed is None or owed[1] is not None:
            self._awaiting.discard(client_order_id)
        elif _held_open(order) and client_order_id not in self._cancels:
            self.cancel_order(order)

    def _instant(self) -> bool:
        """Whether no latency is stated, so the venue answers a command before it matches
        anything further."""
        return float(self._charges.get("latency_ms") or 0.0) <= 0.0

    def _cancelling(self, order: Any) -> None:
        """Note that a cancel was sent or held back for an order, and whether the venue held
        it open when the cancel was sent.

        An order stays working until its cancel lands (`_working`). With no latency stated
        one the venue held open is spent the moment its cancel is sent, since the cancel
        lands before anything further is matched; one still on its way to the venue is not,
        because the venue takes it before the cancel, and a marketable one fills there.

        Whether the venue held it open is `_held_open`, read before the cancel is applied.
        One the venue held open was flagged by its first cancel, and one whose cancel was
        rejected returns to the status it had before (`Order.apply` on
        `OrderCancelRejected` in nautilus_trader 1.231.0), so the next cancel reads it
        afresh. Measured on the backtest engine and on the node by the exit and replay tests.
        """
        current = self._current(order)
        if current.is_closed:
            return
        key = current.client_order_id
        self._cancels[key] = self._cancels.get(key, False) or _held_open(current)

    def _working(self, key: str, side: OrderSide, *, clips: bool) -> list[Any]:
        """This sleeve's own orders on one side of a name that can still fill: every one the
        venue has not closed, resting or in flight to it.

        An order whose cancel has been sent is still working until the cancel lands. Under a
        stated `latency_ms` the cancel reaches the book after the next point has been
        matched, so the order can fill in between and is counted. With no latency stated the
        venue lands a cancel sent in a handler before it matches anything further, on both
        code paths, so an order it held open when the cancel was sent, and that still waits
        on it, is spent and is not counted; one still on its way to the venue when the cancel
        was sent is counted until the cancel lands, since the venue takes the order first and
        a marketable one fills there.

        An order the engine's order emulator holds has not reached the venue and counts
        until it is cancelled. Its cancel is spent the moment it is sent, at any latency: in
        nautilus_trader 1.231.0 the emulator takes the order out of its matching core and
        marks it pending cancel locally before `cancel_order` returns (`OrderEmulator
        ._cancel_order` in `execution/emulator.pyx`), on both paths. The `OrderCanceled` it
        generates reaches the order at once on the backtest engine and on the node only once
        the live execution engine's queue drains, so kanso reads the engine's local mark
        (`Cache.is_order_pending_cancel_local`), which both set at once. Measured on both
        paths by the exit and replay tests.

        `clips` counts the attached overlays' clips as well, for a reading of the whole net
        position rather than the sleeve's own share of it.
        """
        instant = self._instant()
        working = []
        for entry in self._ledger.values():
            order = self._current(entry.order)
            if (
                order.is_closed
                or self.cache.is_order_pending_cancel_local(order.client_order_id)
                or order.side != side
                or order.instrument_id.value != key
                or (
                    instant
                    and order.is_pending_cancel
                    and self._cancels.get(order.client_order_id, False)
                )
                or (not clips and self._is_clip(entry.order))
                or self._not_yet_live(order)
            ):
                continue
            working.append(order)
        return working

    def _not_yet_live(self, order: Any) -> bool:
        """Whether an order is a contingent child, such as a bracket's stop-loss or
        take-profit, whose parent has filled nothing: it can fill only once the parent has,
        and then closes what the parent opened, so until then it leaves the position as it
        is. In nautilus_trader 1.231.0 the venue keeps such a child `SUBMITTED` with no venue
        order id, and the order emulator keeps it `INITIALIZED`, until the parent fills, so
        neither an exit at market nor anything else has a child to cancel. Measured on both
        paths by the exit and replay tests, a bracket held by the venue and one held by the
        emulator."""
        if order.parent_order_id is None:
            return False
        parent = self.cache.order(order.parent_order_id)
        return parent is not None and parent.filled_qty.as_decimal() == 0

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

        Read on what the sleeve will hold once its orders in flight fill, with what its
        resting entries would add held back, not on what the venue holds now: an exit
        submitted in the same handler counts as gone, so a flip fits at leverage one exactly
        as it does under a sizing rule, where the same rule was written first. Read on the
        venue alone, the old leg still filled the book and every flip was refused for a bar.
        """
        intended = self._holdings()
        resting = self._resting(intended)
        key = instrument_id.value
        held = abs(intended.get(key, 0.0)) * reference * self._multiplier_of(instrument_id)
        held += resting.get(key, 0.0)
        gross = self._gross_intended(intended) + sum(resting.values())
        room = min(self.max_notional - held, self.gross_limit - gross)
        return room / self._reserve(key)

    def _gross_intended(self, intended: Mapping[str, float]) -> float:
        """What this sleeve will hold, marked at the last price seen and at cost where none
        was, each name's quantity times its price times its contract multiplier.

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
            price = self._price_now(name)
            if price is None:
                position = positions.get(name)
                price = (
                    0.0
                    if position is None
                    else splits.ledger(
                        splits.moves_of(position, tuple(self._restated.get(name, ())))
                    ).basis
                )
            total += abs(quantity) * price * self._multiplier_of(name)
        return total

    def _restating(self, key: str, printed_ns: int) -> float:
        """What a price printed at `printed_ns` is divided by to be quoted in the shares held now.

        The venue restates every holder of a split at the first point past its ex-date,
        whichever instrument printed it, so on a pair's ex-date the other leg's handler sees
        the held leg's restated share count beside a price still quoted in the old one —
        read as it stands, a one-for-ten reverse split is a ninety per cent loss, and room
        for ten times the exposure. The factor is the product of the ratios of the name's
        splits effective after the print, among the ones the venue has announced applying
        (`_on_restated`) — which are all this sleeve ever holds.
        """
        return prod(
            split.ratio for split in self._restated.get(key, ()) if split.effective_ns > printed_ns
        )

    def _price_now(self, key: str) -> float | None:
        """The last price seen for a name, quoted in the share count the venue holds now."""
        price = self._last_price.get(key)
        if price is None:
            return None
        return price / self._restating(key, self._price_ns.get(key, 0))

    def _print_now(self, key: str) -> float | None:
        """The last print of a name, quoted in the share count the venue holds now."""
        price = self._last_print.get(key)
        if price is None:
            return None
        return price / self._restating(key, self._print_ns.get(key, 0))

    def _reserve(self, key: str) -> float:
        """What a notional is divided by to leave room for its own round trip: commission,
        per share included at the last price, slippage and the whole spread each way — the
        stated width, or the last quoted one."""
        price = self._last_print.get(key)
        rate = self.cost_rate_at(price, self._multiplier_of(key)) if price else self.cost_rate
        if self._charges.get("spread") == "quotes":
            _, values = self._quoted.get(key, ([], []))
            if values:
                rate += 2.0 * values[-1]
        return 1.0 + ROUND_TRIP * rate

    def _resting(self, intended: Mapping[str, float]) -> dict[str, float]:
        """The notional the sleeve's own resting orders would add if they filled, by name.

        A limit or stop entry waits at the venue rather than filling at once, so neither
        `held` nor the orders in flight count it; the room holds its growth back as if it
        had filled, at its own price, or it and a later entry could each take the whole
        book. An order that would shrink a position frees nothing until it fills.
        """
        added: dict[str, float] = {}
        for entry in self._ledger.values():
            order = self._current(entry.order)
            if order.is_closed or order.order_type == OrderType.MARKET or self._is_clip(order):
                continue
            key = order.instrument_id.value
            now = intended.get(key, 0.0)
            leaves = float(order.leaves_qty)
            grows = abs(now + (leaves if order.side == OrderSide.BUY else -leaves)) - abs(now)
            if grows > 0.0:
                price = _order_price(order) or self._price_now(key) or 0.0
                added[key] = added.get(key, 0.0) + grows * price * self._multiplier_of(key)
        return added

    def _current(self, order: Any) -> Any:
        """The order as the cache holds it now. The engine replaces an emulated order with
        another object under the same id when it releases it, and that one gets the fill."""
        return self.cache.order(order.client_order_id) or order

    def _settle(self, key: str | None = None, until_ns: int | None = None) -> list[Any]:
        """Fold every fill not booked yet into the sleeve's cash, as the runner charges it.

        Each order the sleeve sent is kept with how many of its events have been folded in,
        read again only when that count moves, and then only for the events it gained since
        (`_gained`), so a read costs what is new and not what the order has accumulated: an
        order modified on every bar gains two events a bar, and read whole it cost its whole
        history a bar and the square of it a card. A fill is booked once, by its event id,
        whichever object carried it; an order the engine has replaced with another object
        is read again from its first event (`_current`). The order, its count and its fill
        ids are let go once the venue has closed it and every fill of it is booked. With
        `key` only that name's orders are read: a quote makes its own name's spread current
        and no other, so a fill in another name waits for that name's quote or for a read of
        the balance. A quoted spread's series is then cut back to its last quote: every fill
        before now is booked, and a fill still to come is charged at a quote no older than
        that one.

        With `until_ns` a fill after that instant is left unbooked and its order is read
        again next time from where it was: it is returned instead, for the period close that
        has to strike the book as it stood at the instant (`_book_at`), and it is booked by
        the next read, at a quote no older than the last one seen.
        """
        later: list[Any] = []
        closed: list[object] = []
        for client_order_id, entry in self._ledger.items():
            order = self._current(entry.order)
            if order is not entry.order:
                entry.order, entry.read, entry.unread = order, 0, None
            if key is not None and order.instrument_id.value != key:
                continue
            count = order.event_count
            held_back = False
            if count != entry.read:
                gained = self._gained(entry, count)
                for event in gained:
                    if not isinstance(event, OrderFilled) or event.id in entry.booked:
                        continue
                    if until_ns is not None and int(event.ts_event) > until_ns:
                        later.append(event)
                        held_back = True
                        continue
                    entry.booked.add(event.id)
                    self._cash -= self._paid(event)
                    if self._cfg.books_funding:
                        self._filled.append(
                            (int(event.ts_event), event.instrument_id.value, self._signed(event))
                        )
                if held_back:
                    entry.unread = gained
                else:
                    entry.read, entry.unread = count, []
            if not held_back and order.is_closed:
                closed.append(client_order_id)
                self._cancels.pop(order.client_order_id, None)
        for client_order_id in closed:
            del self._ledger[client_order_id]
        for name, (times, values) in self._quoted.items():
            if key is None or name == key:
                del times[:-1]
                del values[:-1]
        return later

    @staticmethod
    def _gained(entry: _Tracked, count: int) -> list[Any]:
        """The events an order gained since the balance last folded it in, `count` being how
        many it holds now.

        They are the ones `handle_event` was handed (`_arrived`) when those are known to be
        exactly them — as many as the order gained, on the object the balance read. Otherwise
        they are taken from the order, whose `events` hands back a copy of every event it
        holds (`Order.events` in nautilus_trader 1.231.0), which is what the handed ones save:
        that copy costs the order's whole history however little of it is new.
        """
        unread = entry.unread
        if unread is not None and len(unread) == count - entry.read:
            return unread
        events: list[Any] = entry.order.events[entry.read :]
        return events

    def _arrived(self, event: Any) -> None:
        """Keep an event of an order the balance reads, for the next read to take from it
        rather than from the order (`_gained`).

        In nautilus_trader 1.231.0 an order's events only ever grow at its end, one for each
        event `Order.apply` takes, and the engine hands the strategy each event of its own
        orders once the order has applied it — the execution engine for what the venue and
        the strategy's own commands send, the strategy itself for the pending modify and
        pending cancel it applies (`execution/engine.pyx`, `trading/strategy.pyx`) — so the
        event handed here is normally the order's last, one past those already kept
        (`kanso.nautilus.facts` measures it on the backtest engine). The engine also hands
        over an event the order refused to take, such as a cancel landing on an order a fill
        has just closed (`ExecutionEngine._apply_event_to_order` goes on after an
        `InvalidStateTrigger`): the order's count has not moved, so what is kept is still
        all of it, and the event is not kept. Anything else — an event handed after a later
        one, one of an order the engine has since replaced — leaves the kept ones unknown to
        be complete, and the next read takes the order's own.
        """
        entry = self._ledger.get(getattr(event, "client_order_id", None))
        if entry is None or entry.unread is None:
            return
        order = self.cache.order(event.client_order_id)
        kept = entry.read + len(entry.unread)
        if order is entry.order and order.event_count == kept + 1 and order.last_event is event:
            entry.unread.append(event)
        elif order is not entry.order or order.event_count != kept:
            entry.unread = None

    def _paid(self, event: Any) -> float:
        """What one fill took out of cash: the value it bought, or gave back what it sold
        for, and what the runner charges it — commission, per share included where the model
        states one, slippage and half the spread, or a maker's own schedule where the venue
        model states one, of its notional and per share, which a rebate makes negative."""
        multiplier = self._multiplier_of(event.instrument_id)
        qty, px = float(event.last_qty), float(event.last_px)
        signed = qty if event.order_side == OrderSide.BUY else -qty
        cost = fill_cost(
            qty * px * multiplier,
            qty,
            float(self._charges.get("commission_bps") or 0.0),
            float(self._charges.get("slippage_bps") or 0.0),
            self._half_spread_at(event.instrument_id.value, int(event.ts_event)),
            _maker_bps(self._charges),
            float(self._charges.get("commission_per_share") or 0.0),
            maker=event.liquidity_side == LiquiditySide.MAKER,
            sell=event.order_side == OrderSide.SELL,
            sell_fee_bps=float(self._charges.get("sell_fee_bps") or 0.0),
            sell_fee_per_share=float(self._charges.get("sell_fee_per_share") or 0.0),
            maker_per_share=_maker_per_share(self._charges),
        )
        return signed * px * multiplier + cost

    def _half_spread_at(self, key: str, ts_ns: int) -> float:
        """Half the spread one fill pays: the stated width, or the last quote before it."""
        if self._charges.get("spread") != "quotes":
            return fixed_half_spread(self._charges.get("fixed_bps"))
        times, values = self._quoted.get(key, ([], []))
        index = bisect_right(times, ts_ns)
        return 0.0 if index == 0 else values[index - 1]

    def _worth(self, position: Any) -> float:
        """One open position's market value at its last print, quoted in the shares held now.

        A price printed before a split the venue has since applied is restated by the
        split's ratio (`_restating`), so a pair's other leg does not read the split as a
        loss in the handlers between the split and the held leg's next print. A position
        the sleeve has seen no price for is marked at its own split-aware cost.
        """
        price = self._print_now(position.instrument_id.value)
        if price is None:
            applied = tuple(self._restated.get(position.instrument_id.value, ()))
            price = splits.ledger(splits.moves_of(position, applied)).basis
        return float(position.signed_qty) * price * self._multiplier_of(position.instrument_id)

    def _multiplier_of(self, instrument_id: InstrumentId | str) -> float:
        """The contract multiplier a quantity at a price is scaled by to be a notional, read
        from the cached instrument: the contract size of a future or an option, one for a
        share, and one when the cache holds no definition. Under nautilus_trader 1.231.0
        every instrument class carries `multiplier` as a `Quantity`, `Equity` fixing it at
        one (`kanso.nautilus.facts`)."""
        instrument = self.cache.instrument(self._instrument_id(instrument_id))
        return 1.0 if instrument is None else float(instrument.multiplier)

    def _opening(
        self, instrument_id: InstrumentId, side: OrderSide, quantity: float, price: float
    ) -> tuple[float, float, float]:
        """What an order closes, what it opens or grows, and the room for the second part.

        The closed part frees its own room before the opened part takes any, so a leg that
        crosses zero is funded like an exit followed by an entry. The room is the reserved
        one `submit_entry` sizes to, in notional — price times multiplier per contract.
        """
        now = self.held(instrument_id)
        signed = quantity if side == OrderSide.BUY else -quantity
        closing = min(quantity, abs(now)) if now * signed < 0 else 0.0
        room = self._headroom(instrument_id, price)
        unit = price * self._multiplier_of(instrument_id)
        room += closing * unit / self._reserve(instrument_id.value)
        return closing, quantity - closing, room

    def _funded(
        self, instrument_id: InstrumentId, side: OrderSide, quantity: float, price: float
    ) -> float:
        """How much of a hedge leg the book can fund: all of what it closes, and of what it
        opens or grows, what the room leaves once the closed part has freed its own."""
        closing, opening, room = self._opening(instrument_id, side, quantity, price)
        if opening <= 0.0:
            return quantity
        unit = price * self._multiplier_of(instrument_id)
        return closing + min(opening, max(0.0, room) / unit)

    def _check_funded(self, orders: Sequence[Any]) -> None:
        """Refuse entries built by hand that the book cannot fund.

        kanso cuts the orders it builds — `submit_entry` and an overlay's hedge legs — to
        its room. An order a strategy built itself is not rebuilt at another size, and the
        question asked of it is the funding one alone: whether what it opens fits inside
        `max_leverage` times the smaller of the capital and `balance`, less the gross
        exposure already held, in flight or resting. `max_position_pct` is kanso's own
        sizing ceiling and is not asked of it. A list is judged whole and in order: what an
        order closes frees room for the ones after it and what it opens is taken from it,
        so two entries in one list cannot each take the book — and a bracket's exits, which
        close what their parent opens and only one of which can fill, are not asked at all.

        One that does not fit is refused inside the handler that placed it, as a sizing rule
        refuses a hand-built order, and the card is discarded with the refusal rather than
        measured on money the account never had. With no price seen for a name there is
        nothing to show an entry in it fits, and it is refused for that.
        """
        intended = self._holdings()
        resting = self._resting(intended)
        free = self.gross_limit - self._gross_intended(intended) - sum(resting.values())
        held = dict(intended)
        parents = {order.client_order_id: order for order in orders}
        for order in orders:
            parent = parents.get(order.parent_order_id)
            if (
                parent is not None
                and parent.instrument_id == order.instrument_id
                and parent.side != order.side
                and float(order.quantity) <= float(parent.quantity)
            ):
                continue
            key = order.instrument_id.value
            side = order_side_to_str(order.side)
            quantity = float(order.quantity)
            now = held.get(key, 0.0)
            signed = quantity if order.side == OrderSide.BUY else -quantity
            closing = min(quantity, abs(now)) if now * signed < 0 else 0.0
            held[key] = now + signed
            price = _order_price(order) or self._price_now(key) or 0.0
            if price <= 0.0 and quantity > closing:
                self._refuse(
                    UNFUNDED_ORDER,
                    key,
                    side,
                    why="an entry built by hand in a name with no price seen yet cannot be "
                    "shown to fit the book; place it with submit_entry, which places nothing "
                    "until a price has been seen",
                )
            unit = price * self._multiplier_of(key)  # one contract's notional
            free += closing * unit
            opening = (quantity - closing) * unit
            if opening > free + 1e-6:
                self._refuse(
                    UNFUNDED_ORDER,
                    key,
                    side,
                    why=f"an entry built by hand opens {opening:,.2f} of exposure and the book "
                    f"can fund {max(0.0, free):,.2f} more: max_leverage times the smaller of the "
                    "capital and the balance, less what is held. kanso does not rebuild an "
                    "order it did not build; place entries with submit_entry, which cuts them "
                    "to the room, or size them from self.balance",
                )
            free -= opening

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
        self._ledger.setdefault(order.client_order_id, _Tracked(order))  # type: ignore[attr-defined]
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
        counted — a take-profit at an unreachable price must not be able to block a stop; an
        exit at market cancels it instead (`submit_exit`).
        """
        return any(
            order.instrument_id == instrument_id and order.side == side  # type: ignore[attr-defined]
            for order in self._open_markets()
        )

    def _consult_exit(self, instrument_id: InstrumentId) -> None:
        """Ask the attached exit rules whether to close the position, and close it if one does.

        A rule that closes sends the whole position at market through `submit_exit`, which
        cancels the sleeve's own resting orders on the closing side first — a take-profit
        above the market, an exit following the ask. With no latency stated the cancels land
        before anything further is matched and the whole position goes; under a stated
        latency the cancelled orders can still fill until their cancels land, so the close
        is what they leave, and the rest is owed and sent once they have landed, whether or
        not the rule says so again.
        """
        # The warming guard is unreachable on a flat start — no position exists in the
        # prefix — and stands for a run that carries or restores a book across the open.
        if self._exiting or not self.is_running or self._warming():
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
        registry is asked before any scan or context is built. While the sleeve is warming
        the overlay is still asked, so a clock of its own warms with the host's, and its
        answer is dropped before any leg is built — a clip built and then refused would
        sit on the clip ledger for the rest of the run.
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
                if self._warming():
                    continue
                for order in self._legs_of(modifier, decision):
                    self._submit_leg(order)
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
        self._book_withheld = False

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

    # --- no book of its own under `depth` -----------------------------------

    def subscribe_order_book_deltas(self, *args: Any, **kwargs: Any) -> None:
        self._refuse_book("subscribe_order_book_deltas")
        super().subscribe_order_book_deltas(*args, **kwargs)

    def subscribe_order_book_at_interval(self, *args: Any, **kwargs: Any) -> None:
        self._refuse_book("subscribe_order_book_at_interval")
        super().subscribe_order_book_at_interval(*args, **kwargs)

    def subscribe_order_book_depth(self, *args: Any, **kwargs: Any) -> None:
        self._refuse_book("subscribe_order_book_depth")
        super().subscribe_order_book_depth(*args, **kwargs)

    def _refuse_book(self, route: str) -> None:
        """Refuse a book of the construct's own when the hypothesis declares `depth`.

        The host is handed only the view of a book an account would see; a construct that
        decides the host's orders is handed none, and asking for one is refused rather than
        answered with every change. The runner sets `_book_withheld` from the hypothesis.
        The engine's subscriptions are `cpdef` (nautilus_trader 1.231.0), and an author's
        call reaches this Python override.
        """
        if self._book_withheld:
            raise ValidationError(
                f"{type(self).__name__}.{route}: under `depth` an attached construct is handed "
                "no book of its own, because its host sees only the view an account would; "
                "read the host's prices from the context `evaluate` and `on_data` are asked with",
                remedy="remove the subscription from strategy.py",
            )

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
