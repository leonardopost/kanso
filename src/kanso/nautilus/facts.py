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

**The SQL query does not keep an instant's points in file order; a query handed its files
does.** `query` reads a built-in type through DataFusion with `ORDER BY ts_init`, and that
sort is not stable: the points of one instant come back in an order that depends on the span
asked for. Measured on a day of one crypto venue's BTC/USDT perpetual-swap prints, 79 of
524,932 instants came back reordered read an hour at a time against read whole — trade ids
848, 849, 850, 851 in the file, 848, 850, 849, 851 in the hour — and a synthetic file of
30,000 prints holding 1 to 40 to an instant came back out of file order read whole. Given
`files=`, `query` takes the dataset path for every type: it reads the files in the order
given, each in its own row order, filters `ts_init` inclusively at both ends and sorts only
when the rows are out of `ts_init` order, with a stable sort. `filter_files(data_cls,
get_file_list_from_data_cls(data_cls), [identifier], start, end)` names one identifier's
files that intersect a span, and their names, the interval each covers, sort in time order.
kanso reads quotes, prints and book changes that way
(`kanso.nautilus.backtest._in_file_order`).

**A series is filed in one directory, each file named by the interval it holds.**
`write_data` files a series' points under `data/<class_to_filename(class)>/` and then
`urisafe_identifier(identifier)` — the bar type of a bar, the instrument id of anything else
instrument-scoped — and a series of no instrument in the class's directory itself; both
functions are public in `nautilus_trader.persistence.funcs`. Each file is named
`<first ts_init>_<last ts_init>.parquet`, both to the nanosecond
(`kanso.data.catalog.ENGINE_FILE`), and `filter_files(data_cls, paths, None, start, end)`
keeps the paths whose interval, read off the name, meets the span, ends included, an end
passed as `None` being open. So a write finds what it produced by listing one directory, and
a replace looks for what it removes among that directory's files so named, leaving out with
`filter_files(..., None, first - 1)` those that begin before a dataset's first instant
(`kanso.data.catalog._filed_in`, `_owned`). It does not ask `delete_data_range`, which
**cannot remove a series of no instrument**: given no identifier it runs once for each
instrument's directory of the class and never on the class's own, so it removes every
instrument's points of the class over the span and leaves the series' files where they are.

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
delivers the second first. Nothing in the engine reads `ts_event` for ordering or
clocking. Two parts filter on it, one inside the other: a top-of-book matching
engine ignores a quote or a print stamped before its book's last update (below), and
the level-one `OrderBook`'s own `update_quote_tick` and `update_trade_tick`, which a
bar's walk writes through, ignore one too. `kanso.nautilus.availability` undoes both,
since its reset zeroes the `ts_last` both read. A catalog round-trip preserves the two
independently, so a point stamped with a publication instant later than its
reference time is delivered at the publication instant and never earlier.

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

A `CustomData` point pickles through `__reduce_cython__`, and its payload goes by
reference: the pickle names the payload class's module and qualified name, and an
unpickler imports that module to find the class. So a process handed pickled points of a
type a workspace extension defines has to import that extension first, under the module
name the extension was imported by where the points were pickled — which is what a card
child is handed its parent's extensions for (`kanso.ext.reimport`).

Instruments
-----------
The six classes kanso resolves are `Equity`, `OptionContract`,
`FuturesContract`, `CurrencyPair`, `IndexInstrument` and `CryptoPerpetual`.
Their constructors are Cython and positional-or-keyword with no introspectable
signature; the required fields, established by construction, are:

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
* `CryptoPerpetual`: `instrument_id`, `raw_symbol`, `base_currency`,
  `quote_currency`, `settlement_currency`, `is_inverse`, `price_precision`,
  `size_precision`, `price_increment`, `size_increment`, `ts_event`, `ts_init`.

Omitting a required field raises `TypeError`. Tick size is a constructor input
with no engine default. Lot size and multiplier have none on `Equity`,
`FuturesContract` and `OptionContract`; `CurrencyPair` defaults `multiplier` to
`Quantity(1)` and `lot_size` to `None`, `IndexInstrument` fixes `multiplier` at
1, and `CryptoPerpetual` defaults both to `Quantity(1)`. kanso requires the
perpetual's anyway, because a contract value of one is a claim about the
contract. So they come from the convention table or the reference provider's
measured definition, never guessed. `Equity`
takes no multiplier and carries `Quantity(1)`, so a share's notional is its
quantity at its price; every other class carries the one it was built with.
`CryptoPerpetual` fixes its asset class to `CRYPTOCURRENCY` and its instrument
class to `SWAP`, and its `to_dict` carries no `asset_class` key.

Every class carries `maker_fee` and `taker_fee`, zero unless given, and
`MakerTakerFeeModel.get_commission` charges a fill its notional times the
instrument's maker or taker rate, in the quote currency: measured on a
perpetual of multiplier 0.01, ten contracts at 60,000 pay 3 USDT at a taker
rate of 0.0005 and nothing at zero. That model is the one `BacktestEngine`
substitutes for a venue given none, so kanso's definitions keep both rates at
zero and the runner's cost model is the only charge. `margin_init` and
`margin_maint` stay overridable; the engine's liquidation path computes from
them.

`Instrument.get_settlement_currency()` answers the currency a trade settles
in: a `CryptoPerpetual`'s stated `settlement_currency`, and the quote currency
of every other class kanso builds (none of them inverse).
`Instrument.get_cost_currency()` answers the currency positions, PnL and margin
are booked in — the quote currency of every linear class, a perpetual's
included — and the account manager converts from it to the account's base
currency, deferring the balance update (logged at debug only) when the cache
holds no rate between them. A USDC-settled perpetual quoted in USDT answers
USDC to the first and USDT to the second, and is not quanto, because the engine
treats the two as USD equivalents: it settles in one currency and is booked in
the other, so `hyp validate` requires both to be the account's.

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

**An order's events grow only at its end, and its strategy is handed each one as it is
taken.** `Order.apply` appends the event it takes last, once its state machine has accepted
it, and nothing else writes the list; `Order.events` hands back a new list holding every
event each time it is read, where `event_count` and `last_event` read the length and the
last element without one. The execution engine publishes each event it applied to the
strategy's `events.order.<id>` topic after applying it, and the strategy publishes the
pending modify and pending cancel it applies itself, so `handle_event` is handed each event
when it is the order's last. Measured, a buy resting at 9.50, moved on every print it was
open on and filled in parts by sellers' prints of 10, with no latency and with thirty
seconds: every event after the first was handed in the order the order holds them, each
as its last with the count one higher than before, and every list read on the way was a
prefix of the last. The first, `OrderInitialized`, is handed before the order is in the
cache. That is what lets `KansoStrategy` fold an order's events into its balance as they
come instead of copying the order's history on every read: a sleeve that moves a resting
order on every bar adds two events a bar, and copied whole they cost the square of the bars.
An engine release in which this stops holding leaves the balance exact — a read that cannot
show the events it was handed are all of them takes the copy instead — and brings the square
back.

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

**A top-of-book venue ignores a point older than its book.** On an `L1_MBP` book,
`OrderMatchingEngine.process_quote_tick` and `process_trade_tick` return before they touch
the book or the last price when the point's `ts_event` is earlier than the book's `ts_last`,
having only advanced the venue's clock to the point's `ts_init` and matched the resting
orders against the book it already held — which can credit a quote or a print standing as that
book at a resting order's price again; one stamped at `ts_last` is applied. `ts_last` is the
running maximum of the `ts_event` of every quote and print applied, and of the `ts_init` a
walked bar stamps its prints with. The book's own `update_quote_tick` and `update_trade_tick`
filter the same way, so a bar published before `ts_last` is not walked into it either — which
no point handed over in `ts_init` order, each at or after its `ts_event`, can bring about. No
configuration turns either filter off, a level-two book has none, and the data engine hands
the point to the strategy all the same. A tape that takes `ts_init` from itself and
`ts_event` from the participant stamps a point delivered after another earlier often enough
to matter: measured on 85 sessions each of two Nasdaq names' quotes and lit prints, the
venue ignored about 12 % of the quotes and 53 % of the prints the strategy was handed.
`OrderBook.reset` empties both sides and zeroes `ts_last`, and a quote or a print applied
after it leaves a top-of-book book exactly as that point alone sets it — so
`kanso.nautilus.availability`, a module both of kanso's venues load, resets the book when the
filter would fire and the venue applies every quote and print. Measured, a buy of 10 resting at
9.50 under quotes at minutes one and three, then a quote of 9.45/9.48 or a seller's print at
9.48 published at minute four: stamped at minute two, neither fills it; stamped at minute
three, each does; stamped at minute two with the module loaded, each does.

**A point beyond a resting limit fills all of it.** A print *at* a resting limit's price fills
it by the print's own size and no more, one part per print: a buy of 320 met by four sellers'
prints of 100 at its price fills 100, 100, 100 and 20. A quote whose far side sits at the price
fills it by the size shown, and again at every quote that shows it, because with
`liquidity_consumption` off the venue keeps no record of what it credited there: a buy of 445
met by six identical quotes of 9.40/9.50 showing 100 on the ask fills 100 four times and then
45, under either probability. A quote *beyond* the price, or a print beyond it — in the
match the print triggers, one from the side that can trade with the order (below) — fills all
that is left of the order, at its price, as a maker, whatever its own size: once a level
strictly better than the limit has been matched, the matching engine's check of a limit "on
exhausted book volume" (`backtest/engine.pyx`) fills the order's `leaves_qty` at the limit with
`liquidity_consumption` off, assuming that a market which moved through it had the size.
Measured, the same buy of 320 resting at 9.50: a seller's print of 100 at 9.49 fills 100 and
then 220, and so does a quote of 9.40/9.49 showing 100 on the ask; a seller's print of 100 at
9.50 fills 100. With more than one order resting, a point is credited to each order it fills
rather than shared among them. A quote at a price fills every limit resting there by its whole
size, and so does a print under probability one: two buys of 320 at 9.50 met by a seller's
print of 100 there fill 100 each, and so they do met by a quote showing 100 at 9.50 under
either probability. A point beyond several fills all that is left of the best-priced and
nothing of one resting at a worse price, even one it also went through, because matching the
first moves the engine's far side and last price to that order's limit until the match ends:
buys of 320 at 9.50 and at 9.49 met by a seller's print of 100 at 9.45, or by a quote of
9.40/9.45 showing 100, fill 100 and 220 at 9.50 and nothing at 9.49, under either probability.
Another order at the filled one's price is then reached at its limit, so under probability one
it is filled whole too, and under zero a quote fills it and a print does not — the engine asks
the fill model when the last price sits at the limit for a print, and when the order's own side
does for a quote. So only an order alone at its price and no larger than the points that reach
it is filled as honestly as they are; a larger one is credited size the tape never showed, and
so is every order resting beside another at one price. Nothing kanso sets on the engine's own
fill model changes that, and under `touch` and `through` kanso leaves the engine's fill as it
is. A fill model that hands the engine a simulated book does change it: a zero-quantity fill
among the fills it answers ends the fill before the check (below), which is how `print_through`
fills a print beyond a resting limit by the print's own size (`kanso.nautilus.tape`).

**What a fill model can decide.** The matching engine asks its fill model,
`get_orderbook_for_fill_simulation`, for the fills of every order it has matched — a market
order, a limit marketable when it lands, and a resting limit a point reached, marked `MAKER` —
and fills the order from the book it answers in place of its own. `apply_fills` takes those
fills in order and returns at the first of zero quantity, before it cancels an IOC remainder,
walks a market order's rest or fills a resting limit's remainder whole: measured, a buy of 320
resting at 9.50 under 9.48/9.52 and met by a print of 100 at 9.49 with no aggressor fills 100
alone when the model answers 100 at 9.50 and then a zero, and 100 and then 220 when it answers
the 100 alone. A market order the answered book does not cover walks one increment past it
for the rest — a market buy of 300 answered with the quote's ask of 100 at 9.52 fills 100 at
9.52 and 200 at 9.53 — and the answered book replaces the print standing as the venue's book:
the same buy sent on a print of 100 at 9.50 under an ask of 1,000 fills 300 at 9.52. A lone
zero refuses a market order, "no market for" its instrument, and leaves a limit accepted on
arrival resting at its price. The model cannot tell from its arguments which point it is
matching: once a command lands, the research engine matches every resting order again and asks
with exactly the arguments of the print's own match — best bid and ask both at the print, the
order `ACCEPTED` — and so reaches a resting buy a buyer's print did not reach in its own match.
And a simulation module that calls its exchange's `process` from `pre_process` lands every
command due by then before the matching engine applies the point, where the engine alone lands
it after matching the point: a buy of 320 at 9.50 due at 120 ms, met by a quote of 9.45/9.49 at
125 ms, rests and is filled as a maker at 9.50 with such a module, and lands on the quote it had
already applied, a taker at 9.49, without it.

**kanso's print rule.** Under `print_through` the venue loads kanso's own fill model with a
module that tells it, before each point, which point is in hand and which orders rested before
a print (`kanso.nautilus.tape`), and `kanso doctor` checks it as kanso loads it: a buy of 320
resting at 9.50 under 9.48/9.52 is not filled by a quote of 9.45/9.49 showing 100, nor by a
print of 100 at 9.50, and is filled 100 by each of two prints of 100 at 9.49 and 9.48 — 320 by
the first under `print_through_whole` — and a market buy of 300 sent on a print of 100 at 9.50
under 9.48/9.52 fills 300 at 9.52.

**A print is the top-of-book until the next quote.** `process_trade_tick` sets both sides of a
level-one book — the `OrderBook` — to the print's price and size, and they stay there until the
next quote or print moves them, while the match the print itself triggers moves only the far
side its aggressor allows (below). So a market order sent on a print is matched against the
print and walks one increment past it for the rest: measured under a quote of 9.97/10.03
showing 1,000 a side, a market buy of 300 sent on the quote fills 300 at 10.03, and sent on a
seller's print of 100 at 10.00 after it fills 100 at 10.00 and 200 at 10.01 — the matching
engine's one-increment step for a market order larger than the top level, which the fill model
has no part in, not the quote's ask.

