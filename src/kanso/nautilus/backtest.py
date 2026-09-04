"""The runner: one strategy, one window, one `CardRun`, and the costs applied exactly once.

A run is a request and its extraction. The request names the hypothesis, the bytes of the
sleeve, the window, the snapshot those bytes are judged on, the resolved venue model and
the capital; the extraction is the single measured object every objective, every gate and
every expectation is computed from. Between the two sits an engine built in process: the
venues of the universe, the resolved instruments, the window's data, the sleeve and any
attached modifiers.

**Costs are applied here and nowhere else.** The simulated venue is cost-neutral
(`kanso.nautilus.venue`), and commission, slippage and half the spread on each side are
deducted per fill in this extraction. One application means one number: a card, a
certification gate, a composition expectation and a realised paper objective all read the
same arithmetic, and a cost model can be re-applied to recorded fills without re-running
anything.

**The window is a refusal, not a parameter.** A request may name only a window the
hypothesis declares, and the card path — `run_subprocess` — accepts only the research
window. That is the embargo, enforced in code rather than in a convention: research
cannot read the data that is meant to judge it, because the runner will not load it. The
child re-checks the data it was handed against the window it was asked for, so the
refusal survives the trip across the process boundary.

**A card runs in a child of its own.** `run_subprocess` starts one in a new session with
an environment allow-list and no path to any catalog: the parent reads the window from
the catalog and serialises the points to the child, so a card has no route to data
outside its window even if its code looked for one. The parent supervises wall time and
resident memory and kills the process group on breach; resident memory is bounded by
supervision rather than by `setrlimit`, which does not bound RSS on Linux and is rejected
for address space on macOS. The peak comes from the reaped child's own resource usage.

**The same request twice gives the same numbers.** Every global random source is seeded
from the snapshot id, every aggregation is over a sorted sequence, and no set or dict
iteration order reaches a number.

Engine facts this module relies on (nautilus_trader 1.231.0): `BacktestEngine` accepts
data objects directly through `add_data`, which assumes one type per call, requires the
instrument in the cache first, and needs a `client_id` for anything that is not a `Bar`,
`QuoteTick` or `TradeTick`; `sort_data` orders the accumulated stream by `ts_init` once;
`run(start, end)` bounds the stream by `ts_init`; an exception raised in a strategy
handler is logged and re-raised, so a failing card fails the run rather than passing
quietly; the engine's simulated exchange processes an order on the next data instant, so
every fill is stamped at a data instant; `Position` carries `ts_opened`, `ts_closed`,
`avg_px_open`, `avg_px_close`, `peak_qty`, `realized_pnl` and the `OrderFilled` events
that made it, which is the whole trade record; with `use_random_ids` left off the
exchange generates deterministic trade ids.
"""

from __future__ import annotations

import bisect
import contextlib
import hashlib
import os
import pickle
import random
import resource
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from math import fsum
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from kanso.criteria import CardRun, Fill, Trade
from kanso.criteria.run import BPS, NS_PER_DAY, NS_PER_SECOND, midnight_ns
from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus.venue import venue_configs
from kanso.schemas import Hypothesis, VenueModel, parse_duration

__all__ = [
    "ALLOWED_ENV",
    "BUDGET",
    "DEFAULT_PERIOD",
    "DIED",
    "EXCEPTION",
    "MEMORY",
    "RunRequest",
    "RunResult",
    "child_env",
    "execute",
    "main",
    "run",
    "run_subprocess",
    "stage_of",
    "tunable",
    "window_data",
]

DEFAULT_PERIOD: Final = "1d"
"""The return period a request that names none is measured over."""

RESEARCH: Final = "research"
CERTIFICATION: Final = "certification"

SLEEVE_ENTRY: Final = "Strategy"
MODIFIER_ENTRY: Final = "Modifier"

ALLOWED_ENV: Final = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
"""The only ambient variables a card subprocess inherits. No catalog path is among
them, and neither is any credential: the parent hands the child its data instead."""

COVERAGE_PREFIX: Final = "COVERAGE_"
"""Forwarded so a card's own lines are measured when the suite measures them."""

CLIENT_ID: Final = "KANSO"
"""The data client custom types are attributed to; the engine requires one for anything
that is not a bar, a quote or a trade."""

BUDGET: Final = "budget"
MEMORY: Final = "memory"
EXCEPTION: Final = "exception"
DIED: Final = "died"

TAIL_LINES: Final = 50
"""A recorded crash carries the tail of its traceback, never the whole of it."""

