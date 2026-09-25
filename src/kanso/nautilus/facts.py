"""What NautilusTrader actually provides, verified against the installed engine.

kanso binds to NautilusTrader through a small number of load-bearing engine
behaviours. Each is stated below as a fact about `nautilus_trader 1.231.0`,
established by reading and exercising the installed package rather than its
documentation. `verify()` re-establishes every one of them at runtime on the
host it runs on, so `doctor` reports the truth of this file rather than
trusting it, and an engine upgrade that breaks a binding is caught before a
card, a certificate or a deployment depends on it. Facts that do **not** hold
are recorded here as plainly as the ones that do; they are design constraints,
not omissions.

Configuration and components
----------------------------
`StrategyConfig` and `ActorConfig` are two distinct subclasses of
`NautilusConfig`; neither derives from the other. `Strategy` derives from
`Actor`, but their configs do not share that relationship: `Actor.__init__`
raises `TypeError` when handed a `StrategyConfig`, and `Strategy.__init__`
raises `TypeError` when handed an `ActorConfig`. A strategy config therefore
cannot configure an actor, and the separation is enforced by the engine at
construction rather than by convention.

The engine defines **no** `config_cls` class attribute on `Strategy` or
`Actor`. Nothing in the engine pairs a component class with its config class as
an attribute; the pairing is the constructor's runtime type check, plus the
dotted paths an `ImportableStrategyConfig` or `ImportableActorConfig` carries
when a node builds components from configuration. Where kanso exposes
`config_cls` it is kanso's own attribute, honoured by kanso's own loader, and
the engine neither reads nor validates it.

The data catalog
----------------
`ParquetDataCatalog(path)` is the store. Writes go through
`write_data(list[Data])`, which groups objects by class and identifier, converts
them to Arrow internally and writes one parquet file per contiguous interval;
it refuses a batch that is not non-decreasing in `ts_init` and refuses a write
whose interval overlaps an existing file unless `skip_disjoint_check=True`.
Reads come back typed: `instruments()`, `bars()`, `quote_ticks()`,
`trade_ticks()`, `custom_data(cls)` and the general `query(data_cls, ...)`.
The catalog is also the instrument store — an `Instrument` written with
`write_data` is returned by `instruments()` — so there is no second registry to
keep consistent with it.

There is **no public Arrow write path**. `write_data` accepts Python `Data`
objects only; the conversion to a `pyarrow.Table` happens in the private
`_write_chunk`, and passing a table or record batch raises. An ingest path that
wants to avoid materialising one Python object per row must serialise batches
itself against the catalog's own schemas — `nautilus_trader.serialization.arrow.serializer`
exposes `get_schema`, `list_schemas`, `ArrowSerializer.serialize_batch` and
`ArrowSerializer.deserialize` — and write the parquet files into the catalog's
directory layout directly. The engine offers the schemas; it does not offer the
writer.

Availability timestamps
-----------------------
Every `Data` carries two nanosecond timestamps, `ts_event` (the economic
reference time) and `ts_init` (when the information became available). The
engine orders by `ts_init` and only by `ts_init`: the catalog's SQL query path
appends `ts_init >= start` / `ts_init <= end` and `ORDER BY ts_init`, its
dataset path filters on the `ts_init` field, `BacktestEngine` sorts its stream
with `key=lambda x: x.ts_init`, and `BacktestDataIterator` merges streams on a
heap keyed by `ts_init` and advances the clock to each datum's `ts_init`. Given
two streams whose `ts_event` and `ts_init` orders disagree — one bar at
`ts_event=10, ts_init=100`, another at `ts_event=50, ts_init=20` — the iterator
delivers the second first. Nothing in the engine reads `ts_event` for ordering,
filtering or clocking. A catalog round-trip preserves the two independently, so
a point stamped with a publication instant later than its reference time is
delivered at the publication instant and never earlier.

Custom data types
-----------------
A custom type is a subclass of `nautilus_trader.core.data.Data` decorated with
`@customdataclass` (`nautilus_trader.model.custom`), which synthesises
`__init__` (taking `ts_event` and `ts_init` first), `to_dict`/`from_dict`,
`to_bytes`/`from_bytes`, `to_arrow`/`from_arrow` and a `_schema`, then registers
the type for both msgspec serialisation and Arrow via
`register_arrow(data_cls, schema, encoder, decoder)`. Field annotations are
restricted to exactly `InstrumentId`, `str`, `bool`, `float`, `int`, `bytes`,
`ndarray` and `dict`; any other annotation — `Decimal` and `datetime` included —
raises `TypeError` at class definition. Timestamps must therefore travel as
`int` nanoseconds and decimals as `float` or `str`. A registered type
round-trips through the catalog, where it is returned wrapped in `CustomData`.
`DataEngine._handle_data` publishes only that wrapper among custom types and
logs `unrecognized type` for a bare `Data` subclass; `_handle_custom_data`
then publishes the inner `data.data` on the custom-data topic, so a strategy's
`handle_data` receives the inner object.

Two traps sit in that decorator. First, **it cannot read postponed
annotations.** It reads `cls.__annotations__` verbatim and resolves nothing on
this interpreter, so under `from __future__ import annotations` every field
arrives as a string and the decorator raises `TypeError: Unsupported custom
data annotation: 'str'`. A module that defines a custom data type must
therefore let its annotations evaluate eagerly, or build the class with
`__annotations__` set to real objects. Second, **type names are a global
namespace**: registration is keyed by the bare class name across the process,
and a second class of the same name raises `KeyError` from the serializable-type
registry, whatever module it came from.

Instruments
-----------
The five classes kanso resolves are `Equity`, `OptionContract`,
`FuturesContract`, `CurrencyPair` and `IndexInstrument`. Their constructors are
Cython and positional-or-keyword with no introspectable signature; the required
fields, established by construction, are:

* `Equity`: `instrument_id`, `raw_symbol`, `currency`, `price_precision`,
  `price_increment`, `lot_size`, `ts_event`, `ts_init`.
* `OptionContract`: `instrument_id`, `raw_symbol`, `asset_class`, `currency`,
  `price_precision`, `price_increment`, `multiplier`, `lot_size`,
  `underlying`, `option_kind`, `strike_price`, `activation_ns`,
  `expiration_ns`, `ts_event`, `ts_init`.
* `FuturesContract`: `instrument_id`, `raw_symbol`, `asset_class`, `currency`,
  `price_precision`, `price_increment`, `multiplier`, `lot_size`,
  `underlying`, `activation_ns`, `expiration_ns`, `ts_event`, `ts_init`.
* `CurrencyPair`: `instrument_id`, `raw_symbol`, `base_currency`,
  `quote_currency`, `price_precision`, `size_precision`, `price_increment`,
  `size_increment`, `ts_event`, `ts_init`.
* `IndexInstrument`: `instrument_id`, `raw_symbol`, `currency`,
  `price_precision`, `size_precision`, `price_increment`, `size_increment`,
  `ts_event`, `ts_init`.

Omitting a required field raises `TypeError`. Tick size, lot size and
multiplier are constructor inputs with no engine defaults, which is why they
must come from a convention table rather than from a vendor.

`nautilus_trader.common.providers.InstrumentProvider` is not the interface
kanso needs: `load(instrument_id, filters)` takes an already fully qualified
`InstrumentId` — symbol *and* venue — returns `None`, and populates an internal
cache read back through `find()`; `load_async`, `load_ids_async`,
`load_all_async` and `initialize` are coroutines. Discovering the venue for a
bare symbol, and returning resolutions to a synchronous caller, are outside it,
so kanso's own provider interface is a separate thing that adapts to this one.

Market data objects
-------------------
`BarType(instrument_id, BarSpecification(step, aggregation, price_type),
aggregation_source)` renders as `AAPL.XNAS-1-DAY-LAST-EXTERNAL` and parses back
from that string. `Bar(bar_type, open, high, low, close, volume, ts_event,
ts_init)` validates the OHLC ordering and raises `ValueError` on a high below
the open or the close, or a low above either (`high was < open`, `low was >
close`). `QuoteTick(instrument_id, bid_price, ask_price, bid_size, ask_size,
ts_event, ts_init)` requires the two prices to share a precision and the two
sizes to share a precision. `TradeTick(instrument_id, price, size,
aggressor_side, trade_id, ts_event, ts_init)` requires a strictly positive
size — a zero-size print cannot be represented and must be dropped or
recorded as another type. Every one of them carries `ts_init` as a readable
attribute, and a `Bar` carries `low` and `high` beside `open` and `close`, with
`low` at or under the lesser of the two and `high` at or over the greater
because the constructor refuses anything else — the adverse extreme of a
period is readable off the point that closed it, at the instant that point
became available.

Historical data
---------------
`Actor.handle_bar(bar, historical=True)` routes to `handle_historical_data`,
never to `on_bar`, and `handle_historical_data` calls `on_historical_data`
only while the component is starting or running (`common/actor.pyx`).
Measured, with one bar handed as history and one as live in each state: a
running actor records the first in `on_historical_data` alone and the second
in `on_bar` alone; a ready actor (registered, not started) and a stopped one
record nothing for either, so a bar delivered as history before `on_start`
completes or after `stop` is dropped, not queued. A bar that arrives in answer
to `request_bars` is invisible to the handler a strategy trades from in every
state; points a strategy must trade from have to arrive through the ordinary
stream.

Live and sandbox nodes
----------------------
`TradingNode(config=TradingNodeConfig)` is the live code path.
`TradingNodeConfig` shares the kernel fields with `BacktestEngineConfig` and
adds `data_clients: dict[str, Any]` and `exec_clients: dict[str, Any]`, keyed by
client name. `add_data_client_factory(name, factory)` and
`add_exec_client_factory(name, factory)` register the factories; the builder
resolves a config key `"name"` or `"name-suffix"` to the factory registered
under the part before the first hyphen, and logs an error and skips the client
when none is registered.

A custom live data client is feasible with three pieces and no engine changes:
a subclass of `LiveMarketDataClient` (constructed with `loop`, `client_id`,
`venue`, `msgbus`, `cache`, `clock`, `instrument_provider`, `config`), a config
subclassing `LiveDataClientConfig`, and a factory subclassing
`LiveDataClientFactory` whose `create(loop, name, config, msgbus, cache, clock)`
returns the client. The client overrides the `_subscribe_*` and `_request_*`
coroutines it supports; the base class tracks subscriptions and publishes to the
message bus.

kanso assembles its own `SimulatedExchange` and `BacktestExecClient` pair for a
node rather than taking the engine's convenience client. The engine fact that
assembly rests on is that `LiveExecutionEngine.process` overrides
`ExecutionEngine.process` in order to queue the event: on an L1 book an order
larger than the top level is filled there and then, *if it is still open*,
slipped one increment and filled again, both in the call that matched it. A
backtest's engine has applied the first fill by the time "still open" is read
and a live engine has not, so the remainder is never filled, never cancelled and
never reported — which is why kanso's own venue sends its events to the
synchronous implementation.

**Closing a position costs more the more positions were closed before it.** On a
NETTING venue the execution engine snapshots every closed position, and
`Portfolio.update_position` re-reads the snapshots of *this* instrument on every close
while pruning its count of every other instrument's, so two instruments closing in
turn re-unpickle each other's whole snapshot list each time: `pickle.loads` grows as
the square of the trades a run has made (a quarter of it, measured), and a
two-instrument sleeve that flips every bar never finishes. That is a design
constraint kanso cannot repair from outside the engine; what it does is measure it
here, record it, and tell a researcher that turnover — not per-bar work — is what a
card's budget buys.

Three more facts about matching bind the sleeve's sizing and its in-flight
guard. `Order.is_closed` is false for a fresh order and true once a terminal
event — filled, cancelled, rejected, denied, expired — has been applied, on
both paths alike, where the cache's `orders_inflight` and `orders_open` indexes
answer differently for the same instant (`SUBMITTED` in a backtest,
`INITIALIZED` in a node, where the live risk engine queues the order). The
exchange matches against the finest bar type it has seen for an instrument: once
a second grain is loaded, a market order from a minute handler fills at the last
one-second print and a coarser bar no longer moves the book. And a market order
larger than a quarter of the bar's volume fills as two events — a quarter of the
volume at the close and the remainder one price increment worse — so a quantity
sized to a budget at the close lands one increment over it unless the increment
is reserved.

**A resting limit the market only reaches is the fill model's to fill.** The
matching engine marks a limit that rests on the book `MAKER`, and when the market
next reaches it, `fill_limit_order` asks `FillModel.is_limit_filled()` —
`prob_fill_on_limit` — only if the order's own side of the book is exactly at its
price: for a print, the print itself; for a bar, each of the open, high, low and
close in turn, which the engine replays as prints that move both sides of an L1
book; for a quote, the bid of a buy and the ask of a sell. A price beyond the limit
fills it at the limit whatever the model says. Measured, a buy at 9.50 resting against
a market at 10.00: a bar whose low is 9.50, or a print at 9.50, fills it at
probability one and not at zero; a low of 9.49, or a print at 9.49, fills it at 9.50
under either; a sell at 10.50 behaves the same against a bar's high. A quote is the
exception worth knowing: an ask falling to exactly 9.50 while the bid is 9.48 fills
the buy under either probability, because the buy's own side is not at its price —
only a market locked at 9.50 leaves it to the model. At zero or one the model draws
no random number, so `limit_fill` is deterministic either way.

Risk configuration
------------------
`RiskEngineConfig` has exactly five fields: `bypass`, `max_order_submit_rate`,
`max_order_modify_rate`, `max_notional_per_order` and `debug`.
`max_notional_per_order` is a `dict[str, int]` keyed by instrument id string:
a per-order, per-instrument notional cap and nothing more. The engine
configures no per-strategy limit, no gross or net exposure limit and no
portfolio-level cap, so any such limit must be enforced where the order size is
chosen and where the deployed set is seen as a whole, with this as the
per-order backstop underneath.

**The risk engine performs no balance or margin check for a margin account.**
`RiskEngine._check_orders_risk_for_account` returns `True` before it reads the
free balance whenever `account.is_margin_account` (`risk/engine.pyx`, a `TODO`
in the engine's own words). Measured: a limit order for 100,000 shares at 10.00
— a million dollars — on a margin account funded with 1,000 is accepted and
filled in full, where a cash account denies it with
`NOTIONAL_EXCEEDS_FREE_BALANCE`. And `LeveragedMarginModel`, the model
`BacktestEngine.add_venue` substitutes when none is configured, computes
`notional / leverage x instrument.margin_init`: zero for an instrument whose
margin rates are zero, which is where kanso's resolver leaves them unless an
entry's `override` names `margin_init` or `margin_maint` — both are accepted
(`data/instruments.py`), and an instrument that arrives with a non-zero rate has
the venue lock margin beside whatever kanso computes, the maker/taker rule in
`AGENTS.md` again. Measured: rates of 0.5/0.25 have the model ask 500,000 initial
and 250,000 maintenance on 100,000 shares at 10.00 at leverage 1. At zero the
venue locks no margin, calls none and liquidates nothing, and the only borrowing
limit in a kanso backtest is the sleeve's own room.

Network I/O
-----------
`nautilus_trader.core.nautilus_pyo3` exposes the Rust HTTP client.
`HttpClient(default_headers=..., header_keys=..., keyed_quotas=...,
default_quota=..., timeout_secs=..., proxy_url=...)` rate-limits by key: the
keyword is `keyed_quotas` — a list of `(key, Quota)` pairs — and a request
passes the keys it should be counted against, most specific first. `Quota` is
built by `rate_per_second`, `rate_per_minute` or `rate_per_hour`, each taking a
max burst; the burst is the number of cells available in one go. `HttpClient`
offers `request`, `get`, `post`, `patch` and `delete`; `HttpResponse` carries
`status`, `headers` and `body`. `http_download(url, filepath, params=None,
headers=None, timeout_secs=None)` streams a response straight to disk without
holding it in memory, which is the transport for bulk history objects.

Corporate actions
-----------------
The engine has **no corporate-action concept**. `PositionAdjustmentType` has
exactly two members, `COMMISSION` and `FUNDING`; `PositionAdjusted` is
constructed in one place, consumed nowhere, is not a `PositionEvent` and is
never published to the bus, and `Position.apply_adjustment`'s own docstring is
about crypto commissions and perpetual funding. kanso uses it for a split
anyway, and the use is **off-label**: an engine release that gives
`PositionAdjusted` a meaning of its own is a reason to re-measure
`kanso.nautilus.splits` and `kanso.nautilus.actions`.

What it does is exact and free. `apply_adjustment` adds `quantity_change` to
`signed_qty`, recomputes `quantity`, `peak_qty` and the side, appends the event
to the position's own `adjustments`, and touches neither `events` nor
`_trade_ids` nor `_commissions` — so a split places no order, charges no
commission and leaves the fill record the runner's extraction reads exactly as
it was. What it does **not** do is rescale `avg_px_open`, which is `cdef
readonly`: after an adjustment the position's opening basis is still quoted in
shares that no longer exist, so `realized_pnl`, `realized_return` and every
account balance credited from them are wrong from the closing fill onwards.
That is a design constraint and nothing in kanso can repair it; the runner reads
none of those numbers and `criteria.integrity` denies a researched strategy all
of them.

An adjustment's `pnl_change` is added to the position's own `realized_pnl` and to
nothing else — no account balance moves — which is where a split's payment in lieu
travels to the runner's extraction and the harness. An adjustment that takes the quantity
to zero leaves the position `FLAT`, so `is_closed`, with `ts_closed` still zero and the
cache still listing it open; `Cache.update_position` re-indexes it by its own state, and
kanso dates the trade by the adjustment.

`MessageBus.publish` calls every handler subscribed to its topic before it returns, which
is what lets the venue announce a split to every sleeve inside the call that applies it.

`Portfolio.initialize_positions()` resyncs the net-position index behind such an
adjustment. It is declared on the kernel's `Portfolio` and **not** on the
read-only `PortfolioFacade` that a component's `portfolio` attribute is typed
as; in every environment kanso runs, that attribute is the kernel's own
`Portfolio`.

A `SimulationModule` is handed every market point through `pre_process(data)`
*before* the venue's matching engine sees it — `SimulatedExchange.process_bar`,
`process_quote_tick`, `process_trade_tick`, the three order-book variants,
`process_instrument_status` and `process_instrument_close` each loop the modules
first — and `SimulatedExchange.__init__` calls `module.register_base(portfolio,
msgbus, cache, clock)` and then `module.register_venue(self)`, so a module holds
the kernel's portfolio and cache and the exchange itself. That call is the only
place kanso can act between a point arriving and an order being matched against
it, and both of kanso's venues are a `SimulatedExchange`, which is why the
corporate action lives there rather than in a strategy.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
from typing import Any

ENGINE_VERSION = "1.231.0"
"""The `nautilus_trader` version every fact in this module was verified against."""

DESIGN_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "the engine pairs a component with its config through a config_cls class attribute",
        "ParquetDataCatalog accepts pyarrow tables or record batches on its write path",
        "customdataclass reads a module that postpones annotation evaluation",
        "closing a position costs the same whatever was closed before it",
    }
)
"""The claims that do **not** hold against `ENGINE_VERSION`, by design.