**`liquidity_consumption` trades one dishonesty for another.** The engine's remedy for a
level credited again and again is `liquidity_consumption`, off by default, and kanso's venues
leave it off (`kanso.nautilus.venue`). On a top-of-book venue it records what it credited at a
price and forgets the record only when the size shown at that price changes, so it withholds
a repeated print: measured, a buy of 320 resting at 9.50 met by two sellers' prints of 100 at
9.50 fills 100 with it on, and 100 and 100 with it off. Measured on 2026-10-09 on a venue
configured as kanso configures one and switched on, it also leaves a market order against a
level it has consumed neither filled nor rejected, `SUBMITTED` for good; withholds from a
second resting order the repeat of a print it credited to the first; credits three bars whose
lows go under a resting buy a quarter of one bar's volume between them; and fills the buy of
320 met by four prints of 100 at its price 100 in all.
Each of those is a wrong fill of its own, so the claim is checked here: an engine release
that changes it is the moment to look at it again.

**In the match it triggers, a print reaches a resting order only from the side that can
trade with it.** For that match `process_trade_tick` moves the matching engine's ask down to
a seller's print, its bid up to a buyer's, and both to one with no aggressor; the book holds
the print on both sides all the same (above). Measured, the same buy at 9.50 against a print
at 9.49: a seller's print, or one with no aggressor, fills it at 9.50; a buyer's does not, at
any probability. Once a command lands, though, the research path matches every resting order
again against the book, where the print stands as both sides, so there a buyer's print
through a resting buy fills it, and fills all of it; the node's venue does not
(`docs/backlog.md` row 154). A bar's own prints carry no such label — the engine walks them
as book updates — so this bites only on trade data, which is why a trade file that records
no side is loaded as `NO_AGGRESSOR` and never given one.

**A book's changes of one instant reach the venue and the strategy whole only as one
`OrderBookDeltas`.** Fed one `OrderBookDelta` at a time, the backtest engine hands each to
`SimulatedExchange.process_order_book_delta`, which applies it and matches, and the data
engine — buffering nothing — publishes each as a one-element `OrderBookDeltas` once the book
it keeps has applied it, so a strategy is called once per change with the book half-moved
and the venue matches against every intermediate state. Fed the same changes as one
`OrderBookDeltas`, the exchange applies all of them and matches once
(`process_order_book_deltas`), and the data engine publishes the batch whole after its book
has taken all of it. Measured, a buy resting at 10.00 under an offer at 10.05, met by an
instant that adds an offer at 10.00 and deletes it again: one change at a time, the strategy
was called with each delta alone — the first call saw no offer at all — and the buy filled
against the offer that existed only between the two; as one batch, it was called once per
instant with both deltas and an offer of 10.05, and nothing filled. kanso delivers a book a
batch per instrument and instant (`kanso.nautilus.cross_section.batched`).

**An order whose cancel was sent is working until the cancel lands.** `Strategy.cancel_order`
and `cancel_all_orders` apply `OrderPendingCancel` to the order before the command leaves
the strategy, so it reads `PENDING_CANCEL` and not `is_closed` at once, and the order state
machine lets a pending-cancel order fill. Under a latency model the cancel reaches the book
only at the first point after its delay, once that point has been matched, so the market can
fill the order in between; with none, the backtest drains the commands a handler sent before
it matches the next point, and the cancel always lands first. Measured, a buy resting at 9.50
and cancelled a minute before a print at 9.49: filled whole under a 30-second latency,
cancelled unfilled under none — which is why `submit_exit` counts an order the venue held
open when its cancel was sent as working exactly when the venue model states a latency. All
of this is measured on the backtest engine. A cancel sent for an order still on its way to
the venue does different things on the two code paths. The backtest has already handed such
an order to the venue (`SUBMITTED`), which takes it and then the cancel; a node may still
hold it `INITIALIZED`, its submit waiting in the live risk engine's queue while the cancel
goes straight to the live execution engine's, so the cancel reaches the venue first and is
lost, and the order rests (read in `live/risk_engine.py` and `execution/manager.pyx`). So
`KansoStrategy` never sends a cancel for an `INITIALIZED` order: it holds it back until the
node reports the order `SUBMITTED`, and counts the order as working until the cancel lands;
the replay tests measure that the two paths then fill alike. The engine's own guard is the
`reduce_only` flag, and the simulated venue honours it: `close_position` sets it by default,
a reduce-only order the position has no room left for is rejected rather than opening
the other side, and its matching engine trims a reduce-only fill to the quantity still open
— measured, a long of 10 sold at market and closed in the same handler ends flat with the
close rejected, and one sold 4 and closed ends flat with the close cut to 6 and filled —
which is what keeps a node's flatten from racing an exit still in flight. `submit_exit` does
not set it, for two reasons: the shipped broker adapter refuses any order that carries the
flag, and one strategy class runs on every path; and on any fill the simulated venue resizes
every resting standalone reduce-only order to the whole position (`backtest/engine.pyx`),
so a partial exit — 50 of 100 — marked reduce-only would be grown or shrunk behind the
author's back and fill differently even with no latency. `cancel_all_orders` marks an order
open at the venue `PENDING_CANCEL` at once, which is what `KansoStrategy.cancel_all_orders`
mirrors when it notes the orders it cancelled; on the backtest it also sends the cancel for
one still in flight without marking it, and — read in `trading/strategy.pyx` rather than
measured — leaves one a node has not yet sent, so kanso hands it the orders only when none
is still `INITIALIZED`, and cancels them one by one otherwise.

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

**A request is metered by the keys it names, and by nothing else.** `default_quota`
holds a key the request names that `keyed_quotas` gives no quota of its own, each
such key in a bucket of its own; a request that names no key waits for no quota at
all, whatever `default_quota` states. A request naming several keys waits until
every one admits it. The wait comes before the request is built — a request to a
string that is no URL is held just as long before it fails — and a key's bucket
lives in the client, so requests sent from several threads, each awaiting in its
own event loop, share it. Measured with `default_quota` at
`rate_per_second(2)`, twelve requests from six threads to a loopback server that
answered in 0.6 s: naming no key, all twelve arrived within 0.6 s; naming one key,
two arrived at once and then one every half second; naming `a` and `b` in turn,
two buckets let through two a second each. So a caller whose default quota is to
hold names one key on every request.

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

A `SimulationModule` is handed every market point through `pre_process(data)` —
the point itself, so its `ts_event` is the one the loader wrote, which is what
`kanso.nautilus.splits` compares with the instant a split takes effect —
*before* the venue's matching engine sees it — `SimulatedExchange.process_bar`,
`process_quote_tick`, `process_trade_tick`, the three order-book variants,
`process_instrument_status` and `process_instrument_close` each loop the modules
first — and `SimulatedExchange.__init__` calls `module.register_base(portfolio,
msgbus, cache, clock)` and then `module.register_venue(self)`, so a module holds
the kernel's portfolio and cache and the exchange itself. That call is the only
place kanso can act between a point arriving and an order being matched against
it, and both of kanso's venues are a `SimulatedExchange`, which is why the
corporate action lives there rather than in a strategy.

A level-two book
----------------
An `OrderBook` of type `L2_MBP` keeps one size per price: an `ADD` or an `UPDATE` sets the
size at its price whether or not the book holds it, a `DELETE` of a price it does not hold
changes nothing, and a `CLEAR` empties both sides. Under `depth` the harness keeps its own
copy of a book by exactly these rules (`kanso.nautilus.strategy`), because the cache's
copy is already past the change a handler is handling, so the view an author is handed is
the venue's book only while the engine keeps to them.

**The engine's check that a level-two venue holds each name's book reads one point a call.**
`BacktestEngine.add_data` with `validate` records the instrument of the call's first point
as holding data and, when that point is a book change, as holding book data, and no other
point's; `run` raises `InvalidConfiguration` for an instrument of an `L2_MBP` venue recorded
as holding data and not book data; unvalidated, a call records nothing, and `clear_data`
forgets both. Measured: two names' book changes handed as one call, the first name's ahead,
then the second name's print alone — refused for the second name, whose change was in the
stream. A stream of several names is handed in runs of one type (`add_data` assumes one
type a call), and a quiet name's changes can follow another's in every run, so kanso adds a
held book's market data unvalidated and makes the refusal itself, of every point, a UTC day
at a time (`kanso.nautilus.backtest._refuse_unbooked`).

Claims a broker adapter makes
-----------------------------
A broker package binds to the engine's own adapter for that broker, and this module may
not name one. So each broker states the claims its package rests on as `engine_facts`,
`kanso.nautilus.adapters.engine_facts()` collects them from the adapter directory, and
`claims()` puts them after the core's own: `verify()` re-establishes both kinds the same
way, and a broker claim that stops holding is a broken binding like any other.
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
        "delete_data_range with no identifier removes the files of a series of no instrument",
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


def _limit_points(kind: str, *prices: float, side: Any = None) -> list[object]:
    """A market at 10.00 for an order to rest against, then one point per price.

    A bar is flat at 10.00 but for its low, or its high when the price is above ten; a
    print is at the price, a seller's below ten and a buyer's above unless `side` names its
    aggressor; a quote takes the prices two at a time, as a bid and an ask.
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
            if index and side is not None:
                aggressor = side
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


def _probe_resting_limit(
    prob: float,
    points: list[object],
    side: str,
    *,
    quantity: int = 10,
    liquidity_consumption: bool = False,
    modules: tuple[object, ...] = (),
) -> list[tuple[object, ...]]:
    """The fills of one limit order resting from the first point's handler.

    A buy at 9.50 or a sell at 10.50 against a market at 10.00, so the order rests on the
    book as a maker; the venue's fill model fills a limit the market reaches with
    probability `prob`, the venue remembers what it credited at a price only under
    `liquidity_consumption`, and it loads `modules`. Each fill is its quantity, its price and
    its liquidity side.
    """
    price = 10.5 if side == "SELL" else 9.5
    (fills,) = _probe_resting_limits(
        prob,
        points,
        side,
        (price,),
        quantity=quantity,
        liquidity_consumption=liquidity_consumption,
        modules=modules,
    )
    return fills


def _probe_resting_limits(
    prob: float,
    points: list[object],
    side: str,
    prices: tuple[float, ...],
    *,
    quantity: int = 10,
    liquidity_consumption: bool = False,
    modules: tuple[object, ...] = (),
) -> list[list[tuple[object, ...]]]:
    """The fills of one limit order of `quantity` at each of `prices`, all on `side`, all
    submitted in that order from the first point's handler: each order's fills, in the
    order of `prices`, as `_probe_resting_limit` reports one order's."""
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
            self.fills: list[list[tuple[object, ...]]] = [[] for _ in prices]
            self.orders: dict[object, int] = {}
            self.sent = False

        def on_start(self) -> None:
            self.subscribe_bars(_minute_type())
            self.subscribe_trade_ticks(equity.id)
            self.subscribe_quote_ticks(equity.id)

        def _rest(self) -> None:
            if not self.sent:
                self.sent = True
                for index, price in enumerate(prices):
                    order = self.order_factory.limit(
                        equity.id,
                        OrderSide.SELL if selling else OrderSide.BUY,
                        Quantity.from_int(quantity),
                        Price(price, 2),
                    )
                    self.orders[order.client_order_id] = index
                    self.submit_order(order)

        def on_bar(self, bar_: object) -> None:
            self._rest()

        def on_trade_tick(self, tick: object) -> None:
            self._rest()

        def on_quote_tick(self, tick: object) -> None:
            self._rest()

        def on_order_filled(self, event: Any) -> None:
            self.fills[self.orders[event.client_order_id]].append(
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
            liquidity_consumption=liquidity_consumption,
            modules=list(modules),
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


def _probe_cancel_in_flight(latency_ns: int) -> tuple[str, bool, str, float]:
    """A buy resting at 9.50, cancelled at minute three; a seller's print at 9.49 at minute
    four. Returns the order's status and `is_closed` just after the cancel was sent, and its
    status and filled quantity at the end."""
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.models import LatencyModel
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, order_status_to_str
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    equity: Any = _sample_equity()

    class Probe(Strategy):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.seen = 0
            self.order: Any = None
            self.sent: tuple[str, bool] = ("", True)

        def on_start(self) -> None:
            self.subscribe_trade_ticks(equity.id)

        def on_trade_tick(self, tick: object) -> None:
            self.seen += 1
            if self.seen == 1:
                self.order = self.order_factory.limit(
                    equity.id, OrderSide.BUY, Quantity.from_int(10), Price(9.5, 2)
                )
                self.submit_order(self.order)
            elif self.seen == 3:
                self.cancel_order(self.order)
                self.sent = (order_status_to_str(self.order.status), bool(self.order.is_closed))

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("XNAS"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(1_000_000, USD)],
            latency_model=LatencyModel(base_latency_nanos=latency_ns) if latency_ns else None,
        )
        engine.add_instrument(equity)
        engine.add_data(_limit_points("trade", 10.0, 10.0, 9.49, 10.0))
        probe = Probe()
        engine.add_strategy(probe)
        engine.run()
        order = probe.order
        return (
            *probe.sent,
            order_status_to_str(order.status),
            float(order.filled_qty),
        )
    finally:
        engine.dispose()