POLL_S: Final = 0.02
"""How often the supervisor asks whether the child has exited or overrun its clock."""

MEMORY_POLL_S: Final = 0.25
"""How often the supervisor asks the operating system for the child's resident size."""

GIB: Final = float(1024**3)
_MAXRSS_BYTES: Final = 1 if sys.platform == "darwin" else 1024
"""`ru_maxrss` is bytes on macOS and kibibytes everywhere else."""

_BOOTSTRAP: Final = (
    "import sys; from kanso.nautilus.backtest import main; raise SystemExit(main(sys.argv[1:]))"
)

_SEED_MODULUS: Final = 2**32


@dataclass(frozen=True)
class RunRequest:
    """One backtest: what to run, over which window, on whose data, with what money.

    `modifiers` are the attached constructs to run alongside the sleeve, each as its
    construct id, the bytes of its module and the parameters its config takes.
    `budget_s` and `mem_cap_gb` bound the card path only; unset means unbounded, which
    is what a baseline card runs under.

    `overrides` replaces sleeve configuration fields the author declared with values
    chosen by the caller. Nothing in research sets any: a card is judged at the settings
    its own file states. A perturbation gate is what moves them, and it moves only the
    author's own numeric fields, never one the hypothesis injects.
    """

    hyp: Hypothesis
    strategy_source: bytes
    window: tuple[date, date]
    snapshot_id: str
    venue_model: Mapping[str, object]
    capital: float
    modifiers: Sequence[tuple[str, bytes, Mapping[str, object]]] = ()
    budget_s: float | None = None
    mem_cap_gb: float | None = None
    period: str = DEFAULT_PERIOD
    overrides: Mapping[str, float] = field(default_factory=dict)

    @property
    def bounds(self) -> tuple[int, int]:
        """The window as a half-open instant span `[opens, closes)` in nanoseconds."""
        return midnight_ns(self.window[0]), midnight_ns(self.window[1]) + NS_PER_DAY

    def plain(self) -> RunRequest:
        """The same request with every mapping a plain dict, so it can be serialised."""
        return replace(
            self,
            venue_model=dict(self.venue_model),
            modifiers=tuple(
                (construct, source, dict(params)) for construct, source, params in self.modifiers
            ),
            overrides=dict(self.overrides),
        )


@dataclass(frozen=True)
class RunResult:
    """What a run produced, and what it cost to produce it.

    A crashed run still carries a `CardRun` — an empty one over the requested window — so
    a caller that records a card never has to special-case the shape of a failure.
    """

    run: CardRun
    wall_s: float
    peak_mem_gb: float
    intents: tuple[tuple[int, str, str, float, str, float | None], ...]
    crashed: bool = False
    reason: str | None = None
    traceback_tail: str | None = None


def stage_of(hyp: Hypothesis, window: tuple[date, date]) -> str:
    """Which of the hypothesis's windows this is, or a refusal.

    Only two windows are ever backtested. The forward window is what the deployed book
    lives in and is never replayed as a card, and a window the hypothesis does not
    declare is not a window of this hypothesis at all.
    """
    windows = hyp.windows
    if window == (windows.research.start, windows.research.end):
        return RESEARCH
    if window == (windows.certification.start, windows.certification.end):
        return CERTIFICATION
    raise PreconditionError(
        f"window: {window[0]}..{window[1]} is not a window {hyp.id} declares; its research "
        f"window is {windows.research.start}..{windows.research.end} and its certification "
        f"window is {windows.certification.start}..{windows.certification.end}",
        remedy="run the window the hypothesis declares, or edit the hypothesis and re-add it",
    )


