"""The simulated venue a node executes against, assembled from the engine's own components.

Every stage kanso ships runs its orders against a simulated exchange: an execution client
that owns a `SimulatedExchange` and drives it from the market data flowing past on the
message bus. It is the only execution client in 0.1.0, and it is what a replay uses whatever
a stage is configured with, because a replay feeds historical data and a broker fills against
current prices.

**kanso builds the exchange rather than taking the engine's convenience one.** The engine
ships a client that wires a `SimulatedExchange` and a `BacktestExecClient` together for a
live node, but it fixes the matching, the fee model and the fill model behind a configuration
that exposes none of them. So this module builds the same pair itself, with the arguments
`BacktestEngine.add_venue` builds the research path's exchange with — the same margin, fill
and fee models, the same book type, the same leverage and balances, read out of the one
`BacktestVenueConfig` both paths are configured from. Two things differ, both because this is
a node and not a backtest: the exchange keeps its own `TestClock`, since a node's kernel
clock is wall time and a fill has to be stamped from the data; and its command queue is off,
for the reason recorded below.

**The client's own subscription does not reach a bar, so kanso makes that binding.** The
client subscribes to `data.*.{venue}.*` when it connects. A quote is published to
`data.quotes.{venue}.{symbol}` and a trade to `data.trades.{venue}.{symbol}`, so both match;
a bar is published to `data.bars.{bar_type}` and a bar type begins with the instrument id,
which puts the venue inside the last segment (`data.bars.DEMO.XNAS-1-DAY-LAST-EXTERNAL`)
where the pattern needs a separator before it. Nothing matches, `on_data` is never called for
a bar, the exchange never sees a market and a bar-resolution hypothesis — which is most of
them — fills nothing at all. So this module subscribes each client's own `on_data` to the bar
topics of its own venue, read off the points the session actually holds rather than off a
hypothesis, and at a priority above a strategy's, which is also the order the backtest loop
uses: the exchange is given the point, then the strategy is.

**The exchange fills an oversized order the way the research path does, and that took a
relay.** On an L1 book a market order larger than the top level fills what is there and then,
if it is *still open*, slips one price increment and fills the remainder — both inside the
one call that matched it. "Still open" is read off the order object, and an order only
becomes partially filled once its first fill event has been applied to it. A backtest's
execution engine applies that event in the same call; a live node's queues it, so the order
is still merely submitted when the check is made and the remainder is never filled, never
cancelled and never reported. Measured on the demo before this was repaired: 976 wanted, 739
filled in the node against 739 and then 237 in a backtest. So kanso gives its exchange and
its `BacktestExecClient` their own message bus, which forwards every endpoint to the node's
bus unchanged except the execution engine's event endpoint, which it delivers straight to the
synchronous implementation the live engine overrides in order to enqueue. Nothing else in the
node changes: every other component, and any broker client beside this one, still reaches the
live engine's queue.

**Turning the exchange's own command queue on is not the repair, and would be a second bug.**
It was measured: with the queue on, a command waits for the next `process`, so the order is
matched against the *next* point's book — it fills a bar late, at a price the strategy never
saw, and the missing second fill still never arrives. The queue therefore stays off, which is
also what makes flattening a node possible at all: an order submitted after the last point of
a window is matched against that point's book rather than against a point that never comes.

**The exchange is cost-neutral because of the instrument, not because of the venue.** Both
paths run a `MakerTakerFeeModel`, because `SimulatedExchange` will not accept no fee model
and `BacktestEngine.add_venue` substitutes that one for the `None` kanso hands it. What makes
the venue charge nothing is that every instrument kanso resolves carries a zero maker and
taker fee, because commission, slippage and the spread are applied once, in the runner's
extraction. One application means one number across a card, a certificate, a replay and a
stage — and it holds here only for as long as that zero-fee invariant does. The two paths
cannot drift apart over it, since they are handed the same model.

Engine facts this module relies on (nautilus_trader 1.231.0):

* `BacktestEngine.add_venue` builds a `SimulatedExchange` and a `BacktestExecClient` over the
  kernel's portfolio, message bus, cache and clock, substituting `LeveragedMarginModel()`,
  `FillModel()` and `MakerTakerFeeModel()` for the `None`s it is given, leaving
  `latency_model` unset, and taking `use_message_queue=True` and `BookType.L1_MBP`. It then
  registers the client with the exchange and with the execution engine.
* `SimulatedExchange.send` matches a command in the same call when `use_message_queue` is
  false, and otherwise holds it until the next `process(ts_now)`; `process` sets the
  exchange's `TestClock` to that instant, so the venue's clock follows the data's `ts_init`
  rather than wall time and a fill is stamped with the last point's availability instant.
* `SimulatedExchange` uses its message bus for exactly two things: sending every execution
  event to the `ExecEngine.process` endpoint, and registering one endpoint for spread quotes.
  `ExecutionClient` sends its account state to `Portfolio.update_account` and its order
  events to `ExecEngine.process`. Neither publishes to a topic, so a bus that carries the
  endpoints carries everything they do.
* `MessageBus.send` to an endpoint nothing is registered at logs an error and drops the
  message, so the relay registers a forwarder for every endpoint the node's bus holds rather
  than for the two that are used today.
* `ExecutionEngine.process(event)` applies an order event immediately;
  `LiveExecutionEngine.process` overrides it to put the event on a queue consumed by its own
  task.
* A live execution client subscribes `on_data` to `data.*.{venue}.*` when it connects;
  `on_data` feeds `Instrument`, `InstrumentStatus`, `InstrumentClose`, the three order-book
  types, `QuoteTick`, `TradeTick` and `Bar` into the exchange and then advances it to the
  point's `ts_init`. Subscribing the same handler to a second, disjoint pattern therefore
  adds points to the exchange without ever delivering one twice.
* `ExecutionEngine.register_client` and `register_venue_routing` are the whole of what a
  client needs to receive a venue's commands, so a client constructed here and registered
  before the node starts is indistinguishable from one a factory built.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from functools import partial
from typing import Any, Final

from nautilus_trader.accounting.margin_models import LeveragedMarginModel
from nautilus_trader.backtest.engine import SimulatedExchange
from nautilus_trader.backtest.execution_client import BacktestExecClient
from nautilus_trader.backtest.models import FillModel, MakerTakerFeeModel
from nautilus_trader.backtest.node import (
    get_account_type,
    get_base_currency,
    get_oms_type,
    get_starting_balances,
)
from nautilus_trader.common.component import MessageBus, TestClock
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import BacktestVenueConfig
from nautilus_trader.core.data import Data
from nautilus_trader.execution.engine import ExecutionEngine
from nautilus_trader.execution.messages import (
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
)
from nautilus_trader.execution.reports import FillReport, OrderStatusReport, PositionStatusReport
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.data import (
    Bar,
    InstrumentClose,
    InstrumentStatus,
    OrderBookDelta,
    OrderBookDeltas,
    OrderBookDepth10,
    QuoteTick,
    TradeTick,
)
from nautilus_trader.model.identifiers import AccountId, ClientId, Venue
from nautilus_trader.model.instruments import Instrument

__all__ = [
    "ACCOUNT_SUFFIX",
    "BAR_TOPIC",
    "EXEC_EVENTS",
    "MARKET_FIRST",
    "ROUTES",
    "SimulatedVenue",
    "attach",
    "bar_topics",
    "relay",
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

EXEC_EVENTS: Final = "ExecEngine.process"
"""Where every execution event a simulated venue generates is addressed.

