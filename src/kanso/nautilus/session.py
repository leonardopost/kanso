"""A replay session on the live code path: a trading node, the replay feed, a simulated venue.

This is the other half of the parity claim. `kanso.nautilus.backtest` runs a strategy in a
backtest engine — the research code path, the one every card and every certificate is
measured on. This module runs the same strategy bytes, over the same points, inside a
`TradingNode`: the object a deployed stage is, with its asynchronous engines, its live clock
and its message bus. If the two produce the same order intents then what was researched is
what is deployed, and if they do not, the deployment is not the experiment.

**Replay always executes against a simulated venue.** Not the stage's broker, whatever the
stage is configured with: a replay feeds historical data while a broker's paper account
matches against today's prices, so the pairing would fill orders at prices unrelated to the
data that triggered them. The simulated exchange here is configured from the same resolved
venue model the backtest venue is built from — account type, currency, leverage, starting
balance and bar execution — so the two exchanges differ in nothing that touches a fill.

**Nothing is flattened at the end.** A stage node flattens before it stops, because a
simulated account keeps no position across a restart; a replay session must not, because the
backtest it is compared with does not, and an exit nobody's strategy asked for is a
divergence.

**Costs are applied by the runner's extraction, here as everywhere.** The measured run this
session produces is built by the same extraction the backtest path uses, reading the same
positions and fills out of the cache, so a replayed window and a backtested window are
costed by one piece of arithmetic rather than two.

Engine facts this module relies on (nautilus_trader 1.231.0):

* `TradingNode(config, loop)` builds a kernel whose `data_engine`, `risk_engine`,
  `exec_engine`, `msgbus`, `cache`, `portfolio` and `trader` are public; `build()` builds
  the clients its configuration names, and `register_client`, `register_default_client` and
  `register_venue_routing` on the engines are the same registration path, so a client
  constructed directly and registered before the node starts is indistinguishable from one
  a factory built. `run_async()` gathers the engines' queue tasks and returns only when they
  end, so it is driven as a task beside the feed rather than awaited.
* `SandboxExecutionClient` owns a `SimulatedExchange` on a `TestClock` and is configured by
  `SandboxExecutionClientConfig`, whose defaults match `BacktestEngine.add_venue`'s. It
  drives the exchange from `on_data`, which it subscribes at `connect()` to
  `data.*.{venue}.*`.
* **That subscription does not match a bar.** The data engine publishes a bar to
  `data.bars.{bar_type}`, and a bar type begins with the instrument id, so the venue lands
  inside the last segment (`data.bars.DEMO.XNAS-1-DAY-LAST-EXTERNAL`) where the pattern
  needs a separator. Quotes and trades match (`data.quotes.{venue}.{symbol}`) and bars never
  do, so on a bar-only universe the sandbox exchange sees no market at all and fills
  nothing. This module therefore subscribes each sandbox client's own `on_data` to the bar
  topics of its venue, at a priority above the strategies', which is also the order the
  backtest loop uses: the exchange sees a point, then the strategy does.
* A live engine kills the process outright on an unhandled exception in queue processing
  unless `graceful_shutdown_on_exception` is set, so every engine here sets it and a
  strategy that raises stops the node instead of the interpreter.
* `LiveRiskEngineConfig.max_order_submit_rate` throttles submissions against the engine's
  clock. In a backtest that clock advances with the data, so the default never binds; in a
  node it is wall time, and a replay compresses years of decisions into seconds. The rate is
  therefore lifted here, so the same strategy is throttled the same amount — none — on both
  paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import chain
from typing import Any, Final

from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.common import Environment
from nautilus_trader.config import (
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import TraderId, Venue

from kanso.errors import PreconditionError
from kanso.nautilus import backtest
from kanso.nautilus.backtest import RunRequest, RunResult
from kanso.nautilus.replay_client import SETTLE_TURNS, ReplayDataClient
from kanso.nautilus.venue import venue_configs

__all__ = [
    "MARKET_FIRST",
    "Replayed",
    "SHUTDOWN_TOPIC",
    "STOPPED",
    "TRADER_ID",
    "Halt",
    "bar_topics",
    "ordered",
    "run_node",
]

TRADER_ID: Final = "KANSO-001"
"""Every replay session runs under one trader id; a session is identified by its own id."""

STOPPED: Final = "stopped"
"""The reason a replay carries when the node stopped before the feed was finished."""

SHUTDOWN_TOPIC: Final = "commands.system.shutdown"
"""Where a component asks the kernel to shut the system down.

A live engine that catches an exception in one of its queues publishes here instead of
raising, so a strategy that fails in a node fails quietly where the same strategy in a
backtest fails the run. Listening on this topic is how a replay learns of it, and the reason
it carries is the one the engine gave.
"""

MARKET_FIRST: Final = 10
"""The message-bus priority the simulated venue reads a bar at.