def _check_a_cancel_in_flight_leaves_the_order_to_fill() -> tuple[bool, str]:
    """`submit_exit`'s premise: an order whose cancel was sent is working until the cancel
    lands — under a latency it fills if the market reaches it first; with none it cannot."""
    slow = _probe_cancel_in_flight(_MINUTE_NS // 2)
    instant = _probe_cancel_in_flight(0)
    holds = slow == ("PENDING_CANCEL", False, "FILLED", 10.0) and instant == (
        "PENDING_CANCEL",
        False,
        "CANCELED",
        0.0,
    )
    return holds, (
        "a buy resting at 9.50 and cancelled a minute before a print at 9.49 read (status "
        f"after the cancel, is_closed, final status, filled) {slow} with a 30-second latency "
        f"and {instant} with none: the cancel is applied as PENDING_CANCEL before it leaves "
        "the strategy, the order is not closed until it lands, a latency lets the market "
        "fill it in between, and with no latency it lands before the next point is matched"
    )


def _probe_handed_events(latency_ns: int) -> tuple[list[str], list[tuple[object, ...]], bool]:
    """A buy of 30 resting at 9.50 and moved between 9.50 and 9.51 on every print it is still
    open on, against sellers' prints of 10 that reach it. Returns the names of the events
    the order holds at the end; for each event of it the strategy was handed, its name and,
    once the order is in the cache, the order's event count then and whether the event was
    the order's last; and whether every list `Order.events` handed back on the way is a
    prefix of the last one, and a list of its own."""
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.models import LatencyModel
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.data import TradeTick
    from nautilus_trader.model.enums import AccountType, AggressorSide, OmsType, OrderSide
    from nautilus_trader.model.identifiers import TradeId, Venue
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    equity: Any = _sample_equity()

    class Probe(Strategy):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.seen = 0
            self.order: Any = None
            self.handed: list[tuple[object, ...]] = []
            self.lists: list[list[Any]] = []

        def on_start(self) -> None:
            self.subscribe_trade_ticks(equity.id)

        def handle_event(self, event: Any) -> None:
            mine = getattr(event, "client_order_id", None)
            if self.order is not None and mine == self.order.client_order_id:
                held = self.cache.order(mine)
                if held is None:
                    self.handed.append((type(event).__name__,))
                else:
                    self.handed.append(
                        (type(event).__name__, held.event_count, held.last_event is event)
                    )
                    self.lists.append(held.events)
            super().handle_event(event)

        def on_trade_tick(self, tick: object) -> None:
            self.seen += 1
            if self.seen == 1:
                self.order = self.order_factory.limit(
                    equity.id, OrderSide.BUY, Quantity.from_int(30), Price(9.5, 2)
                )
                self.submit_order(self.order)
                return
            price = Price(9.51 if self.seen % 2 else 9.5, 2)
            if (
                self.order.is_open
                and not self.order.is_pending_update
                and price != self.order.price
            ):
                self.modify_order(self.order, price=price)

    points = [
        TradeTick(
            equity.id,
            Price(price, 2),
            Quantity.from_int(10),
            AggressorSide.SELLER if price < 10.0 else AggressorSide.BUYER,
            TradeId(f"P-{index}"),
            (index + 1) * _MINUTE_NS,
            (index + 1) * _MINUTE_NS,
        )
        for index, price in enumerate((10.0, 10.0, 9.51, 10.0, 9.5, 10.0, 9.51, 10.0, 9.5, 10.0))
    ]
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("XNAS"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(1_000_000, USD)],
            latency_model=LatencyModel(base_latency_nanos=latency_ns) if latency_ns else None,
        )
        engine.add_instrument(equity)
        engine.add_data(points)
        probe = Probe()
        engine.add_strategy(probe)
        engine.run()
        final = probe.order.events
        grown = all(
            seen == final[: len(seen)] and seen is not later
            for seen, later in zip(probe.lists, [*probe.lists[1:], final], strict=True)
        )
        return [type(event).__name__ for event in final], probe.handed, grown
    finally:
        engine.dispose()