Each is a constraint recorded in the module docstring, not a defect. `doctor` lists
them as such and grades any other claim that fails to hold as a broken binding, which
is what keeps a raising check from reading as one more design constraint.
"""


@dataclass(frozen=True)
class Fact:
    """One engine claim, whether it holds here, and what was observed."""

    claim: str
    holds: bool
    evidence: str


def _raises(call: Callable[[], object]) -> str | None:
    """Return the rendered exception a call raises, or `None` if it succeeded."""
    try:
        call()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


# --- configs and components --------------------------------------------------


def _check_config_bases() -> tuple[bool, str]:
    from nautilus_trader.config import ActorConfig, NautilusConfig, StrategyConfig

    siblings = (
        issubclass(StrategyConfig, NautilusConfig)
        and issubclass(ActorConfig, NautilusConfig)
        and not issubclass(StrategyConfig, ActorConfig)
        and not issubclass(ActorConfig, StrategyConfig)
    )
    return siblings, (
        f"StrategyConfig MRO {[c.__name__ for c in StrategyConfig.__mro__]}; "
        f"ActorConfig MRO {[c.__name__ for c in ActorConfig.__mro__]}"
    )


def _check_config_isolation() -> tuple[bool, str]:
    from nautilus_trader.common.actor import Actor
    from nautilus_trader.config import ActorConfig, StrategyConfig
    from nautilus_trader.trading.strategy import Strategy

    class _S(StrategyConfig, frozen=True):
        pass

    class _A(ActorConfig, frozen=True):
        pass

    actor_refused = _raises(lambda: Actor(config=_S()))
    strategy_refused = _raises(lambda: Strategy(config=_A()))
    actor_ok = _raises(lambda: Actor(config=_A()))
    strategy_ok = _raises(lambda: Strategy(config=_S()))
    holds = (
        actor_refused is not None
        and strategy_refused is not None
        and actor_ok is None
        and strategy_ok is None
    )
    return holds, (
        f"Actor(StrategyConfig) -> {actor_refused}; Strategy(ActorConfig) -> {strategy_refused}; "
        f"matched pairs construct"
    )


def _check_component_bases() -> tuple[bool, str]:
    from nautilus_trader.common.actor import Actor
    from nautilus_trader.config import ActorConfig, StrategyConfig
    from nautilus_trader.trading.strategy import Strategy

    class _S(StrategyConfig, frozen=True):
        pass

    class _A(ActorConfig, frozen=True):
        pass

    class _MyStrategy(Strategy):  # type: ignore[misc]
        pass

    class _MyActor(Actor):  # type: ignore[misc]
        pass

    _MyStrategy(config=_S())
    _MyActor(config=_A())
    return issubclass(Strategy, Actor), (
        f"Strategy MRO {[c.__name__ for c in Strategy.__mro__]}; subclasses construct with config"
    )


def _check_config_cls_attribute() -> tuple[bool, str]:
    from nautilus_trader.common.actor import Actor
    from nautilus_trader.trading.strategy import Strategy

    present = hasattr(Strategy, "config_cls") or hasattr(Actor, "config_cls")
    return present, (
        "the engine defines no `config_cls` on Strategy or Actor; a config is bound to a "
        "component by the constructor's type check and by the dotted paths in "
        "ImportableStrategyConfig / ImportableActorConfig. Any `config_cls` is kanso's own."
    )


# --- catalog -----------------------------------------------------------------


def _sample_equity() -> object:
    from nautilus_trader.model.identifiers import InstrumentId, Symbol
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Currency, Price, Quantity

    return Equity(
        instrument_id=InstrumentId.from_str("AAPL.XNAS"),
        raw_symbol=Symbol("AAPL"),
        currency=Currency.from_str("USD"),
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )


def _check_order_is_closed_after_a_terminal_event() -> tuple[bool, str]:
    from nautilus_trader.common.component import TestClock
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.events import OrderDenied
    from nautilus_trader.model.identifiers import StrategyId, TraderId
    from nautilus_trader.model.objects import Quantity

    factory = OrderFactory(
        trader_id=TraderId("T-1"), strategy_id=StrategyId("S-1"), clock=TestClock()
    )
    order = factory.market(_sample_equity().id, OrderSide.BUY, Quantity.from_int(1))  # type: ignore[attr-defined]
    fresh = bool(order.is_closed)
    order.apply(
        OrderDenied(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            reason="probe",
            event_id=UUID4(),
            ts_init=0,
        )
    )
    denied = bool(order.is_closed)
    holds = not fresh and denied
    return holds, (
        f"is_closed read {fresh} on a fresh market order and {denied} once denied: an order "
        "is open from INITIALIZED until a terminal event closes it"
    )


def _probe_fills(quantity: int, *, finer: bool) -> list[tuple[float, float]]:
    """The fills of one market order from a minute handler, with or without a 1s grain.

    Three minute bars closing 11, 12, 13 and, when `finer`, two one-second bars closing
    101 and 102 halfway through the first two minutes, each 1,000 shares. The order goes
    in from the second minute's handler.
    """
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.data import Bar, BarSpecification, BarType
    from nautilus_trader.model.enums import (
        AccountType,
        AggregationSource,
        BarAggregation,
        OmsType,
        OrderSide,
        PriceType,
    )
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    equity = _sample_equity()
    minute = BarType(
        equity.id,  # type: ignore[attr-defined]
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    second = BarType(
        equity.id,  # type: ignore[attr-defined]
        BarSpecification(1, BarAggregation.SECOND, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    minute_ns = 60_000_000_000

    def bar(bar_type: object, ts: int, close: float) -> object:
        return Bar(
            bar_type,
            Price(close, 2),
            Price(close + 0.5, 2),
            Price(close - 0.5, 2),
            Price(close, 2),
            Quantity.from_int(1_000),
            ts_event=ts,
            ts_init=ts,
        )

    class Probe(Strategy):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.fills: list[tuple[float, float]] = []

        def on_start(self) -> None:
            self.subscribe_bars(minute)

        def on_bar(self, bar_: object) -> None:
            if not self.fills and int(bar_.ts_event) == 2 * minute_ns:  # type: ignore[attr-defined]
                self.submit_order(
                    self.order_factory.market(
                        equity.id,  # type: ignore[attr-defined]
                        OrderSide.BUY,
                        Quantity.from_int(quantity),
                    )
                )

        def on_order_filled(self, event: object) -> None:
            self.fills.append((float(event.last_qty), float(event.last_px)))  # type: ignore[attr-defined]

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("XNAS"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(1_000_000, USD)],
        )
        engine.add_instrument(equity)
        points = [bar(minute, i * minute_ns, 10.0 + i) for i in range(1, 4)]
        if finer:
            points += [bar(second, i * minute_ns + minute_ns // 2, 100.0 + i) for i in range(1, 3)]
        engine.add_data(points)
        probe = Probe()
        engine.add_strategy(probe)
        engine.run()
        return probe.fills
    finally:
        engine.dispose()


def _check_exchange_matches_on_the_finest_grain() -> tuple[bool, str]:
    alone = _probe_fills(10, finer=False)
    both = _probe_fills(10, finer=True)
    holds = alone == [(10.0, 12.0)] and both == [(10.0, 101.0)]
    return holds, (
        f"a market order from the second minute's handler filled at {alone} with the minute "
        f"grain alone and at {both} once one-second bars were loaded too: the exchange matches "
        "against the finest bar type it has seen for the instrument"
    )


def _check_market_order_walks_one_increment_past_a_quarter_of_volume() -> tuple[bool, str]:
    fills = _probe_fills(300, finer=True)
    holds = fills == [(250.0, 101.0), (50.0, 101.01)]
    return holds, (
        f"300 shares against a 1,000-share bar filled as {fills}: a quarter of the volume at "
        "the close, the remainder one price increment worse"
    )


_MINUTE_NS = 60_000_000_000


def _minute_type() -> Any:
    """The one-minute external bar type of the sample equity."""
    from nautilus_trader.model.data import BarSpecification, BarType
    from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType

    return BarType(
        _sample_equity().id,  # type: ignore[attr-defined]
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )


def _limit_points(kind: str, *prices: float) -> list[object]:
    """A market at 10.00 for an order to rest against, then one point per price.

    A bar is flat at 10.00 but for its low, or its high when the price is above ten; a
    print is at the price; a quote takes the prices two at a time, as a bid and an ask.
    """
    from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
    from nautilus_trader.model.enums import AggressorSide
    from nautilus_trader.model.identifiers import TradeId
    from nautilus_trader.model.objects import Price, Quantity

    instrument_id = _sample_equity().id  # type: ignore[attr-defined]
    if kind == "quote":
        pairs = [(9.99, 10.01), *zip(prices[::2], prices[1::2], strict=True)]
        return [
            QuoteTick(
                instrument_id,
                Price(bid, 2),
                Price(ask, 2),
                Quantity.from_int(100),
                Quantity.from_int(100),
                (index + 1) * _MINUTE_NS,
                (index + 1) * _MINUTE_NS,
            )
            for index, (bid, ask) in enumerate(pairs)
        ]
    made: list[object] = []
    for index, price in enumerate((10.0, *prices)):
        ts = (index + 1) * _MINUTE_NS
        if kind == "trade":
            aggressor = AggressorSide.SELLER if price < 10.0 else AggressorSide.BUYER
            made.append(
                TradeTick(
                    instrument_id,
                    Price(price, 2),
                    Quantity.from_int(100),
                    aggressor,
                    TradeId(f"P-{index}"),
                    ts,
                    ts,
                )
            )
        else:
            made.append(
                Bar(
                    _minute_type(),
                    Price(10.0, 2),
                    Price(max(price, 10.0), 2),
                    Price(min(price, 10.0), 2),
                    Price(10.0, 2),
                    Quantity.from_int(1_000),
                    ts_event=ts,
                    ts_init=ts,
                )
            )
    return made


def _probe_resting_limit(prob: float, points: list[object], side: str) -> list[tuple[object, ...]]:
    """The fills of one limit order resting from the first point's handler.

    A buy at 9.50 or a sell at 10.50 against a market at 10.00, so the order rests on the
    book as a maker; the venue's fill model fills a limit the market reaches with
    probability `prob`. Each fill is its quantity, its price and its liquidity side.
    """
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.models import FillModel
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, liquidity_side_to_str
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    equity: Any = _sample_equity()
    selling = side == "SELL"

    class Probe(Strategy):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.fills: list[tuple[object, ...]] = []
            self.sent = False

        def on_start(self) -> None:
            self.subscribe_bars(_minute_type())
            self.subscribe_trade_ticks(equity.id)
            self.subscribe_quote_ticks(equity.id)

        def _rest(self) -> None:
            if not self.sent:
                self.sent = True
                self.submit_order(
                    self.order_factory.limit(
                        equity.id,
                        OrderSide.SELL if selling else OrderSide.BUY,
                        Quantity.from_int(10),
                        Price(10.5 if selling else 9.5, 2),
                    )
                )

        def on_bar(self, bar_: object) -> None:
            self._rest()

        def on_trade_tick(self, tick: object) -> None:
            self._rest()

        def on_quote_tick(self, tick: object) -> None:
            self._rest()

        def on_order_filled(self, event: Any) -> None:
            self.fills.append(
                (
                    float(event.last_qty),
                    float(event.last_px),
                    liquidity_side_to_str(event.liquidity_side),
                )
            )

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("XNAS"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(1_000_000, USD)],
            fill_model=FillModel(prob_fill_on_limit=prob),
        )
        engine.add_instrument(equity)
        engine.add_data(points)
        probe = Probe()
        engine.add_strategy(probe)
        engine.run()
        return probe.fills
    finally:
        engine.dispose()


def _both_ways(kind: str, prices: tuple[float, ...], side: str) -> dict[float, list[Any]]:
    """The fills of one resting limit at `prob_fill_on_limit` zero and one."""
    return {
        prob: _probe_resting_limit(prob, _limit_points(kind, *prices), side) for prob in (0.0, 1.0)
    }


def _check_a_touched_limit_is_the_fill_models_to_fill() -> tuple[bool, str]:
    """`limit_fill`'s premise: a market that reaches a resting limit leaves the fill to the
    fill model, and a market that goes beyond it does not."""
    cases = (
        ("a bar's low at a buy's 9.50", "bar", (9.5,), "BUY", False),
        ("a bar's low of 9.49", "bar", (9.49,), "BUY", True),
        ("a bar's high at a sell's 10.50", "bar", (10.5,), "SELL", False),
        ("a bar's high of 10.51", "bar", (10.51,), "SELL", True),
        ("a print at a buy's 9.50", "trade", (9.5,), "BUY", False),
        ("a print at 9.49", "trade", (9.49,), "BUY", True),
    )
    seen: list[str] = []
    holds = True
    for name, kind, prices, side, through in cases:
        fills = _both_ways(kind, prices, side)
        filled = [(10.0, 10.5 if side == "SELL" else 9.5, "MAKER")]
        holds = holds and fills[1.0] == filled and fills[0.0] == (filled if through else [])
        seen.append(f"{name}: {fills[0.0]} at 0, {fills[1.0]} at 1")
    return holds, (
        "fills of a resting limit as (qty, price, liquidity) at prob_fill_on_limit 0 and 1 — "
        + "; ".join(seen)
        + ". A limit the market only reaches is the fill model's to fill, and one it goes "
        "beyond fills at its own price as a maker whatever the model says"
    )


def _check_a_quote_reaching_a_limit_from_the_far_side_fills_it() -> tuple[bool, str]:
    """What `limit_fill: through` cannot withhold: on quotes the model is asked only when
    the order's own side of the book is at its price."""
    far = _both_ways("quote", (9.48, 9.5), "BUY")
    locked = _both_ways("quote", (9.5, 9.5), "BUY")
    holds = len(far[0.0]) == len(far[1.0]) == len(locked[1.0]) == 1 and not locked[0.0]
    return holds, (
        f"a buy at 9.50 resting against 9.99/10.01: a quote of 9.48/9.50 filled it {far[0.0]} at "
        f"prob_fill_on_limit 0 and {far[1.0]} at 1; a quote locked at 9.50/9.50 filled it "
        f"{locked[0.0]} at 0 and {locked[1.0]} at 1. The fill model is asked only when the order's "
        "own side of the book — the bid of a buy — is at its price"
    )


