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
overriding `submit_order` and `submit_order_list`. Every call on the strategy that reaches
the venue goes through one of the two — the cost-aware helpers below, an order the author
built by hand, and the engine's own `close_position` / `close_all_positions`, which
construct a market order and submit it through `submit_order`. An order that bypasses the
helpers is still on the record. One that bypasses the overrides is not: the engine's own
`submit_order` or `submit_order_list` called on its class, as `Strategy.submit_order(self,
order)`, or through `super(KansoStrategy, self)`, records no intent and is asked nothing.
`strategy_integrity` refuses a researched strategy that reaches a method this class
overrides that way as it is spelt (`kanso.criteria.integrity`; what it does not follow is
`docs/backlog.md` row 211), and a book refuses the order itself before the engine routes it
(below).

**It sizes.** The cost-aware helpers `submit_entry` and `submit_exit` choose a quantity
from the capital and the risk limits injected from the hypothesis, leaving room for the
round-trip cost the runner will apply. This is where per-strategy exposure is enforced,
because the engine has nowhere else to enforce it: `RiskEngineConfig` offers exactly one
limit, `max_notional_per_order` keyed by instrument, which is a per-order backstop and
knows nothing of a position, a strategy or a book. An entry the author built is refused at
`submit_order` when the book cannot fund it, and `cancel_order`, `cancel_orders` and
`cancel_all_orders` are kanso's own so that an exit owed or a cancel held back is kept.
`modify_order` is not held to the room or to what the book funds. For a sleeve run alone
kanso's `modify_order` hands the modify to the engine's own unchanged, so a modify that grows
an entry's quantity, or moves a resting entry to a price at which it opens more, goes out as
it is; the room reads the order at its new size and price from its next read on. A sizing
rule denies a researched strategy `modify_order` (`kanso.criteria.integrity`); an unsized
sleeve alone may call it (`docs/backlog.md` row 123). In a book it is refused.

**It counts its orders, and holds itself to its account's rate.** Every command it sends —
a submit per order, a modify, a cancel per order — is counted on the data clock
(`kanso.nautilus.rate`), and a run reports the busiest 60 s beside its fills. Under a venue
model that states `costs.orders_per_minute` each command takes its tokens from a bucket of
that many, refilled at that many a minute on the data clock, before it goes — a submit and a
modify one, a cancel the model's `requests_per_cancel`; with the bucket empty an entry — every
order of an entry's list — and a modify are dropped unsent and counted as refused, and the run
goes on past them (`_drop_rate`); any other exit is owed and a cancel is deferred; and an exit
kanso cannot send again as it was asked, a list that closes among them, is refused
(`order_rate`, the same `Refusal` a sizing rule raises). Whether an order is an entry or an
exit there is read against the position its market orders in flight and its exits owed at
market leave (`_opens`), so the second leg of a flip is an entry. The deferred cancels and
then the owed exits are sent, oldest first, before the handler of the first point that finds
a token for them, and while a cancel waits no submit or modify takes a token
(`_command_spend`). An order the engine was never handed — one dropped so — is nothing to
cancel or to move, and its cancel or modify is no command (`_never_sent`). It is the same
code on every path, so a card, a replay and a node — a stage's among
them — hold one strategy to one rate and drop the same entries; a stage node's flatten after
its window lifts it (`_lift_rate`). Unstated, nothing is held back or dropped and nothing a
run does changes.

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
is classified as an entry or an exit by its effect on the sleeve's own net position: an
order that shrinks it in absolute terms is an exit, anything else is an entry.

**A sleeve's position is its own, never its account's.** Every reader the harness sizes,
classifies and exits by — `held`, `submit_exit`, an owed exit, `ctx.book` — sums the
positions the cache keeps for this strategy in the name, so two versions on one stage that
trade the same name each exit what they hold and nothing of the other's.

**In a book its entries are the book's to place.** A runner that puts a sleeve in a book
(`kanso.nautilus.book`) attaches the book's ledger to it: `submit_entry` collects the entry
and returns `None`, the sleeve tells the ledger of every marker it handles, and once every
member has handled the one that closes the instant the ledger calls `submit_entry` again for
each entry, in the book's order, with `data_time` the entry's own; the entry is sized by the
sleeve's own rule, then cut to what the ledger leaves (`BookLedger.cut`). Its exits are its
own, sized by `submit_exit` never past flat as ever. **A member sends orders only through
`submit_entry` and `submit_exit`**, and may cancel. Its other orders and modifies are refused
(`book_entry`), naming the member, before the engine routes them, where a layer can see
them. The sleeve refuses at its own methods (`_check_booked`): `submit_order`,
`submit_order_list` and `modify_order` called by it, a leg an overridden `hedges()` returns,
and the engine's `close_position`, which sends through `submit_order`. The ledger refuses on
the bus, as the engine hands it the order's `OrderInitialized` or the modify's
`OrderPendingUpdate`, what reaches the engine's own methods past the sleeve's: called on its
class or through `super(KansoStrategy, self)` (`BookLedger.heard`, which asks
`_check_booked_event` with the member that was running, `BookLedger.running`, which the
sleeve marks while it handles an instant's points, an order event and `on_start`). The one
order let through is the one `_submitted` is sending for the two: recognised as that very
object at its check, and let through to the engine once, at its own `OrderInitialized`. An
order the sleeve builds itself and sends from a handler the engine runs inside that
submission — `on_order_submitted` — is refused like any other. What neither sees — a command
or a fill sent on the bus, an event applied to an order, an order handed to `_submitted`
itself — only `strategy_integrity` refuses, in a researched strategy, by the names each is
spelt with (`kanso.criteria.integrity`); reached by a spelling it does not follow, such as a
name held in a string through a module an allowed import hands out, each is refused by
nothing (`docs/backlog.md` row 211).

**It shows a `depth` sleeve the book its account would see.** Under the hypothesis's `depth`
every change of a level-two book still reaches both venues, but the author is handed only
the top levels as they stood on a grid, at the first point after each grid instant, and
level one on every instant the top moved, from a copy of the book the harness keeps itself
by the engine's level-two rules (`kanso.nautilus.facts` checks them). Without the key the
author is handed every change, as the venue is.

Engine facts this module relies on (nautilus_trader 1.231.0): `Strategy` and `Actor` methods
are `cpdef`, so a Python subclass's `handle_bar`, `handle_quote_tick`, `handle_trade_tick`,
`handle_order_book_deltas`, `handle_data`, `submit_order`, `submit_order_list`,
`modify_order`, `cancel_order`, `cancel_orders`, `cancel_all_orders`, `_start` and `_stop` all
take precedence when the engine calls them, and called on the engine's class they are the
engine's own, past the subclass's; an order's `OrderInitialized`, every order's of a list,
and a modify's `OrderPendingUpdate` are published on the bus before the engine routes the
command, so a subscriber that raises sends nothing (`kanso.nautilus.facts` measures it at no
latency and at 20 ms); `Trader` starts actors before strategies, so a
modifier is registered before its host runs; `close_position` and `close_all_positions`
route through `submit_order`; under the NETTING OMS the cache keeps one position per
(instrument, strategy), which `cache.positions_open(instrument_id=…, strategy_id=…)`
answers, and `portfolio.net_position(instrument_id)` is the signed `Decimal` sum of every
strategy's on the account, so the sleeve's own position is read from the first and never
from the second (`kanso.nautilus.facts` measures it); an order whose cancel was sent is not
`is_closed` and can still fill until the cancel lands — under a latency model after the next
point is matched, with none before it (`kanso.nautilus.facts` measures both on the backtest
engine, and the replay tests hold the node to the same fills); `cancel_all_orders` marks
every order open at the venue `PENDING_CANCEL` at once and cancels one in flight to it as
well (`kanso.nautilus.facts` measures it on the backtest engine), and skips one still
`INITIALIZED` (read in `trading/strategy.pyx`), which on the backtest engine an order handed
to `submit_order` never is when the call returns; a cancel sent for an order a node has not
yet handed to the venue reaches the venue before the order, where it is lost and the order
rests (read in `live/risk_engine.py`, `execution/manager.pyx` and `trading/strategy.pyx`),
which is why kanso holds such a cancel back until the node has handed the order over
(`KansoStrategy._hold_cancel`); `Strategy.cancel_orders` sends a `BatchCancelOrders` and
refuses one whose orders are in more than one instrument, or that holds an emulated order
after its first, after marking the orders before it `PENDING_CANCEL` (read in
`trading/strategy.pyx`); the order emulator takes an emulated order out and marks it pending
cancel locally before `cancel_order` returns (read in `execution/emulator.pyx`, measured on
both paths by the exit and replay tests); `Order.events` hands back a new list of every
event the order holds, where `event_count` and `last_event` read its length and its last
element without one, and an order's events grow only at its end, each handed to the
strategy's `handle_event` once the order has taken it (`kanso.nautilus.facts` measures it on
the backtest engine); `StrategyConfig` and `ActorConfig` are frozen msgspec structs whose
subclasses inherit the freeze, and the engine defines no `config_cls` — `config_cls` here is
kanso's own attribute, honoured by kanso's loader alone.
"""

from __future__ import annotations

from bisect import bisect_left, insort
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from functools import cache
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
    ContingencyType,
    LiquiditySide,
    OrderSide,
    OrderStatus,
    OrderType,
    PriceType,
    RecordFlag,
    TimeInForce,
    TriggerType,
    order_side_to_str,
    order_type_to_str,
)
from nautilus_trader.model.events import OrderFilled, OrderInitialized, OrderPendingUpdate
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from kanso.criteria.run import NS_PER_DAY, day_of
from kanso.errors import ValidationError
from kanso.nautilus import actions, splits
from kanso.nautilus.costs import (
    BookPolicy,
    at_touch,
    carry,
    fill_cost,
    fixed_half_spread,
    funding_payment,
    limit_at,
    month_turned,
    quote_half_spread,
    reset,
    tick_slip,
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
    any_attached,
    deregister_modifier,
    modifiers_for,
    register_modifier,
)
from kanso.nautilus.rate import CANCEL, MODIFY, SUBMIT, Bucket, Tally
from kanso.nautilus.sizing import (
    BOOK_ENTRY,
    BUDGET_BELOW_LOT,
    CLIP_DIRECTION,
    CLIP_WITHOUT_BUDGET,
    HAND_BUILT_ORDER,
    HEDGE_UNDER_SIZING,
    ONE_CLIP,
    ONE_POSITION,
    ORDER_RATE,
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


@cache
def _id_of(key: str) -> InstrumentId:
    """The instrument id a name parses to, parsed once and kept.

    Every point asks the cache what this sleeve holds in its name, and an `InstrumentId`
    hashes by formatting its name the first time it is hashed (`model/identifiers.pyx` of
    nautilus_trader 1.231.0): one object a name keeps that hash, where one parsed a point
    formatted the name again for every lookup. An id is a value, so which object asks
    changes no answer.
    """
    return InstrumentId.from_str(key)


@cache
def _name_of(instrument_id: InstrumentId) -> str:
    """The name an instrument id formats to, formatted once per id and kept.

    `str` of an `InstrumentId` formats its name afresh on every call, where hashing one
    formats it once and keeps the hash on the object (`model/identifiers.pyx` of
    nautilus_trader 1.231.0), so the ids a sleeve keeps — its `universe` — find their names
    here for a lookup. Asked of an `InstrumentId` alone: one compared with any other key
    raises `TypeError` in the engine's `__eq__`, and its hash is its name's.
    """
    return str(instrument_id)


def _same(kept: float | None, read: float, reader: str, key: str) -> float:
    """`read`, once it is the bit-for-bit value a kept read of the same name holds, where one
    is held (`KansoStrategy._recheck_kept`)."""
    if kept is not None and kept.hex() != read.hex():
        raise AssertionError(
            f"{reader}({key!r}) kept {kept!r} where it reads {read!r} now: something it reads "
            "changed and did not forget the reads kept (KansoStrategy._forget_kept)"
        )
    return read


def _signed(order: object, *, filled: bool) -> float:
    """An order's filled quantity, or what is left of it, signed by its side."""
    qty = float(order.filled_qty if filled else order.leaves_qty)  # type: ignore[attr-defined]
    return float(-qty if order.side == OrderSide.SELL else qty)  # type: ignore[attr-defined]


