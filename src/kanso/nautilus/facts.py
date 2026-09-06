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
dotted-path triple `ImportableStrategyConfig(strategy_path, config_path,
config)` and `ImportableActorConfig(actor_path, config_path, config)` used when
a node builds components from configuration. Where kanso exposes `config_cls`
it is kanso's own attribute, honoured by kanso's own loader, and the engine
neither reads nor validates it.

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
round-trips through the catalog, where it is returned wrapped in `CustomData`,
and `BacktestDataConfig.data_cls` names it by dotted path, so any registered
type is loadable into a backtest by name.

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
the open. `QuoteTick(instrument_id, bid_price, ask_price, bid_size, ask_size,
ts_event, ts_init)` requires the two prices to share a precision and the two
sizes to share a precision. `TradeTick(instrument_id, price, size,
aggressor_side, trade_id, ts_event, ts_init)` requires a strictly positive
size — a zero-size print cannot be represented and must be dropped or
recorded as another type.

Backtests
---------
`BacktestNode(configs=[BacktestRunConfig, ...])` with `run() -> list[BacktestResult]`
is the research code path. `BacktestRunConfig` carries `venues`, `data`,
`engine`, `chunk_size`, `raise_exception`, `dispose_on_completion`, `start`,
`end` and `data_clients`. `BacktestVenueConfig` is where the trading model
lives: `oms_type`, `account_type`, `starting_balances`, `base_currency`,
`default_leverage`, `leverages`, `fill_model`, `latency_model`, `fee_model`,
`book_type`, `bar_execution`, `trade_execution` and more. `BacktestDataConfig`
selects from a catalog by `catalog_path`, `data_cls` (a dotted path string),
`instrument_id`/`instrument_ids`/`bar_types` and `start_time`/`end_time`.
`BacktestEngineConfig` holds `actors`, `strategies`, `risk_engine`, `streaming`
and the rest of the kernel configuration.

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

Sandbox execution is `SandboxExecutionClient`, a `LiveExecutionClient`,
configured by `SandboxExecutionClientConfig` (`venue`, `starting_balances`,
`base_currency`, `oms_type`, `account_type`, `default_leverage`, `book_type`,
`bar_execution`, `trade_execution` and the order-support switches) and built by
`SandboxLiveExecClientFactory`. It owns a `SimulatedExchange` driven by a
`TestClock` and subscribes on the message bus to `data.*.{venue}.*`; its
`on_data` handler feeds `Instrument`, `InstrumentStatus`, `InstrumentClose`,
`OrderBookDelta`, `OrderBookDeltas`, `OrderBookDepth10`, `QuoteTick`,
`TradeTick` and `Bar` into the exchange and then calls `exchange.process(data.ts_init)`.
Fills therefore exist only for venues whose data reaches the bus, and the
sandbox clock follows the data's `ts_init`, not wall time.

kanso does not use that client. `SandboxExecutionClientConfig` exposes neither
`use_message_queue` nor `fee_model` nor `fill_model`, so the matching a node
does cannot be brought into line with a backtest's through it; kanso assembles
the same `SimulatedExchange` and `BacktestExecClient` pair itself instead. The
engine fact that assembly rests on is that `LiveExecutionEngine.process`
overrides `ExecutionEngine.process` in order to queue the event: on an L1 book
an order larger than the top level is filled there and then, *if it is still
open*, slipped one increment and filled again, both in the call that matched it.
A backtest's engine has applied the first fill by the time "still open" is read
and a live engine has not, so the remainder is never filled, never cancelled and
never reported — which is why kanso's own venue sends its events to the
synchronous implementation.

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

Session persistence and replay
------------------------------
`StreamingConfig(catalog_path=...)` on either `BacktestEngineConfig` or
`TradingNodeConfig` makes the kernel construct a `StreamingFeatherWriter` at
`<catalog_path>/<environment>/<instance_id>` and subscribe it to `"*"` on the
trader's message bus, so every data object and every event flows to feather
files, alongside a `config.json` copy of the kernel configuration.
`include_types` narrows what is written; the rotation options bound file size.