def _seed(snapshot_id: str) -> int:
    """A whole number derived from the data a run is pinned to, and from nothing else."""
    digest = hashlib.sha256(snapshot_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % _SEED_MODULUS


def _seed_globals(snapshot_id: str) -> None:
    """Seed every global random source a strategy could reach, from the snapshot id."""
    import numpy

    value = _seed(snapshot_id)
    random.seed(value)
    numpy.random.seed(value)


# --- loading the window ------------------------------------------------------


def window_data(
    request: RunRequest, catalog_path: Path
) -> tuple[tuple[object, ...], tuple[tuple[object, ...], ...]]:
    """The resolved instruments and the window's points, grouped one type per group.

    Only the requested window is read, and only the types the hypothesis requires, at the
    resolution it declares. Each group is homogeneous because the engine assumes one type
    per `add_data` call.
    """
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    from kanso.data.types import BUILTIN_TYPES, resolve_type

    hyp = request.hyp
    catalog = ParquetDataCatalog(str(catalog_path))
    instruments = catalog.instruments(instrument_ids=list(hyp.universe))
    held = {str(instrument.id): instrument for instrument in instruments}
    missing = [name for name in hyp.universe if name not in held]
    if missing:
        raise PreconditionError(
            f"instruments: the catalog holds no definition for {', '.join(sorted(missing))}",
            remedy="run `kanso data instruments` to resolve the universe into the catalog",
        )
    opens, closes = request.bounds
    start, end = opens, closes - 1
    groups: list[tuple[object, ...]] = []
    for requirement in sorted(hyp.data_requirements):
        if requirement not in BUILTIN_TYPES:
            custom = _custom_points(catalog, resolve_type(requirement), hyp.universe, start, end)
            if custom:
                groups.append(custom)
            continue
        for name in sorted(hyp.universe):
            found = _market_points(catalog, requirement, held[name], hyp.resolution, start, end)
            if found:
                groups.append(found)
    ordered = tuple(held[name] for name in sorted(hyp.universe))
    return ordered, tuple(groups)


def _market_points(
    catalog: Any, requirement: str, instrument: Any, resolution: str, start: int, end: int
) -> tuple[object, ...]:
    """One instrument's bars, quotes or trades over the window.

    Bars are asked for by the bar type the sleeve subscribes to, spelled by the strategy
    module itself, so the runner loads exactly the grain the strategy will receive rather
    than every grain the catalog happens to hold for that instrument.
    """
    from nautilus_trader.model.data import Bar, QuoteTick, TradeTick

    from kanso.nautilus.strategy import BAR, QUOTE, _bar_type

    if requirement == BAR:
        identifier = str(_bar_type(instrument.id, resolution))
        return tuple(catalog.query(Bar, identifiers=[identifier], start=start, end=end))
    data_cls = QuoteTick if requirement == QUOTE else TradeTick
    identifier = str(instrument.id)
    return tuple(catalog.query(data_cls, identifiers=[identifier], start=start, end=end))


def _custom_points(
    catalog: Any, data_cls: type, universe: Sequence[str], start: int, end: int
) -> tuple[object, ...]:
    """A custom type's points over the window: this universe's, plus the market-wide ones.

    A custom series is filed under an instrument id when it has one and under nothing when
    it is market-wide, so both are asked for at once and the ones belonging to another
    universe are dropped.
    """
    admissible = {*universe, None}
    found = catalog.query(data_cls, identifiers=None, start=start, end=end)
    return tuple(point for point in found if _instrument_of(point) in admissible)


def _instrument_of(point: object) -> str | None:
    """The instrument a point belongs to, or `None` for a market-wide one."""
    inner = getattr(point, "data", point)
    bar_type = getattr(inner, "bar_type", None)
    if bar_type is not None:
        return str(bar_type.instrument_id)
    instrument_id = getattr(inner, "instrument_id", None)
    return None if instrument_id is None else str(instrument_id)


# --- loading the code --------------------------------------------------------


def _module(source: bytes, kind: str) -> ModuleType:
    """The bytes of a strategy file as a module, named after the digest of those bytes.

    Naming by digest means the same file loaded twice is one entry rather than one per
    run, and two different files never collide however they are spelled.
    """
    name = f"kanso_{kind}_{hashlib.sha256(source).hexdigest()[:12]}"
    module = ModuleType(name)
    module.__file__ = f"<{kind}>"
    sys.modules[name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def _entry(module: ModuleType, entrypoint: str, base: Any, what: str) -> Any:
    """The one class a strategy file must define, checked before anything is built."""
    found = getattr(module, entrypoint, None)
    if not isinstance(found, type) or not issubclass(found, base):
        raise ValidationError(
            f"strategy.py: defines no class {entrypoint} subclassing {base.__name__}; a {what} "
            f"is run by loading {entrypoint} from the file"
        )
    return found


def _sleeve(request: RunRequest) -> tuple[Any, Any]:
    """The sleeve class and the configuration the hypothesis injects into it."""
    from kanso.nautilus.strategy import KansoStrategy

    module = _module(request.strategy_source, "sleeve")
    cls = _entry(module, SLEEVE_ENTRY, KansoStrategy, "sleeve")
    hyp = request.hyp
    config = cls.config_cls(
        hyp_id=hyp.id,
        universe=tuple(hyp.universe),
        resolution=hyp.resolution,
        data_requirements=tuple(hyp.data_requirements),
        capital=request.capital,
        max_position_pct=hyp.risk_limits.max_position_pct,
        max_drawdown_pct=hyp.risk_limits.max_drawdown_pct,
        max_leverage=hyp.risk_limits.max_leverage,
        venue_model=dict(request.venue_model),
        **dict(request.overrides),
    )
    return cls, config


def tunable(request: RunRequest) -> dict[str, float]:
    """The sleeve's own numeric parameters, at the values this request would run them at.

    The author's fields, not the injected ones: the capital, the risk limits and the venue
    model are the hypothesis's, and moving them would perturb the framework rather than
    the idea. The values are read off the configuration the request builds, so an override
    already applied is what comes back.
    """
    from kanso.nautilus.strategy import tunable_fields

    _, config = _sleeve(request)
    return {name: getattr(config, name) for name in tunable_fields(config)}


def _modifier(
    construct: str, source: bytes, params: Mapping[str, object], hyp_id: str, host: str
) -> Any:
    """One attached construct, configured against the sleeve it modifies."""
    from kanso.nautilus.strategy import KansoModifier

    module = _module(source, "modifier")
    cls = _entry(module, MODIFIER_ENTRY, KansoModifier, "modifier")
    if cls.construct != construct:
        raise ValidationError(
            f"strategy.py: {MODIFIER_ENTRY}.construct is {cls.construct!r}, but it was attached "
            f"as a {construct!r}"
        )
    try:
        config = cls.config_cls(host_strategy_id=host, hyp_id=hyp_id, **dict(params))
    except TypeError as exc:
        raise ValidationError(
            f"construct.params: {cls.config_cls.__name__} does not take these parameters: {exc}"
        ) from None
    return cls(config=config)


# --- the engine --------------------------------------------------------------


def execute(
    request: RunRequest,
    instruments: Sequence[object],
    groups: Sequence[Sequence[object]],
) -> RunResult:
    """Build an engine over this data, run the strategy in it, and extract the run.

    The data is checked against the requested window before anything is added, so a
    child handed points from another window refuses them rather than trading on them.
    """
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.node import (
        get_account_type,
        get_base_currency,
        get_oms_type,
        get_starting_balances,
    )
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
    from nautilus_trader.model.identifiers import ClientId, Venue

    stream = _stream(request, groups)
    _seed_globals(request.snapshot_id)
    started = time.perf_counter()
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            logging=LoggingConfig(bypass_logging=True),
            run_analysis=False,
        )
    )
    try:
        for venue in venue_configs(request.hyp, request.venue_model, request.capital):
            engine.add_venue(
                venue=Venue(venue.name),
                oms_type=get_oms_type(venue),
                account_type=get_account_type(venue),
                base_currency=get_base_currency(venue),
                starting_balances=get_starting_balances(venue),
                # Through a string, so a leverage is the decimal it was written as rather
                # than the binary float that happens to be nearest to it.
                default_leverage=Decimal(str(venue.default_leverage)),
                bar_execution=venue.bar_execution,
            )
        for instrument in instruments:
            engine.add_instrument(instrument)
        for group in groups:
            plain = type(group[0]) in (Bar, QuoteTick, TradeTick)
            engine.add_data(
                list(group),
                client_id=None if plain else ClientId(CLIENT_ID),
                sort=False,
            )
        engine.sort_data()
        cls, config = _sleeve(request)
        strategy = cls(config=config)
        for construct, source, params in request.modifiers:
            engine.add_actor(
                _modifier(construct, source, params, request.hyp.id, cls.__name__),
            )
        engine.add_strategy(strategy)
        opens, closes = request.bounds
        engine.run(start=opens, end=closes - 1)
        card = _extract(request, engine, stream, groups)
        intents = tuple(
            (i.ts_event, i.instrument_id, i.side, i.qty, i.order_type, i.price)
            for i in strategy.intents
        )
    finally:
        engine.dispose()
    return RunResult(
        run=card,
        wall_s=time.perf_counter() - started,
        peak_mem_gb=_own_peak_gb(),
        intents=intents,
    )