def _entry_units(qty: float | None, notional: float | None, room: float, unit: float) -> float:
    """What an unsized entry asks for in units: the quantity asked for, cut to the room and to
    the notional asked for, `unit` being one unit's notional.

    A quantity they leave whole is that quantity, never its notional divided back by the unit:
    1,000 x 18.007 / 18.007 is 999.9999999999999 in floats, which the lot floor makes 999.
    One they cut is the cut notional over the unit, floored like any other.
    """
    cap = room if notional is None else min(room, abs(notional))
    if qty is not None and abs(qty) * unit <= cap:
        return abs(qty)
    return cap / unit


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


def _never_rests(order: Any) -> bool:
    """Whether an order is a market order, whose own cancel kanso never sends or holds.

    On the top-of-book venue, every hypothesis that does not require `book`, a market order
    cannot rest: the venue answers it where it lands, filling it, walking its rest one
    increment past the top of the book, or refusing it for want of a market, and a cancel sent
    after it — on the backtest engine in the same handler or later, under a latency stamped
    after it — lands only behind that answer, so it cancels nothing. What it does do is mark
    the order `PENDING_CANCEL` where the sleeve sent it, and in nautilus_trader 1.231.0 the
    venue rejects a market order it finds nothing to fill only while it is still `SUBMITTED`
    (`OrderMatchingEngine.apply_fills` in `backtest/engine.pyx`): a refused market order whose
    cancel had been sent was left neither rejected nor filled, counted as working for good, so
    the exits sized after it came short. On a node the cancel held back for an order still in
    the risk engine's queue (`_hold_cancel`) is sent when the order is reported `SUBMITTED`,
    before the venue matches it, so the same order parted the two paths there. Measured on
    both paths by the replay tests, a market order sent with a cancel of it or of its name
    under the print rules, which refuse one routinely.

    On a level-two book a market order deeper than the book keeps its rest open, partly
    filled, and nothing fills it later. A cancel of the order itself would not reach it: the
    engine looks a cancel's order up among those resting in its matching core, which a market
    order's rest is not, and rejects the cancel; its cancel of a name, which
    `cancel_all_orders` sends, cancels every order the cache holds open in it, that rest
    included (`OrderMatchingEngine.process_cancel` and `process_cancel_all` in
    `backtest/engine.pyx` of nautilus_trader 1.231.0). So `cancel_all_orders` leaves a market
    order to that command. Measured on both paths in
    `tests/nautilus/backtest/test_market_order_rest.py`."""
    return bool(order.order_type == OrderType.MARKET)


def _owable(order: Any) -> bool:
    """Whether `submit_exit` can send an exit the strategy built itself again as it was asked:
    one order on the terms `KansoStrategy._order` builds one on, since `submit_exit` sends no
    other. That is an order at market or at a limit, good till cancelled, not post-only, in
    the instrument's own units, showing all of itself, with no contingency, no emulation
    trigger and no execution algorithm: in nautilus_trader 1.231.0 `OrderFactory.market` and
    `OrderFactory.limit` default every one of those (`common/factories.pyx`), and kanso passes
    none. An immediate-or-cancel exit owed and sent again so would rest, a post-only one take,
    a good-till-date one outlive its date. Reduce-only is admitted: `submit_exit` never sends
    past flat, which is all that flag asks of an exit."""
    return bool(
        order.order_type in (OrderType.MARKET, OrderType.LIMIT)
        and order.time_in_force == TimeInForce.GTC
        and not order.is_post_only
        and not order.is_quote_quantity
        and getattr(order, "display_qty", None) is None
        and order.exec_algorithm_id is None
        and order.contingency_type == ContingencyType.NO_CONTINGENCY
        and order.emulation_trigger == TriggerType.NO_TRIGGER
    )


