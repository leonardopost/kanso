"""The stage node: several deployed versions in one trading node, and what it realises.

A stage is a node. It holds every version deployed to it, each configured with its own slice
of the stage capital, all sharing one message bus, one cache, one portfolio and one simulated
venue per venue they trade. That sharing is the point: it is how two strategies net against
each other in one account, and it is why a per-strategy or a stage-wide exposure limit cannot
be a setting on the engine's risk configuration, which caps one order on one instrument and
nothing else.

**What runs is the generated implementation, loaded from `impl/<version>/`.** Not the blobs
it was copied from and not the workspace's `strategy.py`: the directory is the one thing a
backtest, a replay and a stage all resolve their classes out of, so the thing that was
certified and the thing that trades cannot be two programs.

**The deployed capital is injected here.** Composition wrote the implementation's manifest
with the capital the version was measured at; deployment assigns a share of the stage's
capital, and a strategy sizes against the number it is given. So the sleeve's configuration
is rebuilt with the deployed figure before the component is constructed, and every attached
construct is re-pointed at the sleeve's final `StrategyId` rather than at its class name —
without which two versions of the same generated class in one node would answer each other's
modifiers.

**A node flattens before it stops.** It cancels every open order and closes every open
position, then stops. Simulated execution keeps no position across a restart, so a stage that
stopped holding a book would silently lose it and reopen flat with its record still claiming
the position; flattening makes the loss explicit and realises the P&L into the record the
paper and live gates read. The book is measured *before* the flatten, because the exposure a
stage carried is a fact about the window and not about the way it ended.

Engine facts this module relies on (nautilus_trader 1.231.0):

* `TradingNode(config, loop)` builds a kernel whose `msgbus`, `cache`, `portfolio`, `clock`,
  `trader` and the three engines are public, and `run_async()` returns only when the engines'
  queue tasks end, so it is driven as a task beside the feed.
* `Strategy.__init__` takes its id from `f"{class name or config.strategy_id}-{order_id_tag}"`,
  so a distinct `order_id_tag` is what separates two instances of one class in one trader —
  including their client order ids. `Actor` takes `component_id` the same way.
* `RiskEngineConfig.max_notional_per_order` is a `dict[str, int]` keyed by instrument id: a
  per-order, per-instrument notional cap and nothing more. It is the backstop under the
  sizing, never the limit itself.
* `LiveRiskEngineConfig.max_order_submit_rate` throttles against the engine's clock, which in
  a node is wall time. A replayed stage compresses months of decisions into seconds, so the
  rate is lifted and the same strategy is throttled the same amount — none — on both paths.
* `Strategy.close_all_positions` and `cancel_all_orders` take an instrument id and submit
  through the strategy's own `submit_order`, which kanso classifies as an exit; the simulated
  exchange runs with no message queue and zero latency, so the closing order is matched
  against the last point's book in the same call rather than on a point that never comes.
* A live engine kills the process on an unhandled exception in queue processing unless
  `graceful_shutdown_on_exception` is set, so every engine here sets it and a strategy that
  raises stops the node instead of the interpreter.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Final

from nautilus_trader.common import Environment
from nautilus_trader.config import (
    BacktestVenueConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId

from kanso.criteria.run import CardRun
from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus import backtest, sandbox
from kanso.nautilus.backtest import RunRequest
from kanso.nautilus.replay_client import SETTLE_TURNS, ReplayDataClient
from kanso.nautilus.session import SHUTDOWN_TOPIC, Halt, ordered
from kanso.nautilus.venue import NETTING, starting_balance, venues_of
from kanso.schemas import Hypothesis, Limits, VenueModel

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.strategy import Loaded

__all__ = [
    "ACCOUNT_TYPES",
    "Book",
    "Placement",
    "Realised",
    "StageNode",
    "StageRun",
    "max_notional_per_order",
    "run",
    "trader_id",
    "venues_for",
]

ACCOUNT_TYPES: Final[dict[str, str]] = {"margin": "MARGIN", "cash": "CASH"}
"""How a resolved venue model's account type is spelled to the engine."""