def _own_peak_gb() -> float:
    """The peak resident size of the process that ran this, in gibibytes."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _MAXRSS_BYTES / GIB


def _stream(
    request: RunRequest, groups: Sequence[Sequence[object]]
) -> tuple[tuple[int, str, float | None], ...]:
    """Every point as `(ts_init, instrument, price)`, in the order the engine sees them.

    This is both the clock the return periods are cut on — a period exists when it holds
    at least one data event — and the source of the marks the equity curve is struck at.
    Ties are broken by instrument and then by price, so two points at one instant always
    order the same way and the mark they leave is reproducible.
    """
    opens, closes = request.bounds
    stream: list[tuple[int, str, float | None]] = []
    for group in groups:
        for point in group:
            ts = int(point.ts_init)  # type: ignore[attr-defined]
            if not opens <= ts < closes:
                raise PreconditionError(
                    f"data: a {type(point).__name__} published at {ts} lies outside the "
                    f"requested window {request.window[0]}..{request.window[1]}",
                    remedy="load the window the run asked for and nothing else",
                )
            stream.append((ts, _instrument_of(point) or "", _price_of(point)))
    stream.sort(key=lambda item: (item[0], item[1], -1e308 if item[2] is None else item[2]))
    if not stream:
        raise PreconditionError(
            f"data: the catalog holds nothing for {request.hyp.id} over "
            f"{request.window[0]}..{request.window[1]}",
            remedy="run `kanso data load` for the window, then take a snapshot",
        )
    return tuple(stream)


def _price_of(point: object) -> float | None:
    """The price a point marks its instrument at: a bar close, a quote mid, a trade."""
    from nautilus_trader.model.data import Bar, QuoteTick, TradeTick

    if isinstance(point, Bar):
        return float(point.close)
    if isinstance(point, QuoteTick):
        return (float(point.bid_price) + float(point.ask_price)) / 2.0
    if isinstance(point, TradeTick):
        return float(point.price)
    return None


# --- the extraction ----------------------------------------------------------


def _extract(
    request: RunRequest,
    engine: Any,
    stream: Sequence[tuple[int, str, float | None]],
    groups: Sequence[Sequence[object]],
) -> CardRun:
    """The one measured object: returns, equity, trades and fills with costs applied."""
    model = VenueModel.model_validate(dict(request.venue_model))
    cache = engine.cache
    multipliers = {
        str(instrument.id): float(instrument.multiplier) for instrument in cache.instruments()
    }
    spreads = _spreads(groups, model)
    positions = _positions(cache)
    events, owners = _fill_events(positions)
    fills = tuple(_fill(event, multipliers, spreads, model) for event in events)
    by_position: dict[int, list[Fill]] = {}
    for owner, made in zip(owners, fills, strict=True):
        by_position.setdefault(owner, []).append(made)
    trades = _trades(positions, by_position)
    ends, equity = _equity(request, stream, fills, multipliers)
    opening = request.capital
    returns = tuple(
        value - previous for value, previous in zip(equity, (opening, *equity), strict=False)
    )
    return CardRun(
        window=request.window,
        period=request.period,
        period_ends_ns=ends,
        returns=returns,
        equity=tuple(equity),
        trades=trades,
        fills=fills,
        capital=opening,
        currency=model.currency,
        venue_model=dict(request.venue_model),
    )


def _spreads(
    groups: Sequence[Sequence[object]],
    model: VenueModel,
) -> dict[str, tuple[tuple[int, ...], tuple[float, ...]]]:
    """Each instrument's observed half-spread as a fraction of mid, by publication time.

    Only built when the cost model takes its spread from quotes; a fixed spread is a
    constant and needs no series. The half is what one side of a round trip pays.
    """
    if model.costs.spread != "quotes":
        return {}
    from nautilus_trader.model.data import QuoteTick

    seen: dict[str, list[tuple[int, float]]] = {}
    for group in groups:
        for point in group:
            if not isinstance(point, QuoteTick):
                continue
            bid, ask = float(point.bid_price), float(point.ask_price)
            mid = (bid + ask) / 2.0
            fraction = 0.0 if mid <= 0 else (ask - bid) / mid / 2.0
            seen.setdefault(str(point.instrument_id), []).append((int(point.ts_init), fraction))
    return {
        key: (tuple(ts for ts, _ in ordered), tuple(v for _, v in ordered))
        for key, values in sorted(seen.items())
        if (ordered := sorted(values))
    }


def _half_spread(
    instrument_id: str,
    ts_ns: int,
    spreads: Mapping[str, tuple[tuple[int, ...], tuple[float, ...]]],
    model: VenueModel,
) -> float:
    """The fraction of notional one side of the spread costs at this instant.

    A fixed spread is a stated width and half of it is charged each way. A quoted spread
    is the last one published for that instrument before the fill; where no quote has
    been published yet, or none exists for the instrument at all, there is no observed
    spread to charge and the fill bears none.
    """
    if model.costs.spread != "quotes":
        return (model.costs.fixed_bps or 0.0) / 2.0 / BPS
    times, values = spreads.get(instrument_id, ((), ()))
    index = bisect.bisect_right(times, ts_ns)
    return 0.0 if index == 0 else values[index - 1]


def _positions(cache: Any) -> tuple[Any, ...]:
    """Every position the run held, superseded ones included, in a stable order.

    Under netting the engine keeps one position id per instrument and strategy and reuses
    it: closing a position and opening the next one produces one live object, with the
    previous one copied into the cache's snapshots. The trade record is therefore the
    snapshots plus whatever is live, and it is ordered by time rather than by id, because
    a snapshot's id carries a fresh UUID and would order differently on every run.
    """
    found = [*cache.position_snapshots(), *cache.positions()]
    found.sort(key=lambda p: (p.ts_opened, p.ts_closed or 0, str(p.instrument_id)))
    return tuple(found)


def _fill_events(positions: Sequence[Any]) -> tuple[tuple[Any, ...], tuple[int, ...]]:
    """Every fill the run produced, once each, and which position each one belongs to.

    Fills are read off the positions they made rather than off the order book, because a
    position is what a trade is; a fill that flipped a net position is split by the engine
    into two events on two positions, so identity is checked rather than assumed.
    """
    seen: set[str] = set()
    owned: list[tuple[Any, int]] = []
    for index, position in enumerate(positions):
        for event in position.events:
            key = str(event.id)
            if key in seen:
                continue
            seen.add(key)
            owned.append((event, index))
    owned.sort(
        key=lambda pair: (
            pair[0].ts_event,
            str(pair[0].instrument_id),
            str(pair[0].client_order_id),
            str(pair[0].trade_id),
        )
    )
    return tuple(event for event, _ in owned), tuple(index for _, index in owned)


def _fill(
    event: Any,
    multipliers: Mapping[str, float],
    spreads: Mapping[str, tuple[tuple[int, ...], tuple[float, ...]]],
    model: VenueModel,
) -> Fill:
    """One execution, with the cost this venue model charges it, applied once."""
    from nautilus_trader.model.enums import order_side_to_str

    instrument_id = str(event.instrument_id)
    qty = float(event.last_qty)
    px = float(event.last_px)
    notional = qty * px * multipliers.get(instrument_id, 1.0)
    rate = (model.costs.commission_bps + model.costs.slippage_bps) / BPS
    half = _half_spread(instrument_id, int(event.ts_event), spreads, model)
    return Fill(
        ts_ns=int(event.ts_event),
        instrument_id=instrument_id,
        side=order_side_to_str(event.order_side),
        qty=qty,
        px=px,
        cost=notional * (rate + half),
    )


def _trades(
    positions: Sequence[Any], by_position: Mapping[int, Sequence[Fill]]
) -> tuple[Trade, ...]:
    """Closed positions as trades, netted of the costs of the fills that made them.

    A position still open when the window closes is not a trade: its profit is in the
    equity curve as an unrealised mark, and it becomes a trade only when it closes.
    """
    from nautilus_trader.model.enums import OrderSide

    trades: list[Trade] = []
    for index, position in enumerate(positions):
        if not position.is_closed:
            continue
        fills = tuple(by_position.get(index, ()))
        cost = fsum(fill.cost for fill in fills)
        realized = position.realized_pnl
        peak = float(position.peak_qty)
        trades.append(
            Trade(
                opened_ns=int(position.ts_opened),
                closed_ns=int(position.ts_closed),
                instrument_id=str(position.instrument_id),
                qty=peak if position.entry == OrderSide.BUY else -peak,
                avg_open=float(position.avg_px_open),
                avg_close=float(position.avg_px_close),
                pnl_net=(0.0 if realized is None else float(realized)) - cost,
                cost=cost,
                fills=fills,
            )
        )
    return tuple(trades)


def _equity(
    request: RunRequest,
    stream: Sequence[tuple[int, str, float | None]],
    fills: Sequence[Fill],
    multipliers: Mapping[str, float],
) -> tuple[tuple[int, ...], list[float]]:
    """The period ends and the equity struck at each, from cash and marked positions.

    A period exists when it holds at least one data event, and ends at the last one it
    holds. Equity there is the capital, less what the fills paid for what they bought and
    less the costs they were charged, plus every open position marked at the last price
    published for it. That is cash plus market value: the number a drawdown, a return and
    a Sharpe are all read from.
    """
    opens, _ = request.bounds
    period_ns = int(parse_duration(request.period, "period").total_seconds() * NS_PER_SECOND)
    if period_ns <= 0:
        raise ValidationError(
            f"period: {request.period!r} is no time at all, so the window holds no return "
            "periods to measure"
        )
    last_in: dict[int, int] = {}
    for ts, _key, _price in stream:
        last_in[(ts - opens) // period_ns] = ts
    ends = tuple(last_in[index] for index in sorted(last_in))
    marks: dict[str, float] = {}
    held: dict[str, float] = {}
    cash = request.capital
    equity: list[float] = []
    point = 0
    fill = 0
    for end in ends:
        while point < len(stream) and stream[point][0] <= end:
            _ts, key, price = stream[point]
            if price is not None:
                marks[key] = price
            point += 1
        while fill < len(fills) and fills[fill].ts_ns <= end:
            made = fills[fill]
            signed = made.qty if made.side == "BUY" else -made.qty
            multiplier = multipliers.get(made.instrument_id, 1.0)
            cash -= signed * made.px * multiplier + made.cost
            held[made.instrument_id] = held.get(made.instrument_id, 0.0) + signed
            fill += 1
        value = fsum(
            held[key] * marks.get(key, 0.0) * multipliers.get(key, 1.0) for key in sorted(held)
        )
        equity.append(cash + value)
    return ends, equity


# --- the two entry points ----------------------------------------------------


def run(request: RunRequest, catalog_path: Path) -> RunResult:
    """Run in this process, over the requested window and no other.

    The window must be one the hypothesis declares; the forward window is never
    backtested, and a window it does not declare is refused before anything is read.
    """
    stage_of(request.hyp, request.window)
    instruments, groups = window_data(request, catalog_path)
    return execute(request, instruments, groups)


def run_subprocess(request: RunRequest, catalog_path: Path, workdir: Path) -> RunResult:
    """Run a card in a child of its own, supervised, with no path to any catalog.

    The window must be the research window: this is the embargo, and it is a refusal in
    code rather than a rule anyone has to remember. The parent reads that window from the
    catalog and hands the points to the child, which starts in a new session with an
    environment allow-list, so nothing in the card can reach data the run was not given.
    """
    stage = stage_of(request.hyp, request.window)
    if stage != RESEARCH:
        raise PreconditionError(
            f"window: {request.window[0]}..{request.window[1]} is the {stage} window of "
            f"{request.hyp.id}; a card runs on research data only, because the embargo keeps "
            "the window that judges a strategy out of the loop that writes it",
            remedy=f"run the research window {request.hyp.windows.research.start}.."
            f"{request.hyp.windows.research.end}",
        )
    instruments, groups = window_data(request, catalog_path)
    payload = pickle.dumps(
        {"request": request.plain(), "instruments": list(instruments), "groups": list(groups)},
        protocol=pickle.HIGHEST_PROTOCOL,
    )
    with tempfile.TemporaryDirectory(prefix="kanso-card-") as transfer:
        room = Path(transfer)
        (room / "request.pkl").write_bytes(payload)
        return _supervised(request, room, workdir)


def child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a card subprocess starts with: an allow-list and nothing else.

    No catalog path, no credential and no workspace variable survives, so a card cannot
    reach anything the parent did not hand it. The hash seed is pinned so two runs of the
    same code order their own sets identically, and the coverage variables are forwarded
    so a card's lines are measured when the suite measures them.
    """
    source = os.environ if environ is None else environ
    env = {name: source[name] for name in ALLOWED_ENV if name in source}
    env.update(
        {name: value for name, value in sorted(source.items()) if name.startswith(COVERAGE_PREFIX)}
    )
    env["PYTHONHASHSEED"] = "0"
    return env