def _check_an_order_hands_its_strategy_each_event_as_its_last() -> tuple[bool, str]:
    """`KansoStrategy._settle`'s premise for reading an order's events once: what the strategy
    is handed is what the order took, in order, as it took it."""
    seen: list[str] = []
    holds = True
    for latency_ns in (0, _MINUTE_NS // 2):
        final, handed, grown = _probe_handed_events(latency_ns)
        expected = [(final[0],)] + [
            (name, count, True) for count, name in enumerate(final[1:], start=2)
        ]
        moved = "OrderUpdated" in final and final.count("OrderFilled") >= 2
        holds = holds and grown and moved and handed == expected
        seen.append(
            f"{'no latency' if not latency_ns else 'thirty seconds'}: the order holds {final}; "
            f"handed (event, count, was last) {handed}"
        )
    return holds, (
        "a buy of 30 resting at 9.50, moved on every print it was open on and filled in parts "
        "by sellers' prints of 10 — "
        + "; ".join(seen)
        + ". Every event the order took after its first was handed to handle_event in the "
        "order it holds them, each as its last with the count one higher than the one before, "
        "and every list Order.events handed back on the way was a new list and a prefix of "
        "the last"
    )


def _check_cancel_all_orders_takes_what_is_open_and_what_is_in_flight() -> tuple[bool, str]:
    """What `KansoStrategy.cancel_all_orders` mirrors when it notes the orders it cancelled:
    the engine marks an order open at the venue `PENDING_CANCEL` at once. On the backtest it
    also cancels one still in flight, left as it is; one a node has not yet sent it skips,
    which is why kanso hands it the orders only when none is still `INITIALIZED`."""
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, order_status_to_str
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    equity: Any = _sample_equity()

    class Probe(Strategy):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.seen = 0
            self.orders: list[Any] = []
            self.sent: tuple[str, ...] = ()

        def on_start(self) -> None:
            self.subscribe_trade_ticks(equity.id)

        def on_trade_tick(self, tick: object) -> None:
            self.seen += 1
            if self.seen in (1, 2):
                order = self.order_factory.limit(
                    equity.id, OrderSide.BUY, Quantity.from_int(10), Price(9.0, 2)
                )
                self.orders.append(order)
                self.submit_order(order)
            if self.seen == 2:
                self.cancel_all_orders(equity.id)
                self.sent = tuple(order_status_to_str(order.status) for order in self.orders)

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
        engine.add_data(_limit_points("trade", 10.0, 10.0))
        probe = Probe()
        engine.add_strategy(probe)
        engine.run()
        seen = (*probe.sent, *(order_status_to_str(order.status) for order in probe.orders))
    finally:
        engine.dispose()
    return seen == ("PENDING_CANCEL", "SUBMITTED", "CANCELED", "CANCELED"), (
        "a buy resting at 9.00, and a second sent in the handler that then called "
        f"cancel_all_orders, read (resting, in flight, once cancelled; then both at the end) = "
        f"{seen}: the engine marks the resting order pending cancel at once, leaves the one "
        "in flight as it is, and cancels both"
    )


def _probe_close_after_a_sale(sold: int) -> tuple[bool, str, float, float, float]:
    """A long of 10; then, in one handler, a sale of `sold` at market and `close_position`.
    Returns the close's (is_reduce_only, status, quantity, filled) and the net position at
    the end."""
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, order_status_to_str
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money, Quantity
    from nautilus_trader.trading.strategy import Strategy

    equity: Any = _sample_equity()

    class Probe(Strategy):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.seen = 0

        def on_start(self) -> None:
            self.subscribe_trade_ticks(equity.id)

        def on_trade_tick(self, tick: object) -> None:
            self.seen += 1
            if self.seen == 1:
                self.submit_order(
                    self.order_factory.market(equity.id, OrderSide.BUY, Quantity.from_int(10))
                )
            elif self.seen == 3:
                self.submit_order(
                    self.order_factory.market(equity.id, OrderSide.SELL, Quantity.from_int(sold))
                )
                (position,) = self.cache.positions_open(strategy_id=self.id)
                self.close_position(position)

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
        engine.add_data(_limit_points("trade", 10.0, 10.0, 10.0))
        engine.add_strategy(Probe())
        engine.run()
        close = engine.cache.orders(side=OrderSide.SELL)[-1]
        return (
            bool(close.is_reduce_only),
            order_status_to_str(close.status),
            float(close.quantity),
            float(close.filled_qty),
            float(engine.portfolio.net_position(equity.id)),
        )
    finally:
        engine.dispose()


def _check_a_close_is_reduce_only_and_the_venue_holds_it_to_the_position() -> tuple[bool, str]:
    """The node flatten's premise: `close_position` sends a reduce-only order, and the
    simulated venue refuses it once another sale has already closed the position, and trims
    it to what is left when another sale has closed part of it."""
    closed = _probe_close_after_a_sale(10)
    part = _probe_close_after_a_sale(4)
    holds = closed == (True, "REJECTED", 10.0, 0.0, 0.0) and part == (
        True,
        "FILLED",
        6.0,
        6.0,
        0.0,
    )
    return holds, (
        "a long of 10, then in one handler a sale at market and close_position: (close is "
        f"reduce-only, its status, quantity, filled, net position) = {closed} after a sale "
        f"of 10 and {part} after a sale of 4. The close carries reduce_only by default, and "
        "the venue refuses a reduce-only order the position no longer has room for and "
        "trims one to the quantity still open, rather than open the other side"
    )


def _check_a_buyer_s_print_never_reaches_a_resting_buy() -> tuple[bool, str]:
    """What a trade file's aggressor column decides: in the match a print triggers, a buyer's
    print moves only the bid, so a resting buy beneath it is not reached; a seller's print, and
    one with no aggressor, move the ask down to the print and fill it."""
    from nautilus_trader.model.enums import AggressorSide, aggressor_side_to_str

    seen = {
        aggressor_side_to_str(side): _probe_resting_limit(
            1.0, _limit_points("trade", 9.49, side=side), "BUY"
        )
        for side in (AggressorSide.BUYER, AggressorSide.SELLER, AggressorSide.NO_AGGRESSOR)
    }
    filled = [(10.0, 9.5, "MAKER")]
    holds = seen == {"BUYER": [], "SELLER": filled, "NO_AGGRESSOR": filled}
    return holds, (
        f"a buy at 9.50 resting against a print at 10.00, then a print at 9.49 by each "
        f"aggressor, filled: {seen}. In the match it triggers, a buyer's print moves only the "
        "bid up, so it never reaches a resting buy; a seller's or no one's moves the ask down "
        "to it"
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


def _stale_points(kind: str, minute: int) -> list[object]:
    """A market quoted 9.99/10.01 at minute one and 9.98/10.00 at minute three, then, published
    at minute four and stamped at `minute`, a quote of 9.45/9.48 or a seller's print at 9.48:
    beyond a buy resting at 9.50, and older than the book when `minute` is under three."""
    from nautilus_trader.model.data import QuoteTick, TradeTick
    from nautilus_trader.model.enums import AggressorSide
    from nautilus_trader.model.identifiers import TradeId
    from nautilus_trader.model.objects import Price, Quantity

    instrument_id = _sample_equity().id  # type: ignore[attr-defined]

    def quoted(bid: float, ask: float, ts_event: int, ts_init: int) -> object:
        return QuoteTick(
            instrument_id,
            Price(bid, 2),
            Price(ask, 2),
            Quantity.from_int(100),
            Quantity.from_int(100),
            ts_event,
            ts_init,
        )

    stamped, published = minute * _MINUTE_NS, 4 * _MINUTE_NS
    late = (
        quoted(9.45, 9.48, stamped, published)
        if kind == "quote"
        else TradeTick(
            instrument_id,
            Price(9.48, 2),
            Quantity.from_int(100),
            AggressorSide.SELLER,
            TradeId("P-1"),
            stamped,
            published,
        )
    )
    return [
        quoted(9.99, 10.01, _MINUTE_NS, _MINUTE_NS),
        quoted(9.98, 10.0, 3 * _MINUTE_NS, 3 * _MINUTE_NS),
        late,
    ]


def _check_a_level_one_venue_ignores_a_point_older_than_its_book() -> tuple[bool, str]:
    """What `kanso.nautilus.availability` undoes: a top-of-book matching engine skips a quote
    or a print stamped before its book's last update, and applies one stamped at it."""
    seen = {
        (kind, minute): _probe_resting_limit(1.0, _stale_points(kind, minute), "BUY")
        for kind in ("quote", "print")
        for minute in (2, 3)
    }
    holds = all(
        fills == ([] if minute == 2 else [(10.0, 9.5, "MAKER")])
        for (_kind, minute), fills in seen.items()
    )
    return holds, (
        "a buy of 10 resting at 9.50 under quotes at minutes one and three, then a quote of "
        "9.45/9.48 or a seller's print at 9.48 published at minute four — "
        + "; ".join(
            f"the {kind} stamped at minute {minute}: {fills}"
            for (kind, minute), fills in seen.items()
        )
        + ". The venue ignored the point stamped before the book's last update and applied "
        "the one stamped at it"
    )


def _check_a_reset_level_one_book_applies_the_next_point() -> tuple[bool, str]:
    """`kanso.nautilus.availability`'s premise: emptying a top-of-book book loses nothing the
    next quote or print does not set again, and lets the engine apply it."""
    from nautilus_trader.backtest.config import SimulationModuleConfig
    from nautilus_trader.model.book import OrderBook
    from nautilus_trader.model.data import QuoteTick, TradeTick
    from nautilus_trader.model.enums import AggressorSide, BookType
    from nautilus_trader.model.identifiers import TradeId
    from nautilus_trader.model.objects import Price, Quantity

    from kanso.nautilus.availability import NAME, Availability

    admitted = {
        kind: _probe_resting_limit(
            1.0,
            _stale_points(kind, 2),
            "BUY",
            modules=(Availability(SimulationModuleConfig(component_id=f"{NAME}-XNAS")),),
        )
        for kind in ("quote", "print")
    }
    instrument_id = _sample_equity().id  # type: ignore[attr-defined]

    def quoted(bid_size: int) -> object:
        return QuoteTick(
            instrument_id,
            Price(9.9, 2),
            Price(10.0, 2),
            Quantity.from_int(bid_size),
            Quantity.from_int(400),
            2 * _MINUTE_NS,
            4 * _MINUTE_NS,
        )

    points = {
        "a quote": quoted(300),
        "a print": TradeTick(
            instrument_id,
            Price(10.05, 2),
            Quantity.from_int(89),
            AggressorSide.NO_AGGRESSOR,
            TradeId("P-9"),
            2 * _MINUTE_NS,
            4 * _MINUTE_NS,
        ),
        "a quote with no bid": quoted(0),
    }
    same: dict[str, bool] = {}
    for name, point in points.items():
        reset, alone = (
            OrderBook(instrument_id, BookType.L1_MBP),
            OrderBook(instrument_id, BookType.L1_MBP),
        )
        reset.update_quote_tick(_stale_points("quote", 3)[1])
        reset.reset()
        for book in (reset, alone):
            if isinstance(point, QuoteTick):
                book.update_quote_tick(point)
            else:
                book.update_trade_tick(point)
        tops = [
            (
                str(book.best_bid_price()),
                str(book.best_bid_size()),
                str(book.best_ask_price()),
                str(book.best_ask_size()),
                book.ts_last,
            )
            for book in (reset, alone)
        ]
        same[name] = tops[0] == tops[1]
    filled = [(10.0, 9.5, "MAKER")]
    holds = all(fills == filled for fills in admitted.values()) and all(same.values())
    return holds, (
        f"the same buy of 10 with kanso's Availability module loaded, the late quote and print "
        f"stamped at minute two: {admitted}. A book quoted 9.98/10.00 at minute three, reset and "
        f"then handed a point stamped at minute two, held what that point alone sets — "
        + "; ".join(f"{name}: {held}" for name, held in same.items())
    )


def _probe_by_size(order_qty: int, print_sizes: tuple[int, ...]) -> list[float]:
    """The fill quantities of a resting buy at 9.50 met by successive sellers' prints at 9.50,
    each of the given size, under `prob_fill_on_limit` one."""
    from nautilus_trader.model.data import TradeTick
    from nautilus_trader.model.enums import AggressorSide
    from nautilus_trader.model.identifiers import TradeId
    from nautilus_trader.model.objects import Price, Quantity

    instrument_id = _sample_equity().id  # type: ignore[attr-defined]
    points: list[object] = [
        TradeTick(
            instrument_id,
            Price(10.0, 2),
            Quantity.from_int(100),
            AggressorSide.BUYER,
            TradeId("P-0"),
            _MINUTE_NS,
            _MINUTE_NS,
        )
    ]
    for index, size in enumerate(print_sizes, start=1):
        ts = (index + 1) * _MINUTE_NS
        points.append(
            TradeTick(
                instrument_id,
                Price(9.5, 2),
                Quantity.from_int(size),
                AggressorSide.SELLER,
                TradeId(f"P-{index}"),
                ts,
                ts,
            )
        )
    fills = _probe_resting_limit(1.0, points, "BUY", quantity=order_qty)
    return [float(qty) for qty, _, _ in fills]  # type: ignore[arg-type]


def _check_a_print_fills_a_resting_limit_by_its_own_size() -> tuple[bool, str]:
    """What a venue's own executions buy the simulation: a print at a resting limit's price
    fills it by the print's size and no more, so a clip larger than the flow it meets fills
    in parts, one per print, until it is done."""
    parts = _probe_by_size(320, (100, 100, 100, 100))
    whole = _probe_by_size(320, (1_000,))
    holds = parts == [100.0, 100.0, 100.0, 20.0] and whole == [320.0]
    return holds, (
        f"a buy of 320 at 9.50 met by four sellers' prints of 100 at 9.50 filled {parts}; met by "
        f"one print of 1,000 it filled {whole}. A print at a resting limit's price fills it by "
        "its own size — one beyond the price that can trade with it fills all of it (the claim "
        "after) — so the fills a run reports for an order alone at its price are only as honest "
        "as the print sizes it is fed: a venue's own executions, unmerged, fill in parts; "
        "consolidated or merged prints fill whole"
    )


def _check_a_point_beyond_a_level_one_limit_fills_it_whole() -> tuple[bool, str]:
    """Where a point's size stops bounding a fill: a quote beyond a resting limit on a
    top-of-book venue, or a print beyond it that can trade with it, fills all that is left of
    it, at its price, as a maker — the one order resting here, and of several the best-priced
    (`_check_a_point_beyond_two_level_one_limits_fills_only_the_better`)."""
    whole = [(100.0, 9.5, "MAKER"), (220.0, 9.5, "MAKER")]
    beyond = _probe_resting_limit(1.0, _limit_points("trade", 9.49), "BUY", quantity=320)
    quoted = _probe_resting_limit(1.0, _limit_points("quote", 9.4, 9.49), "BUY", quantity=320)
    at = _probe_resting_limit(1.0, _limit_points("trade", 9.5), "BUY", quantity=320)
    holds = beyond == whole and quoted == whole and at == [(100.0, 9.5, "MAKER")]
    return holds, (
        f"a buy of 320 resting at 9.50 — met by a seller's print of 100 at 9.49 filled {beyond}; "
        f"by a quote of 9.40/9.49 showing 100 on the ask, {quoted}; by a seller's print of 100 "
        f"at 9.50, {at}. Once a point goes beyond the limit, the engine fills what is left of "
        "the order at its price, whatever the point's own size, assuming a market that moved "
        "through it had the size — of the one order resting here, as of the best-priced of "
        "several (the claim after); in the match a print triggers, it does so only from the "
        "side that can trade with the order"
    )


def _check_a_point_beyond_two_level_one_limits_fills_only_the_better() -> tuple[bool, str]:
    """Where the whole fill stops: a point beyond two resting limits on a top-of-book venue
    fills all that is left of the better-priced and nothing of the other, because matching the
    first moves the engine's market to that order's price for the rest of the match."""
    beyond = {
        "a seller's print of 100 at 9.45": _limit_points("trade", 9.45),
        "a quote of 9.40/9.45 showing 100 on the ask": _limit_points("quote", 9.4, 9.45),
    }
    seen = {
        (name, prob): _probe_resting_limits(prob, points, "BUY", (9.5, 9.49), quantity=320)
        for name, points in beyond.items()
        for prob in (1.0, 0.0)
    }
    better = [(100.0, 9.5, "MAKER"), (220.0, 9.5, "MAKER")]
    holds = all(fills == [better, []] for fills in seen.values())
    return holds, (
        "buys of 320 resting at 9.50 and at 9.49 — "
        + "; ".join(
            f"{name} at prob_fill_on_limit {prob:g} filled the first {fills[0]} and the second "
            f"{fills[1]}"
            for (name, prob), fills in seen.items()
        )
        + ". The point fills all that is left of the better-priced order and nothing of the "
        "other, which it also went through: once the first is filled at its limit, the engine "
        "holds its market at that price for the rest of the match"
    )


def _check_a_point_at_two_level_one_limits_fills_each_by_its_size() -> tuple[bool, str]:
    """What a point at a price credits on a top-of-book venue when two limits rest there:
    its whole size to each, since the venue keeps no record of what it credited."""
    at = {
        "a seller's print of 100 at 9.50": (_limit_points("trade", 9.5), (1.0,)),
        "a quote of 9.40/9.50 showing 100 on the ask": (
            _limit_points("quote", 9.4, 9.5),
            (1.0, 0.0),
        ),
    }
    seen = {
        (name, prob): _probe_resting_limits(prob, points, "BUY", (9.5, 9.5), quantity=320)
        for name, (points, probs) in at.items()
        for prob in probs
    }
    each = [(100.0, 9.5, "MAKER")]
    holds = all(fills == [each, each] for fills in seen.values())
    return holds, (
        "two buys of 320 resting at 9.50 — "
        + "; ".join(
            f"{name} at prob_fill_on_limit {prob:g} filled {fills}"
            for (name, prob), fills in seen.items()
        )
        + ". One point of 100 at their price is credited whole to each order, 200 in all"
    )


def _check_a_quote_at_a_level_one_limit_fills_it_again_at_every_quote() -> tuple[bool, str]:
    """What a quote at a resting limit's price credits on a top-of-book venue: the size it
    shows, and the same size again at every quote that shows it, since the venue keeps no
    record of what it credited there."""
    repeated = _limit_points("quote", *([9.4, 9.5] * 6))
    seen = {prob: _probe_resting_limit(prob, repeated, "BUY", quantity=445) for prob in (1.0, 0.0)}
    filled = [(100.0, 9.5, "MAKER")] * 4 + [(45.0, 9.5, "MAKER")]
    holds = all(fills == filled for fills in seen.values())
    return holds, (
        f"a buy of 445 resting at 9.50 met by six identical quotes of 9.40/9.50 showing 100 on "
        f"the ask filled {seen[1.0]} at prob_fill_on_limit 1 and {seen[0.0]} at 0. A quote "
        "whose far side sits at the limit fills it by the size shown, and the same unchanged "
        "100 fills it again at every quote that shows it"
    )


def _probe_market_on(points: list[object], quantity: int, on: int) -> list[tuple[object, ...]]:
    """The fills of one market buy of `quantity` sent from the handler of the `on`-th point,
    counted from one, as its quantity, its price and its liquidity side."""
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, liquidity_side_to_str
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money, Quantity
    from nautilus_trader.trading.strategy import Strategy

    equity: Any = _sample_equity()

    class Probe(Strategy):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.fills: list[tuple[object, ...]] = []
            self.seen = 0

        def on_start(self) -> None:
            self.subscribe_trade_ticks(equity.id)
            self.subscribe_quote_ticks(equity.id)

        def _count(self) -> None:
            self.seen += 1
            if self.seen == on:
                self.submit_order(
                    self.order_factory.market(equity.id, OrderSide.BUY, Quantity.from_int(quantity))
                )

        def on_trade_tick(self, tick: object) -> None:
            self._count()

        def on_quote_tick(self, tick: object) -> None:
            self._count()

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
        )
        engine.add_instrument(equity)
        engine.add_data(points)
        probe = Probe()
        engine.add_strategy(probe)
        engine.run()
        return probe.fills
    finally:
        engine.dispose()


def _check_a_print_is_the_level_one_book_until_the_next_quote() -> tuple[bool, str]:
    """What a taker sent on a print is matched against: the print, on both sides of the
    book, and one increment past it for what the print does not cover."""
    from nautilus_trader.model.data import QuoteTick, TradeTick
    from nautilus_trader.model.enums import AggressorSide
    from nautilus_trader.model.identifiers import TradeId
    from nautilus_trader.model.objects import Price, Quantity

    instrument_id = _sample_equity().id  # type: ignore[attr-defined]
    quote = QuoteTick(
        instrument_id,
        Price(9.97, 2),
        Price(10.03, 2),
        Quantity.from_int(1_000),
        Quantity.from_int(1_000),
        _MINUTE_NS,
        _MINUTE_NS,
    )
    printed = TradeTick(
        instrument_id,
        Price(10.0, 2),
        Quantity.from_int(100),
        AggressorSide.SELLER,
        TradeId("P-1"),
        2 * _MINUTE_NS,
        2 * _MINUTE_NS,
    )
    on_quote = _probe_market_on([quote], 300, 1)
    on_print = _probe_market_on([quote, printed], 300, 2)
    holds = on_quote == [(300.0, 10.03, "TAKER")] and on_print == [
        (100.0, 10.0, "TAKER"),
        (200.0, 10.01, "TAKER"),
    ]
    return holds, (
        f"under a quote of 9.97/10.03 showing 1,000 a side, a market buy of 300 sent on the "
        f"quote filled {on_quote}; sent on a seller's print of 100 at 10.00 after it, "
        f"{on_print}. A print sets both sides of a level-one book to its price and size until "
        "the next quote, and what it does not cover fills one increment past it, not at the "
        "quote's ask"
    )


def _check_liquidity_consumption_withholds_a_repeated_print() -> tuple[bool, str]:
    """Why kanso's venues leave `liquidity_consumption` at the engine's default, off: on a
    top-of-book venue it withholds the second of two identical prints at a resting price."""
    points = _limit_points("trade", 9.5, 9.5)
    on = _probe_resting_limit(1.0, points, "BUY", quantity=320, liquidity_consumption=True)
    off = _probe_resting_limit(1.0, points, "BUY", quantity=320)
    holds = on == [(100.0, 9.5, "MAKER")] and off == [(100.0, 9.5, "MAKER")] * 2
    return holds, (
        f"a buy of 320 resting at 9.50 met by two sellers' prints of 100 at 9.50 filled {on} "
        f"with liquidity_consumption on and {off} with it off: the venue remembers what it "
        "credited at a price until the size shown there changes, so a second print of the same "
        "size fills nothing"
    )


def _probe_queue(ahead: int, print_sizes: tuple[int, ...], *, queue_position: bool) -> list[int]:
    """The fill instants (in seconds) of a buy of 300 at 10.00 joining a level that already
    shows `ahead` on a level-two book, then met by sellers' prints of the given sizes."""
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.models import FillModel
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.data import BookOrder, OrderBookDelta, TradeTick
    from nautilus_trader.model.enums import (
        AccountType,
        AggressorSide,
        BookAction,
        BookType,
        OmsType,
        OrderSide,
    )
    from nautilus_trader.model.identifiers import TradeId, Venue
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    equity: Any = _sample_equity()
    second = 1_000_000_000

    def delta(ts: int, side: Any, px: float, size: int, order_id: int) -> object:
        order = BookOrder(side, Price(px, 2), Quantity.from_int(size), order_id)
        return OrderBookDelta(equity.id, BookAction.ADD, order, 0, 0, ts, ts)

    points: list[object] = [
        delta(second, OrderSide.BUY, 10.0, ahead, 1),
        delta(second, OrderSide.SELL, 10.05, 500, 2),
    ]
    for index, size in enumerate(print_sizes):
        ts = (2 + index) * second
        points.append(
            TradeTick(
                equity.id,
                Price(10.0, 2),
                Quantity.from_int(size),
                AggressorSide.SELLER,
                TradeId(f"T-{index}"),
                ts,
                ts,
            )
        )

    class Probe(Strategy):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.filled_at: list[int] = []
            self.sent = False

        def on_start(self) -> None:
            self.subscribe_order_book_deltas(equity.id)
            self.subscribe_trade_ticks(equity.id)

        def _join(self) -> None:
            if not self.sent:
                self.sent = True
                self.submit_order(
                    self.order_factory.limit(
                        equity.id, OrderSide.BUY, Quantity.from_int(300), Price(10.0, 2)
                    )
                )

        def on_order_book_deltas(self, deltas: object) -> None:
            self._join()

        def on_trade_tick(self, tick: object) -> None:
            self._join()

        def on_order_filled(self, event: Any) -> None:
            self.filled_at.append(int(event.ts_event) // second)

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("XNAS"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(1_000_000, USD)],
            fill_model=FillModel(prob_fill_on_limit=1.0),
            book_type=BookType.L2_MBP,
            trade_execution=True,
            queue_position=queue_position,
        )
        engine.add_instrument(equity)
        engine.add_data(points)
        probe = Probe()
        engine.add_strategy(probe)
        engine.run()
        return probe.filled_at
    finally:
        engine.dispose()


_MS = 1_000_000


def _tape_quote(bid: float, ask: float, ms: int, ask_size: int = 1_000) -> object:
    """A quote of the sample equity `ms` milliseconds after the first minute, 1,000 on the bid."""
    from nautilus_trader.model.data import QuoteTick
    from nautilus_trader.model.objects import Price, Quantity

    ts = _MINUTE_NS + ms * _MS
    return QuoteTick(
        _sample_equity().id,  # type: ignore[attr-defined]
        Price(bid, 2),
        Price(ask, 2),
        Quantity.from_int(1_000),
        Quantity.from_int(ask_size),
        ts,
        ts,
    )


def _tape_print(price: float, size: int, ms: int, side: Any = None) -> object:
    """A print of the sample equity `ms` milliseconds after the first minute, with no aggressor
    unless `side` names one."""
    from nautilus_trader.model.data import TradeTick
    from nautilus_trader.model.enums import AggressorSide
    from nautilus_trader.model.identifiers import TradeId
    from nautilus_trader.model.objects import Price, Quantity

    ts = _MINUTE_NS + ms * _MS
    return TradeTick(
        _sample_equity().id,  # type: ignore[attr-defined]
        Price(price, 2),
        Quantity.from_int(size),
        AggressorSide.NO_AGGRESSOR if side is None else side,
        TradeId(f"P-{ms}"),
        ts,
        ts,
    )


def _probe_script(
    points: list[object],
    script: dict[int, list[tuple[str, str, int, float]]],
    fill_model: Any,
    *,
    modules: tuple[object, ...] = (),
    latency_ms: int = 0,
) -> dict[str, list[Any]]:
    """What the orders `script` sends do on a top-of-book venue built as kanso builds one, with
    this fill model and these modules: on the n-th point handled, counted from one, each
    `(kind, side, quantity, price)` — a `limit` at the price, or a `market` order. Each fill
    is its instant in milliseconds after the first minute, its quantity, its price and its
    liquidity side; beside them the reasons of every rejection and each order's last status."""
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.models import LatencyModel
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import (
        AccountType,
        BookType,
        OmsType,
        OrderSide,
        liquidity_side_to_str,
        order_status_to_str,
    )
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    equity: Any = _sample_equity()

    class Probe(Strategy):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.seen = 0
            self.fills: list[tuple[object, ...]] = []
            self.rejected: list[str] = []
            self.sent: list[Any] = []

        def on_start(self) -> None:
            self.subscribe_trade_ticks(equity.id)
            self.subscribe_quote_ticks(equity.id)

        def _point(self) -> None:
            self.seen += 1
            for kind, side, quantity, price in script.get(self.seen, []):
                order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
                if kind == "market":
                    order = self.order_factory.market(
                        equity.id, order_side, Quantity.from_int(quantity)
                    )
                else:
                    order = self.order_factory.limit(
                        equity.id, order_side, Quantity.from_int(quantity), Price(price, 2)
                    )
                self.sent.append(order)
                self.submit_order(order)

        def on_trade_tick(self, tick: object) -> None:
            self._point()

        def on_quote_tick(self, tick: object) -> None:
            self._point()

        def on_order_filled(self, event: Any) -> None:
            self.fills.append(
                (
                    (int(event.ts_event) - _MINUTE_NS) // _MS,
                    float(event.last_qty),
                    float(event.last_px),
                    liquidity_side_to_str(event.liquidity_side),
                )
            )

        def on_order_rejected(self, event: Any) -> None:
            self.rejected.append(str(event.reason))

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("XNAS"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(1_000_000, USD)],
            book_type=BookType.L1_MBP,
            bar_execution=True,
            trade_execution=True,
            fill_model=fill_model,
            latency_model=LatencyModel(
                base_latency_nanos=latency_ms * _MS,
                insert_latency_nanos=0,
                update_latency_nanos=0,
                cancel_latency_nanos=0,
            )
            if latency_ms
            else None,
            modules=list(modules),
        )
        engine.add_instrument(equity)
        engine.add_data(points)
        probe = Probe()
        engine.add_strategy(probe)
        engine.run()
        return {
            "fills": probe.fills,
            "rejected": probe.rejected,
            "status": [
                order_status_to_str(engine.cache.order(order.client_order_id).status)
                for order in probe.sent
            ],
        }
    finally:
        engine.dispose()


