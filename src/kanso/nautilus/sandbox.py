"""The simulated venue a node executes against, and the one binding the engine does not make.

Every stage kanso ships runs its orders against `SandboxExecutionClient`: a live execution
client that owns a `SimulatedExchange` and drives it from the market data flowing past on
the message bus. It is the only execution client in 0.1.0, and it is what a replay uses
whatever a stage is configured with, because a replay feeds historical data and a broker
fills against current prices.

**The client's own subscription does not reach a bar, so kanso makes that binding.** The
client subscribes to `data.*.{venue}.*` when it connects. A quote is published to
`data.quotes.{venue}.{symbol}` and a trade to `data.trades.{venue}.{symbol}`, so both
match; a bar is published to `data.bars.{bar_type}` and a bar type begins with the
instrument id, which puts the venue inside the last segment
(`data.bars.DEMO.XNAS-1-DAY-LAST-EXTERNAL`) where the pattern needs a separator before it.
Nothing matches, `on_data` is never called for a bar, the exchange never sees a market and
a bar-resolution hypothesis — which is most of them — fills nothing at all. So this module
subscribes each client's own `on_data` to the bar topics of its own venue, read off the
points the session actually holds rather than off a hypothesis, and at a priority above a
strategy's, which is also the order the backtest loop uses: the exchange is given the point,
then the strategy is.

**The exchange is cost-neutral, but not by configuration.** `SandboxExecutionClient` builds
its exchange with a default `FillModel`, a `MakerTakerFeeModel` and a zero latency model, and
`SandboxExecutionClientConfig` exposes none of the three, so kanso cannot ask for the
`fee_model=None` its backtest venue is given. What makes the two agree is the instrument
rather than the venue: every instrument kanso resolves carries a zero maker and taker fee,
because commission, slippage and the spread are applied once, in the runner's extraction. One
application means one number across a card, a certificate, a replay and a stage — and it
holds here only for as long as that zero-fee invariant does.

**The two paths do not fill an order larger than one book state identically.** A backtest
matches a queued command inside `process_bar`, where a bar is replayed as its synthesised
tick sequence, so a market order that exhausts one level walks the sequence and completes at
the next price. This exchange is built with `use_message_queue=False`, so an order is matched
in the call that submits it, against the single book state the bar left behind. What does not
fit there is never filled and never reported: no second `OrderFilled` and no cancellation ever
arrives for it, and a further `process` does not recover it. Below
that depth — where every order kanso has measured sits — the two paths agree to the
nanosecond. Above it they diverge, which is a difference `parity_replay` reports rather than
hides.

Engine facts this module relies on (nautilus_trader 1.231.0):

* `SandboxExecutionClient(loop, portfolio, msgbus, cache, clock, config)` is a
  `LiveExecutionClient` built from `SandboxExecutionClientConfig` (`venue`,
  `starting_balances`, `base_currency`, `oms_type`, `account_type`, `default_leverage`,
  `book_type`, `bar_execution`, `trade_execution` and the order-support switches). It owns a
  `SimulatedExchange` on a `TestClock`, so the venue's clock follows the data's `ts_init`
  rather than wall time.
* `connect()` subscribes `on_data` to `data.*.{venue}.*`; `on_data` feeds `Instrument`,
  `InstrumentStatus`, `InstrumentClose`, the three order-book types, `QuoteTick`,
  `TradeTick` and `Bar` into the exchange and then calls `exchange.process(data.ts_init)`.
  Subscribing the same handler to a second, disjoint pattern therefore adds points to the
  exchange without ever delivering one twice.
* The exchange is built with `use_message_queue=False` and a zero latency model, so a
  command sent to it is matched in the same call rather than on the next `process`. A
  market order submitted after the last point of a window is filled against that point's
  book, which is what makes flattening a node possible at all.
* `ExecutionEngine.register_client` and `register_venue_routing` are the whole of what a
  client needs to receive a venue's commands, so a client constructed here and registered
  before the node starts is indistinguishable from one a factory built.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Final

from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.config import BacktestVenueConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import Venue

__all__ = [
    "BAR_TOPIC",
    "MARKET_FIRST",
    "attach",
    "bar_topics",
    "client_config",
]

MARKET_FIRST: Final = 10
"""The message-bus priority the simulated venue reads a bar at.

Above a strategy's, so the exchange holds the point before the strategy acts on it. That is
the order the backtest loop feeds them in, and the order the client's own quote and trade
subscription already has, since it is made when the node connects its clients and a strategy
subscribes only once the trader starts it.
"""

BAR_TOPIC: Final = "data.bars."
"""The prefix the data engine publishes every bar under, followed by the bar type."""


def bar_topics(points: Sequence[Any]) -> dict[str, tuple[str, ...]]:
    """The bar topics each venue's exchange must be subscribed to, by venue.

    Read off the points a session holds rather than off a hypothesis's declared resolution,
    so whatever grain the window actually carries reaches the exchange that has to match
    against it.
    """
    found: dict[str, set[str]] = {}
    for point in points:
        if isinstance(point, Bar):
            venue = point.bar_type.instrument_id.venue.value
            found.setdefault(venue, set()).add(f"{BAR_TOPIC}{point.bar_type}")
    return {venue: tuple(sorted(topics)) for venue, topics in sorted(found.items())}


def client_config(venue: BacktestVenueConfig) -> SandboxExecutionClientConfig:
    """The sandbox configuration of one venue, from the venue the backtest path builds.

    The two paths are configured from one object so that nothing that touches a fill can
    differ between them: account type, currency, leverage, starting balance and bar
    execution are the same values in both. The leverage travels through its own string,
    so a decimal written as `2.5` stays that number rather than the binary float nearest it.
    """
    return SandboxExecutionClientConfig(
        venue=venue.name,
        starting_balances=list(venue.starting_balances),
        base_currency=venue.base_currency,
        oms_type=venue.oms_type,
        account_type=venue.account_type,
        default_leverage=Decimal(str(venue.default_leverage)),
        bar_execution=venue.bar_execution,
    )


def attach(
    kernel: Any,
    venues: Sequence[BacktestVenueConfig],
    points: Sequence[Any],
) -> tuple[SandboxExecutionClient, ...]:
    """One simulated venue per configured venue, registered and bound to this window's bars.

    Returns the clients in the order the venues were given, so a caller can reach an
    exchange afterwards — to read the book a position was marked at, or to check that a
    flattening order was matched.
    """
    made: list[SandboxExecutionClient] = []
    topics = bar_topics(points)
    for venue in venues:
        client = SandboxExecutionClient(
            loop=kernel.loop,
            portfolio=kernel.portfolio,
            msgbus=kernel.msgbus,
            cache=kernel.cache,
            clock=kernel.clock,
            config=client_config(venue),
        )
        kernel.exec_engine.register_client(client)
        kernel.exec_engine.register_venue_routing(client, Venue(venue.name))
        for topic in topics.get(venue.name, ()):
            kernel.msgbus.subscribe(topic=topic, handler=client.on_data, priority=MARKET_FIRST)
        made.append(client)
    return tuple(made)