def _supervised(request: RunRequest, room: Path, workdir: Path) -> RunResult:
    """Start the child, watch its clock and its memory, and read back what it produced."""
    result_path = room / "result.pkl"
    started = time.monotonic()
    with (room / "stderr.txt").open("wb") as errors:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _BOOTSTRAP,
                str(room / "request.pkl"),
                str(result_path),
            ],
            cwd=str(workdir),
            env=child_env(),
            stdin=subprocess.DEVNULL,
            stdout=errors,
            stderr=errors,
            start_new_session=True,
        )
        breach, peak_gb = _watch(child, request.budget_s, request.mem_cap_gb)
    wall_s = time.monotonic() - started
    tail = _tail((room / "stderr.txt").read_text(encoding="utf-8", errors="replace"))
    if breach is not None:
        return _crashed(request, wall_s, peak_gb, breach, tail)
    return _reported(request, result_path, wall_s, peak_gb, tail)


def _watch(
    child: Any, budget_s: float | None, mem_cap_gb: float | None
) -> tuple[str | None, float]:
    """Wait for the child, killing its process group when it overruns either bound."""
    started = time.monotonic()
    checked = started
    breach: str | None = None
    while True:
        pid, status, usage = os.wait4(child.pid, os.WNOHANG)
        if pid != 0:
            child.returncode = os.waitstatus_to_exitcode(status)
            return breach, usage.ru_maxrss * _MAXRSS_BYTES / GIB
        now = time.monotonic()
        if budget_s is not None and now - started > budget_s:
            breach = BUDGET
        elif mem_cap_gb is not None and now - checked >= MEMORY_POLL_S:
            checked = now
            if _resident_gb(child.pid) > mem_cap_gb:
                breach = MEMORY
        if breach is not None:
            _kill(child.pid)
            _pid, status, usage = os.wait4(child.pid, 0)
            child.returncode = os.waitstatus_to_exitcode(status)
            return breach, usage.ru_maxrss * _MAXRSS_BYTES / GIB
        time.sleep(POLL_S)