Reading a session back is only half-covered by the engine. `Environment` has
three values — `backtest`, `sandbox` and `live` — and the writer path uses the
environment's value, but the catalog exposes run listings for two of them only:
`list_backtest_runs()` / `read_backtest(instance_id)` and `list_live_runs()` /
`read_live_run(instance_id)`. **A sandbox node's session is written to
`<catalog>/sandbox/<instance_id>` and is invisible to both listings.** The
reachable path for it is `convert_stream_to_data(instance_id, data_cls,
subdirectory="sandbox")`, which deserialises a stream and writes it into a
catalog; the generic reader behind the two run listings is private. A component
that runs sandbox sessions must therefore keep its own index of session ids
rather than discovering them from the catalog.

`read_backtest` and `read_live_run` return the whole stream sorted by
`ts_init`. Order intents survive the round trip: `OrderInitialized` serialises
with `instrument_id`, `order_side`, `order_type`, `quantity`, `price` and its
timestamp, and deserialises to the same values. Its Arrow schema carries
`ts_init` but no `ts_event` column, which loses nothing because
`OrderInitialized` takes only `ts_init` at construction and reports
`ts_event == ts_init`; a comparison of order-intent sequences across two code
paths reads that one timestamp.

Network I/O
-----------
`nautilus_trader.core.nautilus_pyo3` exposes the Rust HTTP and WebSocket
clients. `HttpClient(default_headers=..., header_keys=..., keyed_quotas=...,
default_quota=..., timeout_secs=..., proxy_url=...)` rate-limits by key: the
keyword is `keyed_quotas` — a list of `(key, Quota)` pairs — and a request
passes the keys it should be counted against, most specific first. `Quota` is
built by `rate_per_second`, `rate_per_minute` or `rate_per_hour`, each taking a
max burst; the burst is the number of cells available in one go. `HttpClient`
offers `request`, `get`, `post`, `patch` and `delete`; `HttpResponse` carries
`status`, `headers` and `body`. `http_download(url, filepath, params=None,
headers=None, timeout_secs=None)` streams a response straight to disk without
holding it in memory, which is the transport for bulk history objects.
`WebSocketClient.connect(...)` runs in handler mode with automatic reconnection
and exponential backoff, configured by `WebSocketConfig(url, headers,
heartbeat, heartbeat_msg, reconnect_timeout_ms, reconnect_delay_initial_ms,
reconnect_delay_max_ms, reconnect_backoff_factor, reconnect_jitter_ms,
reconnect_max_attempts, idle_timeout_ms, proxy_url, backend)`; `send`,
`send_text`, `send_pong` and `disconnect` complete the surface.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count

ENGINE_VERSION = "1.231.0"
"""The `nautilus_trader` version every fact in this module was verified against."""

DESIGN_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "the engine pairs a component with its config through a config_cls class attribute",
        "ParquetDataCatalog accepts pyarrow tables or record batches on its write path",
        "customdataclass reads a module that postpones annotation evaluation",
        "a persisted session is discoverable through the catalog's run listings for every "
        "node environment",
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


# --- version -----------------------------------------------------------------


def _check_version() -> tuple[bool, str]:
    import nautilus_trader

    installed = str(nautilus_trader.__version__)
    return installed == ENGINE_VERSION, f"installed {installed}, verified against {ENGINE_VERSION}"


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
        "ImportableStrategyConfig(strategy_path, config_path, config) / "
        "ImportableActorConfig(actor_path, config_path, config). Any `config_cls` is kanso's own."
    )


def _check_importable_configs() -> tuple[bool, str]:
    from nautilus_trader.config import ImportableActorConfig, ImportableStrategyConfig

    s = tuple(ImportableStrategyConfig.__struct_fields__)
    a = tuple(ImportableActorConfig.__struct_fields__)
    holds = s == ("strategy_path", "config_path", "config") and a == (
        "actor_path",
        "config_path",
        "config",
    )
    return holds, f"ImportableStrategyConfig{s}; ImportableActorConfig{a}"


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