The one endpoint the relay does not forward: a live engine's own handler queues the event,
and an order that is filled in two parts needs the first part applied before the second is
decided.
"""

ACCOUNT_SUFFIX: Final = "-001"
"""How a venue's simulated account is numbered; one account per venue, stable across runs."""

ROUTES: Final[tuple[tuple[type, str], ...]] = (
    (Bar, "process_bar"),
    (QuoteTick, "process_quote_tick"),
    (TradeTick, "process_trade_tick"),
    (Instrument, "update_instrument"),
    (InstrumentStatus, "process_instrument_status"),
    (InstrumentClose, "process_instrument_close"),
    (OrderBookDelta, "process_order_book_delta"),
    (OrderBookDeltas, "process_order_book_deltas"),
    (OrderBookDepth10, "process_order_book_depth10"),
)
"""Every kind of point an exchange can be moved by, and the call that moves it.

Read in order, so the grains kanso actually loads are matched first. A point of a kind no
entry names still advances the exchange's clock, which is what a custom data type published
into a venue's topic space must not silently stop.
"""


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


def relay(kernel: Any) -> MessageBus:
    """The message bus kanso's own exchange and execution client send on.

    Every endpoint the node's bus holds is forwarded to it unchanged, so an account state,
    a portfolio update or anything a later engine version adds behaves exactly as it would
    have. The one exception is the execution engine's event endpoint, which is handed to the
    engine's synchronous implementation instead of its queueing override: a fill has to be
    applied to its order before the venue decides whether the rest of the order still stands.
    """
    bus = MessageBus(trader_id=kernel.msgbus.trader_id, clock=kernel.clock)
    for endpoint in kernel.msgbus.endpoints():
        if endpoint != EXEC_EVENTS:
            bus.register(endpoint=endpoint, handler=partial(kernel.msgbus.send, endpoint))
    bus.register(endpoint=EXEC_EVENTS, handler=partial(ExecutionEngine.process, kernel.exec_engine))
    return bus