def _kill(pid: int) -> None:
    """Kill the child's whole process group; it leads one of its own."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _resident_gb(pid: int) -> float:
    """The child's resident size now, in gibibytes, or zero when it cannot be read.

    Read from `ps`, because resident memory has no portable interface and `setrlimit`
    does not bound it: `RLIMIT_RSS` is unenforced on Linux and `RLIMIT_AS` is rejected on
    macOS, so supervision from outside is the only bound that actually holds.
    """
    try:
        proc = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no `ps` on this host
        return 0.0
    reported = proc.stdout.strip()
    return float(reported) * 1024.0 / GIB if reported.isdigit() else 0.0


def _tail(text: str) -> str | None:
    """The last lines of what the child said, or nothing when it said nothing."""
    lines = text.strip().splitlines()
    return "\n".join(lines[-TAIL_LINES:]) if lines else None


def _reported(
    request: RunRequest, result_path: Path, wall_s: float, peak_gb: float, tail: str | None
) -> RunResult:
    """The child's own report, or a crash when it left none."""
    try:
        reported = pickle.loads(result_path.read_bytes())
    except (OSError, pickle.UnpicklingError, EOFError):
        return _crashed(request, wall_s, peak_gb, DIED, tail)
    if not reported["ok"]:
        return _crashed(request, wall_s, peak_gb, EXCEPTION, reported["traceback"])
    return RunResult(
        run=reported["run"],
        wall_s=wall_s,
        peak_mem_gb=peak_gb,
        intents=reported["intents"],
    )