def _answering(fills: Any = None, taker: Any = None) -> Any:
    """A fill model that answers a resting order's match with the book `fills(order)` builds
    and a taker's with `taker(instrument, order)`, the engine's own book where either is
    `None`; and that records, for every match it is asked about, the order's liquidity side,
    the best bid and ask it was handed, the order's status and the instant of the last point a
    `_clocked` module saw. `taker` is handed the best bid and ask the engine holds as well."""
    from nautilus_trader.backtest.models import FillModel
    from nautilus_trader.model.book import OrderBook
    from nautilus_trader.model.enums import BookType, LiquiditySide, liquidity_side_to_str

    class Answer(OrderBook):  # type: ignore[misc]
        def __init__(self, instrument_id: Any, answer: list[Any]) -> None:
            super().__init__(instrument_id, BookType.L2_MBP)
            self.answer = answer

        def simulate_fills(self, *args: Any) -> list[Any]:
            return list(self.answer)

    class Answering(FillModel):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__(prob_fill_on_limit=1.0, prob_slippage=0.0)
            self.asked: list[tuple[object, ...]] = []
            self.now = 0

        def get_orderbook_for_fill_simulation(
            self, instrument: Any, order: Any, best_bid: Any, best_ask: Any
        ) -> Any:
            self.asked.append(
                (
                    (self.now - _MINUTE_NS) // _MS,
                    liquidity_side_to_str(order.liquidity_side),
                    str(best_bid),
                    str(best_ask),
                    order.status_string(),
                )
            )
            if order.liquidity_side == LiquiditySide.MAKER:
                return None if fills is None else Answer(instrument.id, fills(order))
            return None if taker is None else taker(instrument, order, best_bid, best_ask)

    return Answering()


def _clocked(*, lands: bool) -> object:
    """A module that tells an `_answering` model the instant of each point before the venue
    applies it, and, when `lands`, first lands every command due by a quote's or a print's
    instant."""
    from nautilus_trader.backtest.config import SimulationModuleConfig
    from nautilus_trader.backtest.modules import SimulationModule
    from nautilus_trader.model.data import QuoteTick, TradeTick

    class Clocked(SimulationModule):  # type: ignore[misc]
        def pre_process(self, data: Any) -> None:
            if lands and isinstance(data, QuoteTick | TradeTick):
                self.exchange.process(int(data.ts_init))
            if hasattr(self.exchange.fill_model, "asked"):
                self.exchange.fill_model.now = int(data.ts_init)

        def process(self, ts_now: int) -> None:
            """Nothing: the module acts on points."""

        def log_diagnostics(self, logger: Any) -> None:
            """Nothing: what it did is in what the model recorded."""

        def reset(self) -> None:
            """Nothing: it holds no state."""

    return Clocked(SimulationModuleConfig(component_id="Clocked-XNAS"))


def _zero(order: Any) -> list[Any]:
    from nautilus_trader.model.objects import Quantity

    return [(order.price, Quantity.zero(0))]


def _check_a_zero_quantity_fill_ends_a_simulated_fill() -> tuple[bool, str]:
    """What lets a fill model fill a resting limit by a print's size: the engine takes the
    model's book in place of its own, and stops at a fill of zero quantity before it fills
    what is left of the order whole."""
    from nautilus_trader.model.objects import Quantity

    points = [_tape_quote(9.48, 9.52, 10), _tape_print(9.49, 100, 100)]
    script = {1: [("limit", "BUY", 320, 9.5)]}
    hundred = Quantity.from_int(100)
    ended = _probe_script(
        points,
        script,
        _answering(lambda order: [(order.price, hundred), (order.price, Quantity.zero(0))]),
    )["fills"]
    open_ = _probe_script(points, script, _answering(lambda order: [(order.price, hundred)]))[
        "fills"
    ]
    holds = ended == [(100, 100.0, 9.5, "MAKER")] and open_ == [
        (100, 100.0, 9.5, "MAKER"),
        (100, 220.0, 9.5, "MAKER"),
    ]
    return holds, (
        "a buy of 320 resting at 9.50 under 9.48/9.52, met by a print of 100 at 9.49 with no "
        f"aggressor: a fill model answering 100 at 9.50 and then a fill of zero filled {ended}; "
        f"answering 100 at 9.50 alone, {open_}. The engine fills from the model's book and "
        "returns at the zero, before it fills the remainder of a limit a point went beyond"
    )


def _check_a_market_order_fills_from_the_fill_model_s_book() -> tuple[bool, str]:
    """What prices a taker under `print_through`: the book the fill model answers for a market
    order, not the print standing as the venue's book, with the engine's one-increment walk for
    what that book does not cover."""
    from nautilus_trader.model.book import OrderBook
    from nautilus_trader.model.enums import BookType

    shown = _tape_quote(9.48, 9.52, 10, ask_size=100)

    def quoted(instrument: Any, order: Any, best_bid: Any, best_ask: Any) -> Any:
        book = OrderBook(instrument.id, BookType.L1_MBP)
        book.update_quote_tick(shown)
        return book

    walked = _probe_script(
        [shown, _tape_quote(9.48, 9.52, 20, ask_size=100)],
        {2: [("market", "BUY", 300, 0.0)]},
        _answering(taker=quoted),
    )["fills"]
    shown = _tape_quote(9.48, 9.52, 10)
    on_print = _probe_script(
        [shown, _tape_print(9.5, 100, 20)],
        {2: [("market", "BUY", 300, 0.0)]},
        _answering(taker=quoted),
    )["fills"]
    holds = walked == [(20, 100.0, 9.52, "TAKER"), (20, 200.0, 9.53, "TAKER")] and on_print == [
        (20, 300.0, 9.52, "TAKER")
    ]
    return holds, (
        "a fill model answering a market order with a top-of-book book of the quote 9.48/9.52: "
        f"a market buy of 300 against an ask of 100 filled {walked}; one sent on a print of 100 "
        f"at 9.50 under an ask of 1,000 filled {on_print}. The engine fills a market order from "
        "the model's book, not the print standing as its own, and walks one increment past it "
        "for the rest"
    )


def _check_a_lone_zero_fill_refuses_a_market_order() -> tuple[bool, str]:
    """What the print rule answers a taker with no quote to fill on: a market order is refused
    for want of a market, and a limit accepted on arrival rests at its price."""

    def nothing(instrument: Any, order: Any, best_bid: Any, best_ask: Any) -> Any:
        from nautilus_trader.model.book import OrderBook
        from nautilus_trader.model.enums import BookType
        from nautilus_trader.model.objects import Quantity

        class Zero(OrderBook):  # type: ignore[misc]
            def simulate_fills(self, *args: Any) -> list[Any]:
                return [(order.price if order.has_price else best_ask, Quantity.zero(0))]

        return Zero(instrument.id, BookType.L2_MBP)

    prints = [_tape_print(9.5, 100, 10), _tape_print(9.51, 100, 20)]
    market = _probe_script(
        prints, {1: [("market", "BUY", 50, 0.0)]}, _answering(_zero, taker=nothing)
    )
    limit = _probe_script(
        prints, {1: [("limit", "BUY", 50, 9.55)]}, _answering(_zero, taker=nothing)
    )
    holds = (
        market["fills"] == []
        and len(market["rejected"]) == 1
        and market["rejected"][0].startswith("no market for")
        and market["status"] == ["REJECTED"]
        and limit["fills"] == []
        and limit["rejected"] == []
        and limit["status"] == ["ACCEPTED"]
    )
    return holds, (
        "on prints alone, answered with a single fill of zero: a market buy of 50 filled "
        f"{market['fills']}, was rejected {market['rejected']} and ended {market['status']}; a "
        f"buy of 50 limited at 9.55, marketable against the print at 9.50, filled "
        f"{limit['fills']} and ended {limit['status']}"
    )


def _check_a_drain_re_matches_every_resting_order() -> tuple[bool, str]:
    """Why the print rule keeps a record of each print: the research path matches every resting
    order again once a command lands, asking the fill model with exactly the arguments of the
    print's own match, and so reaches an order the print's own match did not."""
    from nautilus_trader.model.enums import AggressorSide

    quote = _tape_quote(9.48, 9.52, 10)
    seen: dict[str, list[tuple[object, ...]]] = {}
    for name, side, script in (
        ("no aggressor, a sell sent on it", None, {2: [("limit", "SELL", 10, 9.9)]}),
        ("a buyer's, nothing sent on it", AggressorSide.BUYER, {}),
        ("a buyer's, a sell sent on it", AggressorSide.BUYER, {2: [("limit", "SELL", 10, 9.9)]}),
    ):
        model = _answering(_zero)
        _probe_script(
            [quote, _tape_print(9.49, 100, 100, side)],
            {1: [("limit", "BUY", 320, 9.5)], **script},
            model,
            modules=(_clocked(lands=False),),
        )
        seen[name] = [asked for asked in model.asked if asked[1] == "MAKER"]
    asked = (100, "MAKER", "9.49", "9.49", "ACCEPTED")
    holds = seen == {
        "no aggressor, a sell sent on it": [asked, asked],
        "a buyer's, nothing sent on it": [],
        "a buyer's, a sell sent on it": [asked],
    }
    return holds, (
        "a buy of 320 resting at 9.50, then a print of 100 at 9.49 at 100 ms, a fill model "
        "answering every match with nothing: the resting buy was asked about — as (instant, "
        "side, best bid, best ask, status) — "
        + "; ".join(f"with a print of {name}, {calls}" for name, calls in seen.items())
        + ". Once a command lands the engine matches every resting order again, with the "
        "arguments of the print's own match, and reaches a buy a buyer's print did not"
    )