SUBMIT_RATE: Final = "1000000/00:00:01"
"""An order rate a replayed stage cannot reach, so no wall-clock throttle binds."""

START_TURNS: Final = 100_000
"""How many turns of the loop a node is given to come up before the stage gives up."""

POST_STOP_S: Final = 0.1
CONNECT_S: Final = 10.0
DISCONNECT_S: Final = 5.0


def trader_id(stage: str) -> TraderId:
    """The trader a stage runs under: one identity per stage, stable across restarts."""
    return TraderId(f"KANSO-{stage.upper()}")


@dataclass(frozen=True)
class Placement:
    """One deployed version as a node runs it: its implementation and its money.

    `capital` is what deployment assigned, which is what the strategy sizes against;
    `hyp`, `venue_model` and `period` are the sleeve hypothesis's and the version's pins,
    which are what the realised window is measured and costed with.
    """

    strategy_id: str
    version: int
    capital: float
    hyp: Hypothesis
    venue_model: VenueModel
    snapshot_id: str
    period: str
    source: bytes
    loaded: Loaded

    @property
    def tag(self) -> str:
        """What separates this version from every other in one node."""
        return f"{self.strategy_id}-{self.version}"

    @property
    def label(self) -> str:
        """How this version is named to an operator."""
        return f"{self.strategy_id}@{self.version}"

    @property
    def identity(self) -> str:
        """The `StrategyId` this version's sleeve runs under, without constructing it."""
        return f"{self.loaded.sleeve.cls.__name__}-{self.tag}"

    def request(self, window: tuple[date, date]) -> RunRequest:
        """The run this version asks for over the stage's window."""
        return RunRequest(
            hyp=self.hyp,
            strategy_source=self.source,
            window=window,
            snapshot_id=self.snapshot_id,
            venue_model=self.venue_model.model_dump(),
            capital=self.capital,
            period=self.period,
        )


@dataclass(frozen=True)
class Book:
    """One instrument's net position at the end of a stage's window, and its mark."""

    instrument_id: str
    qty: float
    price: float

    @property
    def notional(self) -> float:
        """The signed exposure this position carries, in the account currency."""
        return self.qty * self.price


@dataclass(frozen=True)
class Realised:
    """What one deployed version actually did over one run of its stage."""

    strategy_id: str
    version: int
    capital: float
    run: CardRun
    positions: tuple[Book, ...]

    @property
    def pnl(self) -> float:
        """The window's realised and marked change in equity."""
        return sum(self.run.returns)


@dataclass(frozen=True)
class StageRun:
    """What one run of a stage node produced, for every version on it."""

    stage: str
    window: tuple[date, date]
    points: tuple[Any, ...]
    released: int
    clock_ns: int | None
    realised: tuple[Realised, ...]
    intents: tuple[tuple[int, str, str, float, str, float | None], ...]
    halted: str | None = None

    @property
    def crashed(self) -> bool:
        """Whether the node stopped itself before the window was finished."""
        return self.halted is not None