def _modify_sends(order: Any, quantity: Any, price: Any, trigger_price: Any) -> bool:
    """Whether the engine's `modify_order` would send a modify of `order` at all.

    In nautilus_trader 1.231.0 `Strategy._create_modify_order` sends nothing, and logs why,
    for a modify that changes nothing — every one of quantity, price and trigger price `None`
    or the order's own — and for an order closed or already `PENDING_CANCEL`
    (`trading/strategy.pyx`). A price or a trigger price the order does not carry is the
    engine's to refuse, by raising, so it is read as a change here."""
    changes = (
        (quantity is not None and quantity != order.quantity)
        or (price is not None and price != getattr(order, "price", None))
        or (trigger_price is not None and trigger_price != getattr(order, "trigger_price", None))
    )
    return bool(changes and not order.is_closed and not order.is_pending_cancel)


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

    _recheck_kept: ClassVar[bool] = False
    """Whether every position read kept is read again and compared (`_forget_kept`)."""

    def __init__(self, config: KansoConfig | None = None) -> None:
        resolved = self.config_cls() if config is None else config
        if not isinstance(resolved, KansoConfig):
            raise ValidationError(
                f"config: {type(resolved).__name__} configures a sleeve but does not subclass "
                "KansoConfig; a sleeve is an engine strategy and needs a strategy config"
            )
        super().__init__(resolved)
        self._cfg: KansoConfig = resolved
        self._kept_universe: tuple[InstrumentId, ...] | None = None
        self._kept_names: dict[InstrumentId, str] = {}
        """The name each id of the kept `universe` formats to (`held`)."""
        self._kept_open: list[Any] | None = None
        """This sleeve's open positions as the cache lists them, kept (`_kept_positions`)."""
        self._kept_settled = False
        """Whether every order the balance reads has been folded in since anything it folds
        last changed (`_settle`)."""
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
        self._pending: deque[tuple[object, InstrumentId | None, str | None]] = deque()
        """The points held for a flush marker, each with the id and the name its handler read
        (`None` for a point that is neither a quote nor a print)."""
        self._overlay_due: tuple[InstrumentId, Bar | None, float | None] | None = None
        self._entry_answers: tuple[tuple[object, Decision], ...] | None = None
        self._clip_orders: dict[str, list[object]] = {}
        self._kept_filled: dict[str, float] = {}
        self._kept_intended: dict[str, float] = {}
        self._sizing_call = False
        self._built = False
        self._owed: dict[tuple[str, OrderSide], tuple[float | None, float | None, bool]] = {}
        self._cancels: dict[object, bool] = {}
        self._unsent: dict[object, tuple[Any, Any, dict[str, object] | None]] = {}
        self._behind_modify: dict[object, Any] = {}
        self._awaiting: set[object] = set()
        self._charges = _charges(resolved)
        self._command_tally = Tally()
        """Every order command this sleeve sent, counted on the data clock (`_command_spend`)."""
        rate = self._charges.get("orders_per_minute")
        self._command_bucket: Bucket | None = None if rate is None else Bucket(int(rate))
        """The account's rate as a token bucket, when the venue model states one."""
        self._cancel_tokens = int(self._charges.get("requests_per_cancel") or 1)
        """The tokens a cancel of one order takes: the requests the broker's client sends for
        one, a submit and a modify taking one each."""
        self._command_ns = 0
        """The data clock the tally and the bucket read: `data_time`, never run backwards."""
        self._command_deferred: dict[object, tuple[Any, Any, dict[str, object] | None]] = {}
        """The cancels the bucket had no token for, oldest first, by client order id, each
        with what it was sent with (`_defer_cancel`)."""
        self._rate_owed: dict[tuple[str, OrderSide], None] = {}
        """The exits owed because the bucket held no token for them, oldest first: they have
        the first claim on a token that comes back (`_pay_rated`)."""
        self._command_closing: tuple[float | None, float | None] | None = None
        """What `_close` asked for, as the next `submit_order` sends it: the exit an empty
        bucket owes rather than refuses (`_short_of_rate`)."""
        self._kept_quotes = self._charges.get("spread") == "quotes"
        """Whether the spread is the quoted one, which a quote of a name settles that name for."""
        self._kept_day: tuple[int, date] | None = None
        """The last UTC day a scoped point fell on, and its index from the epoch
        (`_out_of_scope`)."""
        self._cash = resolved.capital
        self._ledger: dict[object, _Tracked] = {}
        self._print_ns: dict[str, int] = {}
        self._scope_days: dict[str, set[date]] = {}
        self._scope_cls: type | None = None
        self._price_ns: dict[str, int] = {}
        self._restated: dict[str, list[splits.Split]] = {}
        self._levelled = BOOK in resolved.data_requirements
        self._refs: dict[object, tuple[float, float] | None] = {}
        self._open_fills: dict[object, Any] = {}
        self._booked_open: dict[object, float] = {}
        self._instant_ns = 0
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
        self._book_ledger: Any = None
        """The book this sleeve is a member of, when a runner put it in one
        (`kanso.nautilus.book.BookLedger`); `None` for a sleeve run alone."""
        self._sending: Any = None
        """The order `_submitted` is sending, until the engine hands the bus its
        `OrderInitialized` and a book lets it through, once (`_check_booked_event`)."""

    # --- what the hypothesis injected ---------------------------------------

    @property
    def kanso_config(self) -> KansoConfig:
        """The injected configuration, typed."""
        return self._cfg

    @property
    def universe(self) -> tuple[InstrumentId, ...]:
        """The instruments this hypothesis trades, as the engine identifies them.

        Parsed on the first read and kept. The names are the injected configuration's, a
        frozen struct the sleeve is built with and never handed again, so they cannot change
        under it; and an id is a value, so the same objects answer every read where each
        read used to parse twenty names again — measured on 2026-10-22, a tick sleeve that
        walked its universe on every point spent 7% of its card parsing them. A name that
        does not parse is kept as nothing and raises on every read, as before.
        """
        held = self._kept_universe
        if held is None:
            held = self._kept_universe = tuple(_id_of(value) for value in self._cfg.universe)
            self._kept_names = {instrument_id: _name_of(instrument_id) for instrument_id in held}
        return held

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

    def cost_rate_at(self, price: float, multiplier: float = 1.0, increment: float = 0.0) -> float:
        """`cost_rate` at a price: the per-share commission, where the model states one, the
        model's `slippage_ticks` of the instrument's price `increment`, and half the per-share
        sell fee are fractions of notional only once the price is known, and a dearer share
        pays less. An order does not know whether it will rest, so where the model states a
        maker's charge per share the larger of it and what a taker pays a share — the
        commission and the ticks, whole, since an order sized at market has no limit to cap
        them — is the one reserved, as the larger rate is in `cost_rate`; a rebate reserves
        nothing of its own. On a multiplied instrument per share means per contract, and one
        contract's notional is `price x multiplier`, so the fraction is the charge over that,
        a tick on a contract being worth the increment times the multiplier."""
        per_share = float(self._charges.get("commission_per_share") or 0.0)
        ticks = float(self._charges.get("slippage_ticks") or 0.0)
        per_share += ticks * increment * multiplier
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

        The reader a strategy sizes and flips by, on both paths: this sleeve's own position
        — never the account's, which a stage-mate in the same name shares — less what the
        attached overlays hold as clips in the same name, plus what the sleeve's own market
        orders not yet filled will add. An exit at market submitted in
        this handler already reads as gone, which is what lets the entry that follows it
        size to the room the exit frees. A limit or stop order is not applied, whether it
        rests at the venue or is still in flight to it: a sleeve whose exit rests at the ask
        reads the whole position until the exit fills. `submit_exit` is what counts the
        exits still working, so an exit sized from this reader never closes more than they
        leave.

        A read is kept until anything it reads changes (`_forget_kept`), so a sleeve may ask
        it of every name on every point. One of the sleeve's own `universe` ids finds its
        name in a table built with the ids, and a kept read is answered here: measured on
        2026-10-26, a tick sleeve asked this about fourteen times a point.
        """
        if type(instrument_id) is InstrumentId:
            key = self._kept_names.get(instrument_id)
            if key is None:
                key = _name_of(instrument_id)
        else:
            key = str(instrument_id)
        kept = self._kept_intended.get(key)
        if kept is None or self._recheck_kept:
            return self._own_intended(key)
        return kept

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
        self._kept_settle()
        worth = sum((self._worth(position) for position in self._kept_positions()), 0.0)
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
        first (`_refund`), so the period closes on the book the runner closes it on, and before
        that every fill of an earlier instant is let go as struck (`_struck`): no quote of its
        instant is still to come.
        """
        if ts_ns > self._instant_ns:
            if self._open_fills:
                self._struck(ts_ns)
            else:  # nothing to let go: what `_struck` does then
                self._instant_ns = ts_ns
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
        self._forget_kept()
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
        ledger = self._book_ledger
        if ledger is not None:
            ledger.running.append(self)
        try:
            super()._start()
        finally:
            if ledger is not None:
                ledger.running.pop()

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
        ts = int(ts_init)
        held = self._kept_day
        if held is None or held[0] != ts // NS_PER_DAY:  # a day's points ask the day once
            held = self._kept_day = (ts // NS_PER_DAY, day_of(ts))
        return held[1] not in self._scope_days.get(key, ())

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
        key = bar.bar_type.instrument_id.value
        if self._undelivered(bar.ts_init) or self._out_of_scope(key, bar.ts_init):
            return
        self._show_book(int(bar.ts_init))
        self._turn(int(bar.ts_init))
        self._delivered_ns = int(bar.ts_init)
        self._printed(key, float(bar.close), int(bar.ts_event), int(bar.ts_init))
        if not self._is_extra_bar(bar):
            self._last_bar[key] = bar
            self._observe_price(key, float(bar.close), int(bar.ts_event))
        if self._held():
            self._pending.append((bar, None, None))
            return
        self._dispatch_bar(bar)
        self._consult_due()

    def handle_quote_tick(self, tick: QuoteTick, historical: bool = False) -> None:
        if historical:
            super().handle_quote_tick(tick, historical)
            return
        # The point's id, its name and its instants are read once and handed on: a tick feed
        # sends a quote and its flush marker on every point, and in nautilus_trader 1.231.0
        # `InstrumentId.value` formats the name afresh on every read. What each step does is
        # what the helper it stands for does (`_undelivered`, `_show_book`, `_printed`,
        # `_observe_price`, `_quoted`, `_held`), in the same order.
        ts_init = int(tick.ts_init)
        instrument_id = tick.instrument_id
        key = instrument_id.value
        if ts_init < self._fed_from_ns or self._out_of_scope(key, ts_init):
            return
        self._show_book(ts_init)  # as before: under `depth` no quote is loaded
        self._turn(ts_init)
        self._delivered_ns = ts_init
        self._last_quote[key] = tick
        bid, ask = float(tick.bid_price), float(tick.ask_price)
        mid = (bid + ask) / 2.0
        ts_event = int(tick.ts_event)
        self._last_print[key] = mid
        self._print_ns[key] = ts_event
        mark = self._mark_at.get(key)
        if mark is None or ts_init > mark[0] or (ts_init == mark[0] and mid > mark[1]):
            self._mark_at[key] = (ts_init, mid)
        self._last_price[key] = mid
        self._price_ns[key] = ts_event
        if self._open_fills:
            self._quoted(key, ts_init, (bid, ask))
        if self._kept_quotes:
            self._settle(key)
        if self._hold_until_cross_section and not self._flushing:
            self._pending.append((tick, instrument_id, key))
            return
        self._dispatch_quote(tick, instrument_id, key)
        self._consult_due()

    def handle_trade_tick(self, tick: TradeTick, historical: bool = False) -> None:
        if historical:
            super().handle_trade_tick(tick, historical)
            return
        ts_init = int(tick.ts_init)  # read once and handed on, as `handle_quote_tick` does
        instrument_id = tick.instrument_id
        key = instrument_id.value
        if ts_init < self._fed_from_ns or self._out_of_scope(key, ts_init):
            return
        if self._cfg.depth is not None:
            self._show_depth(ts_init)
        self._turn(ts_init)
        self._delivered_ns = ts_init
        self._last_trade[key] = tick
        price = float(tick.price)
        ts_event = int(tick.ts_event)
        self._printed(key, price, ts_event, ts_init)
        self._last_price[key] = price
        self._price_ns[key] = ts_event
        if self._hold_until_cross_section and not self._flushing:
            self._pending.append((tick, instrument_id, key))
            return
        self._dispatch_trade(tick, instrument_id, key)
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
            ledger = self._book_ledger
            if ledger is None:  # a sleeve run alone: what the book's branch does without one
                if self._pending:
                    self._flush_one()
                if not self._pending:
                    self._consult_due()
                return
            ledger.running.append(self)
            try:
                if self._pending:
                    self._flush_one()
                if not self._pending:
                    self._consult_due()
                ledger.marked(self, data)
            finally:
                ledger.running.pop()
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
            self._pending.append((data, None, None))
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
        """Dispatch the point the oldest buffered handler holds, with the id and the name its
        handler read: a bar, a quote or a print, each by its kind as an `isinstance` tells it
        (a quote's and a print's exact type asked first, which no bar is), or anything else.
        A quote or a print held by `handle_data`, which reads no id, has its id read here."""
        event, instrument_id, key = self._pending.popleft()
        self._flushing = True
        try:
            kind = type(event)
            if kind is QuoteTick:
                quote = True
            elif kind is TradeTick:
                quote = False
            elif isinstance(event, Bar):
                self._dispatch_bar(event)
                return
            elif isinstance(event, QuoteTick):
                quote = True
            elif isinstance(event, TradeTick):
                quote = False
            else:
                self._dispatch_data(event)
                return
            if instrument_id is None:
                instrument_id = event.instrument_id  # type: ignore[attr-defined]
            if quote:
                self._dispatch_quote(event, instrument_id, key)
            else:
                self._dispatch_trade(event, instrument_id, key)
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
        if due is not None and any_attached():  # `_consult_overlay` asks this first
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

    def _dispatch_quote(
        self, tick: QuoteTick, instrument_id: InstrumentId, key: str | None
    ) -> None:
        """Hand a quote to the author, then ask for the exits owed and the exit rules, with the
        id its handler read and its name, or `None` for the name to be read when it is asked
        for. The cancels held behind a modify or deferred for a token, and the exits an empty
        bucket owed, are sent and the owed exits asked for only when there are any, which is
        what `_send_behind_modify` and `_pay_owed` ask first."""
        self._data_time = int(tick.ts_event)
        if self._behind_modify or self._command_deferred or self._rate_owed:
            self._send_behind_modify()
        super().handle_quote_tick(tick, False)
        if self._owed:
            self._pay_owed()
        self._consult_exit(instrument_id, key)
        if not self._cfg.extra_resolutions:
            self._overlay_due = (instrument_id, None, None)

    def _dispatch_trade(
        self, tick: TradeTick, instrument_id: InstrumentId, key: str | None
    ) -> None:
        """As `_dispatch_quote`, for a print."""
        self._data_time = int(tick.ts_event)
        if self._behind_modify or self._command_deferred or self._rate_owed:
            self._send_behind_modify()
        super().handle_trade_tick(tick, False)
        if self._owed:
            self._pay_owed()
        self._consult_exit(instrument_id, key)
        if not self._cfg.extra_resolutions:
            self._overlay_due = (instrument_id, None, None)

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

        The order is kept as this sleeve's (`_track`) before the engine is handed it, so a
        handler the engine runs inside the call sizes on it as working and in flight. In
        nautilus_trader 1.231.0 the backtest engine's execution client reports an order
        submitted inside the call that hands it over (`BacktestExecClient.submit_order` in
        `backtest/execution_client.pyx`), so `on_order_submitted` runs inside this one, where a
        node's live risk engine queues the command and the event comes once the call has
        returned. Kept after the call, an exit asked again from `on_order_submitted` did not
        count the first: a sleeve that asked so for the exit at market of its long of 1000 sold
        2000. Both paths now size it alike.

        Once every check has let it through it takes a token of the venue model's
        `orders_per_minute`, when one is stated, and is counted as a submit (`_command_spend`); with
        the bucket empty it is not sent: an entry is dropped and counted as refused, and its
        hedges are not placed; an exit kanso can send again as `submit_exit` would is owed;
        and any other exit is refused (`_short_of_rate`).
        """
        closing, self._command_closing = self._command_closing, None
        if self._warming():
            return
        self._check_booked(order, "submit_order")
        self._check_hand_built(order)
        kind = self.classify(order)
        if kind == ENTRY and not (self.sized or self._built or self._sizing_call):
            self._check_funded((order,))
        ctx = self._context_for(order)
        if kind == ENTRY and not self._hedging and not self.before_entry(ctx):
            return
        if not self._command_spend(SUBMIT):
            self._short_of_rate(order, kind, closing)
            return
        self._intents.append(self._intent(order))
        self._track(order)
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
        """As `submit_order`, classifying and filtering the list by its first order. In a book
        it is refused (`_check_booked`): kanso sends no list. Under a stated `orders_per_minute`
        it takes a token for each of its orders, all or none, and with fewer in the bucket it is
        not sent: a list whose first order is an entry — read as `submit_order` reads one there,
        against the position the sleeve's orders on their way leave (`_opens`) — is dropped
        whole and each of its orders counted as refused, as an entry is; one whose first order
        is an exit is refused, since kanso owes an exit only as `submit_exit` sends one, and an
        order of a list is not."""
        if self._warming():
            return
        orders = list(order_list.orders)  # type: ignore[attr-defined]
        self._check_booked(orders[0], "submit_order_list")
        self._check_hand_built(orders[0])
        kind = self.classify(orders[0])
        if not (self.sized or self._built or self._sizing_call):
            self._check_funded(orders)
        ctx = self._context_for(orders[0])
        if kind == ENTRY and not self._hedging and not self.before_entry(ctx):
            return
        if not self._command_spend(SUBMIT, len(orders)):
            if kind == ENTRY or self._opens(orders[0]):
                self._drop_rate(SUBMIT, len(orders))
                return
            self._refuse_rate(
                orders[0].instrument_id.value,
                order_side_to_str(orders[0].side),
                f"a list of {len(orders)} orders closing a position",
                len(orders),
            )
        for order in orders:
            self._intents.append(self._intent(order))
            self._track(order)
        super().submit_order_list(order_list, position_id, client_id, params)
        if kind == ENTRY and not self._hedging:
            self._place_hedges(ctx)

    def classify(self, order: object) -> str:
        """`entry` or `exit`, by the order's effect on this sleeve's own net position.

        An order that shrinks the absolute net position is an exit; one that opens a
        position, grows it, or flips past flat into a larger opposite one is an entry. The
        position is the sleeve's own (`_own_position`), so a stage-mate's holding in the
        same name decides nothing.
        """
        key = order.instrument_id.value  # type: ignore[attr-defined]
        if not self.sized:
            net = float(self._own_position(order.instrument_id))  # type: ignore[attr-defined]
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

    def _check_booked(self, order: Any, call: str) -> None:
        """Refuse, in a book, an order this sleeve sends by anything but `submit_entry` and
        `submit_exit`, at the call of kanso's that it was sent by.

        A book hands each entry of an instant its share of one room in its own order, and an
        exit is the book's only through `submit_exit`, which sizes it never past flat. So at
        kanso's methods a member's orders go out only through the two (`kanso.nautilus.book`
        says what reaches the venue past them): the one order let through is the one
        `_submitted` is sending for them, recognised as that very object,
        and let through to the engine once, as the engine hands the bus its `OrderInitialized`
        (`_check_booked_event`). kanso sends no list and modifies no order,
        so `submit_order_list` and `modify_order` are refused whatever they hold, and so is a
        leg an overridden `hedges()` returns. Cancelling is not refused: a cancel takes nothing
        past the room or past flat. A sleeve run alone is not asked.
        """
        ledger = self._book_ledger
        if ledger is None:
            return
        if call == "submit_order" and order is self._sending:
            return
        if call == "submit_order" and self._hedging:
            call = "hedges()"
        verb = "modifies an order" if call == "modify_order" else "sends an order"
        self._refuse_booked(order.instrument_id, order.side, f"{call} {verb}")

    def _check_booked_event(self, event: Any, sender: Any = None) -> None:
        """Refuse, in a book, an order or a modify of this sleeve's that kanso is not sending,
        before the engine routes it; the book asks it of every event of the sleeve's orders
        (`BookLedger.heard`), with the member it was sent while (`sender`, the member whose
        handler the engine was running, or `None` where the book cannot tell).

        The engine's own methods can be reached past kanso's — called on the engine's class,
        as `Strategy.submit_order(self, order)`, or through `super(KansoStrategy, self)` — and
        then kanso's overrides never see the order. The engine still hands the bus the order's
        `OrderInitialized` before it routes the order, for one order and for every order of a
        list, and a modify's `OrderPendingUpdate` before it routes the modify, and the book
        hears both there ahead of the sleeve's own handlers; a refusal raised then leaves the
        order unsent and the order at the venue as it was (`kanso.nautilus.facts`, at no
        latency and at 20 ms). So the one `OrderInitialized` let through is the one for the
        order `_submitted` is sending, once, and not while another member runs; every
        `OrderPendingUpdate` is refused, since kanso modifies no order. An order the member
        builds itself and sends from a handler the engine runs inside one's submission —
        `on_order_submitted` among them — is refused like any other, since its own
        `OrderInitialized` comes after the one let through. The engine sends a modify of an
        emulated order with no `OrderPendingUpdate`, and a book holds no emulated order: kanso
        sends none, and a member's own is refused as it is initialised.

        An order or a modify in this sleeve's name sent while another member ran is refused
        as that member's, naming both; one sent where the book cannot tell which member ran
        is refused as this sleeve's, saying only that it is in its name.
        """
        if sender is None:
            by, named = self, " in its name where the book cannot tell which member ran,"
        elif sender is self:
            by, named = self, ""
        else:
            owner = self._book_ledger.member_of(self)
            by, named = sender, f" in the name of {owner} while this member runs,"
        if isinstance(event, OrderInitialized):
            sending = self._sending
            if (
                by is self
                and sending is not None
                and event.client_order_id == sending.client_order_id
            ):
                self._sending = None
                return
            by._refuse_booked(
                event.instrument_id,
                event.side,
                "the engine's own submit_order or submit_order_list, reached past "
                f"KansoStrategy's, sends an order{named}",
            )
        if isinstance(event, OrderPendingUpdate):
            order = self.cache.order(event.client_order_id)
            by._refuse_booked(
                event.instrument_id,
                order.side,
                "the engine's own modify_order, reached past KansoStrategy's, modifies an "
                f"order{named}",
            )

    def _refuse_booked(self, instrument_id: InstrumentId, side: OrderSide, did: str) -> NoReturn:
        """The refusal of an order a book member sent or moved past `submit_entry` and
        `submit_exit`, naming the member and what it did."""
        self._refuse(
            BOOK_ENTRY,
            instrument_id.value,
            order_side_to_str(side),
            why=f"{self._book_ledger.member_of(self)}: {did} past the book, where a member's "
            "orders go out only through submit_entry, which the book allots at the instant's "
            "end, and submit_exit, which sizes the exit never past flat; call "
            "submit_entry(instrument_id, side) or submit_exit(instrument_id), and "
            "cancel_order to take an order back",
        )

    def modify_order(
        self,
        order: Any,
        quantity: Quantity | None = None,
        price: Price | None = None,
        trigger_price: Price | None = None,
        client_id: object = None,
        params: dict[str, object] | None = None,
    ) -> None:
        """Hand the engine's own `modify_order` the modify unchanged, for a sleeve run alone: it
        is held neither to the room nor to what the book funds (`docs/backlog.md` row 123).
        In a book it is refused (`_check_booked`), since it would move an order the book or
        `submit_exit` sized past what either left it.

        Under a stated `orders_per_minute` it takes a token, and with the bucket empty it is
        dropped unsent and counted as refused, as an entry is, whichever order it moves: the
        order stays as it was, at its own price and quantity, working or not as the venue
        holds it. kanso cannot send it later without sending terms the strategy may since have
        moved past, and the strategy reads the order it holds, unmoved, on its next point. A
        modify the engine sends nothing for — one that changes nothing, or of an order closed or
        already being cancelled (`_modify_sends`), or of one it was never handed, such as an
        entry the rate dropped, which the engine's risk engine denies as not found
        (`_never_sent`) — takes no token and is not counted."""
        self._check_booked(order, "modify_order")
        if (
            _modify_sends(order, quantity, price, trigger_price)
            and not self._never_sent(order)
            and not self._command_spend(MODIFY)
        ):
            self._drop_rate(MODIFY)
            return
        super().modify_order(order, quantity, price, trigger_price, client_id, params)

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
                self.cost_rate_at(price, multiplier, float(instrument.price_increment)),
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
        self._forget_kept()
        self._clip_orders.setdefault(key, []).append(order)
        return order

    def _is_clip(self, order: object) -> bool:
        key = order.instrument_id.value  # type: ignore[attr-defined]
        return any(order is held for held in self._clip_orders.get(key, ()))

    def _clips_filled(self, key: str) -> float:
        orders = self._clip_orders.get(key)
        if not orders:
            return 0.0
        return sum((_signed(order, filled=True) for order in orders), 0.0)

    def _clips_intended(self, key: str) -> float:
        return self._clips_filled(key) + sum(
            (
                _signed(order, filled=False)
                for order in self._clip_orders.get(key, ())
                if not order.is_closed  # type: ignore[attr-defined]
            ),
            0.0,
        )

    def _own_position(self, instrument_id: InstrumentId) -> Decimal:
        """This sleeve's signed position in an instrument, filled, clips included: its own
        and never the account's.

        Under the NETTING OMS the cache keeps one position per (instrument, strategy)
        (`kanso.nautilus.facts`), where `portfolio.net_position` is every strategy's on the
        account added up, so on a stage of two versions in one name it would be neither's.
        The sum is the portfolio's own arithmetic, `signed_decimal_qty()` from `Decimal(0)`,
        so a sleeve alone on its account reads exactly what the portfolio would answer.
        """
        return sum(
            (
                position.signed_decimal_qty()
                for position in self.cache.positions_open(
                    instrument_id=instrument_id, strategy_id=self.id
                )
            ),
            Decimal(0),
        )

    def _own_filled(self, key: str) -> float:
        """This sleeve's own filled position in a name, its clips taken out; kept until it
        could change (`_forget_kept`)."""
        kept = self._kept_filled.get(key)
        if kept is not None and not self._recheck_kept:
            return kept
        read = float(self._own_position(_id_of(key))) - self._clips_filled(key)
        self._kept_filled[key] = _same(kept, read, "_own_filled", key)
        return read

    def _own_intended(self, key: str) -> float:
        """`_own_filled` with this sleeve's own market orders in flight applied (`held`);
        kept until it could change (`_forget_kept`)."""
        kept = self._kept_intended.get(key)
        if kept is not None and not self._recheck_kept:
            return kept
        read = self._own_filled(key) + sum(
            (
                _signed(order, filled=False)
                for order in self._open_markets()
                if order.instrument_id.value == key and not self._is_clip(order)  # type: ignore[attr-defined]
            ),
            0.0,
        )
        self._kept_intended[key] = _same(kept, read, "_own_intended", key)
        return read

    def _forget_kept(self) -> None:
        """Forget every position read kept (`_own_filled`, `_own_intended`, and so `held`), the
        open positions the balance marks (`_kept_positions`), and that the balance has folded
        in every order (`_kept_settle`).

        A tick sleeve asks them of every name on every point — every tk_* seed walks its
        universe asking `held` once its day is done, and reads its balance — and measured on
        2026-10-22 under a profiler the position reads were 40% to 52% of such a card. What
        they read changes only with one of these, and each forgets the reads first:

        * **an event of this sleeve's orders or positions**, as the engine hands it to
          `handle_event`: every submission, acceptance, refusal, denial, modify, cancel,
          expiry and fill, and every position opened, changed or closed. In
          nautilus_trader 1.231.0 the execution engine applies an order event, and a fill to
          the position it makes, before it publishes either on the strategy's own topics
          (`ExecutionEngine._handle_event`, `execution/engine.pyx`), and a strategy
          publishes the pending cancel or modify, or the denial, it applies itself
          (`trading/strategy.pyx`); nothing of the sleeve's runs between the change and the
          event. The bus's other subscribers to those topics — the portfolio, the risk
          engine, the order emulator and an execution algorithm — run nothing of the
          sleeve's, and a book's ledger asks a member only of an `OrderInitialized` or an
          `OrderPendingUpdate`, whose changes — an order in the cache, a status — are none
          these read; a researched strategy may not reach the bus or apply an event
          (`kanso.criteria.integrity`). One change is applied and not published:
          `cancel_all_orders` marks what it cancels `PENDING_CANCEL`, which moves no
          quantity and leaves `is_closed` as it was (`Order.apply`,
          `model/orders/base.pyx`);
        * **an order this sleeve keeps** in flight (`_track`) or as a clip (`_clip_order`).
          Neither moves a read before the engine hears of the order: a clip reads its
          fills, none yet, and the order's `OrderInitialized` reaches the sleeve before the
          engine routes it. They are forgotten where they are kept all the same, so that no
          read rests on that;
        * **a split** the venue applied to the positions, which it announces once it has
          adjusted them (`_on_restated`, `kanso.nautilus.actions`).

        Nothing they read depends on the clock. `kanso doctor` re-checks the engine's part
        of this against the installed engine (`kanso.nautilus.facts`: every change to a
        strategy's orders and positions reaches it as an event before any handler of its
        runs again). With `_recheck_kept` set every kept read is read again and compared,
        bit for bit, and a difference raises; the suite sets it for every test
        (`tests/conftest.py`), and a card does not.
        """
        self._kept_filled.clear()
        self._kept_intended.clear()
        self._kept_open = None
        self._kept_settled = False

    def _kept_positions(self) -> list[Any]:
        """This sleeve's open positions, `cache.positions_open(strategy_id=self.id)`: the
        cache's list, kept until anything it lists could change (`_forget_kept`). In
        nautilus_trader 1.231.0 that list is built afresh on each call, sorted by position id,
        and holds the cache's own position objects, so what each holds is read afresh from it
        wherever it is read."""
        kept = self._kept_open
        if kept is not None and not self._recheck_kept:
            return kept
        read: list[Any] = self.cache.positions_open(strategy_id=self.id)
        if kept is not None and (
            len(kept) != len(read)
            or any(was is not now for was, now in zip(kept, read, strict=True))
        ):
            raise AssertionError(
                f"_kept_positions kept {kept!r} where the cache lists {read!r} now: something "
                "it reads changed and did not forget the reads kept (KansoStrategy._forget_kept)"
            )
        self._kept_open = read
        return read

    def _kept_settle(self) -> None:
        """Fold every fill not booked yet into cash (`_settle`), unless nothing it folds has
        changed since a pass that folded in everything.

        Such a pass leaves every order it reads with all its events read and lets every closed
        one go. What it books moves only with what forgets the kept reads (`_forget_kept`): an
        event of an order, an order kept, a split. Two writers in nautilus_trader 1.231.0 add
        an event to an order with no event reaching the sleeve: `cancel_all_orders` applies an
        unpublished `OrderPendingCancel`, and the order emulator applies `OrderUpdated`.
        Neither fills or closes an order, so a pass skipped after one books nothing, and the
        next pass that is made reads the order on from the last event it read, the same fills
        in the same order. A pass with nothing moved books nothing, closes nothing and lets
        nothing go, so it is not made. With
        `_recheck_kept` set it is made anyway, and one that moves the cash or lets an order
        go raises."""
        if not self._kept_settled:
            self._settle()
        elif self._recheck_kept:
            cash, held = self._cash, len(self._ledger)
            self._settle()
            if self._cash != cash or len(self._ledger) != held:
                raise AssertionError(
                    f"_kept_settle skipped a pass that moved the cash from {cash!r} to "
                    f"{self._cash!r} or let an order go: something it reads changed and did "
                    "not forget the reads kept (KansoStrategy._forget_kept)"
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

    # --- the order rate: what the account takes, on the data clock -------------

    def _command_spend(self, kind: str, count: int = 1) -> bool:
        """Whether `count` commands of `kind` may go now, and count them if they may.

        Under a stated `orders_per_minute` they take their tokens from the bucket, all of them
        or none (`kanso.nautilus.rate.Bucket`) — one for a submit or a modify, and for a cancel
        the venue model's `requests_per_cancel`, the requests the broker's client sends for
        one; with no rate stated every command may go. Read on the data clock, `data_time`
        held so it never runs backwards, so both paths count the same commands at the same
        instants. A command is counted where the sleeve decides to send it: a cancel kanso
        holds back until the venue has the order, or behind a modify, is counted, and pays
        its token, when it is asked for, where both paths ask for it alike, not when it
        reaches the venue, where they do not.

        The bucket is first come, first served. While a cancel waits for its tokens
        (`_defer_cancel`) a submit or a modify takes none, so a cancel that costs more than
        one request is not starved by the single tokens that come back, each of which a
        handler's next order would otherwise take before the cancel had enough: a throttled
        account sends what it was asked first, first. A cancel asked for meanwhile waits
        behind it, since it costs as much. An exit owed for want of a token needs no such
        rule: it is asked for again before every handler (`_pay_rated`), so while one waits
        either a cancel waits ahead of it or the bucket holds less than the token any
        command needs.
        """
        if count <= 0:
            return True
        now = self._data_time
        if now < self._command_ns:
            now = self._command_ns
        self._command_ns = now
        bucket = self._command_bucket
        if bucket is not None:
            if kind != CANCEL and self._command_deferred:
                return False
            tokens = count * self._cancel_tokens if kind == CANCEL else count
            if not bucket.take(now, tokens):
                return False
        self._command_tally.add(now, kind, count)
        return True

    def _never_sent(self, order: Any) -> bool:
        """Whether the engine was never handed this order, so the venue holds nothing of it to
        cancel or to move: an entry the rate dropped (`_short_of_rate`), one a filter refused or
        sent while warming, or one never submitted at all.

        In nautilus_trader 1.231.0 `Strategy.submit_order` and `Strategy.submit_order_list` add
        every order to the cache before they route it (`trading/strategy.pyx`), on both paths,
        and the risk engine denies a modify of an order the cache does not hold as not found
        (`RiskEngine._handle_modify_order` in `risk/engine.pyx`), sending nothing. So a cancel
        or a modify of one is no command: it takes no token and is not counted. Were it
        counted, a strategy that cancels its last order on every point, sent or not, would
        spend its tokens on orders the account never saw."""
        return self.cache.order(order.client_order_id) is None

    def _cancel_sent(self, order: Any) -> bool:
        """Whether a cancel of this order would send nothing new: it is closed, or a cancel of
        it is already on its way — sent, taken out by the order emulator, held back until
        the venue has the order (`_hold_cancel`), or held behind a modify. Such a cancel takes
        no token and is not counted; the engine sends nothing for an order closed or already
        `PENDING_CANCEL` (`Strategy._create_cancel_order` in `trading/strategy.pyx` of
        nautilus_trader 1.231.0). One the engine was never handed sends nothing either, and
        never reaches here (`_never_sent`)."""
        current = self._current(order)
        key = current.client_order_id
        return bool(
            current.is_closed
            or current.is_pending_cancel
            or key in self._unsent
            or key in self._behind_modify
            or self.cache.is_order_pending_cancel_local(key)
        )

    def _defer_cancel(self, order: Any, client_id: Any, params: dict[str, object] | None) -> None:
        """Keep a cancel the bucket had no token for, to be sent once it has one
        (`_send_deferred`); one already waiting keeps its place. The order counts as working
        meanwhile, as one whose cancel is in flight does, so an exit is sized to what it can
        still fill and an exit at a price is owed behind it (`_close`)."""
        self._command_deferred.setdefault(order.client_order_id, (order, client_id, params))

    def _send_deferred(self) -> None:
        """Send the deferred cancels, oldest first, while the bucket holds a token.

        One whose order no longer needs it — closed, or cancelled since — is dropped and pays
        nothing. Any other is sent as `cancel_order` sends it, held back if the venue does not
        have the order yet. One deferred behind a modify (`_cancel_behind_modify`) is sent so
        too: it is sent no earlier than the point after the handler that sent the modify, so
        on a node it reaches the venue behind the modify as the cancels `_send_behind_modify`
        sends do, answered or not."""
        deferred = self._command_deferred
        while deferred:
            key, (order, client_id, params) = next(iter(deferred.items()))
            current = self._current(order)
            wanted = not self._cancel_sent(current)
            if wanted and not self._command_spend(CANCEL):
                return
            del deferred[key]
            if wanted and not self._hold_cancel(current, client_id, params):
                self._cancelling(current)
                super().cancel_order(current, client_id, params)

    def _short_of_rate(
        self, order: Any, kind: str, closing: tuple[float | None, float | None] | None
    ) -> None:
        """What an order the bucket held no token for comes to.

        An exit `submit_exit` sized — an author's, an exit rule's, one owed already — is owed,
        as it was asked for (`_owe`), and asked for again until the bucket takes it. An entry —
        `submit_entry`'s, one built by hand, an overlay's hedge leg or clip — is dropped unsent
        and counted as refused (`_drop_rate`), and the run goes on: the account would not have
        taken it then, so a strategy that sends past its rate trades the entries the rate lets
        through, and its card says how many it did not. A clip dropped so is forgotten as a
        clip, since it holds nothing. An exit the strategy built itself that `submit_exit` can
        send again as it was asked (`_owable`), and is not an overlay's clip, is owed at its
        quantity and its limit. An exit kanso cannot send again as it was asked —
        a stop, an immediate-or-cancel or post-only limit, a clip — is refused (`order_rate`):
        dropped, it would leave open what the strategy asked to close, and owed, it would go
        as another order.

        An order `classify` reads as an exit is an entry here when it opens or grows the
        position its market orders in flight and its exits owed at market leave (`_opens`):
        the second leg of a flip, sent after the exit whether the exit went or was owed, is
        dropped and counted as the entry it is, and is not owed as an exit to be forgotten
        once the first leg has left the position flat.
        """
        key = order.instrument_id.value
        if closing is not None:
            self._owe(key, order.side, closing[0], closing[1])
            return
        if kind == ENTRY or self._opens(order):
            if self._is_clip(order):
                self._clip_orders[key] = [
                    held for held in self._clip_orders[key] if held is not order
                ]
            self._drop_rate(SUBMIT)
            return
        if _owable(order) and not self._is_clip(order):
            self._owe(key, order.side, float(order.quantity), _order_price(order))
            return
        self._refuse_rate(
            key, order_side_to_str(order.side), "an exit kanso cannot send again as it was asked"
        )

    def _opens(self, order: Any) -> bool:
        """Whether an order opens or grows the position this sleeve's orders already on their
        way leave: what `classify` decides on the position filled, decided once its market
        orders in flight have filled and its exit owed at market on the order's side has gone.

        It is the position `held` reads, the reader a strategy sizes and flips by — an exit at
        market sent in this handler reads as gone — with an exit owed at market, which goes
        as soon as a token comes back, read as gone too. So `submit_exit(name)` and then an
        entry on the other side, in one handler, is an exit and an entry here whether the exit
        went or was owed, where `classify` reads both as exits until the first fills. A limit
        or a stop resting is not applied, as `held` applies none: it may never fill, so an exit
        at market sent beside one is an exit. The position is read in the scope `classify`
        reads it in: an unsized sleeve's whole, its overlays' clips in it; under a sizing rule
        a clip's own, owing nothing of the host's, or the sleeve's own without its clips. A
        clip is kept as one before it is sent (`_clip_order`), so its own quantity is taken
        back out of what the clips intend."""
        key = order.instrument_id.value
        side = order.side
        clip = self._is_clip(order)
        mine = _signed(order, filled=False) if clip else 0.0
        if self.sized and clip:
            held = self._clips_intended(key) - mine
        else:
            held = self._own_intended(key)
            if not self.sized:
                held += self._clips_intended(key) - mine
            owed = self._owed.get((key, side))
            closes = held > 0 if side == OrderSide.SELL else held < 0
            if owed is not None and owed[1] is None and closes:
                gone = abs(held) if owed[0] is None else min(owed[0], abs(held))
                held = held - gone if held > 0 else held + gone
        signed = -float(order.quantity) if side == OrderSide.SELL else float(order.quantity)
        return abs(held + signed) >= abs(held)

    def _leaving(self, key: str, held: float) -> bool:
        """Whether what this sleeve holds in a name is owed out at market for want of a token
        (`_owe`), which under a sizing rule is the whole of it, the only exit `submit_exit`
        sends there. The account takes it as soon as a token comes back, before anything asked
        after it (`_pay_rated`), so a sized entry reads it as an exit in flight
        (`_sized_entry`): a flip or a rotation behind it is the entry it is, dropped and
        counted for want of the same token, rather than refused as `one_position`."""
        name = (key, OrderSide.SELL if held > 0 else OrderSide.BUY)
        return name in self._rate_owed and name in self._owed

    def _drop_rate(self, kind: str, count: int = 1) -> None:
        """Count `count` commands of `kind` the bucket held no token for — a submit of each order
        of an entry, a modify — as refused, and send none of them (`kanso.nautilus.rate.Tally`).

        The run goes on past them, on every path alike: a card is measured on what the rate
        let through and carries how many it dropped (`kanso.schemas.Commands`), and a node,
        a stage's among them, drops the same commands at the same instants and does not stop.
        A researched strategy may not reach it (`kanso.criteria.integrity.RATE_MARKS`)."""
        self._command_tally.refuse(kind, count)

    def _owe(self, key: str, side: OrderSide, qty: float | None, price: float | None) -> None:
        """Owe the exit the bucket held no token for, as `_close` owes one a cancel in flight
        held back (`_pay_owed`), with the first claim on a token that comes back
        (`_pay_rated`); an exit rule's owed exit is not replaced by its host's."""
        held = self._owed.get((key, side))
        if held is None or not held[2] or self._exiting:
            self._owed[(key, side)] = (qty, price, self._exiting)
        self._rate_owed[(key, side)] = None

    def _pay_rated(self) -> None:
        """Ask again, oldest first, for the exits an empty bucket owed, before the strategy's
        handler of a point can spend a token that came back on an order of its own.

        A throttled account sends what it was asked first, first: an exit the rate held back
        is older than anything the handler sends, so it takes the token, and an entry the
        handler sends after it finds the bucket as the exit left it. One owed for another
        reason, or no longer owed, is left to `_pay_owed` after the handler, as before."""
        held, self._rate_owed = self._rate_owed, {}
        for name in held:
            if name in self._owed:
                self._pay_owed(name)

    def _refuse_rate(self, key: str, asked: str, what: str, count: int = 1) -> NoReturn:
        """Refuse a command the bucket held too few tokens for, saying what it held."""
        bucket = self._command_bucket
        assert bucket is not None  # only a stated rate refuses anything
        now = self._command_ns
        self._refuse(
            ORDER_RATE,
            key,
            asked,
            why=f"{what} needed {count} of the {bucket.per_minute} tokens a minute "
            f"costs.orders_per_minute allows, and the bucket held {bucket.holds(now)} after "
            f"{self._command_tally.within(now)} command(s) in the 60 s before it. Past the "
            "rate an exit kanso cannot send again as it was asked is refused, while an entry "
            "or a modify is dropped and counted, any other exit owed and a cancel deferred: "
            "close with submit_exit, which is owed rather than refused, send fewer — re-post "
            "only when the price has moved enough to matter — or state the account's own "
            "figure as costs.orders_per_minute",
        )

    def _lift_rate(self) -> None:
        """Hold this sleeve to no rate from here on, sending every deferred cancel first.

        A stage node's flatten after its window's last point is the stage's, not the
        sleeve's, and must go out whole however busy the sleeve was (`kanso.nautilus.node`).
        A researched strategy may not reach it (`kanso.criteria.integrity.RATE_MARKS`).
        """
        self._command_bucket = None
        if self._command_deferred:
            self._send_deferred()

    # --- cost-aware helpers: where exposure is enforced ---------------------

    def submit_entry(
        self,
        instrument_id: InstrumentId | str,
        side: OrderSide | str,
        *,
        notional: float | None = None,
        qty: float | None = None,
        price: float | None = None,
        edge_bp: float | None = None,
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
        when nothing was submitted — no room, no price to size against, an attached filter
        refused the entry, or a stated `orders_per_minute` left no token for it, which drops
        it and counts it as refused (`_short_of_rate`).

        **In a book the entry is the book's to place** (`kanso.nautilus.book`). It is
        collected, returning `None`, and placed when the instant ends, after every member's
        entries of that instant are in: in the book's order — `edge_bp` first, the predicted
        move in basis points less the round trip's cost at the instant, highest first and an
        entry without one last — sized as above at that moment, then cut to what the book has
        left: its room, its cap on the name, and the size displayed at the touch the instant's
        takers share. One cut below a whole lot is refused and counted. `edge_bp` is read by a
        book alone; a sleeve run by itself ignores it. An entry asked again in the same name
        and side while the first is still collected is the same entry, and is dropped.
        """
        resolved_id = self._instrument_id(instrument_id)
        resolved_side = self._side(side)
        instrument = self.cache.instrument(resolved_id)
        if instrument is None:
            raise ValidationError(
                f"instrument: {resolved_id} is not in the cache; "
                "it is not part of this run's resolved universe"
            )
        ledger = self._book_ledger
        if ledger is not None and not ledger.claim(self):
            if not self._warming():
                ledger.defer(
                    self,
                    resolved_id,
                    resolved_side,
                    edge_bp,
                    {"notional": notional, "qty": qty, "price": price},
                )
            return None
        if self.sized:
            return self._sized_entry(instrument, resolved_id, resolved_side, notional, qty, price)
        reference = price if price is not None else self._price_now(resolved_id.value)
        if reference is None or reference <= 0:
            self._sized_nothing(instrument, resolved_side)
            return None
        room = self._headroom(resolved_id, reference)
        if room <= 0:
            self._sized_nothing(instrument, resolved_side)
            return None
        unit = reference * float(instrument.multiplier)  # one contract's notional
        raw = _entry_units(qty, notional, room, unit)
        ctx = self._context(
            resolved_id, resolved_side, raw, price, "LIMIT" if price is not None else "MARKET"
        )
        self._entry_answers = self._ask_overlays(ctx)
        try:
            units = self.size(ctx, raw)
            if ledger is not None:
                units = ledger.cut(self, instrument, resolved_side, units, reference, price)
            quantity = self._quantise(instrument, units)
            if quantity is None:
                return None
            order = self._order(instrument, resolved_side, quantity, price)
            return self._booked(order)
        finally:
            self._entry_answers = None

    def _sized_nothing(self, instrument: object, side: OrderSide) -> None:
        """An entry this sleeve's own limits leave nothing for — no price to size it against,
        or no room — which a book counts as sized below its minimum."""
        if self._book_ledger is not None:
            self._book_ledger.cut(self, instrument, side, 0.0, 0.0, None)

    def _booked(self, order: object) -> object | None:
        """Submit an entry this sleeve sized, and in a book tell the book of it once it reached
        the venue."""
        ledger = self._book_ledger
        submitted = self._submitted(order)
        if submitted is not None and ledger is not None:
            ledger.placed(self, submitted)
        return submitted

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
        after it and close what it opens; once it has filled any of it they count in full.
        The child of an exit sent one-triggers-other counts in full from the start: its parent
        is on the same side of the same name, and its fill releases the child to close more.
        A child whose parent is in another name counts once that parent has filled any of it
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
        close what was asked, including when what is left is owed. Under a stated
        `orders_per_minute` an exit the bucket holds no token for is owed whole, as it was
        asked for, and returns `None` too (`_short_of_rate`).

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
        net = self._own_position(resolved_id)
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
        a flip is legal at leverage one. So does an exit a stated `orders_per_minute` owed for
        want of a token (`_leaving`), which goes before anything asked after it: the entry
        behind it finds no token either, and is dropped and counted (`_short_of_rate`). The
        price is the last print of the finest grain loaded — what the venue's book holds —
        never the host grain's own close.
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
        if own != 0 and not self._leaving(key, own):
            if (own > 0) == (signed > 0):
                return None
            self._refuse(
                ONE_POSITION,
                key,
                asked,
                why=f"{key} is held ({own:g}) on the other side and no exit of it is in "
                f"flight; submit_exit({key!r}) first, in the same handler",
            )
        elsewhere = {
            name: held
            for name, held in self._holdings().items()
            if held != 0 and not self._leaving(name, held)
        }
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
            self._sized_nothing(instrument, side)
            return None
        multiplier = float(instrument.multiplier)  # type: ignore[attr-defined]
        increment = float(instrument.price_increment)  # type: ignore[attr-defined]
        raw = full_book_quantity(
            self.budget,
            reference * multiplier,
            increment * multiplier,
            self.cost_rate_at(reference, multiplier, increment),
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
            ledger = self._book_ledger
            if ledger is not None:
                cut = ledger.cut(self, instrument, side, float(quantity), reference, None)
                if cut < float(quantity):
                    quantity = self._quantise(instrument, cut)
                    if quantity is None:
                        return None
            order = self._order(instrument, side, quantity, None)
            self._sizing_call = True
            try:
                return self._booked(order)
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
            price is None
            or any(
                order.client_order_id in self._cancels
                or order.client_order_id in self._command_deferred
                for order in working
            )
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
        self._command_closing = (qty, price)
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

        The cancel takes its token here, where the exit decided to send it, and with the
        bucket empty it is deferred (`_defer_cancel`), to go on a later point, which reaches
        the venue behind the modify on both paths (`_send_deferred`).
        """
        if not self._cancel_sent(order) and not self._command_spend(CANCEL):
            self._defer_cancel(order, None, None)
            return
        self._command_deferred.pop(order.client_order_id, None)
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

        The cancels an empty bucket deferred are sent first, as many as it now holds tokens
        for (`_send_deferred`), and then the exits it owed, oldest first (`_pay_rated`):
        this is the one place both paths reach before every handler of a point, and what a
        token refilled since the last point buys goes to what waited for it before the
        handler can spend it. A point is matched by the venue before any handler of it runs,
        on both paths, so what waited goes on the first point at or after the instant its
        token came back, after the venue has matched that point.
        """
        if self._command_deferred:
            self._send_deferred()
        if self._rate_owed:
            self._pay_rated()
        if not self._behind_modify:
            return
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
        back. An exit owed for want of a token is asked for before the handler too
        (`_pay_rated`), since it has the first claim on one.
        """
        if not self._owed:
            return
        for (key, side), (qty, price, ruled) in list(self._owed.items()):
            if only is not None and (key, side) != only:
                continue
            net = self._own_filled(key) if self.sized else float(self._own_position(_id_of(key)))
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
        been (`_hold_cancel`). A market order's cancel is never sent nor held (`_never_rests`),
        so on a level-two book the rest of one deeper than the book stays open. Nor is the
        cancel of an order the engine was never handed — an entry the rate dropped, one a filter
        refused or sent while warming (`_never_sent`): the venue holds nothing of it, so nothing
        is sent, held or counted, and a strategy that cancels its last order on every point,
        sent or not, sends what one that cancels only what was sent does.

        Under a stated `orders_per_minute` a cancel that would go out takes a token, and with
        the bucket empty it is deferred, to go out as soon as the bucket holds one; a second
        cancel of an order whose cancel waits goes now if the bucket holds a token, and keeps
        the first one's place if it does not (`_defer_cancel`).
        """
        self._forget(order.instrument_id.value, order.side, priced_only=True)
        if _never_rests(order) or self._never_sent(order):
            return
        if not self._cancel_sent(order) and not self._command_spend(CANCEL):
            self._defer_cancel(order, client_id, params)
            return
        self._command_deferred.pop(order.client_order_id, None)
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
        for the engine to refuse. A market order in it is left alone (`_never_rests`), so on
        a level-two book the rest of one deeper than the book stays open, and so is one the
        engine was never handed (`_never_sent`). Under a stated `orders_per_minute` each
        order's cancel takes a token or is deferred, as `cancel_order`'s does.
        """
        batches: dict[object, list[Any]] = {}
        for order in orders:
            self._forget(order.instrument_id.value, order.side, priced_only=True)
            if _never_rests(order) or self._never_sent(order):
                continue
            if not self._cancel_sent(order) and not self._command_spend(CANCEL):
                self._defer_cancel(order, client_id, params)
                continue
            self._command_deferred.pop(order.client_order_id, None)
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
        names the exceptions), where `cancel_order` leaves a market order alone (`_never_rests`).
        The engine's own also cancels the rest a market order deeper than a level-two book left
        open, which is counted as cancelled with the rest; on the top-of-book venue a market
        order has been answered before any cancel lands, and none is marked pending cancel.

        Under a stated `orders_per_minute` the engine's own takes a token for every order it
        cancels that kanso would send a cancel of — not a market order, and not one whose
        cancel is already on its way — and when the bucket holds fewer, each is cancelled on
        its own through `cancel_order` instead, so as many go as it holds tokens for and the
        rest are deferred, the same orders on both paths — and the rest of a market order
        deeper than a level-two book, which only the engine's own cancel of the name reaches,
        is left for the next one the bucket can pay for.
        """
        orders = [
            order
            for order in (self._current(entry.order) for entry in self._ledger.values())
            if order.instrument_id == instrument_id
            and order_side in (OrderSide.NO_ORDER_SIDE, order.side)
            and not order.is_closed
        ]
        self._forget(instrument_id.value, order_side, priced_only=True)
        wanted = [
            order for order in orders if not _never_rests(order) and not self._cancel_sent(order)
        ]
        if any(
            order.status == OrderStatus.INITIALIZED for order in orders
        ) or not self._command_spend(CANCEL, len(wanted)):
            for order in orders:
                self.cancel_order(order, client_id, params)
            return
        for order in wanted:
            self._command_deferred.pop(order.client_order_id, None)
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
        ._handle_submit_order_list` in `execution/emulator.pyx`, read, not measured). Its
        cancel is sent once the order leaves `INITIALIZED`. An order never handed to the
        engine at all is not held here: `cancel_order` sends nothing for it (`_never_sent`),
        so a strategy whose entries the rate drops does not keep every one it cancels.
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

        In a book the sleeve is the member running while its handlers run
        (`BookLedger.running`), so an order heard on the bus is named for the member it was sent
        while.

        Every event forgets the position reads kept, before anything else is done with it
        (`_forget_kept`).
        """
        self._forget_kept()
        ledger = self._book_ledger
        if ledger is not None:
            ledger.running.append(self)
        try:
            if isinstance(event, OrderFilled):
                self._fill_arrived(event)
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
        finally:
            if ledger is not None:
                ledger.running.pop()

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
        working = []
        for entry in self._ledger.values():
            order = self._current(entry.order)
            if (
                order.side != side
                or order.instrument_id.value != key
                or self._spent(order)
                or (not clips and self._is_clip(entry.order))
                or self._not_yet_live(order)
            ):
                continue
            working.append(order)
        return working

    def _spent(self, order: Any) -> bool:
        """Whether an order of this sleeve's can no longer fill: the venue has closed it, the
        order emulator has taken it out on its cancel, or, with no latency stated, the venue
        held it open when its cancel was sent (`_working` states each)."""
        return bool(
            order.is_closed
            or self.cache.is_order_pending_cancel_local(order.client_order_id)
            or (
                self._instant()
                and order.is_pending_cancel
                and self._cancels.get(order.client_order_id, False)
            )
        )

    def _not_yet_live(self, order: Any) -> bool:
        """Whether an order is a contingent child that leaves the position as it is until its
        parent fills: its parent has filled nothing, and is not on the child's own side of
        the child's own name — a bracket's entry, whose stop-loss and take-profit close what it
        opens, or an order in another name — or can no longer fill (`_spent`). A child whose
        parent is on its own side of its own name is not: an exit sent one-triggers-other —
        against a long, a sell whose fill releases another sell — sells both, so the child
        counts in full like any order working there. A child whose parent is in another name
        is left out until that parent fills: the venue holds it pending and an exit at market
        cannot cancel it, so counted it would keep the exit of its own name from going out
        for as long as the other name's parent rests (`docs/backlog.md` row 206 says what the
        count leaves out).

        In nautilus_trader 1.231.0 the venue keeps a child `SUBMITTED` with no venue order id
        while its parent is accepted or triggered, and processes it on the parent's first
        fill (`OrderMatchingEngine.process_order` and `fill_order` in `backtest/engine.pyx`);
        the order emulator keeps it `INITIALIZED` until the parent fills (`OrderManager
        .handle_order_filled` in `execution/manager.pyx`). So neither an exit at market nor
        anything else has such a child to cancel, and one whose parent can no longer fill
        never fills: a parent cancelled with nothing filled takes its child with it
        (`OrderMatchingEngine.cancel_order`, and `OrderManager.handle_contingencies` for the
        emulator's). Measured on both paths by the exit and replay tests, a bracket held by
        the venue and one held by the emulator, and on the backtest engine by the exit tests
        for an exit sent one-triggers-other."""
        if order.parent_order_id is None:
            return False
        parent = self.cache.order(order.parent_order_id)
        if parent is None or parent.filled_qty.as_decimal() != 0:
            return False
        adds = parent.instrument_id == order.instrument_id and parent.side == order.side
        return not adds or self._spent(parent)

    def _submitted(self, order: object) -> object | None:
        """Send an order kanso sized — an entry `submit_entry` sized, an exit `submit_exit`
        did — and hand it back, or `None` when nothing went out. It is the order kanso is
        sending (`_sending`), the one a book lets through, once (`_check_booked`,
        `_check_booked_event`); `strategy_integrity` denies it to a researched strategy,
        since it sends whatever it is handed as kanso's own."""
        placed = len(self._intents)
        built, self._built = self._built, True
        self._sending = order
        try:
            self.submit_order(order)
        finally:
            self._built = built
            if self._sending is order:
                self._sending = None
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
        if price is None or key not in self._restated:
            return price  # no split applied to the name: the factor is one, the price itself
        return price / self._restating(key, self._price_ns.get(key, 0))

    def _print_now(self, key: str) -> float | None:
        """The last print of a name, quoted in the share count the venue holds now.

        A name no split has been applied to is divided by no ratio, and a float divided by
        one is that float, so its price is handed back as it is."""
        price = self._last_print.get(key)
        if price is None or key not in self._restated:
            return price
        return price / self._restating(key, self._print_ns.get(key, 0))

    def _reserve(self, key: str) -> float:
        """What a notional is divided by to leave room for its own round trip: commission,
        per share included at the last price, slippage and the whole spread each way — the
        stated width, or the last quoted one."""
        price = self._last_print.get(key)
        rate = (
            self.cost_rate_at(price, self._multiplier_of(key), self._increment_of(key))
            if price
            else self.cost_rate
        )
        if self._charges.get("spread") == "quotes":
            quote = self._last_quote.get(key)
            if quote is not None:
                rate += 2.0 * quote_half_spread(float(quote.bid_price), float(quote.ask_price))
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
        `key` only that name's orders are read, which a quote of that name asks for under
        `spread: quotes`. A fill is charged against the quote it was struck against
        (`_charge`), so when it is booked moves nothing it is charged; one booked while a quote
        of its own instant may still come is struck again when it does (`_quoted`).

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
                    self._book_fill(event)
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
        if key is None and until_ns is None:
            self._kept_settled = True
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

    def _fill_arrived(self, event: Any) -> None:
        """Pin the quote a fill is read against when the engine hands it over: its name's last
        quote delivered so far, which a quote of the fill's own instant delivered after it
        replaces (`_quoted`) until a later instant is (`_struck`). On a venue that matches a
        level-two book every fill is struck at the touch whatever the quote (`_charge`), so
        nothing is pinned: a sleeve that holds only the book is handed no point that lets a
        fill go (`_turn`), and would keep every fill of the run."""
        if self._levelled or event.id in self._refs:
            return
        quote = self._last_quote.get(event.instrument_id.value)
        self._refs[event.id] = (
            None if quote is None else (float(quote.bid_price), float(quote.ask_price))
        )
        if int(event.ts_event) >= self._instant_ns:
            self._open_fills[event.id] = event

    def _quoted(self, key: str, ts_ns: int, quote: tuple[float, float]) -> None:
        """A quote of `key` delivered at `ts_ns`: the quote each fill of the name at that
        instant is read against from now on, the last of the instant's quotes being the last
        the venue applied there — as the extraction reads it (`kanso.nautilus.backtest.Marks`).
        A fill already booked is struck again, and the difference booked.

        The engine hands a sleeve a fill before the point the venue made it on (row 212 of
        `docs/backlog.md`), so a fill a quote made arrives against the quote before that one,
        and is put right here, when its quote is delivered."""
        for event_id, event in self._open_fills.items():
            if event.instrument_id.value != key or int(event.ts_event) < ts_ns:
                continue
            self._refs[event_id] = quote
            booked = self._booked_open.get(event_id)
            if booked is not None:
                charged = self._charge(event)
                self._cash -= charged - booked
                self._booked_open[event_id] = charged

    def _struck(self, ts_ns: int) -> None:
        """A point of `ts_ns` is being delivered, so every fill of an earlier instant has the
        quote it is read against and is struck as booked: let go of it."""
        for event_id, event in list(self._open_fills.items()):
            if int(event.ts_event) >= ts_ns:
                continue
            del self._open_fills[event_id]
            if self._booked_open.pop(event_id, None) is not None:
                self._refs.pop(event_id, None)
        self._instant_ns = ts_ns

    def _book_fill(self, event: Any) -> None:
        """Take one fill out of cash: the value it bought, or give back what it sold for, and
        what the runner charges it (`_charge`)."""
        self._fill_arrived(event)
        multiplier = self._multiplier_of(event.instrument_id)
        signed = self._signed(event)
        charged = self._charge(event)
        self._cash -= signed * float(event.last_px) * multiplier + charged
        if event.id in self._open_fills:
            self._booked_open[event.id] = charged
        else:
            self._refs.pop(event.id, None)

    def _charge(self, event: Any) -> float:
        """What the runner charges one fill: commission, per share included where the model
        states one, slippage and half the spread, and a taker's `slippage_ticks` of the
        increment, no further than the limit the order carried at the fill, or a maker's own
        schedule where the venue model states one, of its notional and per share, which a
        rebate makes negative. A fill struck at the touch of the quote it is read against — a
        buy at or above its ask, a sale at or below its bid — or on a venue that matches a
        level-two book carries the spread in its price, so neither the half-spread nor
        `slippage_bps` is charged on it (`kanso.nautilus.costs.at_touch`), exactly as the
        extraction charges it (`kanso.nautilus.backtest.Marks.struck`)."""
        multiplier = self._multiplier_of(event.instrument_id)
        qty, px = float(event.last_qty), float(event.last_px)
        sell = event.order_side == OrderSide.SELL
        maker = event.liquidity_side == LiquiditySide.MAKER
        ticks = float(self._charges.get("slippage_ticks") or 0.0)
        slip = 0.0
        if ticks and not maker:
            slip = tick_slip(
                ticks,
                self._increment_of(event.instrument_id),
                px,
                limit_at(self.cache.order(event.client_order_id), event.trade_id),
                sell=sell,
            )
        quote = self._refs.get(event.id)
        return fill_cost(
            qty * px * multiplier,
            qty,
            float(self._charges.get("commission_bps") or 0.0),
            float(self._charges.get("slippage_bps") or 0.0),
            self._half_of(quote),
            _maker_bps(self._charges),
            float(self._charges.get("commission_per_share") or 0.0),
            maker=maker,
            sell=sell,
            sell_fee_bps=float(self._charges.get("sell_fee_bps") or 0.0),
            sell_fee_per_share=float(self._charges.get("sell_fee_per_share") or 0.0),
            maker_per_share=_maker_per_share(self._charges),
            slip=slip,
            multiplier=multiplier,
            touched=self._levelled or at_touch(px, sell=sell, quote=quote),
        )

    def _half_of(self, quote: tuple[float, float] | None) -> float:
        """Half the spread one fill pays: the stated width, or under `spread: quotes` the half
        of the quote it is read against — the stated width when there is none before it."""
        fixed = fixed_half_spread(self._charges.get("fixed_bps"))
        if self._charges.get("spread") != "quotes" or quote is None:
            return fixed
        return quote_half_spread(*quote)

    def _worth(self, position: Any) -> float:
        """One open position's market value at its last print, quoted in the shares held now.

        A price printed before a split the venue has since applied is restated by the
        split's ratio (`_restating`), so a pair's other leg does not read the split as a
        loss in the handlers between the split and the held leg's next print. A position
        the sleeve has seen no price for is marked at its own split-aware cost. A position in
        one of the sleeve's own names finds the name in the table `held` reads.
        """
        instrument_id = position.instrument_id
        key = self._kept_names.get(instrument_id)
        if key is None:
            key = instrument_id.value
        price = self._print_now(key)
        if price is None:
            applied = tuple(self._restated.get(key, ()))
            price = splits.ledger(splits.moves_of(position, applied)).basis
        return float(position.signed_qty) * price * self._multiplier_of(instrument_id)

    def _multiplier_of(self, instrument_id: InstrumentId | str) -> float:
        """The contract multiplier a quantity at a price is scaled by to be a notional, read
        from the cached instrument: the contract size of a future or an option, one for a
        share, and one when the cache holds no definition. Under nautilus_trader 1.231.0
        every instrument class carries `multiplier` as a `Quantity`, `Equity` fixing it at
        one (`kanso.nautilus.facts`)."""
        instrument = self.cache.instrument(self._instrument_id(instrument_id))
        return 1.0 if instrument is None else float(instrument.multiplier)

    def _increment_of(self, instrument_id: InstrumentId | str) -> float:
        """The price increment a venue model's `slippage_ticks` is counted in, read from the
        cached instrument: zero when the cache holds no definition, which charges no tick."""
        instrument = self.cache.instrument(self._instrument_id(instrument_id))
        return 0.0 if instrument is None else float(instrument.price_increment)

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
            return _id_of(instrument_id)
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
        self._forget_kept()
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

    def _consult_exit(self, instrument_id: InstrumentId, key: str | None = None) -> None:
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
        if self._exiting or not self.is_running or self._delivered_ns < self._trading_from_ns:
            return  # the last test is `_warming`
        if key is None:
            key = instrument_id.value
        net = self._kept_filled.get(key)  # what is open, not what is in flight
        if net is None or self._recheck_kept:
            net = self._own_filled(key)  # the kept read, as `_own_filled` answers it
        if net == 0.0:
            return
        side = OrderSide.SELL if net > 0 else OrderSide.BUY
        if self._market_order_in_flight(instrument_id, side):
            return
        if not any_attached() and _kept_plain(type(self)):
            # Nothing is attached to any host in this process, so `before_exit` finds no exit
            # rule (`modifiers_for` answers nothing, and changes nothing) and answers False
            # whatever context it is handed. Building that context reads the sleeve's
            # positions, clips, prices and balance, and of those reads only the balance's
            # settling changes anything (`_settle`), so that is done here, where the context
            # would have done it, and nothing else is: measured on 2026-10-26, a tick sleeve
            # holding a position spent 11% of its card building contexts nothing read.
            self._kept_settle()
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
        if self._hedging or not self.is_running or not any_attached():
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
            instrument_id.value: float(self._own_position(instrument_id))
            for instrument_id in self.universe
        }

    def _is_extra_bar(self, bar: Bar) -> bool:
        extra = self._cfg.extra_resolutions
        return bool(extra) and _resolution_of_bar(bar) in extra

    def _any_market_in_flight(self) -> bool:
        return bool(self._open_markets())


_CONTEXT_READS: Final = frozenset(
    {
        "_book",
        "_clips",
        "_clips_filled",
        "_clips_intended",
        "_context",
        "_decisions",
        "_instrument_id",
        "_is_clip",
        "_multiplier_of",
        "_open_markets",
        "_own_filled",
        "_own_intended",
        "_own_position",
        "_print_now",
        "_restating",
        "_worth",
        "balance",
        "before_exit",
        "capital",
        "held",
        "host_names",
        "sized",
        "universe",
    }
)
"""What an exit consult reads through to build its context and ask `before_exit`
(`KansoStrategy._consult_exit`)."""


_PLAIN: dict[type, bool] = {}
"""`_kept_plain`'s answer for each class it has been asked about."""


def _kept_plain(cls: type) -> bool:
    """Whether a sleeve class reads an exit consult's context and asks `before_exit` through
    `KansoStrategy`'s own methods, binding none of `_CONTEXT_READS` itself: only then is a
    context nothing is attached to read nothing but the balance's settling. A researched
    `strategy.py` binds none (`kanso.criteria.integrity`); one that did would be read as before.
    """
    plain = _PLAIN.get(cls)
    if plain is None:
        plain = _PLAIN[cls] = False
        for klass in cls.__mro__:
            if klass is KansoStrategy:
                plain = _PLAIN[cls] = True
                break
            if not _CONTEXT_READS.isdisjoint(vars(klass)):
                break
    return plain


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