def _check_close_cost_is_flat() -> tuple[bool, str]:
    """Count the pickle loads a run makes closing positions in two instruments in turn.

    Two instruments, one share bought and sold on alternating bars, so every bar closes a
    position; the count of `pickle.loads` across the run is compared at two lengths. A
    flat cost grows with the trades; the engine's grows with their square.
    """
    import pickle

    counts: list[int] = []
    original = pickle.loads
    calls = 0

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    for bars in (24, 48):
        calls = 0
        pickle.loads = counting
        try:
            trades = _alternating_closes(bars)
        finally:
            pickle.loads = original
        counts.append(calls)
        if trades < bars - 2:  # pragma: no cover - the engine did not close what it was told to
            return False, f"only {trades} of {bars - 1} closes happened"
    shorter, longer = counts
    flat = longer <= 2 * shorter + 8
    return flat, (
        f"{shorter} pickle loads over 24 alternating closes and {longer} over 48: the cost "
        + ("is linear in the trades" if flat else "grows with the square of the trades")
    )


def _alternating_closes(bars: int) -> int:
    """A run closing one position per bar across two instruments; the trades it made."""
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.data import Bar, BarSpecification, BarType
    from nautilus_trader.model.enums import (
        AccountType,
        AggregationSource,
        BarAggregation,
        OmsType,
        OrderSide,
        PriceType,
    )
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    venue = Venue("XNAS")
    names = [InstrumentId(Symbol(symbol), venue) for symbol in ("AAA", "BBB")]
    minute_ns = 60_000_000_000

    def equity(instrument_id: InstrumentId) -> Equity:
        return Equity(
            instrument_id=instrument_id,
            raw_symbol=instrument_id.symbol,
            currency=USD,
            price_precision=2,
            price_increment=Price.from_str("0.01"),
            lot_size=Quantity.from_int(1),
            ts_event=0,
            ts_init=0,
        )

    def bar_type(instrument_id: InstrumentId) -> BarType:
        return BarType(
            instrument_id,
            BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
            AggregationSource.EXTERNAL,
        )

    class Flipper(Strategy):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.trades = 0
            self.held: InstrumentId | None = None

        def on_start(self) -> None:
            for name in names:
                self.subscribe_bars(bar_type(name))

        def on_bar(self, bar_: object) -> None:
            target = bar_.bar_type.instrument_id  # type: ignore[attr-defined]
            if self.held is not None:
                self.submit_order(
                    self.order_factory.market(self.held, OrderSide.SELL, Quantity.from_int(1))
                )
                self.trades += 1
            self.submit_order(
                self.order_factory.market(target, OrderSide.BUY, Quantity.from_int(1))
            )
            self.held = target

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(1_000_000, USD)],
        )
        points = []
        for index in range(bars):
            name = names[index % 2]
            engine.add_instrument(equity(name)) if index < 2 else None
            ts = (index + 1) * minute_ns
            points.append(
                Bar(
                    bar_type(name),
                    Price(10.0, 2),
                    Price(10.5, 2),
                    Price(9.5, 2),
                    Price(10.0, 2),
                    Quantity.from_int(1_000),
                    ts_event=ts,
                    ts_init=ts,
                )
            )
        engine.add_data(points)
        strategy = Flipper()
        engine.add_strategy(strategy)
        engine.run()
        return strategy.trades
    finally:
        engine.dispose()