def _sample_bar(ts_event: int = 1_000, ts_init: int = 2_000) -> object:
    from nautilus_trader.model.data import Bar, BarSpecification, BarType
    from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.objects import Price, Quantity

    bar_type = BarType(
        InstrumentId.from_str("AAPL.XNAS"),
        BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    return Bar(
        bar_type,
        Price.from_str("1.00"),
        Price.from_str("2.00"),
        Price.from_str("0.50"),
        Price.from_str("1.50"),
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


def _check_data_timestamps() -> tuple[bool, str]:
    bar = _sample_bar(ts_event=5, ts_init=9)
    ts_event = int(bar.ts_event)  # type: ignore[attr-defined]
    ts_init = int(bar.ts_init)  # type: ignore[attr-defined]
    return (ts_event, ts_init) == (5, 9), (
        f"Data exposes independent ts_event={ts_event} and ts_init={ts_init} nanosecond fields"
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


def _check_backtest_data_cls_is_a_path() -> tuple[bool, str]:
    import msgspec
    from nautilus_trader.config import BacktestDataConfig

    types = {f.name: f.type for f in msgspec.structs.fields(BacktestDataConfig)}
    return types.get("data_cls") is str, (
        f"BacktestDataConfig.data_cls is {types.get('data_cls')}, a dotted path, so a registered "
        "custom type is selectable by name"
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


def _check_backtest_node() -> tuple[bool, str]:
    import inspect

    from nautilus_trader.backtest.node import BacktestNode
    from nautilus_trader.backtest.results import BacktestResult
    from nautilus_trader.config import (
        BacktestDataConfig,
        BacktestEngineConfig,
        BacktestRunConfig,
        BacktestVenueConfig,
    )

    init = inspect.signature(BacktestNode.__init__)
    run = inspect.signature(BacktestNode.run)
    run_fields = tuple(BacktestRunConfig.__struct_fields__)
    holds = (
        "configs" in init.parameters
        and BacktestResult is not None
        and {"venues", "data", "engine", "start", "end"} <= set(run_fields)
        and hasattr(BacktestVenueConfig, "__struct_fields__")
        and hasattr(BacktestDataConfig, "__struct_fields__")
        and hasattr(BacktestEngineConfig, "__struct_fields__")
    )
    return holds, (f"BacktestNode{init} with run{run}; BacktestRunConfig{run_fields}")


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


def _check_sandbox_exec_client() -> tuple[bool, str]:
    import inspect

    from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
    from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
    from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
    from nautilus_trader.live.execution_client import LiveExecutionClient

    create = inspect.signature(SandboxLiveExecClientFactory.create)
    fields = tuple(SandboxExecutionClientConfig.__struct_fields__)
    holds = (
        issubclass(SandboxExecutionClient, LiveExecutionClient)
        and {"venue", "starting_balances", "account_type", "oms_type"} <= set(fields)
        and set(create.parameters) >= {"loop", "name", "config", "msgbus", "cache", "clock"}
    )
    return holds, (
        f"SandboxExecutionClient MRO {[c.__name__ for c in SandboxExecutionClient.__mro__]}; "
        f"SandboxExecutionClientConfig{fields}; factory.create{create}"
    )


def _check_sandbox_subscription() -> tuple[bool, str]:
    import inspect

    from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient

    init_source = inspect.getsource(SandboxExecutionClient.__init__)
    on_data_source = inspect.getsource(SandboxExecutionClient.on_data)
    subscribes = 'self._msgbus.subscribe(f"data.*.{self.venue}.*", handler=self.on_data)' in (
        inspect.getsource(SandboxExecutionClient)
    )
    handled = [
        name
        for name in (
            "Instrument",
            "InstrumentStatus",
            "InstrumentClose",
            "OrderBookDelta",
            "OrderBookDeltas",
            "OrderBookDepth10",
            "QuoteTick",
            "TradeTick",
            "Bar",
        )
        if f"isinstance(data, {name})" in on_data_source
    ]
    clocked = "self.exchange.process(data.ts_init)" in on_data_source
    holds = subscribes and len(handled) == 9 and clocked and "SimulatedExchange(" in init_source
    return holds, (
        f"subscribes to data.*.{{venue}}.* ({subscribes}); on_data handles {handled}; "
        f"advances the simulated exchange to data.ts_init ({clocked})"
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


# --- sessions ----------------------------------------------------------------


def _check_streaming_config() -> tuple[bool, str]:
    import inspect

    from nautilus_trader.config import (
        BacktestEngineConfig,
        StreamingConfig,
        TradingNodeConfig,
    )
    from nautilus_trader.persistence.writer import StreamingFeatherWriter
    from nautilus_trader.system.kernel import NautilusKernel

    fields = tuple(StreamingConfig.__struct_fields__)
    setup = inspect.getsource(NautilusKernel._setup_streaming)
    path_shape = 'f"{config.catalog_path}/{self._environment.value}/{self.instance_id}"' in setup
    subscribes_all = 'self._trader.subscribe("*", self._writer.write)' in setup
    holds = (
        "streaming" in BacktestEngineConfig.__struct_fields__
        and "streaming" in TradingNodeConfig.__struct_fields__
        and "catalog_path" in fields
        and path_shape
        and subscribes_all
        and StreamingFeatherWriter is not None
    )
    return holds, (
        f"StreamingConfig{fields} on both engine and node configs; the kernel writes feather to "
        f"<catalog_path>/<environment>/<instance_id> ({path_shape}) with the writer subscribed to "
        f'"*" on the message bus ({subscribes_all})'
    )


def _check_session_listings() -> tuple[bool, str]:
    from nautilus_trader.common import Environment
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    environments = [e.value for e in Environment]
    listed = [
        value
        for value in environments
        if hasattr(ParquetDataCatalog, f"list_{value}_runs")
        or hasattr(ParquetDataCatalog, f"read_{value}")
    ]
    holds = sorted(listed) == sorted(environments)
    return holds, (
        f"Environment values {environments} but run listings exist only for {listed}: "
        "list_backtest_runs/read_backtest and list_live_runs/read_live_run. A sandbox session is "
        "written to <catalog>/sandbox/<instance_id> and is invisible to both; the generic reader "
        "behind them is private, so sandbox session ids must be tracked outside the catalog."
    )


def _check_stream_conversion() -> tuple[bool, str]:
    import inspect

    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    signature = inspect.signature(ParquetDataCatalog.convert_stream_to_data)
    subdirectory = signature.parameters.get("subdirectory")
    holds = subdirectory is not None and subdirectory.default == "backtest"
    return holds, (
        f"convert_stream_to_data{signature}: `subdirectory` selects the environment folder, so "
        '`subdirectory="sandbox"` reaches a sandbox session the run listings cannot see'
    )


def _check_order_intent_roundtrip() -> tuple[bool, str]:
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.model.enums import (
        ContingencyType,
        OrderSide,
        OrderType,
        TimeInForce,
        TriggerType,
    )
    from nautilus_trader.model.events import OrderInitialized
    from nautilus_trader.model.identifiers import (
        ClientOrderId,
        InstrumentId,
        StrategyId,
        TraderId,
    )
    from nautilus_trader.model.objects import Quantity
    from nautilus_trader.serialization.arrow.serializer import ArrowSerializer, get_schema

    event = OrderInitialized(
        TraderId("TRADER-001"),
        StrategyId("S-1"),
        InstrumentId.from_str("AAPL.XNAS"),
        ClientOrderId("O-1"),
        OrderSide.BUY,
        OrderType.MARKET,
        Quantity.from_int(1),
        TimeInForce.GTC,
        False,
        False,
        False,
        {},
        TriggerType.NO_TRIGGER,
        None,
        ContingencyType.NO_CONTINGENCY,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        UUID4(),
        123,
    )
    table = ArrowSerializer.serialize_batch([event], OrderInitialized)
    back = ArrowSerializer.deserialize(OrderInitialized, table)[0]
    names = list(get_schema(OrderInitialized).names)
    holds = (
        event.ts_event == event.ts_init == 123
        and back.ts_init == 123
        and back.instrument_id == event.instrument_id
        and back.side == event.side
        and back.order_type == event.order_type
        and back.quantity == event.quantity
        and "ts_event" not in names
    )
    return holds, (
        "OrderInitialized takes only ts_init and reports ts_event == ts_init; its Arrow schema "
        f"carries ts_init and no ts_event column ({'ts_event' not in names}); instrument_id, "
        "order_side, order_type, quantity and price survive the round trip"
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


def _check_websocket_client() -> tuple[bool, str]:
    from nautilus_trader.core import nautilus_pyo3

    config = nautilus_pyo3.WebSocketConfig(
        url="wss://localhost:1/",
        headers=[("User-Agent", "kanso")],
        heartbeat=30,
    )
    methods = [
        m
        for m in ("connect", "send", "send_text", "send_pong", "disconnect")
        if hasattr(nautilus_pyo3.WebSocketClient, m)
    ]
    holds = len(methods) == 5 and config is not None
    return holds, (
        f"WebSocketConfig{nautilus_pyo3.WebSocketConfig.__text_signature__} constructs offline; "
        f"WebSocketClient methods {methods}; handler mode reconnects with exponential backoff"
    )


def _check_http_download() -> tuple[bool, str]:
    import inspect

    from nautilus_trader.core import nautilus_pyo3

    signature = str(inspect.signature(nautilus_pyo3.http_download))
    holds = callable(nautilus_pyo3.http_download) and "filepath" in signature
    return holds, (
        f"http_download{signature} streams a response straight to disk without holding it in memory"
    )


def _check_sandbox_fill_knobs() -> tuple[bool, str]:
    """The engine's convenience venue exposes none of its exchange's matching knobs.

    This is why kanso assembles its own `SimulatedExchange` and `BacktestExecClient`
    from the engine's components instead of using that client: with the fill model,
    the fee model and the command queue all private, a node could not be made to fill
    what a backtest fills. It is checked so that an engine release exposing them is
    noticed here rather than left as a simplification nobody takes.
    """
    from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig

    fields = set(SandboxExecutionClientConfig.__struct_fields__)
    private = {"use_message_queue", "fee_model", "fill_model"} - fields
    holds = private == {"use_message_queue", "fee_model", "fill_model"}
    return holds, (
        f"SandboxExecutionClientConfig exposes none of {sorted(private)}, so the matching a "
        "node does could not be brought into line with a backtest's through it"
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
        "the installed engine is the version these facts were verified against",
        _check_version,
    ),
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
        "ImportableStrategyConfig and ImportableActorConfig name a component and its config "
        "by dotted path",
        _check_importable_configs,
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
        "Data carries independent ts_event and ts_init nanosecond timestamps",
        _check_data_timestamps,
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
        "BacktestDataConfig names its data class by dotted path",
        _check_backtest_data_cls_is_a_path,
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
        "BacktestNode runs a list of BacktestRunConfig and returns BacktestResults",
        _check_backtest_node,
    ),
    (
        "TradingNode is configured by TradingNodeConfig and takes client factories by name",
        _check_trading_node,
    ),
    (
        "sandbox execution is a LiveExecutionClient with a config and a factory",
        _check_sandbox_exec_client,
    ),
    (
        "the sandbox client drives a SimulatedExchange from data on the message bus",
        _check_sandbox_subscription,
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
        "StreamingConfig persists a node session through the kernel's feather writer",
        _check_streaming_config,
    ),
    (
        "a persisted session is discoverable through the catalog's run listings for every "
        "node environment",
        _check_session_listings,
    ),
    (
        "convert_stream_to_data replays a persisted stream from any environment folder",
        _check_stream_conversion,
    ),
    (
        "an order intent survives the session stream with the fields a parity check compares",
        _check_order_intent_roundtrip,
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
        "nautilus_pyo3.WebSocketClient connects with a reconnecting WebSocketConfig",
        _check_websocket_client,
    ),
    (
        "nautilus_pyo3.http_download streams a URL to a file path",
        _check_http_download,
    ),
    (
        "the sandbox execution client's matching knobs are private and it matches on submit",
        _check_sandbox_fill_knobs,
    ),
    (
        "a live execution engine queues an order event where a backtest's applies it",
        _check_live_exec_engine_queues_events,
    ),
)


def claims() -> list[str]:
    """Every engine claim `verify()` checks, in order."""
    return [claim for claim, _ in _CHECKS]


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