def _check_a_module_lands_due_commands_before_the_point() -> tuple[bool, str]:
    """Why a command due by a print reaches the book before it under `print_through`: a module
    that calls its exchange's `process` from `pre_process` lands it before the matching engine
    applies the point, where the engine alone lands it after matching that point."""
    from nautilus_trader.backtest.models import FillModel

    points = [
        _tape_quote(9.48, 9.52, 10),
        _tape_quote(9.48, 9.52, 100),
        _tape_quote(9.45, 9.49, 125),
        _tape_quote(9.48, 9.52, 140),
    ]
    seen = {
        lands: _probe_script(
            points,
            {2: [("limit", "BUY", 320, 9.5)]},
            FillModel(prob_fill_on_limit=1.0, prob_slippage=0.0),
            modules=(_clocked(lands=lands),),
            latency_ms=20,
        )["fills"]
        for lands in (True, False)
    }
    holds = seen == {True: [(125, 320.0, 9.5, "MAKER")], False: [(125, 320.0, 9.49, "TAKER")]}
    return holds, (
        "a buy of 320 at 9.50 sent on a quote of 9.48/9.52 at 100 ms under a latency of 20 ms, "
        "due at 120, then a quote of 9.45/9.49 at 125 ms, under the engine's own fill model: "
        f"with a module landing what is due before each point it filled {seen[True]}, resting "
        f"before the quote went through it; without it, {seen[False]}, landing on the quote it "
        "had already applied. Alone the engine lands a command after it matches the first point "
        "at or after its delay"
    )


def _check_a_print_inside_the_quote_leaves_every_order_it_makes_marketable_a_taker() -> tuple[
    bool, str
]:
    """Why the two paths judge a taker alike under `print_through`: whatever a print inside the
    last quote does to the engine's own bid and ask, and whether or not a landing command has
    since made the engine read them again from its book, they are never wider than that quote —
    the bid never under its bid, the ask never over its ask — so every limit the quote makes
    marketable is matched on landing and the fill model, which answers from the quote, decides
    it."""
    from nautilus_trader.model.enums import AggressorSide

    quote = _tape_quote(9.48, 9.52, 10)
    marketable = {3: [("limit", "SELL", 10, 9.48), ("limit", "BUY", 10, 9.52)]}
    seen: dict[str, list[tuple[str, str]]] = {}
    for side, name in (
        (None, "no aggressor"),
        (AggressorSide.BUYER, "a buyer's"),
        (AggressorSide.SELLER, "a seller's"),
    ):
        for matched, script in (
            ("", marketable),
            (", a command landed between", {2: [("limit", "BUY", 1, 9.0)], **marketable}),
        ):
            model = _answering(_zero, taker=lambda instrument, order, bid, ask: None)
            _probe_script(
                [quote, _tape_print(9.5, 100, 20, side), _tape_print(9.5, 100, 30, side)],
                script,
                model,
                modules=(_clocked(lands=False),),
            )
            seen[name + matched] = [
                (str(asked[2]), str(asked[3]))
                for asked in model.asked
                if asked[0] == 30 and asked[1] == "TAKER"
            ]
    holds = all(
        len(asked) >= 2 and all(float(bid) >= 9.48 and float(ask) <= 9.52 for bid, ask in asked)
        for asked in seen.values()
    )
    return holds, (
        "under a quote of 9.48/9.52, two prints of 100 at 9.50, a sell limited at 9.48 and a "
        "buy at 9.52 sent on the second: each was matched on landing as a taker, with the "
        "engine's bid and ask — "
        + "; ".join(f"after prints of {name}, {asked}" for name, asked in seen.items())
        + ". A print inside the quote never puts the engine's bid under the quote's bid nor its "
        "ask over the quote's ask, before a re-match or after one"
    )


def _check_kanso_s_print_through_venue() -> tuple[bool, str]:
    """`kanso.nautilus.tape` as kanso loads it: a resting limit fills only from a later print
    strictly through its price, by that print's size, never from a quote or a print at its
    price, and a taker fills at the last quote's touch rather than at a print."""
    from nautilus_trader.backtest.node import get_fill_model
    from nautilus_trader.config import BacktestVenueConfig

    from kanso.nautilus.actions import modules
    from kanso.nautilus.venue import fill_model

    def built(rule: str) -> Any:
        return get_fill_model(
            BacktestVenueConfig(
                name="XNAS",
                oms_type="NETTING",
                account_type="MARGIN",
                starting_balances=["1000000 USD"],
                fill_model=fill_model(rule),  # type: ignore[arg-type]
            )
        )

    resting = [
        _tape_quote(9.48, 9.52, 10),
        _tape_quote(9.45, 9.49, 20, ask_size=100),
        _tape_quote(9.48, 9.52, 30),
        _tape_print(9.5, 100, 40),
        _tape_print(9.49, 100, 50),
        _tape_print(9.48, 100, 60),
    ]
    rested = {
        rule: _probe_script(
            resting,
            {1: [("limit", "BUY", 320, 9.5)]},
            built(rule),
            modules=tuple(modules("XNAS")),
        )["fills"]
        for rule in ("print_through", "print_through_whole")
    }
    taken = _probe_script(
        [_tape_quote(9.48, 9.52, 10), _tape_print(9.5, 100, 20)],
        {2: [("market", "BUY", 300, 0.0)]},
        built("print_through"),
        modules=tuple(modules("XNAS")),
    )["fills"]
    holds = (
        rested["print_through"] == [(50, 100.0, 9.5, "MAKER"), (60, 100.0, 9.5, "MAKER")]
        and rested["print_through_whole"] == [(50, 320.0, 9.5, "MAKER")]
        and taken == [(20, 300.0, 9.52, "TAKER")]
    )
    return holds, (
        "kanso's venue modules and the fill model `limit_fill` names, a buy of 320 resting at "
        "9.50 under 9.48/9.52, then a quote of 9.45/9.49 showing 100, a print of 100 at 9.50 "
        f"and prints of 100 at 9.49 and 9.48: under print_through it filled "
        f"{rested['print_through']}, under print_through_whole {rested['print_through_whole']}; "
        f"a market buy of 300 sent on a print of 100 at 9.50 under 9.48/9.52 filled {taken}"
    )


def _check_a_level_two_book_keeps_one_size_per_price() -> tuple[bool, str]:
    """What the harness's own copy of a `depth` book mirrors: the engine's L2 rules."""
    from nautilus_trader.model.book import OrderBook
    from nautilus_trader.model.data import BookOrder, OrderBookDelta
    from nautilus_trader.model.enums import BookAction, BookType, OrderSide
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.objects import Price, Quantity

    instrument_id = InstrumentId.from_str("BOOK.SIM")
    book = OrderBook(instrument_id, BookType.L2_MBP)

    def apply(action: Any, side: Any, price: float, size: int) -> list[tuple[float, float]]:
        order = BookOrder(side, Price(price, 2), Quantity(size, 0), 0)
        book.apply_delta(OrderBookDelta(instrument_id, action, order, 0, 0, 1, 1))
        return [(level.price.as_double(), level.size()) for level in book.bids()]

    updated = apply(BookAction.UPDATE, OrderSide.BUY, 10.00, 500)
    replaced = apply(BookAction.ADD, OrderSide.BUY, 10.00, 300)
    ignored = apply(BookAction.DELETE, OrderSide.BUY, 9.99, 0)
    apply(BookAction.ADD, OrderSide.SELL, 10.02, 100)
    cleared = apply(BookAction.CLEAR, OrderSide.NO_ORDER_SIDE, 0.0, 0)
    holds = (
        updated == [(10.0, 500.0)]
        and replaced == [(10.0, 300.0)]
        and ignored == [(10.0, 300.0)]
        and cleared == []
        and book.asks() == []
    )
    return holds, (
        f"an update of a price not held left the bids {updated}; an add over it {replaced}; a "
        f"delete of a price not held {ignored}; a clear left bids {cleared} and asks "
        f"{[level.price.as_double() for level in book.asks()]}"
    )


def _check_queue_position_waits_for_the_size_ahead() -> tuple[bool, str]:
    """What `queue_position` buys on a level-two book: a limit joining a level fills only once
    the size shown ahead of it at placement has traded through, and without it fills from
    the first print at its price."""
    prints = (100,) * 8
    waited = _probe_queue(500, prints, queue_position=True)
    jumped = _probe_queue(500, prints, queue_position=False)
    holds = waited == [7, 8, 9] and jumped == [2, 3, 4]
    return holds, (
        f"a buy of 300 joining a bid level of 500 on a level-two book, then eight sellers' "
        f"prints of 100 one second apart from t=2: filled at seconds {waited} with queue_position "
        f"and at {jumped} without. With it the order waits until the 500 ahead has traded "
        "through, then fills by print size; without it every print at the price fills it"
    )


def _probe_book_batch(*, batched: bool) -> tuple[list[tuple[int, float | None]], list[int]]:
    """A book of 100 bid at 9.95 and 100 offered at 10.05, a buy of 10 resting at 10.00 sent
    from the first call, then an instant that adds an offer at 10.00 and deletes it: each
    call's delta count and the best offer the cache showed it, and the fill instants (in
    seconds). Fed a delta at a time, or as one `OrderBookDeltas` per instant."""
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.models import FillModel
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas
    from nautilus_trader.model.enums import AccountType, BookAction, BookType, OmsType, OrderSide
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    equity: Any = _sample_equity()
    second = 1_000_000_000

    def delta(ts: int, action: Any, side: Any, px: float, size: int) -> Any:
        order = BookOrder(side, Price(px, 2), Quantity.from_int(size), 0)
        return OrderBookDelta(equity.id, action, order, 0, 0, ts, ts)

    instants = [
        [
            delta(second, BookAction.ADD, OrderSide.BUY, 9.95, 100),
            delta(second, BookAction.ADD, OrderSide.SELL, 10.05, 100),
        ],
        [
            delta(3 * second, BookAction.ADD, OrderSide.SELL, 10.00, 100),
            delta(3 * second, BookAction.DELETE, OrderSide.SELL, 10.00, 0),
        ],
    ]
    points: list[object] = (
        [OrderBookDeltas(equity.id, changes) for changes in instants]
        if batched
        else [change for changes in instants for change in changes]
    )

    class Probe(Strategy):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[int, float | None]] = []
            self.filled_at: list[int] = []

        def on_start(self) -> None:
            self.subscribe_order_book_deltas(equity.id)

        def on_order_book_deltas(self, deltas: Any) -> None:
            best = self.cache.order_book(equity.id).best_ask_price()
            self.calls.append((len(deltas.deltas), None if best is None else float(best)))
            if len(self.calls) == 1:
                self.submit_order(
                    self.order_factory.limit(
                        equity.id, OrderSide.BUY, Quantity.from_int(10), Price(10.0, 2)
                    )
                )

        def on_order_filled(self, event: Any) -> None:
            self.filled_at.append(int(event.ts_event) // second)

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("XNAS"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(1_000_000, USD)],
            fill_model=FillModel(prob_fill_on_limit=1.0),
            book_type=BookType.L2_MBP,
            trade_execution=True,
            queue_position=True,
        )
        engine.add_instrument(equity)
        engine.add_data(points)
        probe = Probe()
        engine.add_strategy(probe)
        engine.run()
        return probe.calls, probe.filled_at
    finally:
        engine.dispose()


def _check_a_book_batch_is_matched_once() -> tuple[bool, str]:
    """What a batch buys the venue: it matches after the whole instant, not after each change."""
    _, singly = _probe_book_batch(batched=False)
    _, whole = _probe_book_batch(batched=True)
    holds = singly == [3] and whole == []
    return holds, (
        f"a buy resting at 10.00 under an offer of 10.05, then an instant adding an offer at "
        f"10.00 and deleting it: filled at seconds {singly} fed a change at a time and at "
        f"{whole} fed as one OrderBookDeltas, which the exchange applies whole before it matches"
    )


def _check_a_book_batch_is_published_whole() -> tuple[bool, str]:
    """What a batch buys the strategy: one call per instant, after the book has taken all of it."""
    singly, _ = _probe_book_batch(batched=False)
    whole, _ = _probe_book_batch(batched=True)
    holds = singly == [(1, None), (1, 10.05), (1, 10.0), (1, 10.05)] and whole == [
        (2, 10.05),
        (2, 10.05),
    ]
    return holds, (
        f"two instants of two changes each reached on_order_book_deltas as {singly} "
        "(deltas per call, best offer the cache showed) fed a change at a time, and as "
        f"{whole} fed as one OrderBookDeltas per instant: the data engine publishes a batch "
        "whole, after its own book has applied every change in it"
    )


def _probe_book_check(*, validate: bool) -> str | None:
    """Two names on a level-two venue, handed as kanso hands a stream: one call of book
    changes, the first name's ahead of the second's, then the second name's print alone, all
    added with `validate`. What `run` raised, or `None` when it ran."""
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.data import BookOrder, OrderBookDelta, TradeTick
    from nautilus_trader.model.enums import (
        AccountType,
        AggressorSide,
        BookAction,
        BookType,
        OmsType,
        OrderSide,
    )
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Currency, Money, Price, Quantity

    first: Any = _sample_equity()
    second = Equity(
        instrument_id=InstrumentId.from_str("MSFT.XNAS"),
        raw_symbol=Symbol("MSFT"),
        currency=Currency.from_str("USD"),
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )

    def change(name: Any, ts: int) -> object:
        order = BookOrder(OrderSide.BUY, Price(10.0, 2), Quantity.from_int(100), 0)
        return OrderBookDelta(name.id, BookAction.ADD, order, 0, 0, ts, ts)

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("XNAS"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(1_000_000, USD)],
            book_type=BookType.L2_MBP,
        )
        engine.add_instrument(first)
        engine.add_instrument(second)
        engine.add_data([change(first, 1), change(second, 2)], validate=validate, sort=False)
        printed = TradeTick(
            second.id,
            Price(10.0, 2),
            Quantity.from_int(10),
            AggressorSide.SELLER,
            TradeId("T-1"),
            3,
            3,
        )
        engine.add_data([printed], validate=validate, sort=False)
        engine.sort_data()
        return _raises(engine.run)
    finally:
        engine.dispose()