def _sample_bar(ts_event: int = 1_000, ts_init: int = 2_000, close: float | None = None) -> object:
    """One daily bar. `close` flattens the four prices onto it, so a probe reads one number."""
    from nautilus_trader.model.data import Bar, BarSpecification, BarType
    from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.objects import Price, Quantity

    bar_type = BarType(
        InstrumentId.from_str("AAPL.XNAS"),
        BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    prices = (
        (
            Price.from_str("1.00"),
            Price.from_str("2.00"),
            Price.from_str("0.50"),
            Price.from_str("1.50"),
        )
        if close is None
        else (Price(close, 2),) * 4
    )
    return Bar(
        bar_type,
        *prices,
        Quantity.from_int(10),
        ts_event=ts_event,
        ts_init=ts_init,
    )


def _check_catalog_roundtrip() -> tuple[bool, str]:
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    bar = _sample_bar()
    with tempfile.TemporaryDirectory() as directory:
        catalog = ParquetDataCatalog(directory)
        catalog.write_data([bar])
        read = catalog.bars()
        holds = len(read) == 1 and read[0].ts_event == 1_000 and read[0].ts_init == 2_000
        detail = f"bars() returned {len(read)}"
        if read:
            detail += f" with ts_event={read[0].ts_event}, ts_init={read[0].ts_init}"
    return holds, f"write_data/bars round trip: {detail}"


def _check_catalog_instrument_store() -> tuple[bool, str]:
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    equity = _sample_equity()
    with tempfile.TemporaryDirectory() as directory:
        catalog = ParquetDataCatalog(directory)
        catalog.write_data([equity])
        found = [i.id.value for i in catalog.instruments()]
    return found == ["AAPL.XNAS"], f"instruments() returned {found}"


def _check_catalog_arrow_write() -> tuple[bool, str]:
    from nautilus_trader.model.data import Bar
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
    from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

    table = ArrowSerializer.serialize_batch([_sample_bar()], Bar)
    with tempfile.TemporaryDirectory() as directory:
        catalog = ParquetDataCatalog(directory)
        table_error = _raises(lambda: catalog.write_data(table))
        batch_error = _raises(lambda: catalog.write_data(table.to_batches()))
    holds = table_error is None and batch_error is None
    return holds, (
        f"write_data(pyarrow.Table) -> {table_error}; "
        f"write_data(list[RecordBatch]) -> {batch_error}. "
        "write_data accepts Data objects only and converts them in the private _write_chunk; "
        "an Arrow ingest path must serialise with ArrowSerializer and write parquet itself."
    )


def _check_arrow_schemas() -> tuple[bool, str]:
    from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
    from nautilus_trader.serialization.arrow.serializer import (
        ArrowSerializer,
        get_schema,
        list_schemas,
        register_arrow,
    )

    registered = list_schemas()
    known = all(cls in registered for cls in (Bar, QuoteTick, TradeTick))
    table = ArrowSerializer.serialize_batch([_sample_bar()], Bar)
    back = ArrowSerializer.deserialize(Bar, table)
    holds = known and callable(register_arrow) and len(back) == 1
    return holds, (
        f"{len(registered)} registered schemas; Bar schema fields "
        f"{get_schema(Bar).names}; serialize_batch/deserialize round trip returned {len(back)}"
    )


def _check_catalog_orders_by_ts_init() -> tuple[bool, str]:
    import inspect

    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    source = inspect.getsource(ParquetDataCatalog)
    holds = "ORDER BY ts_init" in source and "ORDER BY ts_event" not in source
    return holds, (
        "the catalog's query builder appends `ts_init >= start` / `ts_init <= end` and "
        '`ORDER BY ts_init`, and its dataset path filters `pds.field("ts_init")`; '
        "`ts_event` is never used to order or filter"
    )


def _check_engine_orders_by_ts_init() -> tuple[bool, str]:
    from nautilus_trader.backtest.engine import BacktestDataIterator

    late_availability = _sample_bar(ts_event=10, ts_init=100)
    early_availability = _sample_bar(ts_event=50, ts_init=20)
    iterator = BacktestDataIterator()
    iterator.add_data("late_availability", [late_availability])
    iterator.add_data("early_availability", [early_availability])
    delivered: list[tuple[int, int]] = []
    while True:
        item = iterator.next()
        if item is None:
            break
        delivered.append((int(item.ts_event), int(item.ts_init)))
    holds = delivered == [(50, 20), (10, 100)]
    return holds, (
        "two streams whose ts_event and ts_init orders disagree were merged as "
        f"{delivered}: the engine delivers by ts_init and ignores ts_event for ordering"
    )


# --- custom data types -------------------------------------------------------


_probe_counter = count()


def _define_custom_type(annotations: dict[str, object]) -> Callable[[], type]:
    """Build a one-shot custom data class with the given field annotations.

    The annotations are real objects rather than strings because this module
    evaluates annotations lazily, which the engine's decorator cannot read; the
    class name is unique per call because the engine's registry is keyed by name
    and refuses a duplicate.
    """

    def build() -> type:
        from nautilus_trader.core.data import Data
        from nautilus_trader.model.custom import customdataclass

        name = f"_KansoProbe{next(_probe_counter)}"
        cls = type(name, (Data,), {"__annotations__": dict(annotations)})
        built: type = customdataclass(cls)  # type: ignore[no-untyped-call]
        return built

    return build


def _check_custom_data_type() -> tuple[bool, str]:
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
    from nautilus_trader.serialization.arrow.serializer import list_schemas

    probe = _define_custom_type(
        {"instrument_id": InstrumentId, "kind": str, "ratio": float},
    )()
    point = probe(
        instrument_id=InstrumentId.from_str("AAPL.XNAS"),
        kind="split",
        ratio=4.0,
        ts_event=1,
        ts_init=2,
    )
    with tempfile.TemporaryDirectory() as directory:
        catalog = ParquetDataCatalog(directory)
        catalog.write_data([point])
        read = catalog.custom_data(probe)
    registered = probe in list_schemas()
    schema_names = list(probe._schema.names)  # type: ignore[attr-defined]
    holds = registered and len(read) == 1 and read[0].data.ratio == 4.0
    return holds, (
        f"a Data subclass decorated with @customdataclass registered ({registered}) with schema "
        f"{schema_names}; catalog.custom_data returned {len(read)}, "
        "wrapped in CustomData"
    )


def _check_custom_data_field_types() -> tuple[bool, str]:
    from datetime import datetime
    from decimal import Decimal

    from nautilus_trader.model.identifiers import InstrumentId

    accepted = []
    for annotation in (InstrumentId, str, bool, float, int, bytes, dict):
        if _raises(_define_custom_type({"value": annotation})) is None:
            accepted.append(annotation.__name__)
    decimal_error = _raises(_define_custom_type({"value": Decimal}))
    datetime_error = _raises(_define_custom_type({"value": datetime}))
    holds = len(accepted) == 7 and decimal_error is not None and datetime_error is not None
    return holds, (
        f"accepted annotations {accepted}; Decimal -> {decimal_error}; datetime -> {datetime_error}"
    )


def _check_custom_data_postponed_annotations() -> tuple[bool, str]:
    from nautilus_trader.core.data import Data
    from nautilus_trader.model.custom import customdataclass

    name = f"_KansoProbe{next(_probe_counter)}"
    postponed = type(name, (Data,), {"__annotations__": {"value": "str"}})
    error = _raises(lambda: customdataclass(postponed))  # type: ignore[no-untyped-call]
    return error is None, (
        f"a string annotation, which is all `from __future__ import annotations` leaves behind on "
        f"this interpreter, raises {error}: the engine reads `cls.__annotations__` verbatim and "
        "resolves nothing, so a module defining custom data types must evaluate its annotations "
        "eagerly or set __annotations__ to real objects"
    )


def _check_custom_data_names_are_global() -> tuple[bool, str]:
    from nautilus_trader.core.data import Data
    from nautilus_trader.model.custom import customdataclass

    name = f"_KansoProbe{next(_probe_counter)}"

    def define(annotation: type) -> Callable[[], object]:
        namespace = {"__annotations__": {"value": annotation}}
        return lambda: customdataclass(type(name, (Data,), namespace))  # type: ignore[no-untyped-call]

    first = _raises(define(str))
    second = _raises(define(int))
    holds = first is None and second is not None
    return holds, (
        f"registering {name} twice raised {second}: the serializable-type registry is keyed "
        "by bare "
        "class name across the process, so custom type names are a global namespace"
    )


# --- instruments -------------------------------------------------------------


def _check_instrument_classes() -> tuple[bool, str]:
    from nautilus_trader.model.enums import AssetClass, OptionKind
    from nautilus_trader.model.identifiers import InstrumentId, Symbol
    from nautilus_trader.model.instruments import (
        CurrencyPair,
        Equity,
        FuturesContract,
        IndexInstrument,
        OptionContract,
    )
    from nautilus_trader.model.objects import Currency, Price, Quantity

    usd = Currency.from_str("USD")
    eur = Currency.from_str("EUR")
    built = [
        _sample_equity(),
        OptionContract(
            instrument_id=InstrumentId.from_str("AAPL240119C00150000.OPRA"),
            raw_symbol=Symbol("AAPL240119C00150000"),
            asset_class=AssetClass.EQUITY,
            currency=usd,
            price_precision=2,
            price_increment=Price.from_str("0.01"),
            multiplier=Quantity.from_int(100),
            lot_size=Quantity.from_int(1),
            underlying="AAPL",
            option_kind=OptionKind.CALL,
            strike_price=Price.from_str("150.00"),
            activation_ns=0,
            expiration_ns=1,
            ts_event=0,
            ts_init=0,
        ),
        FuturesContract(
            instrument_id=InstrumentId.from_str("ESZ4.XCME"),
            raw_symbol=Symbol("ESZ4"),
            asset_class=AssetClass.INDEX,
            currency=usd,
            price_precision=2,
            price_increment=Price.from_str("0.25"),
            multiplier=Quantity.from_int(50),
            lot_size=Quantity.from_int(1),
            underlying="ES",
            activation_ns=0,
            expiration_ns=1,
            ts_event=0,
            ts_init=0,
        ),
        CurrencyPair(
            instrument_id=InstrumentId.from_str("EUR/USD.SIM"),
            raw_symbol=Symbol("EUR/USD"),
            base_currency=eur,
            quote_currency=usd,
            price_precision=5,
            size_precision=0,
            price_increment=Price.from_str("0.00001"),
            size_increment=Quantity.from_int(1),
            ts_event=0,
            ts_init=0,
        ),
        IndexInstrument(
            instrument_id=InstrumentId.from_str("SPX.XCBO"),
            raw_symbol=Symbol("SPX"),
            currency=usd,
            price_precision=2,
            size_precision=0,
            price_increment=Price.from_str("0.01"),
            size_increment=Quantity.from_int(1),
            ts_event=0,
            ts_init=0,
        ),
    ]
    missing_field = _raises(
        lambda: Equity(
            instrument_id=InstrumentId.from_str("AAPL.XNAS"),
            raw_symbol=Symbol("AAPL"),
            currency=usd,
            price_precision=2,
            price_increment=Price.from_str("0.01"),
            ts_event=0,
            ts_init=0,
        )
    )
    holds = len(built) == 5 and missing_field is not None
    return holds, (
        f"constructed {[type(i).__name__ for i in built]}; "
        f"Equity without lot_size -> {missing_field}"
    )


def _check_engine_instrument_provider() -> tuple[bool, str]:
    import inspect

    from nautilus_trader.common.providers import InstrumentProvider
    from nautilus_trader.model.identifiers import InstrumentId

    signature = inspect.signature(InstrumentProvider.load)
    annotation = signature.parameters["instrument_id"].annotation
    asynchronous = [
        name
        for name in ("load_async", "load_ids_async", "load_all_async", "initialize")
        if inspect.iscoroutinefunction(getattr(InstrumentProvider, name))
    ]
    holds = annotation is InstrumentId and len(asynchronous) == 4
    return holds, (
        f"InstrumentProvider.load{signature} takes a fully qualified InstrumentId and "
        "returns None; "
        f"coroutines: {asynchronous}. Venue discovery from a bare symbol is outside this interface."
    )


# --- market data objects -----------------------------------------------------


def _check_market_data_objects() -> tuple[bool, str]:
    from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick, TradeTick
    from nautilus_trader.model.enums import (
        AggregationSource,
        AggressorSide,
        BarAggregation,
        PriceType,
    )
    from nautilus_trader.model.identifiers import InstrumentId, TradeId
    from nautilus_trader.model.objects import Price, Quantity

    instrument_id = InstrumentId.from_str("AAPL.XNAS")
    bar_type = BarType(
        instrument_id,
        BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    rendered = str(bar_type)
    parsed = BarType.from_str(rendered) == bar_type
    QuoteTick(
        instrument_id,
        Price.from_str("1.00"),
        Price.from_str("1.01"),
        Quantity.from_int(1),
        Quantity.from_int(1),
        1,
        2,
    )
    TradeTick(
        instrument_id,
        Price.from_str("1.00"),
        Quantity.from_int(1),
        AggressorSide.BUYER,
        TradeId("1"),
        1,
        2,
    )
    bad_bar = _raises(
        lambda: Bar(
            bar_type,
            Price.from_str("2.00"),
            Price.from_str("1.00"),
            Price.from_str("0.50"),
            Price.from_str("1.50"),
            Quantity.from_int(1),
            1,
            2,
        )
    )
    bad_quote = _raises(
        lambda: QuoteTick(
            instrument_id,
            Price.from_str("1.0"),
            Price.from_str("1.01"),
            Quantity.from_int(1),
            Quantity.from_int(1),
            1,
            2,
        )
    )
    bad_trade = _raises(
        lambda: TradeTick(
            instrument_id,
            Price.from_str("1.00"),
            Quantity.from_int(0),
            AggressorSide.BUYER,
            TradeId("1"),
            1,
            2,
        )
    )
    holds = (
        rendered == "AAPL.XNAS-1-DAY-LAST-EXTERNAL"
        and parsed
        and bad_bar is not None
        and bad_quote is not None
        and bad_trade is not None
    )
    return holds, (
        f"BarType renders as {rendered!r} and parses back ({parsed}); "
        f"inverted OHLC -> {bad_bar}; mismatched quote precision -> {bad_quote}; "
        f"zero trade size -> {bad_trade}"
    )


# --- nodes -------------------------------------------------------------------


def _check_trading_node() -> tuple[bool, str]:
    import inspect

    import msgspec
    from nautilus_trader.config import TradingNodeConfig
    from nautilus_trader.live.node import TradingNode

    init = inspect.signature(TradingNode.__init__)
    types = {f.name: f.type for f in msgspec.structs.fields(TradingNodeConfig)}
    holds = (
        "config" in init.parameters
        and hasattr(TradingNode, "add_data_client_factory")
        and hasattr(TradingNode, "add_exec_client_factory")
        and "data_clients" in types
        and "exec_clients" in types
    )
    return holds, (
        f"TradingNode{init}; data_clients={types.get('data_clients')}, "
        f"exec_clients={types.get('exec_clients')}; factories registered by name, resolved "
        "from the "
        "client key's text before the first hyphen"
    )


def _check_custom_live_data_client() -> tuple[bool, str]:
    import inspect

    from nautilus_trader.config import LiveDataClientConfig
    from nautilus_trader.live.data_client import LiveMarketDataClient
    from nautilus_trader.live.factories import LiveDataClientFactory

    class _Config(LiveDataClientConfig, frozen=True):
        pass

    class _Client(LiveMarketDataClient):
        pass

    class _Factory(LiveDataClientFactory):
        @staticmethod
        def create(  # type: ignore[override]
            loop: object,
            name: str,
            config: object,
            msgbus: object,
            cache: object,
            clock: object,
        ) -> object:
            return _Client

    dispatched = _Factory.create(None, "probe", _Config(), None, None, None)
    init = inspect.signature(LiveMarketDataClient.__init__)
    required = {
        "loop",
        "client_id",
        "venue",
        "msgbus",
        "cache",
        "clock",
        "instrument_provider",
        "config",
    }
    hooks = [
        name
        for name in ("_subscribe_bars", "_subscribe_quote_ticks", "_request_bars")
        if hasattr(LiveMarketDataClient, name)
    ]
    holds = required <= set(init.parameters) and len(hooks) == 3 and dispatched is _Client
    return holds, (
        f"LiveMarketDataClient{init}; a client subclass, a LiveDataClientConfig subclass and a "
        f"LiveDataClientFactory subclass ({_Client.__name__}, {_Config.__name__}, "
        f"{_Factory.__name__}) define and dispatch cleanly; overridable hooks include {hooks}"
    )


def _check_risk_engine_config() -> tuple[bool, str]:
    import msgspec
    from nautilus_trader.config import RiskEngineConfig

    types = {f.name: f.type for f in msgspec.structs.fields(RiskEngineConfig)}
    notional = types.get("max_notional_per_order")
    portfolio_limits = [
        name
        for name in types
        if any(token in name for token in ("gross", "net", "strategy", "portfolio"))
    ]
    holds = notional == dict[str, int] and not portfolio_limits
    return holds, (
        f"RiskEngineConfig fields {sorted(types)}; max_notional_per_order={notional}, keyed by "
        "instrument id — per order, per instrument. No per-strategy, gross or net exposure field."
    )


def _probe_oversized_limit(account_type: object) -> tuple[list[tuple[object, ...]], str]:
    """One limit order a thousand times the account, and what the engine did with it.

    Three minute bars at 10.00 on a venue funded with 1,000 USD; from the first bar's
    handler the strategy submits a BUY limit for 100,000 shares at 10.00. The events the
    order raised, and its final status name.
    """
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.data import Bar, BarSpecification, BarType
    from nautilus_trader.model.enums import (
        AggregationSource,
        BarAggregation,
        OmsType,
        OrderSide,
        PriceType,
    )
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    equity = _sample_equity()
    minute = BarType(
        equity.id,  # type: ignore[attr-defined]
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    minute_ns = 60_000_000_000

    class Probe(Strategy):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.events: list[tuple[object, ...]] = []
            self.order: Any = None

        def on_start(self) -> None:
            self.subscribe_bars(minute)

        def on_bar(self, bar_: object) -> None:
            if self.order is None:
                self.order = self.order_factory.limit(
                    equity.id,  # type: ignore[attr-defined]
                    OrderSide.BUY,
                    Quantity.from_int(100_000),
                    Price(10.0, 2),
                )
                self.submit_order(self.order)

        def on_order_denied(self, event: object) -> None:
            self.events.append(("denied", event.reason))  # type: ignore[attr-defined]

        def on_order_filled(self, event: object) -> None:
            self.events.append(("filled", float(event.last_qty), float(event.last_px)))  # type: ignore[attr-defined]

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("XNAS"),
            oms_type=OmsType.NETTING,
            account_type=account_type,
            base_currency=USD,
            starting_balances=[Money(1_000, USD)],
        )
        engine.add_instrument(equity)
        engine.add_data(
            [
                Bar(
                    minute,
                    Price(10.0, 2),
                    Price(10.5, 2),
                    Price(9.5, 2),
                    Price(10.0, 2),
                    Quantity.from_int(1_000_000),
                    ts_event=i * minute_ns,
                    ts_init=i * minute_ns,
                )
                for i in range(1, 4)
            ]
        )
        probe = Probe()
        engine.add_strategy(probe)
        engine.run()
        return probe.events, str(probe.order.status_string())
    finally:
        engine.dispose()


def _check_margin_account_skips_the_balance_check() -> tuple[bool, str]:
    """`if account.is_margin_account: return True` — the engine's TODO, measured."""
    from nautilus_trader.model.enums import AccountType

    on_margin, margin_status = _probe_oversized_limit(AccountType.MARGIN)
    on_cash, cash_status = _probe_oversized_limit(AccountType.CASH)
    holds = (
        on_margin == [("filled", 100_000.0, 10.0)]
        and margin_status == "FILLED"
        and len(on_cash) == 1
        and on_cash[0][0] == "denied"
        and "NOTIONAL_EXCEEDS_FREE_BALANCE" in str(on_cash[0][1])
        and cash_status == "DENIED"
    )
    return holds, (
        f"a BUY limit for 100,000 @ 10.00 on an account funded with 1,000 USD: margin -> "
        f"{on_margin} ({margin_status}); cash -> {on_cash} ({cash_status}). The risk "
        f"engine reads no balance for a margin account, so the only borrowing limit in a "
        f"backtest is what the sleeve refuses itself"
    )


def _check_zero_instrument_margin_yields_zero_margin() -> tuple[bool, str]:
    """The default margin model scales the instrument's own rate, which kanso leaves at zero."""
    from decimal import Decimal

    from nautilus_trader.accounting.margin_models import LeveragedMarginModel
    from nautilus_trader.model.enums import PositionSide
    from nautilus_trader.model.identifiers import InstrumentId, Symbol
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Currency, Price, Quantity

    model = LeveragedMarginModel()
    quantity, price = Quantity.from_int(100_000), Price.from_str("10.00")

    def asked(instrument: Any) -> tuple[list[float], list[float]]:
        initial = [
            float(model.calculate_margin_init(instrument, quantity, price, Decimal(leverage)))
            for leverage in (1, 4)
        ]
        maintenance = [
            float(
                model.calculate_margin_maint(
                    instrument, PositionSide.LONG, quantity, price, Decimal(leverage)
                )
            )
            for leverage in (1, 4)
        ]
        return initial, maintenance

    unrated: Any = _sample_equity()
    rates = (float(unrated.margin_init), float(unrated.margin_maint))
    at_zero = asked(unrated)
    rated = Equity(
        instrument_id=InstrumentId.from_str("AAPL.XNAS"),
        raw_symbol=Symbol("AAPL"),
        currency=Currency.from_str("USD"),
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(1),
        margin_init=Decimal("0.5"),
        margin_maint=Decimal("0.25"),
        ts_event=0,
        ts_init=0,
    )
    at_rate = asked(rated)
    holds = (
        rates == (0.0, 0.0)
        and at_zero == ([0.0, 0.0], [0.0, 0.0])
        and at_rate == ([500_000.0, 125_000.0], [250_000.0, 62_500.0])
    )
    return holds, (
        f"an Equity with the engine's default margin rates — the zeros kanso's resolver "
        f"leaves in place unless an override names margin_init/margin_maint — carries "
        f"{rates}, and LeveragedMarginModel on 100,000 @ 10.00 at leverage 1 and 4 asks it "
        f"{at_zero[0]} initial and {at_zero[1]} maintenance: the venue locks no margin and "
        f"liquidates nothing. The same at rates 0.5/0.25 asks {at_rate[0]} and {at_rate[1]}, "
        f"so an instrument that arrives with a rate has the venue lock margin of its own"
    )


def _check_historical_bar_never_reaches_on_bar() -> tuple[bool, str]:
    """History never reaches `on_bar`; it reaches `on_historical_data` on a running actor."""
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.actor import Actor
    from nautilus_trader.common.component import MessageBus, TestClock
    from nautilus_trader.model.identifiers import TraderId
    from nautilus_trader.portfolio.portfolio import Portfolio

    class _Recorder(Actor):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[tuple[str, int]] = []

        def on_bar(self, bar: object) -> None:
            self.seen.append(("on_bar", int(bar.ts_init)))  # type: ignore[attr-defined]

        def on_historical_data(self, data: object) -> None:
            self.seen.append(("on_historical_data", int(data.ts_init)))  # type: ignore[attr-defined]

    clock = TestClock()
    cache = Cache()
    msgbus = MessageBus(trader_id=TraderId("KANSO-001"), clock=clock)
    actor = _Recorder()
    actor.register_base(Portfolio(msgbus, cache, clock), msgbus, cache, clock)
    recorded: dict[str, list[tuple[str, int]]] = {}
    for stamp, step in enumerate((None, actor.start, actor.stop)):
        if step is not None:
            step()
        seen = len(actor.seen)
        actor.handle_bar(
            _sample_bar(ts_event=4 * stamp + 1, ts_init=4 * stamp + 2), historical=True
        )
        actor.handle_bar(_sample_bar(ts_event=4 * stamp + 3, ts_init=4 * stamp + 4))
        recorded[actor.state.name] = actor.seen[seen:]
    holds = recorded == {
        "READY": [],
        "RUNNING": [("on_historical_data", 6), ("on_bar", 8)],
        "STOPPED": [],
    }
    return holds, (
        f"one bar handed as history and one as live in each state recorded {recorded}: "
        f"history reaches on_historical_data on a running actor and never on_bar in any "
        f"state, and a ready or stopped actor drops both, so points a strategy trades from "
        f"have to arrive through the ordinary stream"
    )


def _check_bar_carries_low_high_and_ts_init() -> tuple[bool, str]:
    """The adverse range of a period, and the instant every point became available."""
    from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick, TradeTick
    from nautilus_trader.model.enums import (
        AggregationSource,
        AggressorSide,
        BarAggregation,
        PriceType,
    )
    from nautilus_trader.model.identifiers import InstrumentId, TradeId
    from nautilus_trader.model.objects import Price, Quantity

    instrument_id = InstrumentId.from_str("AAPL.XNAS")
    bar_type = BarType(
        instrument_id,
        BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )

    def daily(high: str, low: str, close: str) -> Any:
        return Bar(
            bar_type,
            Price.from_str("10.00"),
            Price.from_str(high),
            Price.from_str(low),
            Price.from_str(close),
            Quantity.from_int(1_000),
            1_000,
            2_000,
        )

    bar = daily("10.50", "9.50", "10.20")
    refused = [
        _raises(lambda: daily("9.00", "8.50", "8.80")),
        _raises(lambda: daily("10.50", "10.30", "10.20")),
    ]
    quote = QuoteTick(
        instrument_id,
        Price.from_str("9.99"),
        Price.from_str("10.01"),
        Quantity.from_int(1),
        Quantity.from_int(1),
        1_000,
        2_000,
    )
    trade = TradeTick(
        instrument_id,
        Price.from_str("10.00"),
        Quantity.from_int(1),
        AggressorSide.BUYER,
        TradeId("1"),
        1_000,
        2_000,
    )
    ohlc = tuple(float(getattr(bar, name)) for name in ("open", "high", "low", "close"))
    stamps = [int(point.ts_init) for point in (bar, quote, trade)]
    holds = (
        ohlc[2] <= min(ohlc[0], ohlc[3]) <= max(ohlc[0], ohlc[3]) <= ohlc[1]
        and refused == ["ValueError: high was < open", "ValueError: low was > close"]
        and stamps == [2_000, 2_000, 2_000]
        and all(int(point.ts_event) == 1_000 for point in (bar, quote, trade))
    )
    return holds, (
        f"Bar exposes open/high/low/close {ohlc} with low and high bracketing the two, and "
        f"refuses a high under the open or a low over the close ({refused}); Bar, QuoteTick "
        f"and TradeTick stamped ts_event=1000, ts_init=2000 answer ts_init {stamps}, so a "
        f"period's adverse extreme and the instant a point became public are both readable "
        f"off the point itself"
    )


# --- network -----------------------------------------------------------------


def _check_http_client_quotas() -> tuple[bool, str]:
    from nautilus_trader.core import nautilus_pyo3

    quota = nautilus_pyo3.Quota.rate_per_second(5)
    nautilus_pyo3.HttpClient(
        default_headers={"User-Agent": "kanso"},
        keyed_quotas=[("probe", quota)],
        default_quota=quota,
    )
    wrong_keyword = _raises(
        lambda: nautilus_pyo3.HttpClient(ratelimiter_quotas=[("probe", quota)])  # type: ignore[call-arg]
    )
    methods = [
        m
        for m in ("request", "get", "post", "patch", "delete")
        if hasattr(nautilus_pyo3.HttpClient, m)
    ]
    holds = len(methods) == 5 and wrong_keyword is not None
    return holds, (
        f"HttpClient{nautilus_pyo3.HttpClient.__text_signature__} built with keyed_quotas and "
        f"default_quota; methods {methods}; the keyword is `keyed_quotas` "
        f"(`ratelimiter_quotas` -> {wrong_keyword})"
    )


def _check_quota() -> tuple[bool, str]:
    from nautilus_trader.core import nautilus_pyo3

    built = [
        name
        for name in ("rate_per_second", "rate_per_minute", "rate_per_hour")
        if getattr(nautilus_pyo3.Quota, name)(1) is not None
    ]
    zero_burst = _raises(lambda: nautilus_pyo3.Quota.rate_per_second(0))
    holds = len(built) == 3 and zero_burst is not None
    return holds, f"Quota constructors {built}; a zero max burst is refused ({zero_burst})"


def _check_http_download() -> tuple[bool, str]:
    import inspect

    from nautilus_trader.core import nautilus_pyo3

    signature = str(inspect.signature(nautilus_pyo3.http_download))
    holds = callable(nautilus_pyo3.http_download) and "filepath" in signature
    return holds, (
        f"http_download{signature} streams a response straight to disk without holding it in memory"
    )


def _sample_position() -> tuple[Any, Callable[[str, float | None], Any]]:
    """A 1,005-share position at ten opened by one fill, and a maker of the adjustment a
    split applies to it: the quantity change, and what it paid in lieu, or nothing."""
    from decimal import Decimal

    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.model.enums import (
        OrderSide,
        OrderType,
        PositionAdjustmentType,
    )
    from nautilus_trader.model.events import OrderFilled, PositionAdjusted
    from nautilus_trader.model.identifiers import (
        AccountId,
        ClientOrderId,
        PositionId,
        StrategyId,
        TradeId,
        TraderId,
        VenueOrderId,
    )
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.model.position import Position

    instrument: Any = _sample_equity()
    trader_id, strategy_id = TraderId("KANSO-001"), StrategyId("S-1")
    account_id = AccountId("XNAS-001")
    filled = OrderFilled(
        trader_id=trader_id,
        strategy_id=strategy_id,
        instrument_id=instrument.id,
        client_order_id=ClientOrderId("O-1"),
        venue_order_id=VenueOrderId("1"),
        account_id=account_id,
        trade_id=TradeId("T-1"),
        position_id=PositionId("P-1"),
        order_side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        last_qty=Quantity.from_int(1_005),
        last_px=Price.from_str("10.00"),
        currency=instrument.quote_currency,
        commission=Money(0, instrument.quote_currency),
        liquidity_side=1,
        event_id=UUID4(),
        ts_event=0,
        ts_init=0,
    )
    position = Position(instrument=instrument, fill=filled)

    def adjustment(change: str, paid: float | None) -> Any:
        return PositionAdjusted(
            trader_id=trader_id,
            strategy_id=strategy_id,
            instrument_id=instrument.id,
            position_id=position.id,
            account_id=account_id,
            adjustment_type=PositionAdjustmentType.COMMISSION,
            quantity_change=Decimal(change),
            pnl_change=None if paid is None else Money(paid, instrument.quote_currency),
            reason="split",
            event_id=UUID4(),
            ts_event=1,
            ts_init=1,
        )

    return position, adjustment


def _check_position_adjustment() -> tuple[bool, str]:
    """A split is applied as a `PositionAdjusted`, which must cost the position nothing."""
    from nautilus_trader.model.enums import PositionAdjustmentType

    position, adjustment = _sample_position()
    position.apply_adjustment(adjustment("-905", None))
    members = sorted(member.name for member in PositionAdjustmentType)
    holds = (
        float(position.quantity) == 100.0
        and len(position.events) == 1
        and [float(money) for money in position.commissions()] == [0.0]
        and len(position.adjustments) == 1
        and members == ["COMMISSION", "FUNDING"]
        and float(position.avg_px_open) == 10.0
        and float(position.peak_qty) == 1_005.0
    )
    return holds, (
        f"a 1,005-share position adjusted by -905 holds {position.quantity} with "
        f"{len(position.events)} fill(s) and {[str(m) for m in position.commissions()]} of "
        f"commission, and keeps avg_px_open={position.avg_px_open} peak_qty={position.peak_qty}; "
        f"PositionAdjustmentType is {members}, so a split has no member of its own and the "
        f"use is off-label but free, and the opening basis stays in pre-split units"
    )


def _check_adjustment_to_flat() -> tuple[bool, str]:
    """A split that pays a position out whole leaves it flat and undated, listed open until
    the cache re-indexes it, and its payment reaches the position's own P&L and no more."""
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.model.enums import OmsType

    position, adjustment = _sample_position()
    cache = Cache()
    cache.add_position(position, OmsType.NETTING)
    position.apply_adjustment(adjustment("-1005", 50.0))
    listed = [str(held.id) for held in cache.positions_open()]
    cache.update_position(position)
    reindexed = [str(held.id) for held in cache.positions_open()]
    closed = [str(held.id) for held in cache.positions_closed()]
    holds = (
        position.is_closed
        and int(position.ts_closed) == 0
        and float(position.realized_pnl) == 50.0
        and len(position.events) == 1
        and listed == [str(position.id)]
        and reindexed == []
        and closed == [str(position.id)]
    )
    return holds, (
        f"a 1,005-share position adjusted by -1,005 with 50 paid is closed={position.is_closed} "
        f"with ts_closed={position.ts_closed}, realized_pnl={position.realized_pnl} and "
        f"{len(position.events)} fill(s); the cache lists it open {listed} until "
        f"update_position, then open {reindexed} and closed {closed}"
    )


def _check_publish_is_synchronous() -> tuple[bool, str]:
    """A split announced on the bus is taken in before the point that applied it moves on."""
    from nautilus_trader.common.component import MessageBus, TestClock
    from nautilus_trader.model.identifiers import TraderId

    msgbus = MessageBus(trader_id=TraderId("KANSO-001"), clock=TestClock())
    heard: list[object] = []
    msgbus.subscribe(topic="kanso.restated", handler=heard.append)
    msgbus.publish(topic="kanso.restated", msg="split")
    before_return = list(heard)
    return before_return == ["split"], (
        f"a handler subscribed to a topic had heard {before_return} when publish returned, "
        f"so a venue's announcement is taken in inside the call that applies the split"
    )


def _check_portfolio_resync() -> tuple[bool, str]:
    """The portfolio's net-position index has to be told a position changed outside a fill."""
    from nautilus_trader.portfolio.base import PortfolioFacade
    from nautilus_trader.portfolio.portfolio import Portfolio

    on_portfolio = hasattr(Portfolio, "initialize_positions")
    on_facade = hasattr(PortfolioFacade, "initialize_positions")
    return on_portfolio and not on_facade, (
        f"Portfolio.initialize_positions exists: {on_portfolio}; PortfolioFacade declares it: "
        f"{on_facade}. A component's `portfolio` is typed as the facade and is the kernel's "
        f"Portfolio in every environment kanso runs, which is what makes the resync reachable"
    )


def _check_simulation_module_precedes_matching() -> tuple[bool, str]:
    """The whole of `kanso.nautilus.actions`: the venue acts before it matches."""
    from decimal import Decimal

    from nautilus_trader.accounting.margin_models import LeveragedMarginModel
    from nautilus_trader.backtest.config import SimulationModuleConfig
    from nautilus_trader.backtest.engine import SimulatedExchange
    from nautilus_trader.backtest.models import FillModel, MakerTakerFeeModel
    from nautilus_trader.backtest.modules import SimulationModule
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import MessageBus, TestClock
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import TraderId, Venue
    from nautilus_trader.model.objects import Money
    from nautilus_trader.portfolio.portfolio import Portfolio

    seen: list[object] = []

    class _Probe(SimulationModule):  # type: ignore[misc]
        def pre_process(self, data: object) -> None:
            seen.append(exchange.best_bid_price(instrument.id))

        def process(self, ts_now: int) -> None:
            pass

        def log_diagnostics(self, logger: object) -> None:
            pass

        def reset(self) -> None:
            pass

    probe = _Probe(SimulationModuleConfig())
    instrument: Any = _sample_equity()
    clock = TestClock()
    cache = Cache()
    msgbus = MessageBus(trader_id=TraderId("KANSO-001"), clock=clock)
    exchange = SimulatedExchange(
        venue=Venue("XNAS"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(100_000, USD)],
        base_currency=USD,
        default_leverage=Decimal(1),
        leverages={},
        margin_model=LeveragedMarginModel(),
        modules=[probe],
        portfolio=Portfolio(msgbus, cache, clock),
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        fill_model=FillModel(),
        fee_model=MakerTakerFeeModel(),
        bar_execution=True,
    )
    exchange.add_instrument(instrument)
    exchange.process_bar(_sample_bar(ts_event=1_000, ts_init=1_000, close=10.0))
    exchange.process_bar(_sample_bar(ts_event=2_000, ts_init=2_000, close=100.0))
    # The other three of a module's four calls, so this check fails if any of them stops
    # being reachable: the clock tick the venue makes after it settles, and the two the
    # engine's own reset and diagnostics paths make.
    exchange.process(2_000)
    probe.log_diagnostics(None)
    probe.reset()
    marks = [None if price is None else float(price) for price in seen]  # type: ignore[arg-type]
    holds = marks == [None, 10.0]
    return holds, (
        f"a module's pre_process saw the venue's best bid at {marks} while processing bars "
        f"priced 10.00 then 100.00: the second call reached it before the matching engine "
        f"had moved the book, so a module acts on a point before the venue matches against it"
    )


def _check_live_exec_engine_queues_events() -> tuple[bool, str]:
    """`LiveExecutionEngine.process` overrides the synchronous implementation.

    The fact kanso's simulated venue rests on. On an L1 book an order larger than the
    top level is filled there and, if it is *still open*, slipped one increment and
    filled again — both inside the call that matched it, with "still open" read off the
    order. A backtest's engine applies the first fill event synchronously, so the order
    is partially filled by the time that is read; a live engine queues it, so it is not,
    and the remainder is never filled, cancelled or reported. kanso therefore delivers
    its own venue's events to the synchronous implementation. An engine release in which
    these are the same function would make that relay unnecessary — and one in which the
    synchronous one stopped existing would make it impossible.
    """
    from nautilus_trader.execution.engine import ExecutionEngine
    from nautilus_trader.live.execution_engine import LiveExecutionEngine

    synchronous = getattr(ExecutionEngine, "process", None)
    queued = getattr(LiveExecutionEngine, "process", None)
    holds = callable(synchronous) and callable(queued) and synchronous is not queued
    return holds, (
        f"ExecutionEngine.process is {type(synchronous).__name__} and "
        f"LiveExecutionEngine.process is {type(queued).__name__}; the live engine overrides "
        f"it ({synchronous is not queued}), so an event sent to the base implementation is "
        "applied to its order before the venue decides what is left of it"
    )


_CHECKS: tuple[tuple[str, Callable[[], tuple[bool, str]]], ...] = (
    (
        "StrategyConfig and ActorConfig are distinct sibling bases under NautilusConfig",
        _check_config_bases,
    ),
    (
        "a StrategyConfig cannot configure an Actor, nor an ActorConfig a Strategy",
        _check_config_isolation,
    ),
    (
        "Strategy subclasses Actor and both accept their own config at construction",
        _check_component_bases,
    ),
    (
        "the engine pairs a component with its config through a config_cls class attribute",
        _check_config_cls_attribute,
    ),
    (
        "ParquetDataCatalog writes Data objects and reads them back by type",
        _check_catalog_roundtrip,
    ),
    (
        "the catalog is the instrument store",
        _check_catalog_instrument_store,
    ),
    (
        "ParquetDataCatalog accepts pyarrow tables or record batches on its write path",
        _check_catalog_arrow_write,
    ),
    (
        "the engine exposes its Arrow schemas and a registration entry point",
        _check_arrow_schemas,
    ),
    (
        "the catalog filters and orders every query by ts_init",
        _check_catalog_orders_by_ts_init,
    ),
    (
        "the backtest engine sorts, merges and clocks its data stream by ts_init",
        _check_engine_orders_by_ts_init,
    ),
    (
        "a custom Data type is registered by subclassing Data and applying customdataclass",
        _check_custom_data_type,
    ),
    (
        "custom data fields are restricted to InstrumentId, str, bool, float, int, bytes, "
        "ndarray and dict",
        _check_custom_data_field_types,
    ),
    (
        "customdataclass reads a module that postpones annotation evaluation",
        _check_custom_data_postponed_annotations,
    ),
    (
        "a custom data type name may be registered once per process",
        _check_custom_data_names_are_global,
    ),
    (
        "the five instrument classes construct from the fields kanso must supply",
        _check_instrument_classes,
    ),
    (
        "the engine's InstrumentProvider requires a fully qualified InstrumentId and is "
        "asynchronous",
        _check_engine_instrument_provider,
    ),
    (
        "Bar, BarType, QuoteTick and TradeTick construct and validate their fields",
        _check_market_data_objects,
    ),
    (
        "TradingNode is configured by TradingNodeConfig and takes client factories by name",
        _check_trading_node,
    ),
    (
        "a custom live data client needs only a client, a config and a factory subclass",
        _check_custom_live_data_client,
    ),
    (
        "RiskEngineConfig caps notional per order per instrument and nothing wider",
        _check_risk_engine_config,
    ),
    (
        "the risk engine performs no balance or margin check for a margin account",
        _check_margin_account_skips_the_balance_check,
    ),
    (
        "LeveragedMarginModel asks zero margin of an instrument whose margin rates are zero",
        _check_zero_instrument_margin_yields_zero_margin,
    ),
    (
        "handle_bar(historical=True) routes to on_historical_data and never to on_bar",
        _check_historical_bar_never_reaches_on_bar,
    ),
    (
        "a Bar carries low and high, and every market point carries ts_init",
        _check_bar_carries_low_high_and_ts_init,
    ),
    (
        "nautilus_pyo3.HttpClient takes a default quota and per-key quotas",
        _check_http_client_quotas,
    ),
    (
        "nautilus_pyo3.Quota expresses a rate per second, minute or hour",
        _check_quota,
    ),
    (
        "nautilus_pyo3.http_download streams a URL to a file path",
        _check_http_download,
    ),
    (
        "a live execution engine queues an order event where a backtest's applies it",
        _check_live_exec_engine_queues_events,
    ),
    (
        "a position adjustment changes the quantity and leaves the fills, the commissions "
        "and the opening basis alone",
        _check_position_adjustment,
    ),
    (
        "a position adjustment to zero leaves the position flat and undated until the cache "
        "re-indexes it, and its pnl_change reaches only the position's own realized P&L",
        _check_adjustment_to_flat,
    ),
    (
        "MessageBus.publish calls every handler subscribed to the topic before it returns",
        _check_publish_is_synchronous,
    ),
    (
        "Portfolio.initialize_positions resyncs the net-position index the facade does not declare",
        _check_portfolio_resync,
    ),
    (
        "a simulation module is handed every market point before the venue matches against it",
        _check_simulation_module_precedes_matching,
    ),
    (
        "an order is open from INITIALIZED until a terminal event closes it, on both paths",
        _check_order_is_closed_after_a_terminal_event,
    ),
    (
        "the exchange matches against the finest bar type it has seen for an instrument",
        _check_exchange_matches_on_the_finest_grain,
    ),
    (
        "a market order past a quarter of the bar's volume walks one increment for the rest",
        _check_market_order_walks_one_increment_past_a_quarter_of_volume,
    ),
    (
        "a resting limit a bar or a print only reaches fills by prob_fill_on_limit, and one "
        "it goes beyond fills at its price whatever that is",
        _check_a_touched_limit_is_the_fill_models_to_fill,
    ),
    (
        "a quote reaching a resting limit from the far side of the book fills it whatever "
        "prob_fill_on_limit is",
        _check_a_quote_reaching_a_limit_from_the_far_side_fills_it,
    ),
    (
        "closing a position costs the same whatever was closed before it",
        _check_close_cost_is_flat,
    ),
)


def verify() -> list[Fact]:
    """Check every engine claim against the installed package.

    Runs offline and touches only a temporary directory. A check that raises is
    reported as a claim that does not hold, with the exception as its evidence,
    so one broken binding never hides the rest.
    """
    facts: list[Fact] = []
    for claim, check in _CHECKS:
        try:
            holds, evidence = check()
        except Exception as exc:
            facts.append(Fact(claim=claim, holds=False, evidence=f"{type(exc).__name__}: {exc}"))
            continue
        facts.append(Fact(claim=claim, holds=holds, evidence=evidence))
    return facts