class SimulatedVenue(LiveExecutionClient):
    """One venue simulated inside a live node, configured as the research path's venue is.

    The exchange and its execution client are built from the same `BacktestVenueConfig` the
    backtest path builds its venue from, through the engine's own converters, so nothing that
    touches a fill can differ between the two: account type, currency, leverage, starting
    balance, bar execution, and the margin, fill and fee models. The leverage travels through
    its own string, so a decimal written as `2.5` stays that number rather than the binary
    float nearest it.
    """

    def __init__(self, kernel: Any, venue: BacktestVenueConfig) -> None:
        identifier = Venue(venue.name)
        account_type = get_account_type(venue)
        base_currency = get_base_currency(venue)
        oms_type = get_oms_type(venue)
        self.test_clock = TestClock()
        super().__init__(
            loop=kernel.loop,
            client_id=ClientId(venue.name),
            venue=identifier,
            oms_type=oms_type,
            account_type=account_type,
            base_currency=base_currency,
            instrument_provider=InstrumentProvider(),
            msgbus=kernel.msgbus,
            cache=kernel.cache,
            clock=kernel.clock,
            config=None,
        )
        self._set_account_id(AccountId(f"{venue.name}{ACCOUNT_SUFFIX}"))
        self.relay = relay(kernel)
        self.exchange = SimulatedExchange(
            venue=identifier,
            oms_type=oms_type,
            account_type=account_type,
            starting_balances=get_starting_balances(venue),
            base_currency=base_currency,
            # Through a string, so a leverage is the decimal it was written as rather than
            # the binary float that happens to be nearest to it.
            default_leverage=Decimal(str(venue.default_leverage)),
            leverages={},
            margin_model=LeveragedMarginModel(),
            modules=[],
            portfolio=kernel.portfolio,
            msgbus=self.relay,
            cache=kernel.cache,
            clock=self.test_clock,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            bar_execution=venue.bar_execution,
            # Matched in the call that submits it, which is where the research path's
            # settle step matches it and what lets a node flatten after its last point.
            use_message_queue=False,
        )
        self._client = BacktestExecClient(
            exchange=self.exchange,
            msgbus=self.relay,
            cache=kernel.cache,
            clock=self.test_clock,
        )
        self.exchange.register_client(self._client)
        self.exchange.initialize_account()

    # --- the node's half of a client -----------------------------------------

    def connect(self) -> None:
        """Take this venue's market and its instruments, and report the client up."""
        self._msgbus.subscribe(f"data.*.{self.venue}.*", handler=self.on_data)
        for instrument in self.exchange.cache.instruments(venue=self.venue):
            self.exchange.add_instrument(instrument)
        self._client._set_connected(True)
        self._set_connected(True)

    def disconnect(self) -> None:
        """Stop reporting up; the exchange is a simulation and has nothing to close."""
        self._set_connected(False)

    # --- what a simulated venue cannot report --------------------------------

    async def generate_order_status_report(
        self, command: GenerateOrderStatusReport
    ) -> OrderStatusReport | None:
        """Nothing: a simulated venue's orders are the node's own, already in its cache."""
        return None

    async def generate_order_status_reports(
        self, command: GenerateOrderStatusReports
    ) -> list[OrderStatusReport]:
        """Nothing, for the same reason; reconciliation is off on every node kanso runs."""
        return []

    async def generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]:
        """Nothing: every fill this venue made was reported as it happened."""
        return []

    async def generate_position_status_reports(
        self, command: GeneratePositionStatusReports
    ) -> list[PositionStatusReport]:
        """Nothing: a simulated account holds no position across a restart to reconcile."""
        return []

    # --- the market, and the orders it matches -------------------------------

    def submit_order(self, command: Any) -> None:
        """Send the order to the exchange, which matches it in this call."""
        self._client.submit_order(command)

    def submit_order_list(self, command: Any) -> None:
        """Send the list to the exchange, which matches each order in this call."""
        self._client.submit_order_list(command)

    def modify_order(self, command: Any) -> None:
        """Amend a resting order on the exchange."""
        self._client.modify_order(command)

    def cancel_order(self, command: Any) -> None:
        """Cancel a resting order on the exchange."""
        self._client.cancel_order(command)

    def cancel_all_orders(self, command: Any) -> None:
        """Cancel every resting order of one instrument on the exchange."""
        self._client.cancel_all_orders(command)

    def on_data(self, data: Data) -> None:
        """Move the market with this point, then advance the exchange to its instant."""
        for kind, into in ROUTES:
            if isinstance(data, kind):
                getattr(self.exchange, into)(data)
                break
        self.exchange.process(data.ts_init)


def attach(
    kernel: Any,
    venues: Sequence[BacktestVenueConfig],
    points: Sequence[Any],
) -> tuple[SimulatedVenue, ...]:
    """One simulated venue per configured venue, registered and bound to this window's bars.

    Returns the clients in the order the venues were given, so a caller can reach an
    exchange afterwards — to read the book a position was marked at, or to check that a
    flattening order was matched.
    """
    made: list[SimulatedVenue] = []
    topics = bar_topics(points)
    for venue in venues:
        client = SimulatedVenue(kernel, venue)
        kernel.exec_engine.register_client(client)
        kernel.exec_engine.register_venue_routing(client, Venue(venue.name))
        for topic in topics.get(venue.name, ()):
            kernel.msgbus.subscribe(topic=topic, handler=client.on_data, priority=MARKET_FIRST)
        made.append(client)
    return tuple(made)