def _crashed(
    request: RunRequest, wall_s: float, peak_gb: float, reason: str, tail: str | None
) -> RunResult:
    """A run that produced no numbers, shaped like one that did."""
    return RunResult(
        run=_empty(request),
        wall_s=wall_s,
        peak_mem_gb=peak_gb,
        intents=(),
        crashed=True,
        reason=reason,
        traceback_tail=tail,
    )


def _empty(request: RunRequest) -> CardRun:
    """The run a crash leaves behind: the window, the money, and nothing measured."""
    currency = str(dict(request.venue_model).get("currency", "USD"))
    return CardRun(
        window=request.window,
        period=request.period,
        period_ends_ns=(),
        returns=(),
        equity=(),
        trades=(),
        fills=(),
        capital=request.capital,
        currency=currency,
        venue_model=dict(request.venue_model),
    )


def main(argv: Sequence[str]) -> int:
    """The child: read the request and its data, run it, and write the result back.

    Every failure is reported as a crash with the tail of its traceback rather than as a
    non-zero exit alone, because a card that raised is a card whose reason the loop has
    to be able to read.
    """
    request_path, result_path = Path(argv[0]), Path(argv[1])
    payload = pickle.loads(request_path.read_bytes())
    try:
        result = execute(payload["request"], payload["instruments"], payload["groups"])
    except Exception:
        result_path.write_bytes(
            pickle.dumps({"ok": False, "traceback": _tail(traceback.format_exc())})
        )
        return 1
    result_path.write_bytes(
        pickle.dumps({"ok": True, "run": result.run, "intents": result.intents})
    )
    return 0