Above a strategy's, so the exchange has the point before the strategy acts on it — which is
the order the backtest loop feeds them in, and the order the sandbox client's own quote and
trade subscription already has, since it is registered when the node connects its clients
and a strategy subscribes only when the trader starts it.
"""

START_TURNS: Final = 100_000
"""How many turns of the loop a node is given to come up before the session gives up."""

SUBMIT_RATE: Final = "1000000/00:00:01"
"""An order submission rate a replay cannot reach, so no wall-clock throttle binds."""

POST_STOP_S: Final = 0.1
CONNECT_S: Final = 10.0
DISCONNECT_S: Final = 5.0


@dataclass(frozen=True)
class Replayed:
    """What one node session produced, and how far through its window it got.

    `released` and `clock_ns` are how far the feed actually reached, which is the window's
    end for a session that finished and wherever it stopped for one that did not. A stage
    resumes from `clock_ns`, so it has to be the last point the node handled rather than the
    last point the window held.
    """

    result: RunResult
    released: int
    clock_ns: int | None

    @property
    def intents(self) -> tuple[tuple[int, str, str, float, str, float | None], ...]:
        """The order intents this session's strategy submitted."""
        return self.result.intents


@dataclass
class Halt:
    """Whether the node asked to shut down, and what it said when it did."""

    reason: str | None = None

    def record(self, command: Any) -> None:
        """Take the shutdown command the kernel is about to act on."""
        self.reason = str(getattr(command, "reason", "") or "") or STOPPED

    def running(self) -> bool:
        """Whether the feed may keep releasing points."""
        return self.reason is None


def ordered(groups: Sequence[Sequence[Any]]) -> tuple[Any, ...]:
    """Every point of every group in the order an engine would deliver them.

    The engine sorts its accumulated stream by `ts_init` with a stable sort, so points
    sharing an instant keep the order their groups were added in. Sorting the concatenation
    the same way reproduces that exactly, which is what makes the two code paths see one
    stream rather than two.
    """
    return tuple(sorted(chain.from_iterable(groups), key=lambda point: int(point.ts_init)))


def bar_topics(points: Sequence[Any]) -> dict[str, tuple[str, ...]]:
    """The bar topics each venue's simulated exchange must be subscribed to, by venue.

    Read off the points rather than off the hypothesis, so whatever grain the window
    actually holds reaches the exchange that has to match against it.
    """
    found: dict[str, set[str]] = {}
    for point in points:
        if isinstance(point, Bar):
            venue = point.bar_type.instrument_id.venue.value
            found.setdefault(venue, set()).add(f"data.bars.{point.bar_type}")
    return {venue: tuple(sorted(topics)) for venue, topics in sorted(found.items())}