def _check_the_book_check_reads_the_first_point_of_a_call() -> tuple[bool, str]:
    """Why kanso adds a held book's market data unvalidated and refuses a day without a
    name's book itself: the engine's own check counts a call's first name and no other."""
    checked = _probe_book_check(validate=True)
    unchecked = _probe_book_check(validate=False)
    holds = (
        checked is not None
        and checked.startswith("InvalidConfiguration")
        and "MSFT.XNAS" in checked
        and unchecked is None
    )
    return holds, (
        "two names on an L2_MBP venue, handed one call of book changes, AAPL's then MSFT's, and "
        f"then MSFT's print alone: validated, run raised {checked!r}, though MSFT's change was "
        f"in the stream; unvalidated, it {'ran' if unchecked is None else 'raised ' + unchecked}"
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


def _check_files_keep_an_instant_in_file_order() -> tuple[bool, str]:
    """Write 30,000 prints holding 1 to 40 to an instant, ten seconds apart, and read them
    back through `query(files=...)` whole and in five spans cut at instants."""
    from nautilus_trader.model.data import TradeTick
    from nautilus_trader.model.enums import AggressorSide
    from nautilus_trader.model.identifiers import TradeId
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    equity: Any = _sample_equity()
    second = 1_000_000_000
    stamps: list[int] = []
    cohort = 0
    while len(stamps) < 30_000:
        stamps += [second * (1 + 10 * cohort)] * (1 + cohort * 7_919 % 40)
        cohort += 1
    stamps = stamps[:30_000]
    prints = [
        TradeTick(
            equity.id,
            Price(10.0, 2),
            Quantity.from_int(1),
            AggressorSide.BUYER,
            TradeId(str(index)),
            ts,
            ts,
        )
        for index, ts in enumerate(stamps)
    ]
    with tempfile.TemporaryDirectory() as directory:
        catalog = ParquetDataCatalog(directory)
        catalog.write_data([equity])
        catalog.write_data(prints)
        identifier = str(equity.id)

        def read(start: int, end: int) -> list[str]:
            files = sorted(
                catalog.filter_files(
                    TradeTick,
                    catalog.get_file_list_from_data_cls(TradeTick),
                    [identifier],
                    start,
                    end,
                )
            )
            found = catalog.query(TradeTick, start=start, end=end, files=files)
            return [str(point.trade_id) for point in found]

        cuts = [stamps[0], *(stamps[len(stamps) * k // 5] for k in range(1, 5)), stamps[-1] + 1]
        whole = read(stamps[0], stamps[-1])
        spans = [read(start, end - 1) for start, end in zip(cuts, cuts[1:], strict=False)]
    in_file = [str(index) for index in range(len(stamps))]
    pieced = [trade for span in spans for trade in span]
    holds = whole == in_file and pieced == in_file
    return holds, (
        f"{len(stamps)} prints, 1 to 40 an instant, read through query(files=...): whole "
        f"{'in' if whole == in_file else 'out of'} file order, and in five spans cut at "
        f"instants {'in' if pieced == in_file else 'out of'} file order, "
        f"{len(pieced)} prints in all"
    )


def _sale(name: str, ts: int) -> Any:
    """One print of `name` at `ts`, a nanosecond count serving as both timestamps."""
    from nautilus_trader.model.data import TradeTick
    from nautilus_trader.model.enums import AggressorSide
    from nautilus_trader.model.identifiers import InstrumentId, TradeId
    from nautilus_trader.model.objects import Price, Quantity

    return TradeTick(
        InstrumentId.from_str(name),
        Price(10.0, 2),
        Quantity.from_int(1),
        AggressorSide.BUYER,
        TradeId("1"),
        ts,
        ts,
    )


def _check_a_series_is_filed_in_its_own_directory() -> tuple[bool, str]:
    """Write a bar, two names' prints at one instant and a point of a custom type of no
    instrument into one catalog, see which files each write created and what it named them,
    and ask `filter_files` which of a series' files meet spans touching, and just missing,
    the instant it holds, closed and open at either end."""
    from pathlib import Path

    from nautilus_trader.model.data import Bar, TradeTick
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
    from nautilus_trader.persistence.funcs import class_to_filename, urisafe_identifier

    from kanso.data.catalog import ENGINE_FILE

    bar: Any = _sample_bar()
    wide = _define_custom_type({"value": float})()
    series: list[tuple[object, type, str | None]] = [
        (bar, Bar, str(bar.bar_type)),
        (_sale("AAPL.XNAS", 5), TradeTick, "AAPL.XNAS"),
        (_sale("MSFT.XNAS", 5), TradeTick, "MSFT.XNAS"),
        (wide(value=1.0, ts_event=7, ts_init=7), wide, None),
    ]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "data"
        catalog = ParquetDataCatalog(directory)

        def files() -> set[Path]:
            return {path for path in root.rglob("*") if path.is_file()}

        def home(cls: type, identifier: str | None) -> Path:
            filed = root / class_to_filename(cls)
            return filed if identifier is None else filed / urisafe_identifier(identifier)

        placed: list[str] = []
        named: list[str] = []
        filed = True
        for point, cls, identifier in series:
            before = files()
            catalog.write_data([point])
            new = files() - before
            placed.append(
                f"{cls.__name__} in {sorted(p.parent.relative_to(root).as_posix() for p in new)}"
            )
            named.extend(sorted(p.name for p in new))
            filed = filed and len(new) == 1 and {p.parent for p in new} == {home(cls, identifier)}

        def meeting(cls: type, identifier: str | None, start: int | None, end: int | None) -> int:
            held = sorted(str(path) for path in home(cls, identifier).glob("*.parquet"))
            return len(catalog.filter_files(cls, held, None, start, end))

        spans = [(5, 5), (0, 4), (6, 10), (None, 5), (None, 4), (5, None), (6, None)]
        prints = [meeting(TradeTick, "AAPL.XNAS", *span) for span in spans]
        market = [meeting(wide, None, *span) for span in [(7, 7), (0, 6), (8, 10)]]
    form = all(ENGINE_FILE.fullmatch(name) for name in named)
    holds = filed and form and prints == [1, 0, 0, 1, 0, 1, 0] and market == [1, 0, 0]
    return holds, (
        f"write_data filed {'; '.join(placed)}, named {named}; filter_files kept {prints} of "
        f"the print's directory over {spans} and {market} of the series of no instrument's "
        "over its own instant, the instants before it and the instants after it"
    )


def _check_delete_with_no_identifier_removes_a_series_of_no_instrument() -> tuple[bool, str]:
    """Write a point of a custom type of no instrument, and two names' prints, ask the
    engine to remove each class over every instant without naming an identifier, and see
    whose files are left."""
    from pathlib import Path

    from nautilus_trader.model.data import TradeTick
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
    from nautilus_trader.persistence.funcs import class_to_filename

    wide = _define_custom_type({"value": float})()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "data"
        catalog = ParquetDataCatalog(directory)
        catalog.write_data([wide(value=1.0, ts_event=7, ts_init=7)])
        catalog.write_data([_sale("AAPL.XNAS", 5), _sale("MSFT.XNAS", 5)])
        catalog.delete_data_range(wide, None, 0, 10)
        catalog.delete_data_range(TradeTick, None, 0, 10)
        left = sorted({p.parent.relative_to(root).as_posix() for p in root.rglob("*.parquet")})
    market = class_to_filename(wide)
    prints_removed = 2 - len([name for name in left if name != market])
    holds = market not in left
    return holds, (
        "delete_data_range(cls, None, 0, 10) of a series of no instrument and of two names' "
        f"prints removed the prints of {prints_removed} of the 2 names and "
        f"{'none' if market in left else 'all'} of the series of no instrument's files, "
        f"which are filed in the class's own directory; files are left in {left}"
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


def _check_custom_data_pickles_its_payload_by_reference() -> tuple[bool, str]:
    """What a card child needs to unpickle the points it is handed: the payload's class,
    importable under the module and the name it was pickled by."""
    import pickle
    import pickletools

    from nautilus_trader.model.data import CustomData, DataType
    from nautilus_trader.model.identifiers import InstrumentId

    from kanso.data.types import CorporateAction

    point = CustomData(
        DataType(CorporateAction),
        CorporateAction(
            instrument_id=InstrumentId.from_str("AAPL.XNAS"),
            kind="split",
            ratio=4.0,
            cash=0.0,
            currency="USD",
            ex_date_ns=3,
            ts_event=1,
            ts_init=2,
        ),
    )
    blob = pickle.dumps(point, protocol=pickle.HIGHEST_PROTOCOL)
    named = {arg for _, arg, _ in pickletools.genops(blob) if isinstance(arg, str)}
    module, name = CorporateAction.__module__, CorporateAction.__qualname__
    back = pickle.loads(blob)
    holds = {module, name} <= named and type(back.data) is CorporateAction
    return holds, (
        f"a pickled CustomData names {module!r} and {name!r} for its payload and read back "
        f"as {type(back.data).__name__}: the payload travels by reference to its class, so a "
        "process unpickling it must import the class's module under that name first"
    )


# --- instruments -------------------------------------------------------------


def _sample_perpetual(**fields: Any) -> Any:
    """A linear BTC perpetual quoted and settled in USDT, built from exactly the fields
    the engine requires plus whatever `fields` adds or replaces."""
    from nautilus_trader.model.identifiers import InstrumentId, Symbol
    from nautilus_trader.model.instruments import CryptoPerpetual
    from nautilus_trader.model.objects import Currency, Price, Quantity

    usdt = Currency.from_str("USDT")
    required: dict[str, Any] = {
        "instrument_id": InstrumentId.from_str("BTCUSDT-PERP.SIM"),
        "raw_symbol": Symbol("BTCUSDT-PERP"),
        "base_currency": Currency.from_str("BTC"),
        "quote_currency": usdt,
        "settlement_currency": usdt,
        "is_inverse": False,
        "price_precision": 1,
        "size_precision": 0,
        "price_increment": Price.from_str("0.1"),
        "size_increment": Quantity.from_int(1),
        "ts_event": 0,
        "ts_init": 0,
    }
    return CryptoPerpetual(**{**required, **fields})


PERPETUAL_REQUIRED = (
    "instrument_id",
    "raw_symbol",
    "base_currency",
    "quote_currency",
    "settlement_currency",
    "is_inverse",
    "price_precision",
    "size_precision",
    "price_increment",
    "size_increment",
    "ts_event",
    "ts_init",
)
"""What `CryptoPerpetual` refuses to construct without, measured one omission at a time."""


def _check_instrument_classes() -> tuple[bool, str]:
    from nautilus_trader.model.enums import AssetClass, OptionKind
    from nautilus_trader.model.identifiers import InstrumentId, Symbol
    from nautilus_trader.model.instruments import (
        CryptoPerpetual,
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
        _sample_perpetual(multiplier=Quantity.from_str("0.01"), lot_size=Quantity.from_int(1)),
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
    share = built[0].multiplier
    bare: Any = _sample_perpetual()
    fields = _perpetual_fields(bare)
    unbuilt = {field: _without(CryptoPerpetual, fields, field) for field in PERPETUAL_REQUIRED}
    defaulted = (str(bare.multiplier), str(bare.lot_size))
    perpetual = CryptoPerpetual.to_dict(bare)
    holds = (
        len(built) == 6
        and missing_field is not None
        and share == Quantity.from_int(1)
        and all(unbuilt.values())
        and defaulted == ("1", "1")
        and "asset_class" not in perpetual
        and bare.asset_class == AssetClass.CRYPTOCURRENCY
        and bare.instrument_class.name == "SWAP"
    )
    return holds, (
        f"constructed {[type(i).__name__ for i in built]}; "
        f"Equity.multiplier = {share}; Equity without lot_size -> {missing_field}; "
        f"CryptoPerpetual refuses to construct without each of {sorted(unbuilt)} "
        f"({sum(1 for refused in unbuilt.values() if refused)} of {len(unbuilt)} refused); "
        f"built without multiplier and lot_size it carries {defaulted[0]} and {defaulted[1]}; "
        f"its asset class is {bare.asset_class.name}, its instrument class "
        f"{bare.instrument_class.name}, and to_dict carries "
        f"{'no' if 'asset_class' not in perpetual else 'an'} asset_class key"
    )


def _without(cls: Any, fields: dict[str, Any], omitted: str) -> str | None:
    """What constructing `cls` from `fields` less one of them raises, or `None`."""
    return _raises(lambda: cls(**{name: v for name, v in fields.items() if name != omitted}))


def _perpetual_fields(perpetual: Any) -> dict[str, Any]:
    """The required constructor fields of a built perpetual, read back off its attributes."""
    return {
        "instrument_id": perpetual.id,
        "raw_symbol": perpetual.raw_symbol,
        "base_currency": perpetual.base_currency,
        "quote_currency": perpetual.quote_currency,
        "settlement_currency": perpetual.settlement_currency,
        "is_inverse": perpetual.is_inverse,
        "price_precision": perpetual.price_precision,
        "size_precision": perpetual.size_precision,
        "price_increment": perpetual.price_increment,
        "size_increment": perpetual.size_increment,
        "ts_event": perpetual.ts_event,
        "ts_init": perpetual.ts_init,
    }


def _check_fee_model_charges_the_instrument_rates() -> tuple[bool, str]:
    """What a non-zero maker or taker rate on a definition would cost every fill."""
    from decimal import Decimal

    from nautilus_trader.backtest.models import MakerTakerFeeModel
    from nautilus_trader.common.component import TestClock
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.model.enums import LiquiditySide, OrderSide, OrderType
    from nautilus_trader.model.events import OrderAccepted, OrderFilled, OrderSubmitted
    from nautilus_trader.model.identifiers import (
        AccountId,
        StrategyId,
        TradeId,
        TraderId,
        VenueOrderId,
    )
    from nautilus_trader.model.objects import Money, Price, Quantity

    factory = OrderFactory(
        trader_id=TraderId("T-1"), strategy_id=StrategyId("S-1"), clock=TestClock()
    )
    account = AccountId("SIM-001")
    price, quantity = Price.from_str("60000.0"), Quantity.from_int(10)

    def charge(instrument: Any, side: LiquiditySide) -> Money:
        order = factory.market(instrument.id, OrderSide.BUY, quantity)
        ids = {
            "trader_id": order.trader_id,
            "strategy_id": order.strategy_id,
            "instrument_id": order.instrument_id,
            "client_order_id": order.client_order_id,
            "account_id": account,
            "event_id": UUID4(),
            "ts_event": 0,
            "ts_init": 0,
        }
        order.apply(OrderSubmitted(**ids))
        order.apply(OrderAccepted(venue_order_id=VenueOrderId("V-1"), **ids))
        order.apply(
            OrderFilled(
                **{**ids, "event_id": UUID4()},
                venue_order_id=VenueOrderId("V-1"),
                trade_id=TradeId("F-1"),
                position_id=None,
                order_side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                last_qty=quantity,
                last_px=price,
                currency=instrument.quote_currency,
                commission=Money(0, instrument.quote_currency),
                liquidity_side=side,
            )
        )
        charged: Money = MakerTakerFeeModel().get_commission(order, quantity, price, instrument)
        return charged

    contract = Quantity.from_str("0.01")
    taker = charge(
        _sample_perpetual(multiplier=contract, taker_fee=Decimal("0.0005")), LiquiditySide.TAKER
    )
    maker = charge(
        _sample_perpetual(multiplier=contract, maker_fee=Decimal("0.0002")), LiquiditySide.MAKER
    )
    free = charge(_sample_perpetual(multiplier=contract), LiquiditySide.TAKER)
    holds = (taker.as_decimal(), maker.as_decimal(), free.as_decimal()) == (
        Decimal(3),
        Decimal("1.2"),
        Decimal(0),
    )
    return holds, (
        f"ten contracts of multiplier 0.01 at 60,000 were charged {taker} at a taker rate of "
        f"0.0005, {maker} at a maker rate of 0.0002 and {free} at rates of zero: the fee "
        "model charges the instrument's own rate on the notional, so a kanso definition "
        "keeps both rates at zero"
    )


def _check_settlement_currency() -> tuple[bool, str]:
    """The two currencies `hyp validate` compares with a venue's account currency."""
    from nautilus_trader.model.objects import Currency

    usdc = _sample_perpetual(settlement_currency=Currency.from_str("USDC"))
    same = _sample_perpetual()
    equity: Any = _sample_equity()
    answered = tuple(
        (held.get_settlement_currency().code, held.get_cost_currency().code)
        for held in (usdc, same, equity)
    )
    holds = answered == (("USDC", "USDT"), ("USDT", "USDT"), ("USD", "USD")) and not (
        usdc.is_quanto
    )
    return holds, (
        f"a USDT-quoted perpetual settled in USDC settles in {answered[0][0]} and is booked "
        f"in {answered[0][1]} (quanto: {usdc.is_quanto}), one settled in USDT settles and is "
        f"booked in {answered[1][0]}/{answered[1][1]}, and a USD equity in "
        f"{answered[2][0]}/{answered[2][1]}, its quote currency"
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


_NOT_A_URL = "http://[::1"
"""What the quota check sends to: the client fails it once the quota admits it, before any
name is resolved or any socket opened, so the check reaches nothing."""


def _check_http_client_meters_named_keys() -> tuple[bool, str]:
    import asyncio
    import time
    from concurrent.futures import ThreadPoolExecutor

    from nautilus_trader.core import nautilus_pyo3

    client = nautilus_pyo3.HttpClient(default_quota=nautilus_pyo3.Quota.rate_per_second(10))

    def sent(keys: list[str] | None) -> str:
        async def once() -> str:
            try:
                await client.request(nautilus_pyo3.HttpMethod.GET, _NOT_A_URL, keys=keys)
            except Exception as exc:  # the only outcome: the string is no URL
                return type(exc).__name__
            return "answered"  # pragma: no cover - nothing can answer it

        return asyncio.run(once())

    def timed(keys: list[str] | None) -> tuple[float, set[str]]:
        start = time.monotonic()
        with ThreadPoolExecutor(3) as pool:
            outcomes = set(pool.map(lambda _: sent(keys), range(12)))
        return time.monotonic() - start, outcomes

    unnamed, _ = timed(None)
    named, outcomes = timed(["probe"])
    holds = named >= 0.15 and "answered" not in outcomes
    return holds, (
        f"twelve requests from three threads, each in its own event loop, on a default quota "
        f"of ten a second: naming one key they took {named:.2f}s (ten at once, then one each "
        f"100 ms), naming none {unnamed:.2f}s; each ended {sorted(outcomes)} at {_NOT_A_URL!r}"
    )


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
    handed: list[object] = []

    class _Probe(SimulationModule):  # type: ignore[misc]
        def pre_process(self, data: object) -> None:
            seen.append(exchange.best_bid_price(instrument.id))
            handed.append(data)

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
    bars = (
        _sample_bar(ts_event=1_000, ts_init=1_000, close=10.0),
        _sample_bar(ts_event=2_000, ts_init=2_500, close=100.0),
    )
    for bar in bars:
        exchange.process_bar(bar)
    # The other three of a module's four calls, so this check fails if any of them stops
    # being reachable: the clock tick the venue makes after it settles, and the two the
    # engine's own reset and diagnostics paths make.
    exchange.process(2_500)
    probe.log_diagnostics(None)
    probe.reset()
    marks = [None if price is None else float(price) for price in seen]  # type: ignore[arg-type]
    itself = len(handed) == len(bars) and all(a is b for a, b in zip(handed, bars, strict=True))
    stamps = [int(point.ts_event) for point in handed]  # type: ignore[attr-defined]
    holds = marks == [None, 10.0] and itself
    return holds, (
        f"a module's pre_process saw the venue's best bid at {marks} while processing bars "
        f"priced 10.00 then 100.00: the second call reached it before the matching engine "
        f"had moved the book, so a module acts on a point before the venue matches against it; "
        f"each call was handed the bar itself: {itself}, ts_event {stamps}, which is the "
        f"stamp `kanso.nautilus.splits` compares with a split's instant"
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
        "a catalog query handed its files returns the points of one instant in file order, "
        "whatever span it reads, with ts_init inclusive at both ends",
        _check_files_keep_an_instant_in_file_order,
    ),
    (
        "write_data files a series in the one directory class_to_filename and "
        "urisafe_identifier name, each file named by its first and last ts_init, and "
        "filter_files names its files whose interval meets a span, ends included or open",
        _check_a_series_is_filed_in_its_own_directory,
    ),
    (
        "delete_data_range with no identifier removes the files of a series of no instrument",
        _check_delete_with_no_identifier_removes_a_series_of_no_instrument,
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
        "a CustomData point pickles its payload by the module and name of the payload's class",
        _check_custom_data_pickles_its_payload_by_reference,
    ),
    (
        "the six instrument classes construct from the fields kanso must supply",
        _check_instrument_classes,
    ),
    (
        "MakerTakerFeeModel charges a fill the instrument's maker or taker rate on its notional",
        _check_fee_model_charges_the_instrument_rates,
    ),
    (
        "get_settlement_currency answers a perpetual's settlement currency and every other "
        "class's quote currency; get_cost_currency, which the account manager books and "
        "converts from, answers the quote currency of every class",
        _check_settlement_currency,
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
        "nautilus_pyo3.HttpClient holds a request that names a key to its default quota, and "
        "one key's quota is shared by every thread that sends under it",
        _check_http_client_meters_named_keys,
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
        "a simulation module is handed every market point, as loaded, before the venue "
        "matches against it",
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
        "in the match it triggers, a buyer's print never reaches a resting buy beneath it, "
        "where a seller's print or one with no aggressor does",
        _check_a_buyer_s_print_never_reaches_a_resting_buy,
    ),
    (
        "a level-one venue ignores a quote or a print whose ts_event is earlier than its book's "
        "last update, and applies one stamped at it",
        _check_a_level_one_venue_ignores_a_point_older_than_its_book,
    ),
    (
        "a level-one book that kanso's Availability module resets applies the next quote or "
        "print whatever its ts_event, and holds what that point alone sets",
        _check_a_reset_level_one_book_applies_the_next_point,
    ),
    (
        "a print at a resting limit's price fills it by its own size, so a larger clip fills "
        "in parts",
        _check_a_print_fills_a_resting_limit_by_its_own_size,
    ),
    (
        "on a level-one venue a quote beyond a resting limit's price, or a print beyond it from "
        "the side that can trade with it, fills all that is left of the order at its price, "
        "whatever its own size, when it is the best-priced order the point reaches",
        _check_a_point_beyond_a_level_one_limit_fills_it_whole,
    ),
    (
        "a point beyond two resting limits on a level-one venue fills all that is left of the "
        "better-priced and nothing of the other",
        _check_a_point_beyond_two_level_one_limits_fills_only_the_better,
    ),
    (
        "on a level-one venue a quote at the price two limits rest at, or a print at it under "
        "touch, fills each of them by the point's whole size",
        _check_a_point_at_two_level_one_limits_fills_each_by_its_size,
    ),
    (
        "a quote whose far side sits at a resting limit's price on a level-one venue fills it "
        "by the size shown, and again at every quote that shows it",
        _check_a_quote_at_a_level_one_limit_fills_it_again_at_every_quote,
    ),
    (
        "a print is both sides of a level-one book until the next quote, so a market order sent "
        "on it fills the print's size at its price and the rest one increment worse",
        _check_a_print_is_the_level_one_book_until_the_next_quote,
    ),
    (
        "liquidity_consumption on a level-one venue fills a resting limit from the first of two "
        "identical prints at its price and not from the second",
        _check_liquidity_consumption_withholds_a_repeated_print,
    ),
    (
        "a fill model's book replaces the engine's own for the fills of an order it has "
        "matched, and a zero-quantity fill in it ends the fill there, before a limit's "
        "remainder is filled whole",
        _check_a_zero_quantity_fill_ends_a_simulated_fill,
    ),
    (
        "the engine asks a fill model for a market order's fills and fills it from the book "
        "the model answers, walking one increment past it for what that book does not cover",
        _check_a_market_order_fills_from_the_fill_model_s_book,
    ),
    (
        "a lone zero-quantity fill refuses a market order for want of a market, and leaves a "
        "limit accepted on arrival resting at its price",
        _check_a_lone_zero_fill_refuses_a_market_order,
    ),
    (
        "the backtest engine re-matches every resting order after it drains a command, and "
        "asks a fill model with the arguments of the point's own match",
        _check_a_drain_re_matches_every_resting_order,
    ),
    (
        "a simulation module that calls its exchange's process from pre_process lands every "
        "command due by then before the matching engine applies the point",
        _check_a_module_lands_due_commands_before_the_point,
    ),
    (
        "a print inside the last quote leaves the engine's bid and ask no wider than that "
        "quote, re-matched or not, so a limit the quote makes marketable is matched on landing",
        _check_a_print_inside_the_quote_leaves_every_order_it_makes_marketable_a_taker,
    ),
    (
        "kanso's print_through venue fills a resting limit only from a later print strictly "
        "through its price, by that print's size or whole, never from a quote, and fills a "
        "taker at the last quote's touch",
        _check_kanso_s_print_through_venue,
    ),
    (
        "queue_position on a level-two book makes a joining limit wait for the size ahead",
        _check_queue_position_waits_for_the_size_ahead,
    ),
    (
        "a level-two book sets the size an add or an update names, ignores a delete of a "
        "price it does not hold, and empties both sides on a clear",
        _check_a_level_two_book_keeps_one_size_per_price,
    ),
    (
        "an OrderBookDeltas is applied whole by the simulated exchange and matched once",
        _check_a_book_batch_is_matched_once,
    ),
    (
        "the data engine publishes an OrderBookDeltas whole, after its book has applied it, "
        "and a lone OrderBookDelta as a batch of one",
        _check_a_book_batch_is_published_whole,
    ),
    (
        "a level-two venue refuses to run a name it holds data and no book data for, and "
        "counts only the first point of each validated add_data call",
        _check_the_book_check_reads_the_first_point_of_a_call,
    ),
    (
        "closing a position costs the same whatever was closed before it",
        _check_close_cost_is_flat,
    ),
    (
        "an order whose cancel was sent is not closed until the cancel lands, and under a "
        "latency the market can fill it first",
        _check_a_cancel_in_flight_leaves_the_order_to_fill,
    ),
    (
        "an order's events grow only at its end, and its strategy is handed each one as the "
        "order's last when the order takes it",
        _check_an_order_hands_its_strategy_each_event_as_its_last,
    ),
    (
        "close_position sends a reduce-only order, which the simulated venue trims to what "
        "is left of the position and refuses once the position is already closed",
        _check_a_close_is_reduce_only_and_the_venue_holds_it_to_the_position,
    ),
    (
        "cancel_all_orders marks an order open at the venue pending cancel, leaves one in "
        "flight as it is, and cancels both",
        _check_cancel_all_orders_takes_what_is_open_and_what_is_in_flight,
    ),
)


def claims() -> tuple[tuple[str, Callable[[], tuple[bool, str]]], ...]:
    """Every engine claim: the core's own, then each packaged broker's, in broker id order."""
    from kanso.nautilus import adapters

    return (*_CHECKS, *adapters.engine_facts())


def verify() -> list[Fact]:
    """Check every engine claim against the installed package.

    Runs offline and touches only a temporary directory. A check that raises is
    reported as a claim that does not hold, with the exception as its evidence,
    so one broken binding never hides the rest.
    """
    facts: list[Fact] = []
    for claim, check in claims():
        try:
            holds, evidence = check()
        except Exception as exc:
            facts.append(Fact(claim=claim, holds=False, evidence=f"{type(exc).__name__}: {exc}"))
            continue
        facts.append(Fact(claim=claim, holds=holds, evidence=evidence))
    return facts