@dataclass(frozen=True)
class StageNode:
    """A stage as a node is configured from it: its money, its limits and its versions."""

    stage: str
    capital: float
    limits: Limits
    placements: tuple[Placement, ...]
    window: tuple[date, date]
    catalog: Path
    after: int | None = None
    """The stage's session clock: the availability instant a restart resumes past.

    A stage keeps one replay position and picks up where it stopped, so the window is
    loaded whole by day and then cut at the instant, rather than at the day's midnight —
    resuming at the start of the day the stage stopped in would replay that day again.
    """

    pace: float = 0.0
    """Seconds of data released per second of wall clock; zero is no pacing at all.

    A restart replays the backlog between the stage's clock and the end of the catalog, and
    a backlog is by definition already in the past: pacing it would make a restart take as
    long as the history it is catching up on — a year of daily bars at one second of data
    per second of wall clock is a year. So the stage's configured `speed` is recorded on the
    session and enforced against a wall-clock execution client, and the catch-up itself runs
    unpaced. Pacing is what a node that has caught up needs, and a node that has caught up
    is a process that outlives the command that started it.
    """

    def venues(self) -> list[BacktestVenueConfig]:
        """One simulated venue per venue this stage trades, funded with the stage capital.

        The engine keeps one account per venue and no cross-venue book, so a venue is
        funded once with the whole stage capital however many versions trade it; what
        bounds a version's exposure is the capital injected into it and the limits the
        monitor enforces over the stage as a whole.
        """
        return venues_for(self.placements, self.capital)

    def config(self) -> TradingNodeConfig:
        """The node this stage is: sandboxed, silent, and capped per order per instrument."""
        return TradingNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=trader_id(self.stage),
            logging=LoggingConfig(bypass_logging=True),
            data_engine=LiveDataEngineConfig(graceful_shutdown_on_exception=True),
            risk_engine=LiveRiskEngineConfig(
                graceful_shutdown_on_exception=True,
                max_order_submit_rate=SUBMIT_RATE,
                max_order_modify_rate=SUBMIT_RATE,
                max_notional_per_order=max_notional_per_order(
                    self.placements, self.limits, self.capital
                ),
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


def max_notional_per_order(
    placements: Sequence[Placement], limits: Limits, capital: float
) -> dict[str, int]:
    """The engine's per-order cap, one entry per instrument any deployed version trades.

    The cap is `per_strategy_max_pct` of the stage capital: no single order may commit more
    than one strategy's whole allowance. It is a backstop and never the limit itself, since
    the engine can express neither a per-strategy nor a net exposure bound — those are
    enforced where the size is chosen and by the monitor, which sees every version at once.
    """
    ceiling = int(limits.per_strategy_max_pct / 100.0 * capital)
    return {
        name: ceiling
        for name in sorted({name for placed in placements for name in placed.hyp.universe})
    }


def venues_for(placements: Sequence[Placement], capital: float) -> list[BacktestVenueConfig]:
    """One cost-neutral venue configuration per venue these versions trade.

    Two versions trading one venue trade one account, so they must agree about it: an
    account has a single type and a single currency, and a stage whose versions were
    certified against different ones is refused rather than funded in whichever came first.
    The leverage ceiling is the highest any version on that venue was certified with, which
    is the only value that lets each of them size as it was measured.
    """
    if capital <= 0:
        raise ValidationError(f"capital: {capital} is not an amount to fund a stage with")
    models: dict[str, tuple[str, VenueModel, float]] = {}
    for placed in placements:
        for venue in venues_of(placed.hyp.universe):
            leverage = placed.hyp.risk_limits.max_leverage
            held = models.get(venue)
            if held is None:
                models[venue] = (placed.label, placed.venue_model, leverage)
                continue
            _agree(venue, held, placed)
            models[venue] = (held[0], held[1], max(held[2], leverage))
    return [
        BacktestVenueConfig(
            name=venue,
            oms_type=NETTING,
            account_type=ACCOUNT_TYPES[model.account],
            starting_balances=[starting_balance(capital, model.currency)],
            base_currency=model.currency,
            default_leverage=1.0 if model.account == "cash" else leverage,
            bar_execution=True,
            fill_model=None,
            fee_model=None,
            latency_model=None,
        )
        for venue, (_, model, leverage) in sorted(models.items())
    ]


def _agree(venue: str, held: tuple[str, VenueModel, float], placed: Placement) -> None:
    """Refuse two versions that would fund one venue's account two different ways."""
    first, model, _ = held
    mine = placed.venue_model
    for field, theirs, ours in (
        ("account", model.account, mine.account),
        ("currency", model.currency, mine.currency),
    ):
        if theirs != ours:
            raise PreconditionError(
                f"venues.{venue}.{field}: {first} was certified against {theirs!r} and "
                f"{placed.label} against {ours!r}; one venue is one account",
                remedy=f"set venues.{venue}.{field} in portfolio.yaml and re-certify",
            )


# --- running the stage --------------------------------------------------------


def run(
    node: StageNode,
    *,
    settle_turns: int = SETTLE_TURNS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> StageRun:
    """Start this stage, replay its window into it, flatten it and stop it.

    Every version is measured over the same window from the same cache, filtered to the
    positions its own strategy opened, so two strategies sharing a stage are costed and
    reported separately while trading one account.
    """
    if not node.placements:
        raise PreconditionError(
            f"stages.{node.stage}: no version is deployed, so there is no node to run",
            remedy="compose a certified hypothesis and deploy it",
        )
    requests = tuple(placed.request(node.window) for placed in node.placements)
    window = _window_data(requests, node.catalog, node.after)
    points = window.points
    if not points:
        return _idle(node, requests)
    backtest._seed_globals(requests[0].snapshot_id)
    loop = asyncio.new_event_loop()
    built = TradingNode(config=node.config(), loop=loop)
    try:
        built.build()
        kernel = built.kernel
        halt = Halt()
        kernel.msgbus.subscribe(topic=SHUTDOWN_TOPIC, handler=halt.record)
        for instrument in window.instruments:
            kernel.cache.add_instrument(instrument)
        client = ReplayDataClient(
            loop,
            kernel.msgbus,
            kernel.cache,
            kernel.clock,
            points=points,
            speed=node.pace,
            settle_turns=settle_turns,
            sleep=sleep,
            alive=halt.running,
        )
        kernel.data_engine.register_client(client)
        kernel.data_engine.register_default_client(client)
        client.attach(kernel.data_engine, kernel.risk_engine, kernel.exec_engine)
        sandbox.attach(kernel, node.venues(), points)
        strategies = _components(built, node.placements)
        books = loop.run_until_complete(_drive(built, client, strategies, halt, points))
        realised = tuple(
            _realised(placed, request, kernel, groups, books)
            for placed, request, groups in zip(
                node.placements, requests, window.per_version, strict=True
            )
        )
        intents = tuple(
            (i.ts_event, i.instrument_id, i.side, i.qty, i.order_type, i.price)
            for strategy in strategies
            for i in strategy.intents
        )
        released = client.released
        return StageRun(
            stage=node.stage,
            window=node.window,
            points=points,
            released=released,
            clock_ns=client.last_ts if released else None,
            realised=realised,
            intents=intents,
            halted=halt.reason,
        )
    finally:
        built.dispose()


def _idle(node: StageNode, requests: Sequence[RunRequest]) -> StageRun:
    """The run of a stage whose clock already stands past everything the catalog holds.

    A restart with nothing new to replay is not a failure and not a crash: the stage is
    current. It produces an empty window for every version, leaves the clock where it was,
    and builds no node at all — starting one to feed it nothing would only close and reopen
    every position for no reason.
    """
    return StageRun(
        stage=node.stage,
        window=node.window,
        points=(),
        released=0,
        clock_ns=None,
        realised=tuple(
            Realised(
                strategy_id=placed.strategy_id,
                version=placed.version,
                capital=placed.capital,
                run=backtest._empty(request),
                positions=(),
            )
            for placed, request in zip(node.placements, requests, strict=True)
        ),
        intents=(),
    )


@dataclass(frozen=True)
class _Window:
    """One stage window, loaded once: its instruments, its points, and each version's view."""

    instruments: tuple[Any, ...]
    points: tuple[Any, ...]
    per_version: tuple[tuple[tuple[Any, ...], ...], ...]


def _window_data(
    requests: Sequence[RunRequest], catalog: Path, after: int | None = None
) -> _Window:
    """Each version's window, with the instruments merged and no series loaded twice.

    Two versions trading one instrument at one grain ask for exactly the same points, and
    feeding them twice would give the exchange a market that ticked twice per bar. So the
    feed is built from the distinct series, while each version keeps its own view of them,
    because the marks and the observed spreads its window is costed with are its own.

    `after` is the stage's clock, and every point at or before it is dropped from both —
    from the feed, so nothing is replayed twice, and from each version's view, so what is
    measured is exactly what was released.
    """
    instruments: dict[str, Any] = {}
    seen: dict[tuple[str, str], tuple[Any, ...]] = {}
    per_version: list[tuple[tuple[Any, ...], ...]] = []
    for request in requests:
        found, groups = backtest.window_data(request, catalog)
        for instrument in found:
            instruments.setdefault(str(getattr(instrument, "id", instrument)), instrument)
        kept = (_since(group, after) for group in groups)
        per_version.append(
            tuple(seen.setdefault(_group_key(group), group) for group in kept if group)
        )
    return _Window(
        instruments=tuple(instruments[name] for name in sorted(instruments)),
        points=ordered(tuple(seen[key] for key in sorted(seen))),
        per_version=tuple(per_version),
    )


def _since(group: Sequence[Any], after: int | None) -> tuple[Any, ...]:
    """One series with everything the stage has already replayed removed."""
    if after is None:
        return tuple(group)
    return tuple(point for point in group if int(point.ts_init) > after)


def _group_key(group: Sequence[Any]) -> tuple[str, str]:
    """What makes two loaded groups the same series: their type and their identifier."""
    point = group[0]
    inner = getattr(point, "data", point)
    bar_type = getattr(inner, "bar_type", None)
    identifier = str(bar_type) if bar_type is not None else str(getattr(inner, "instrument_id", ""))
    return type(inner).__name__, identifier


def _components(built: TradingNode, placements: Sequence[Placement]) -> tuple[Any, ...]:
    """Add every version's sleeve and constructs to the trader, each under its own id."""
    made: list[Any] = []
    for placed in placements:
        loaded = placed.loaded
        config = _reconfigured(
            loaded.sleeve.config, capital=placed.capital, order_id_tag=placed.tag
        )
        strategy = loaded.sleeve.cls(config=config)
        for index, actor in enumerate(loaded.attached):
            built.trader.add_actor(
                actor.cls(
                    config=_reconfigured(
                        actor.config,
                        host_strategy_id=strategy.id.value,
                        component_id=f"{placed.tag}-{index}-{actor.construct}",
                    )
                )
            )
        built.trader.add_strategy(strategy)
        made.append(strategy)
    return tuple(made)


def _reconfigured(config: Any, **overrides: Any) -> Any:
    """The same configuration with these fields replaced.

    An engine configuration is a frozen struct, and `dict()` round-trips one through its
    own field set with its tuples intact, so replacing a field is building the same class
    from what it already held.
    """
    return type(config)(**{**config.dict(), **overrides})


async def _drive(
    built: TradingNode,
    client: ReplayDataClient,
    strategies: Sequence[Any],
    halt: Halt,
    points: Sequence[Any],
) -> dict[tuple[str, str], Book]:
    """Start the node, feed it the window, take the book, flatten it and stop.

    The book is read before the flatten and returned, because what a stage held is what the
    exposure limits are about; the flatten is only how a node stops without stranding a
    position no restart would inherit.
    """
    runner = asyncio.create_task(built.run_async())
    await _started(built, client, strategies)
    await client.settle()
    await client.replay()
    books = _books(built.kernel, strategies, points)
    if halt.reason is None:
        _flatten(strategies)
        await client.settle()
    else:
        await _halted(built)
    await built.stop_async()
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner
    await _cleared()
    return books


def _flatten(strategies: Sequence[Any]) -> None:
    """Cancel every open order and close every open position, on every strategy."""
    for strategy in strategies:
        for instrument_id in strategy.universe:
            strategy.cancel_all_orders(instrument_id)
            strategy.close_all_positions(instrument_id)


def _books(
    kernel: Any, strategies: Sequence[Any], points: Sequence[Any]
) -> dict[tuple[str, str], Book]:
    """Every open position at the end of the window, marked at the last price seen."""
    marks: dict[str, float] = {}
    for point in points:
        price = backtest._price_of(point)
        instrument = backtest._instrument_of(point)
        if price is not None and instrument is not None:
            marks[instrument] = price
    owners = {strategy.id.value for strategy in strategies}
    found: dict[tuple[str, str], Book] = {}
    for position in kernel.cache.positions():
        owner = str(position.strategy_id)
        if owner not in owners or position.is_closed:
            continue
        name = str(position.instrument_id)
        found[owner, name] = Book(
            instrument_id=name,
            qty=float(position.signed_qty),
            price=marks.get(name, float(position.avg_px_open)),
        )
    return found


def _realised(
    placed: Placement,
    request: RunRequest,
    kernel: Any,
    groups: Sequence[Sequence[Any]],
    books: Mapping[tuple[str, str], Book],
) -> Realised:
    """One version's window, extracted from the shared cache as if it had run alone.

    A version whose own universe held nothing new gets an empty window rather than a
    refusal: the stage ran, this version simply had no market to act on, and a stage that
    could not restart because one of its versions was quiet would be the worse failure.
    """
    if not any(groups):
        return Realised(
            strategy_id=placed.strategy_id,
            version=placed.version,
            capital=placed.capital,
            run=backtest._empty(request),
            positions=(),
        )
    stream = backtest._stream(request, groups)
    view = _StrategyView(kernel.cache, placed.identity)
    return Realised(
        strategy_id=placed.strategy_id,
        version=placed.version,
        capital=placed.capital,
        run=backtest._extract(request, _Engine(view), stream, groups),
        positions=tuple(
            book for (holder, _), book in sorted(books.items()) if holder == placed.identity
        ),
    )


class _Engine:
    """What the extraction asks of an engine: its cache, and nothing else."""

    def __init__(self, cache: Any) -> None:
        self.cache = cache


class _StrategyView:
    """One strategy's view of a shared cache: its own positions, everyone's instruments.

    A stage runs several versions against one account, and the extraction measures one
    version at a time. Instruments are shared because a multiplier is a property of the
    instrument; positions are not, because a position belongs to the strategy that opened
    it and netting keeps them apart by `strategy_id`.
    """

    def __init__(self, cache: Any, strategy_id: str) -> None:
        self._cache = cache
        self._id = strategy_id

    def instruments(self) -> Any:
        """Every instrument the node knows, whoever trades it."""
        return self._cache.instruments()

    def positions(self) -> list[Any]:
        """The live positions this strategy holds."""
        return [p for p in self._cache.positions() if str(p.strategy_id) == self._id]

    def position_snapshots(self) -> list[Any]:
        """The superseded positions this strategy held."""
        return [p for p in self._cache.position_snapshots() if str(p.strategy_id) == self._id]


async def _started(built: TradingNode, client: ReplayDataClient, strategies: Sequence[Any]) -> None:
    """Yield until the node is running, the feed is connected and every sleeve started."""
    for _ in range(START_TURNS):
        await asyncio.sleep(0)
        if (
            built.kernel.is_running()
            and client.is_connected
            and all(strategy.is_running for strategy in strategies)
        ):
            return
    raise PreconditionError(  # pragma: no cover - a node that never starts on this host
        "deploy: the stage node did not start",
        remedy="run `kanso doctor` to check the engine installation",
    )


async def _halted(built: TradingNode) -> None:
    """Yield until the kernel's own shutdown has run its course."""
    for _ in range(START_TURNS):
        if not built.kernel.is_running():
            return
        await asyncio.sleep(0)


async def _cleared() -> None:
    """Cancel whatever the node left running, so the loop closes with nothing pending."""
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