def run_node(
    request: RunRequest,
    instruments: Sequence[Any],
    groups: Sequence[Sequence[Any]],
    *,
    speed: float = 0.0,
    settle_turns: int = SETTLE_TURNS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Replayed:
    """Run this request on the live code path and extract it exactly as a backtest is.

    The data is checked against the requested window before anything is built, so a session
    handed points from outside its range refuses them rather than trading on them.
    """
    stream = backtest._stream(request, groups)
    points = ordered(groups)
    backtest._seed_globals(request.snapshot_id)
    started = time.perf_counter()
    loop = asyncio.new_event_loop()
    node = TradingNode(config=_config(), loop=loop)
    try:
        node.build()
        kernel = node.kernel
        halt = Halt()
        kernel.msgbus.subscribe(topic=SHUTDOWN_TOPIC, handler=halt.record)
        for instrument in instruments:
            kernel.cache.add_instrument(instrument)
        client = ReplayDataClient(
            loop,
            kernel.msgbus,
            kernel.cache,
            kernel.clock,
            points=points,
            speed=speed,
            settle_turns=settle_turns,
            sleep=sleep,
            alive=halt.running,
        )
        kernel.data_engine.register_client(client)
        kernel.data_engine.register_default_client(client)
        client.attach(kernel.data_engine, kernel.risk_engine, kernel.exec_engine)
        _venues(request, kernel, points)
        strategy = _strategy(request, node)
        loop.run_until_complete(_drive(node, client, strategy, halt))
        card = backtest._extract(request, kernel, stream, groups)
        intents = tuple(
            (i.ts_event, i.instrument_id, i.side, i.qty, i.order_type, i.price)
            for i in strategy.intents
        )
        stopped = halt.reason
        released = client.released
        clock_ns = client.last_ts if released else None
    finally:
        node.dispose()
    wall_s = time.perf_counter() - started
    if stopped is not None:
        crashed = backtest._crashed(
            request, wall_s, backtest._own_peak_gb(), backtest.EXCEPTION, stopped
        )
        return Replayed(crashed, released, clock_ns)
    return Replayed(
        RunResult(
            run=card,
            wall_s=wall_s,
            peak_mem_gb=backtest._own_peak_gb(),
            intents=intents,
        ),
        released,
        clock_ns,
    )


def _config() -> TradingNodeConfig:
    """The node a replay runs in: sandboxed, silent, and with no background reconciliation."""
    return TradingNodeConfig(
        environment=Environment.SANDBOX,
        trader_id=TraderId(TRADER_ID),
        logging=LoggingConfig(bypass_logging=True),
        data_engine=LiveDataEngineConfig(graceful_shutdown_on_exception=True),
        risk_engine=LiveRiskEngineConfig(
            graceful_shutdown_on_exception=True,
            max_order_submit_rate=SUBMIT_RATE,
            max_order_modify_rate=SUBMIT_RATE,
        ),
        exec_engine=LiveExecEngineConfig(
            graceful_shutdown_on_exception=True,
            reconciliation=False,
            inflight_check_interval_ms=0,
        ),
        data_clients={},
        exec_clients={},
        timeout_connection=CONNECT_S,
        timeout_disconnection=DISCONNECT_S,
        timeout_post_stop=POST_STOP_S,
    )


def _venues(request: RunRequest, kernel: Any, points: Sequence[Any]) -> None:
    """One simulated venue per venue the universe trades, wired to this session's bars."""
    topics = bar_topics(points)
    for venue in venue_configs(request.hyp, request.venue_model, request.capital):
        client = SandboxExecutionClient(
            loop=kernel.loop,
            portfolio=kernel.portfolio,
            msgbus=kernel.msgbus,
            cache=kernel.cache,
            clock=kernel.clock,
            config=SandboxExecutionClientConfig(
                venue=venue.name,
                starting_balances=list(venue.starting_balances),
                base_currency=venue.base_currency,
                oms_type=venue.oms_type,
                account_type=venue.account_type,
                # Through a string, so a leverage is the decimal it was written as rather
                # than the binary float that happens to be nearest to it.
                default_leverage=Decimal(str(venue.default_leverage)),
                bar_execution=venue.bar_execution,
            ),
        )
        kernel.exec_engine.register_client(client)
        kernel.exec_engine.register_venue_routing(client, Venue(venue.name))
        for topic in topics.get(venue.name, ()):
            kernel.msgbus.subscribe(topic=topic, handler=client.on_data, priority=MARKET_FIRST)


def _strategy(request: RunRequest, node: TradingNode) -> Any:
    """The sleeve and its attached constructs, loaded from the same bytes a card runs."""
    cls, config = backtest._sleeve(request)
    for construct, source, params in request.modifiers:
        node.trader.add_actor(
            backtest._modifier(construct, source, params, request.hyp.id, cls.__name__)
        )
    strategy = cls(config=config)
    node.trader.add_strategy(strategy)
    return strategy


async def _drive(node: TradingNode, client: ReplayDataClient, strategy: Any, halt: Halt) -> None:
    """Start the node, release the window into it, and stop it again.

    A node that asked to shut down is stopping already; the session lets that finish before
    it stops the node itself, so the two stops do not run at once and leave the kernel
    half-stopped when the loop closes.
    """
    runner = asyncio.create_task(node.run_async())
    await _started(node, client, strategy)
    await client.settle()
    await client.replay()
    if halt.reason is not None:
        await _halted(node)
    await node.stop_async()
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner
    await _cleared()


async def _cleared() -> None:
    """Cancel whatever the node left running, so the loop closes with nothing pending.

    A kernel gives every actor and strategy an executor whose worker is an asyncio task of
    its own; those workers outlive the stop, and a loop closed underneath them reports each
    one as a task destroyed while pending. Cancelling them here is the difference between a
    clean session and a page of warnings after every replay.
    """
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _halted(node: TradingNode) -> None:
    """Yield until the kernel's own shutdown has run its course."""
    for _ in range(START_TURNS):
        if not node.kernel.is_running():
            return
        await asyncio.sleep(0)


async def _started(node: TradingNode, client: ReplayDataClient, strategy: Any) -> None:
    """Yield until the node is running, the feed is connected and the sleeve is started."""
    for _ in range(START_TURNS):
        await asyncio.sleep(0)
        if node.kernel.is_running() and client.is_connected and strategy.is_running:
            return
    raise PreconditionError(  # pragma: no cover - a node that never starts on this host
        "replay: the trading node did not start",
        remedy="run `kanso doctor` to check the engine installation",
    )
