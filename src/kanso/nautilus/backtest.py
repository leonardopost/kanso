"""The runner: one strategy, one window, one `CardRun`, and the costs applied exactly once.

A run is a request and its extraction. The request names the hypothesis, the bytes of the
sleeve, the window, the snapshot those bytes are judged on, the resolved venue model and
the capital; the extraction is the single measured object every objective, every gate and
every expectation is computed from. Between the two sits an engine built in process: the
venues of the universe, the resolved instruments, the window's data, the sleeve and any
attached modifiers.

**Costs are applied here and nowhere else.** The simulated venue is cost-neutral
(`kanso.nautilus.venue`), and commission, slippage and half the spread on each side are
deducted per fill in this extraction — or, for a fill the venue reports as a maker's under a
venue model that states `maker_bps`, that rate alone, and a per-share commission on every share
of a fill that pays commission (`kanso.nautilus.costs.fill_cost`). One
application means one number: a card, a certification gate, a composition expectation and a
realised paper objective all read the same arithmetic, and a cost model can be re-applied to
recorded fills without re-running anything, because each fill records whether it was a
maker's. A perpetual's funding is booked here too, once, at each settlement, on what was held
then (`_equity`) — never by the venue — and the runner configures the sleeve to book the same
amount into the balance it sizes from (`books_funding`).

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
The child watches back: it ends itself when the process that started it is gone, so a lane
killed outright never leaves a card running with nobody supervising it, and a process told
to stop starts no card at all. A caller can also say what the work is for (`wanted`): a
check asked before every catalog query for points and every `WANTED_POLL_S` while a card
runs, whose refusal ends the read and kills the card, so work nobody wants any more is
dropped at the next query rather than after the window and the card have run to their end.
What comes back from a failed child is the tail of its traceback and, when the failure was
one kanso itself raised, that refusal's remedy — so a caller reporting a card that did not
run can name the fault that occurred rather than assume every one of them is the code's.

**A benchmark is a run too.** A hypothesis measured against a hold of its first leg is
measured against a request this module derives from the subject's own (`benchmark`) and
runs like any other, so the hold pays the costs, takes the splits and keeps the book the
subject does, and no objective ever prices a benchmark from bars.

**The same request twice gives the same numbers.** Every global random source is seeded
from the snapshot id, every aggregation is over a sorted sequence, and no set or dict
iteration order reaches a number.

**A split is applied, not traded through.** The sleeve adjusts its positions at each
ex-date its instruments schedule (`kanso.nautilus.splits`), and this extraction reads that
adjustment back off the positions' own ledger: the equity curve folds the quantity change
into what is held, and a trade's size, prices and profit come from its fills and that
ledger rather than from `peak_qty`, `avg_px_open` and `realized_pnl`, which the engine
leaves in the share count the position opened in. A window holding a split the definition
does not schedule is refused before an engine is built, because the alternative is a card
reporting a one-for-ten reverse split as a 905% return.

Engine facts this module relies on (nautilus_trader 1.231.0): `BacktestEngine` accepts
data objects directly through `add_data`, which assumes one type per call, requires the
instrument in the cache first, and needs a `client_id` for anything that is not a `Bar`,
`QuoteTick` or `TradeTick`; `sort_data` orders the accumulated stream by `ts_init` once
with a stable sort, so markers inserted last at an instant stay last when homogeneous
runs are concatenated; `run(start, end)` bounds the stream by `ts_init`; an exception
raised in a strategy handler is logged and re-raised, so a failing card fails the run
rather than passing quietly; the engine's simulated exchange processes an order on the
next data instant, so every fill is stamped at a data instant; `Position` carries
`ts_opened`, `ts_closed`, the `OrderFilled` events that made it and the `PositionAdjusted`
events applied to it, which together are the whole trade record; an `OrderFilled` carries
the `liquidity_side` the matching engine gave its order — `MAKER` for a limit that rested
on the book until the market reached it, `TAKER` for an order marketable when it arrived;
with `use_random_ids` left off the exchange generates deterministic trade ids; the risk
engine denies the hundred-and-first order submitted inside one second of its clock unless
`RiskEngineConfig.max_order_submit_rate` says otherwise, and modifies the same way, and a
denied order is closed — so the research path runs at the same rate as the node paths, one
no replay reaches, or the two would size the next entry from different rooms.
"""

from __future__ import annotations

import bisect
import contextlib
import gc
import hashlib
import os
import pickle
import random
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from functools import cache
from itertools import chain
from math import fsum
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from kanso.criteria import CardRun, Fill, FundingPayment, Trade
from kanso.criteria.run import NS_PER_DAY, NS_PER_SECOND, Held, day_of, midnight_ns
from kanso.errors import KansoError, PreconditionError, ValidationError
from kanso.nautilus import splits
from kanso.nautilus.costs import (
    carry,
    fill_cost,
    fixed_half_spread,
    funding_payment,
    maintenance_ratio,
    month_turned,
    policy_of,
    quote_half_spread,
    reset,
)
from kanso.nautilus.sizing import Refusal, SizingError
from kanso.nautilus.venue import venue_configs
from kanso.schemas import Hypothesis, VenueModel, parse_duration

__all__ = [
    "ALLOWED_ENV",
    "BUDGET",
    "DEFAULT_PERIOD",
    "DIED",
    "EXCEPTION",
    "INTERRUPTED",
    "MEMORY",
    "RunRequest",
    "RunResult",
    "booked",
    "checked",
    "child_env",
    "benchmark",
    "end_with",
    "execute",
    "main",
    "run",
    "run_subprocess",
    "stage_of",
    "tunable",
    "wanted",
    "warmup_prefix",
    "watched",
    "window_data",
]

SUBMIT_RATE: Final = "1000000/00:00:01"
"""An order rate no replay or backtest reaches, so no throttle binds on either path."""

DEFAULT_PERIOD: Final = "1d"
"""The return period a request that names none is measured over."""

RESEARCH: Final = "research"
CERTIFICATION: Final = "certification"

OVERLAY: Final = "overlay"
"""The one attached construct with a clock of its own."""

SLEEVE_ENTRY: Final = "Strategy"
MODIFIER_ENTRY: Final = "Modifier"

HOLD: Final = Path(__file__).resolve().parents[1] / "templates" / "strategy_hold.py"
"""The sleeve kanso ships as the benchmark a `benchmark: {hold: first_leg}` hypothesis
is measured against."""

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
INTERRUPTED: Final = "interrupted"
EXCEPTION: Final = "exception"
DIED: Final = "died"

TAIL_LINES: Final = 50
"""A recorded crash carries the tail of its traceback, never the whole of it."""

POLL_S: Final = 0.02
"""How often the supervisor asks whether the child has exited or overrun its clock."""

MEMORY_POLL_S: Final = 0.25
"""How often the supervisor asks the operating system for the child's resident size."""

WANTED_POLL_S: Final = 1.0
"""How often the supervisor asks the `wanted` checks whether the card is still wanted."""

_INTERRUPT = threading.Event()
"""Set when the process supervising a card has been told to stop.

A card child leads its own session, so nothing outside this process can kill it with the
lane that started it; the watcher reads this flag between polls and kills the child itself,
so a stop costs the card in flight and never leaves it running unbudgeted. Once it is set
no card is started at all: `run_subprocess` refuses before it reads the window and again
before it spawns.
"""

CARD_ROOM: Final = ".card"
"""The directory inside a lane a card's payload, report and stream travel through. A dot
directory, so the scope check a card passes before it runs ignores it."""

REQUEST_FILE: Final = "request.pkl"
RESULT_FILE: Final = "result.pkl"

PARENT_POLL_S: Final = 0.5
"""How often a card asks whether the process that started it is still its parent."""

ORPHANED: Final = 3
"""The status a card exits with when it ended itself because the lane that started it was
gone; nothing reads it, since whatever would have read it is what went."""


def interrupt() -> None:
    """Have every card this process is watching killed at its next poll, and start no
    other."""
    _INTERRUPT.set()


def resume() -> None:
    """Forget an interrupt, so a later card in this process runs to its end."""
    _INTERRUPT.clear()


_WANTED: list[Callable[[], None]] = []
"""The checks the `wanted` blocks this process is inside have registered, innermost last."""


@contextlib.contextmanager
def wanted(check: Callable[[], None]) -> Iterator[None]:
    """Read windows and run cards, inside the block, only for as long as `check` passes.

    `check` raises once the work is not wanted any more — a lane's claim on the hypothesis
    whose baseline it is preparing, taken back by `research queue remove`
    (`kanso.research.loop.begin`) — and is asked before every catalog query for points this
    process makes (the one lookup of the universe's definitions a read begins with is not
    one), before a card is spawned, and every `WANTED_POLL_S` while one runs, which is
    killed before the refusal is raised. So a refusal costs at most the query in flight or
    a poll of the card. Measured before this, on 0.13.1.dev1: four lanes whose claims were
    taken back went on reading their windows for nineteen minutes, because the claim was
    asked about only once the window had been read and the baseline had run on it.
    """
    _WANTED.append(check)
    try:
        yield
    finally:
        _WANTED.remove(check)


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

    `sleeve_budget` is what the sleeve sizes every order to under a `sizing` rule — its
    own hypothesis's for a sleeve card, its host's for an attached construct's — and zero
    is free sizing. An attached construct's own budget travels in its `params` as
    `sizing_budget`. `grains` are the bar grains to load, the sleeve's first; empty means
    the hypothesis's own. An overlay researched at a finer grain than its host names both
    (`grains_of`),
    for the combined run and the host-alone run alike. Every grain loaded reaches the
    venue whether or not the sleeve subscribes it, and the exchange matches against the
    finest bar type it has seen for an instrument, so inside such a run the host's market
    orders fill at the last print of the finer grain — not at the close its own grain
    just delivered — and a coarser bar on a day the finer grain is silent does not move
    the book. The host's `on_bar` still runs only on the host grain.

    `prefix` is the warmup: the first and last of the sessions before the window that the
    strategy is fed before it may trade, resolved by the parent (`warmup_prefix`) and put
    here explicitly, so a card child re-checks the span it was handed rather than one it
    computes. `bounds` stays the measured window; `delivered` is what the engine is fed.
    The upper bound is the same in both. A prefix ends before the window opens, or on the
    day it opens: a stage restart's window opens on the day its clock stands in, and the
    sessions at or before the clock end on that day.

    `cushion` and `settled_ns` are where a book policy stood before this window opened:
    what a monthly reset had moved out of the book, and the last period end it settled.
    Zero and `None` for a card, a certificate and a first deploy; on a stage restart what
    the last window of the same version closed at, so a restore in the new window draws on
    what earlier windows set aside and a month that turned across the restart resets at
    the first end after it. `resumes_ns` is the instant a stage restart trades from — the
    one after its clock — and `None` wherever trading begins at the window's open. The
    first carry runs from the later of that instant and the settled end, never across the
    gap between them: every stage window ends flat, so nothing was held while the version
    was not running, however long ago it last settled. The book itself opens at `capital`
    either way.
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
    grains: tuple[str, ...] = ()
    sleeve_budget: float = 0.0
    prefix: tuple[date, date] | None = None
    cushion: float = 0.0
    settled_ns: int | None = None
    resumes_ns: int | None = None

    def __post_init__(self) -> None:
        if self.prefix is None:
            return
        first, last = self.prefix
        if first > last or last > self.window[0]:
            raise ValidationError(
                f"prefix: {first}..{last} is not a span of sessions before the window "
                f"opening {self.window[0]}"
            )

    @property
    def bounds(self) -> tuple[int, int]:
        """The window as a half-open instant span `[opens, closes)` in nanoseconds."""
        return midnight_ns(self.window[0]), midnight_ns(self.window[1]) + NS_PER_DAY

    @property
    def carried_from_ns(self) -> int:
        """The earliest instant a book policy's carry is charged from: the window's open,
        or the instant a stage restart trades from when it is later."""
        opens = self.bounds[0]
        return opens if self.resumes_ns is None else max(opens, self.resumes_ns)

    @property
    def period_ns(self) -> int:
        """The return period in nanoseconds; a period of no time at all is refused."""
        length = int(parse_duration(self.period, "period").total_seconds() * NS_PER_SECOND)
        if length <= 0:
            raise ValidationError(
                f"period: {self.period!r} is no time at all, so the window holds no return "
                "periods to measure"
            )
        return length

    @property
    def span(self) -> tuple[date, date]:
        """The days fed to the engine: the warmup prefix, when there is one, then the window."""
        return (self.window[0] if self.prefix is None else self.prefix[0]), self.window[1]

    @property
    def delivered(self) -> tuple[int, int]:
        """The instants fed to the engine, `[prefix opens, closes)`; `bounds` without a prefix."""
        return midnight_ns(self.span[0]), self.bounds[1]

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

    `remedy` is the remedy of the failure that ended the run, when the failure named one:
    a card fails for reasons that are not the strategy's — rows the catalog no longer
    holds, a point stamped outside the window — and the operator's next action differs for
    each. It crosses the process boundary because a traceback tail carries the message and
    loses everything else the refusal knew.
    """

    run: CardRun
    wall_s: float
    peak_mem_gb: float
    intents: tuple[tuple[int, str, str, float, str, float | None], ...]
    crashed: bool = False
    reason: str | None = None
    traceback_tail: str | None = None
    remedy: str | None = None
    refused: Refusal | None = None
    """The order the sizing rule refused, when one was: the run stopped there, with no
    numbers, because the card can no longer validate and every bar after it is spend."""


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


def grains_of(hyp: Hypothesis, host_resolution: str | None) -> tuple[str, ...]:
    """The bar grains a run of this hypothesis loads: its own, or its host's and its own.

    Only an overlay has a clock of its own; a filter or an exit rule is consulted on its
    host's grain, and `hyp validate` holds it to the host's resolution. So two grains are
    loaded exactly when an overlay's resolution differs from its host's, and both the
    combined run and the host-alone run load them, so the difference between the two is
    the overlay and not the book the host filled against. Empty means the hypothesis's
    own grain.
    """
    if (
        host_resolution
        and host_resolution != hyp.resolution
        and hyp.construct is not None
        and hyp.construct.id == OVERLAY
    ):
        return (host_resolution, hyp.resolution)
    return ()


def _bar_grains(request: RunRequest) -> tuple[str, ...]:
    """The bar sizes this request loads, the sleeve's first."""
    return request.grains or (request.hyp.resolution,)


LOOKBACK_DOUBLINGS: Final = 5
"""How many spans `warmup_prefix` asks the catalog for before giving up: twice the sessions
asked for in calendar days, doubled each time, so thirty-two times them at most."""


@cache
def _hold_source() -> bytes:
    return HOLD.read_bytes()


def benchmark(request: RunRequest) -> RunRequest:
    """The run a benchmark objective differences `request` against: a hold of its first leg.

    Everything the subject's request pins is kept — the hypothesis, the window and its
    warmup prefix, the snapshot, the venue model, the capital, the return period, the
    grains and the sizing budget — and only the strategy is replaced, by the sleeve kanso
    ships, with no attached construct, no override and no budget of wall time or memory.
    So the hold is produced by this runner and nowhere else: its costs, its splits, its
    book and whatever the runner applies to a run in future are the subject's own
    arithmetic rather than a second one written for a benchmark.
    """
    return replace(
        request,
        strategy_source=_hold_source(),
        modifiers=(),
        overrides={},
        budget_s=None,
        mem_cap_gb=None,
    )


def warmup_prefix(
    hyp: Hypothesis,
    window: tuple[date, date],
    catalog_path: Path,
    grains: Sequence[str] = (),
    *,
    until_ns: int | None = None,
) -> tuple[date, date] | None:
    """The sessions this hypothesis warms on before `window`, resolved from the catalog.

    `None` when the hypothesis declares no warmup. A session is a calendar day on which any
    name of the universe printed at the sleeve's own grain — the union, which is how the
    venue sees the market. The catalog is read over a bounded lookback before the window,
    doubling from twice the sessions asked for in calendar days, and the last N distinct
    days found are the prefix; fewer than N inside the bound is a refusal naming what was
    found. Resolved in the parent by every builder and put on the request explicitly: a
    card child has no catalog, and re-checks the span it was handed rather than one it
    computes. The window's own bounds are not touched.

    `until_ns` is a stage restart's clock: the sessions are then the last N at or before
    that instant rather than before the window's open, because a restarted node resumes
    at the instant after its clock, inside a window that opens on the clock's day, and
    what it warms on is what it replayed before.
    """
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    if hyp.warmup is None:
        return None
    wanted = hyp.warmup.sessions
    catalog = ParquetDataCatalog(str(catalog_path))
    held = _held(catalog, hyp)
    grain = (tuple(grains) or (hyp.resolution,))[0]
    end = midnight_ns(window[0]) - 1 if until_ns is None else until_ns
    edge = f"before {window[0]}" if until_ns is None else f"at or before {day_of(end)} ({end})"
    days: list[date] = []
    lookback = wanted
    start = end
    for _ in range(LOOKBACK_DOUBLINGS):
        lookback *= 2
        start = max(0, end + 1 - lookback * NS_PER_DAY)
        points = _primary_points(catalog, hyp, held, grain, start, end)
        days = sorted({day_of(int(point.ts_init)) for point in points})  # type: ignore[attr-defined]
        if len(days) >= wanted:
            return days[-wanted], days[-1]
    raise PreconditionError(
        f"warmup: {hyp.id} asks for {wanted} session(s) {edge} and the catalog holds "
        f"{len(days)} in the {lookback} calendar days before it",
        remedy=f"load {', '.join(hyp.universe)} at {grain} back to at least {day_of(start)} "
        "with `kanso data load`, then `kanso data snapshot`; or lower warmup.sessions",
    )


def _held(catalog: Any, hyp: Hypothesis) -> dict[str, Any]:
    """The catalog's definition of every name in the universe, refused when one is missing."""
    instruments = catalog.instruments(instrument_ids=list(hyp.universe))
    held = {str(instrument.id): instrument for instrument in instruments}
    missing = [name for name in hyp.universe if name not in held]
    if missing:
        raise PreconditionError(
            f"instruments: the catalog holds no definition for {', '.join(sorted(missing))}",
            remedy="run `kanso data instruments` to resolve the universe into the catalog",
        )
    return held


def _primary_points(
    catalog: Any, hyp: Hypothesis, held: Mapping[str, Any], grain: str, start: int, end: int
) -> tuple[object, ...]:
    """The series a session is counted on: the sleeve's own grain of its first market type.

    Bars at the sleeve's grain when the hypothesis requires bars, else its quotes, else its
    trades — one of the three is always required, because a resolution is a bar size or
    one of the unaggregated grains and the schema requires the type it names. The names
    are taken together, so a day any of them printed is a session.
    """
    from kanso.nautilus.strategy import BAR, QUOTE, TRADE

    requirement = next(kind for kind in (BAR, QUOTE, TRADE) if kind in hyp.data_requirements)
    resolution = grain if requirement == BAR else hyp.resolution
    return tuple(
        chain.from_iterable(
            _market_points(catalog, requirement, held[name], resolution, start, end)
            for name in sorted(hyp.universe)
        )
    )


def window_data(
    request: RunRequest, catalog_path: Path
) -> tuple[tuple[object, ...], tuple[tuple[object, ...], ...]]:
    """The resolved instruments and the delivered span's points, grouped one type per group.

    Only the requested span is read — the window, and the warmup prefix before it when the
    request names one — and only the types the hypothesis requires, at the resolution it
    declares, or at every grain the request names when an overlay's differs from its
    host's. Each group is homogeneous because the engine assumes one type per `add_data`
    call. The upper bound is the window's, prefix or not. A card child is handed the same
    points a session at a time (`_stage`), through the same reader.
    """
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    hyp = request.hyp
    catalog = ParquetDataCatalog(str(catalog_path))
    held = _held(catalog, hyp)
    opens, closes = request.delivered
    scope = _scope_days(catalog, hyp, opens, closes - 1)
    groups, loaded = _window_points(request, catalog, held, scope, opens, closes - 1)
    _refuse_missing_grain(request, loaded)
    ordered = tuple(held[name] for name in sorted(hyp.universe))
    return ordered, groups


def _window_points(
    request: RunRequest,
    catalog: Any,
    held: Mapping[str, Any],
    scope: Scope | None,
    start: int,
    end: int,
) -> tuple[tuple[tuple[object, ...], ...], dict[str, int]]:
    """The points of one span, one type per group, and how many bars each grain served."""
    from kanso.data.types import BUILTIN_TYPES, resolve_type

    hyp = request.hyp
    groups: list[tuple[object, ...]] = []
    grains = _bar_grains(request)
    loaded: dict[str, int] = {grain: 0 for grain in grains}
    for requirement in sorted(hyp.data_requirements):
        if requirement not in BUILTIN_TYPES:
            custom = _custom_points(catalog, resolve_type(requirement), hyp.universe, start, end)
            if custom:
                groups.append(custom)
            continue
        if requirement == "bar":
            for resolution in grains:
                for name in sorted(hyp.universe):
                    found = _market_points(catalog, requirement, held[name], resolution, start, end)
                    found = _in_scope(found, name, scope)
                    if found:
                        loaded[resolution] += len(found)
                        groups.append(found)
            continue
        for name in sorted(hyp.universe):
            found = _market_points(catalog, requirement, held[name], hyp.resolution, start, end)
            found = _in_scope(found, name, scope)
            if found:
                groups.append(found)
    return tuple(groups), loaded


def _refuse_missing_grain(request: RunRequest, loaded: Mapping[str, int]) -> None:
    """An overlay card needs its host's grain and its own; a window without both is refused."""
    if len(loaded) > 1:
        missing = [grain for grain, count in loaded.items() if count == 0]
        if missing:
            raise PreconditionError(
                f"data: the catalog holds no {' and no '.join(missing)} bars for "
                f"{request.hyp.id} over {request.span[0]}..{request.span[1]}",
                remedy="load both the host grain and the overlay grain for the window, "
                "then take a snapshot",
            )


def _market_points(
    catalog: Any, requirement: str, instrument: Any, resolution: str, start: int, end: int
) -> tuple[object, ...]:
    """One instrument's bars, quotes or trades over the window.

    Bars are asked for by the bar type the sleeve subscribes to, spelled by the strategy
    module itself, so the runner loads exactly the grain the strategy will receive rather
    than every grain the catalog happens to hold for that instrument. The `wanted` checks
    are asked first, as before every catalog query for points.
    """
    from nautilus_trader.model.data import Bar, OrderBookDelta, QuoteTick, TradeTick

    from kanso.nautilus.strategy import BAR, BOOK, QUOTE, _bar_type

    _refuse_if_unwanted()
    if requirement == BAR:
        identifier = str(_bar_type(instrument.id, resolution))
        return tuple(catalog.query(Bar, identifiers=[identifier], start=start, end=end))
    data_cls = {QUOTE: QuoteTick, BOOK: OrderBookDelta}.get(requirement, TradeTick)
    identifier = str(instrument.id)
    return tuple(catalog.query(data_cls, identifiers=[identifier], start=start, end=end))


Scope = tuple[frozenset[str], dict[str, set[date]]]
"""A session scope resolved over a span: the names delivered every session, and for the
rest, the sessions their flag admits them on."""


def _scope_days(catalog: Any, hyp: Hypothesis, start: int, end: int) -> Scope | None:
    """The sessions each name is in scope on, read off the hypothesis's scope series.

    `None` when the hypothesis declares no scope. A name's session is admitted when its
    point of that session carries the flag above zero; a name in `always` needs no point.
    The series is read once here and again as a requirement, so a strategy is handed the
    same points the runner scoped on and can see why a name was delivered.
    """
    from kanso.data.types import resolve_type

    scope = hyp.session_scope
    if scope is None:
        return None
    days: dict[str, set[date]] = {}
    for point in _custom_points(catalog, resolve_type(scope.series), hyp.universe, start, end):
        name = _instrument_of(point)
        inner = getattr(
            point, "data", point
        )  # the catalog wraps a custom point; the flag is on the point
        flag = getattr(inner, scope.flag, None)
        if name is None or flag is None or float(flag) <= 0:
            continue
        days.setdefault(name, set()).add(day_of(int(point.ts_init)))  # type: ignore[attr-defined]
    return frozenset(scope.always), days


def _in_scope(points: tuple[object, ...], name: str, scope: Scope | None) -> tuple[object, ...]:
    """The points of `name` a scope admits: all of them under no scope or an `always` name."""
    if scope is None or name in scope[0]:
        return points
    admitted = scope[1].get(name)
    if not admitted:
        return ()
    return tuple(point for point in points if day_of(int(point.ts_init)) in admitted)  # type: ignore[attr-defined]


def _custom_points(
    catalog: Any, data_cls: type, universe: Sequence[str], start: int, end: int
) -> tuple[object, ...]:
    """A custom type's points over the window: this universe's, plus the market-wide ones.

    A custom series is filed under an instrument id when it has one and under nothing when
    it is market-wide, so both are asked for at once and the ones belonging to another
    universe are dropped.
    """
    _refuse_if_unwanted()
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
    grains = _bar_grains(request)
    extra: tuple[str, ...] = ()
    if any(construct == OVERLAY for construct, _, _ in request.modifiers):
        extra = grains[1:]
    scope = hyp.session_scope
    try:
        config = cls.config_cls(
            hyp_id=hyp.id,
            universe=tuple(hyp.universe),
            resolution=grains[0],
            extra_resolutions=extra,
            data_requirements=tuple(hyp.data_requirements),
            session_scope=None
            if scope is None
            else (scope.series, scope.flag, tuple(scope.always)),
            capital=request.capital,
            sizing_budget=request.sleeve_budget,
            max_position_pct=hyp.risk_limits.max_position_pct,
            max_drawdown_pct=hyp.risk_limits.max_drawdown_pct,
            max_leverage=hyp.risk_limits.max_leverage,
            venue_model=dict(request.venue_model),
            # The venue a run fills against is simulated and settles no funding, so the
            # sleeve's balance books it as this extraction does.
            books_funding=True,
            depth=None if hyp.depth is None else (hyp.depth.every_ns, hyp.depth.levels),
            **dict(request.overrides),
        )
    except ValueError as exc:
        raise ValidationError(f"strategy.py: {cls.config_cls.__name__}: {exc}") from None
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
    The whole window is one chunk; `execute_chunked` is the form a card child runs.
    """
    return execute_chunked(request, instruments, [groups])


def execute_chunked(
    request: RunRequest,
    instruments: Sequence[object],
    chunks: Iterable[Sequence[Sequence[object]]],
) -> RunResult:
    """Run the strategy over the window one chunk at a time, and extract the run once.

    Each chunk is a slice of the delivered span in time order, grouped one type per
    group, and holds every point of its instants: the engine is fed it, run in streaming
    mode to its end, and cleared, so what a card holds at any moment is one chunk beside
    the marks the extraction folds as the points pass (`Marks`). Every chunk is checked
    as the whole window was: a point outside the window is refused, and so is a split the
    window holds and no definition schedules. The cross-section markers are the chunk's
    own, and the sleeve is armed for them chunk by chunk, which is the same dispatch the
    whole window gets because a chunk boundary falls between instants, never inside one.

    Engine facts this relies on (nautilus_trader 1.231.0): `run(streaming=True)` pauses
    after the data it holds is exhausted without finalising; `clear_data` drops the stream
    and keeps the instruments; `end` finalises once every chunk has run.
    """
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.node import (
        get_account_type,
        get_base_currency,
        get_book_type,
        get_fill_model,
        get_latency_model,
        get_oms_type,
        get_starting_balances,
    )
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, RiskEngineConfig
    from nautilus_trader.model.identifiers import Venue

    from kanso.nautilus.actions import modules
    from kanso.nautilus.cross_section import is_marker, warm

    model = VenueModel.model_validate(dict(request.venue_model))
    marks = Marks(request, model)
    _seed_globals(request.snapshot_id)
    started = time.perf_counter()
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            logging=LoggingConfig(bypass_logging=True),
            risk_engine=RiskEngineConfig(
                max_order_submit_rate=SUBMIT_RATE,
                max_order_modify_rate=SUBMIT_RATE,
            ),
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
                # A level-two book with queue position when the hypothesis holds one; see
                # `kanso.nautilus.venue`.
                book_type=get_book_type(venue),
                trade_execution=venue.trade_execution,
                queue_position=venue.queue_position,
                # Whether a resting limit the market only touched fills: the venue model's
                # `limit_fill`, built from the configuration the node's venue is built from.
                fill_model=get_fill_model(venue),
                # How long the venue takes to see an order: the venue model's `latency_ms`,
                # none when it states none.
                latency_model=get_latency_model(venue),
                # The venue applies a corporate action one call before it matches the point
                # that carried the market past it; see `kanso.nautilus.actions`.
                modules=modules(venue.name),
            )
        for instrument in instruments:
            engine.add_instrument(instrument)
        cls, config = _sleeve(request)
        strategy = cls(config=config)
        for construct, source, params in request.modifiers:
            engine.add_actor(
                _modifier(construct, source, params, request.hyp.id, cls.__name__),
            )
        if request.prefix is not None:
            warm(strategy, request.bounds[0])
        booked(strategy, request)
        engine.add_strategy(strategy)
        opens, closes = request.delivered
        ran = False
        for groups in chunks:
            splits.unscheduled(instruments, chain.from_iterable(groups), request.span)
            points = _ordered(groups)
            marks.take(points)
            if not points:
                continue
            strategy._hold_until_cross_section = any(is_marker(point) for point in points)
            _load_stream(engine, points)
            engine.run(start=opens, end=closes - 1, streaming=True)
            ran = True
            marks.price(_fill_events(_positions(engine.cache))[0])
            marks.next_chunk()
            engine.clear_data()
        marks.check()
        if ran:
            engine.end()
        card = _extract(request, engine, marks)
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


def booked(strategy: object, request: RunRequest) -> None:
    """Hand a sleeve the book policy its request runs under, when the hypothesis has one.

    Every path that runs a sleeve calls this beside `arm` and `warm`, with the request its
    run is extracted from, so the harness cuts the periods the extraction cuts and settles
    them with the same policy from the same place.
    """
    from kanso.nautilus.cross_section import book

    policy = policy_of(request.hyp.book)
    if policy is None:
        return
    book(
        strategy,
        policy,
        request.bounds[0],
        request.period_ns,
        cushion=request.cushion,
        settled_ns=request.settled_ns,
        carried_from_ns=request.carried_from_ns,
    )


def _own_peak_gb() -> float:
    """The peak resident size of the process that ran this, in gibibytes."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _MAXRSS_BYTES / GIB


Point = tuple[int, str, float | None, float | None, float | None]
"""One stream point: `(ts_init, instrument, price, low, high)`. The price is the mark, and
the last two are the adverse range the point spans — a bar's low and high, a quote's bid and
ask, a trade's own price twice — or nothing, for a point that prices nothing."""


class _Fold:
    """What one return period folds the points inside it into."""

    __slots__ = ("end", "highs", "lows", "marks")

    def __init__(self) -> None:
        self.end = 0
        self.marks: dict[str, tuple[int, float]] = {}
        self.lows: dict[str, float] = {}
        self.highs: dict[str, float] = {}

    def mark(self, key: str, ts: int, price: float) -> None:
        """The last price printed for a name; at one instant, the greatest — which is what
        the sorted stream this replaces left last, so a mark is the same whatever order the
        points of an instant arrive in."""
        held = self.marks.get(key)
        if held is None or ts > held[0] or (ts == held[0] and price > held[1]):
            self.marks[key] = (ts, price)


class Marks:
    """The extraction's reading of the points, folded as they pass.

    This is both the clock the return periods are cut on — a period exists when it holds
    at least one data event, and ends at the last one it holds — and the marks the equity
    curve is struck at: each period's last price per name and the adverse range printed
    inside it, and each name's quoted half-spread for the fills that follow. Everything is
    folded point by point, so a run may be fed its window in chunks and hold no chunk
    beyond the marks it leaves; a whole window fed at once folds to the same values,
    because nothing here depends on the order the points arrive in.

    A point is admitted over the delivered span — the warmup prefix the request names and
    the window — and refused outside it. Points before the window's open leave marks and
    nothing else: no period ends inside the prefix, and no range is taken from it, so a
    name that last printed in the prefix is marked at that print in the first period
    rather than at nothing. Whether the window held anything at all is asked by `check`.

    The quoted spreads are kept per chunk: `price` charges every fill not yet priced with
    the last quote at or before it, from this chunk's quotes or the last quote the previous
    chunk left, and `next_chunk` lets the chunk's series go.

    A funding point is a settlement (`settlements`): its instant, its instrument, its rate,
    and the mark its payment is struck at — the instrument's last print at or before the
    instant, the greatest of several at one instant, exactly as a period's mark is chosen.
    A print is credited to the first settlement of its name at or after it, and the mark of
    each settlement is the latest credited at or before it, so nothing beyond one print per
    settlement is held. That needs the settlements a print belongs to known before the
    print arrives, which is what `take` arranges: a chunk's settlements are folded before
    its prints, and a chunk is a slice of time, so a print a later chunk's settlement
    reaches back to is one print per name, carried. It also needs each name's settlements
    folded in time order — a later one folded first would take the carried print from an
    earlier one — which both callers give it: the catalog serves a name's funding as one
    time-sorted series, and chunks are cut in time. A settlement of a name that has not
    printed by then is marked at nothing, and nothing can be held at it, since a fill
    needs a price.
    """

    def __init__(self, request: RunRequest, model: VenueModel) -> None:
        self.opens, self.closes = request.delivered
        self.measured, _ = request.bounds
        self.period_ns = request.period_ns
        self.span = request.span
        self.window = request.window
        self.hyp_id = request.hyp.id
        self.quotes = model.costs.spread == "quotes"
        self.fixed_half = fixed_half_spread(model.costs.fixed_bps)
        self.prefix = _Fold()
        self._folds: dict[int, _Fold] = {}
        self._series: dict[str, list[tuple[int, float]]] = {}
        self._last_quote: dict[str, tuple[int, float]] = {}
        self._halves: dict[str, float] = {}
        self._settled: list[tuple[int, str, float]] = []
        self._instants: dict[str, list[int]] = {}
        self._credited: dict[tuple[str, int | None], tuple[int, float]] = {}

    def take(self, points: Iterable[object]) -> None:
        """Fold a chunk of points, its funding settlements first (see the class)."""
        held = tuple(points)
        for point in held:
            if _funding_of(point) is not None:
                self.feed(point)
        for point in held:
            if _funding_of(point) is None:
                self.feed(point)

    def feed(self, point: object) -> None:
        """Fold one point: its instant, its mark, its range and, for a quote, its spread."""
        from nautilus_trader.model.data import QuoteTick

        from kanso.nautilus.cross_section import is_marker

        if is_marker(point):
            return
        ts = int(point.ts_init)  # type: ignore[attr-defined]
        if not self.opens <= ts < self.closes:
            raise PreconditionError(
                f"data: a {type(point).__name__} published at {ts} lies outside the "
                f"requested window {self.span[0]}..{self.span[1]}",
                remedy="load the window the run asked for and nothing else",
            )
        low, high = _range_of(point)
        key = _instrument_of(point) or ""
        self.fold(ts, key, _price_of(point), low, high)
        funding = _funding_of(point)
        if funding is not None:
            self.settle(ts, key, float(funding.rate))
        if self.quotes and isinstance(point, QuoteTick):
            half = quote_half_spread(float(point.bid_price), float(point.ask_price))
            self._series.setdefault(str(point.instrument_id), []).append((ts, half))

    def fold(
        self, ts: int, key: str, price: float | None, low: float | None, high: float | None
    ) -> None:
        """Fold one mark, as `(ts_init, instrument, price, low, high)`."""
        if ts < self.measured:
            target = self.prefix
        else:
            target = self._folds.setdefault((ts - self.measured) // self.period_ns, _Fold())
            target.end = max(target.end, ts)
            if low is not None and high is not None:
                target.lows[key] = min(target.lows.get(key, low), low)
                target.highs[key] = max(target.highs.get(key, high), high)
        if price is not None:
            target.mark(key, ts, price)
            self._credit(key, ts, price)

    def settle(self, ts: int, key: str, rate: float) -> None:
        """Record one funding settlement: `rate` on what `key` holds at `ts`."""
        self._settled.append((ts, key, rate))
        instants = self._instants.setdefault(key, [])
        if ts not in instants:
            bisect.insort(instants, ts)
        carried = self._credited.pop((key, None), None)
        if carried is not None:
            self._credit(key, *carried)

    def _credit(self, key: str, ts: int, price: float) -> None:
        """Credit a print to the first settlement of its name at or after it, or carry it."""
        instants = self._instants.get(key, ())
        index = bisect.bisect_left(instants, ts)
        slot = (key, instants[index] if index < len(instants) else None)
        held = self._credited.get(slot)
        if held is None or ts > held[0] or (ts == held[0] and price > held[1]):
            self._credited[slot] = (ts, price)

    def settlements(self) -> tuple[tuple[int, str, float, float], ...]:
        """Every funding settlement folded, as `(ts, instrument, rate, mark)` in time order;
        the mark is nothing for a name that has not printed by then."""
        marked: dict[tuple[str, int], float] = {}
        for key, instants in self._instants.items():
            last: tuple[int, float] | None = None
            for instant in instants:
                last = self._credited.get((key, instant), last)
                marked[(key, instant)] = 0.0 if last is None else last[1]
        return tuple(sorted((ts, key, rate, marked[(key, ts)]) for ts, key, rate in self._settled))

    def check(self) -> None:
        """The refusal for a window the catalog holds nothing for; a prefix alone is no run."""
        if not self._folds:
            raise PreconditionError(
                f"data: the catalog holds nothing for {self.hyp_id} over "
                f"{self.window[0]}..{self.window[1]}",
                remedy="run `kanso data load` for the window, then take a snapshot",
            )

    def periods(self) -> tuple[_Fold, ...]:
        """Every return period that held a point, in order."""
        return tuple(self._folds[index] for index in sorted(self._folds))

    def price(self, events: Sequence[Any]) -> None:
        """Charge every fill not yet priced its quoted half-spread: the last quote published
        for its name at or before it, in this chunk or carried from the last one; a fill
        before any quote bears none."""
        if not self.quotes:
            return
        sorted_series: dict[str, tuple[list[int], list[float]]] = {}
        for event in events:
            key = str(event.id)
            if key in self._halves:
                continue
            name = str(event.instrument_id)
            if name not in sorted_series:
                ordered = sorted(self._series.get(name, ()))
                sorted_series[name] = ([ts for ts, _ in ordered], [v for _, v in ordered])
            times, values = sorted_series[name]
            index = bisect.bisect_right(times, int(event.ts_event))
            if index > 0:
                self._halves[key] = values[index - 1]
            else:
                carried = self._last_quote.get(name)
                self._halves[key] = (
                    carried[1] if carried is not None and carried[0] <= int(event.ts_event) else 0.0
                )

    def half(self, event: Any) -> float:
        """The fraction of notional one side of the spread costs this fill: the stated
        width's half, or the quoted half `price` charged it."""
        if not self.quotes:
            return self.fixed_half
        return self._halves.get(str(event.id), 0.0)

    def next_chunk(self) -> None:
        """Let this chunk's quotes go, keeping each name's last for the fills that follow."""
        for name, series in self._series.items():
            if series:
                ts, half = max(series)
                held = self._last_quote.get(name)
                if held is None or ts >= held[0]:
                    self._last_quote[name] = (ts, half)
        self._series.clear()


def folded(request: RunRequest, stream: Sequence[Point]) -> Marks:
    """The marks a plain stream of `(ts_init, instrument, price, low, high)` folds to."""
    marks = Marks(request, VenueModel.model_validate(dict(request.venue_model)))
    for ts, key, price, low, high in stream:
        marks.fold(ts, key, price, low, high)
    return marks


def checked(
    request: RunRequest,
    instruments: Sequence[object],
    groups: Sequence[Sequence[object]],
) -> Marks:
    """Everything a run refuses before it builds an engine, and then its clock.

    Two refusals, both about data the run was handed rather than about the strategy. A
    point published outside the requested window is the embargo, re-checked here because
    a card is handed its points across a process boundary. A split the window holds and
    the instrument's definition does not schedule is the other: the run would trade
    through the corporate action and report it as return, so it is refused with the entry
    the operator has to write.

    Every path that extracts a run calls this — `execute` here, chunk by chunk, and
    `session.run_node` and `node._realised` over their whole window — because a refusal
    one path makes and another does not is a divergence waiting to happen. `run_subprocess`
    makes the split half of it once more in the parent, before the child exists, so a card
    refuses with a message an operator can read instead of crashing with one only the run
    records. The split check runs over the whole delivered span: a split inside the warmup
    prefix adjusts no position, but the venue restates the book at it, and an undeclared
    one would leave the harness's prices wrong without a refusal.
    """
    splits.unscheduled(instruments, chain.from_iterable(groups), request.span)
    marks = Marks(request, VenueModel.model_validate(dict(request.venue_model)))
    marks.take(chain.from_iterable(groups))
    marks.check()
    return marks


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


def _funding_of(point: object) -> Any:
    """The funding settlement a point is, the catalog's wrapper taken off, or `None`."""
    from kanso.data.types import Funding

    inner = getattr(point, "data", point)
    return inner if isinstance(inner, Funding) else None


def _range_of(point: object) -> tuple[float | None, float | None]:
    """The adverse range a point spans: a bar's low and high, a quote's bid and ask, a
    trade's own price on both sides, and nothing for a point that prices nothing."""
    from nautilus_trader.model.data import Bar, QuoteTick, TradeTick

    if isinstance(point, Bar):
        return float(point.low), float(point.high)
    if isinstance(point, QuoteTick):
        return float(point.bid_price), float(point.ask_price)
    if isinstance(point, TradeTick):
        price = float(point.price)
        return price, price
    return None, None


def _ordered(groups: Sequence[Sequence[object]]) -> tuple[object, ...]:
    """The window as the engine delivers it: stable `ts_init` order, then flush markers."""
    from kanso.nautilus.cross_section import ordered

    return ordered(groups)


def _load_stream(engine: Any, points: Sequence[object]) -> None:
    """Add the ordered stream in homogeneous type runs, then sort once.

    The engine assumes one type per `add_data` call. Markers are `CustomData` and need
    `CLIENT_ID`; bars, quotes and trades do not. Consecutive same-type runs keep the
    insertion order `sort_data`'s stable sort then preserves, so a marker that follows
    its cohort in the ordered stream still follows it after the sort.
    """
    from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
    from nautilus_trader.model.identifiers import ClientId

    if not points:
        return
    run: list[object] = []
    for point in points:
        if run and type(point) is not type(run[0]):
            _add_run(engine, run, Bar, QuoteTick, TradeTick, ClientId)
            run = []
        run.append(point)
    _add_run(engine, run, Bar, QuoteTick, TradeTick, ClientId)
    engine.sort_data()


def _add_run(
    engine: Any,
    run: Sequence[object],
    bar_cls: type,
    quote_cls: type,
    trade_cls: type,
    client_id_cls: type,
) -> None:
    """One homogeneous `add_data` call, unsorted."""
    first = run[0]
    from nautilus_trader.model.data import OrderBookDelta

    plain = type(first) in (bar_cls, quote_cls, trade_cls, OrderBookDelta)
    engine.add_data(
        list(run),
        client_id=None if plain else client_id_cls(CLIENT_ID),
        sort=False,
    )


# --- the extraction ----------------------------------------------------------


def multipliers_of(instruments: Iterable[Any]) -> dict[str, float]:
    """Each resolved definition's contract multiplier, by instrument id.

    The one table every notional kanso strikes is read from: a fill's `qty x px x
    multiplier` in the extraction below, and the day's traded volume a certification holds
    that fill to (`kanso.certify.run._daily_volume`), so a contract and a share are compared
    in the same unit whichever the bar counted.
    """
    return {str(instrument.id): float(instrument.multiplier) for instrument in instruments}


def _extract(request: RunRequest, engine: Any, marks: Marks) -> CardRun:
    """The one measured object: returns, equity, trades and fills with costs applied, and
    the book policy applied once at each period end."""
    model = VenueModel.model_validate(dict(request.venue_model))
    cache = engine.cache
    multipliers = multipliers_of(cache.instruments())
    positions = _positions(cache)
    events, owners = _fill_events(positions)
    marks.price(events)
    fills = tuple(_fill(event, multipliers, marks.half(event), model) for event in events)
    by_position: dict[int, list[Fill]] = {}
    for owner, made in zip(owners, fills, strict=True):
        by_position.setdefault(owner, []).append(made)
    schedules = {
        str(instrument.id): splits.schedule_of(instrument) for instrument in cache.instruments()
    }
    curve = _equity(request, marks, fills, multipliers, _adjusted(positions))
    trades = _trades(positions, by_position, multipliers, schedules, curve.funding)
    return CardRun(
        window=request.window,
        period=request.period,
        period_ends_ns=curve.ends,
        returns=curve.returns,
        equity=curve.equity,
        trades=trades,
        fills=fills,
        capital=request.capital,
        currency=model.currency,
        venue_model=dict(request.venue_model),
        held=curve.held,
        cushion=curve.cushion,
        carry=curve.carry,
        worst_ratio=curve.worst_ratio,
        funding=curve.funding,
    )


@dataclass(frozen=True)
class _Curve:
    """What `_equity` strikes at every period end, each series parallel to `ends`.

    The three book series are empty when the hypothesis declares no policy, so a run
    without one is the run it always was, byte for byte.
    """

    ends: tuple[int, ...]
    returns: tuple[float, ...]
    equity: tuple[float, ...]
    held: tuple[Held, ...]
    cushion: tuple[float, ...] = ()
    carry: tuple[float, ...] = ()
    worst_ratio: tuple[float | None, ...] = ()
    funding: tuple[FundingPayment, ...] = ()


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
    half: float,
    model: VenueModel,
) -> Fill:
    """One execution, with the cost this venue model charges it, applied once.

    A fill the venue reports as a maker's pays the model's `maker_bps` when it states one;
    every other fill pays commission, slippage and half the spread, and the per-share
    commission on each share when the model states one; a sale pays the sell-side fees the
    model states on top, maker or taker (`costs.fill_cost`).
    """
    from nautilus_trader.model.enums import LiquiditySide, order_side_to_str

    instrument_id = str(event.instrument_id)
    qty = float(event.last_qty)
    px = float(event.last_px)
    multiplier = multipliers.get(instrument_id, 1.0)
    notional = qty * px * multiplier
    maker = event.liquidity_side == LiquiditySide.MAKER
    side = order_side_to_str(event.order_side)
    costs = model.costs
    cost = fill_cost(
        notional,
        qty,
        costs.commission_bps,
        costs.slippage_bps,
        half,
        costs.maker_bps,
        costs.commission_per_share,
        maker=maker,
        sell=side == "SELL",
        sell_fee_bps=costs.sell_fee_bps,
        sell_fee_per_share=costs.sell_fee_per_share,
    )
    return Fill(
        ts_ns=int(event.ts_event),
        instrument_id=instrument_id,
        side=side,
        qty=qty,
        px=px,
        cost=cost,
        maker=maker,
        multiplier=multiplier,
    )


def _trades(
    positions: Sequence[Any],
    by_position: Mapping[int, Sequence[Fill]],
    multipliers: Mapping[str, float],
    schedules: Mapping[str, Sequence[splits.Split]] | None = None,
    funding: Sequence[FundingPayment] = (),
) -> tuple[Trade, ...]:
    """Closed positions as trades, netted of the costs of the fills that made them and of
    the funding they paid.

    A position still open when the window closes is not a trade: its profit is in the
    equity curve as an unrealised mark, and it becomes a trade only when it closes.

    The size, the two average prices and the profit are computed from the position's own
    fills and its split adjustments (`kanso.nautilus.splits.ledger`) rather than read off
    the engine's `peak_qty`, `avg_px_open` and `realized_pnl`. Those three are what a
    position *would* have been had no corporate action touched it: the first two are
    `cdef readonly` and stay in the share count the position opened in, and the third is
    computed against the second, so a position that spanned a one-for-ten reverse split
    reported a 9,000 profit on a 50 loss. With no adjustment the ledger reproduces the
    engine's own numbers exactly, which is why there is one arithmetic here and not two.

    What a split paid out in lieu is in the profit, realised at the split. A position the
    payout left flat closed there, and the engine dated no close, so it is dated by the
    adjustment that closed it.

    The trade carries the instrument's multiplier, as its fills do, so its opening value is
    in the currency its profit is.

    A funding payment belongs to the trade of its instrument held at its settlement:
    opened before the instant and closed at or after it. Under netting one position of a
    name is open at a time, and a fill at the instant is not held there (`_equity`) — so a
    position opened at a settlement pays nothing there, and one closed at it still pays.
    `cost` stays the fills' cost alone, so a cost model re-applied to the fills leaves the
    funding where it was booked.
    """
    paid_at: dict[str, tuple[list[int], list[float]]] = {}
    for payment in funding:
        times, amounts = paid_at.setdefault(payment.instrument_id, ([], []))
        times.append(payment.ts_ns)
        amounts.append(payment.paid)
    trades: list[Trade] = []
    for index, position in enumerate(positions):
        if not position.is_closed:
            continue
        fills = tuple(by_position.get(index, ()))
        cost = fsum(fill.cost for fill in fills)
        name = str(position.instrument_id)
        schedule = () if schedules is None else schedules.get(name, ())
        multiplier = multipliers.get(name, 1.0)
        book = splits.ledger(splits.moves_of(position, schedule), multiplier)
        opened = min(fills, key=lambda fill: fill.ts_ns) if fills else None
        closed = int(position.ts_closed) or max(
            (int(event.ts_event) for event in position.adjustments), default=0
        )
        times, amounts = paid_at.get(name, ([], []))
        opened_ns = int(position.ts_opened)
        paid = fsum(
            amounts[bisect.bisect_right(times, opened_ns) : bisect.bisect_right(times, closed)]
        )
        trades.append(
            Trade(
                opened_ns=opened_ns,
                closed_ns=closed,
                instrument_id=name,
                qty=book.peak if opened is None or opened.side == "BUY" else -book.peak,
                avg_open=book.avg_open,
                avg_close=book.avg_close,
                pnl_net=book.realized - cost - paid,
                cost=cost,
                fills=fills,
                multiplier=multiplier,
                funding=paid,
            )
        )
    return tuple(trades)


def _adjusted(positions: Sequence[Any]) -> tuple[tuple[int, str, float, float], ...]:
    """Every split adjustment the run applied, once each, as `(ts, instrument, change,
    paid)`: the quantity change and the cash the split paid in lieu of the fraction it left.

    Read off the positions the same way the fills are, and deduplicated by event id for
    the same reason: a netting position that closed and reopened lives in the cache twice,
    once as the snapshot taken before the reset and once as the live object.
    """
    seen: set[str] = set()
    found: list[tuple[int, str, float, float]] = []
    for position in positions:
        for event in position.adjustments:
            key = str(event.id)
            if key in seen or event.quantity_change is None:
                continue
            seen.add(key)
            paid = 0.0 if event.pnl_change is None else event.pnl_change.as_double()
            found.append(
                (
                    int(event.ts_event),
                    str(event.instrument_id),
                    float(event.quantity_change),
                    paid,
                )
            )
    return tuple(sorted(found))


def _equity(
    request: RunRequest,
    marks: Marks,
    fills: Sequence[Fill],
    multipliers: Mapping[str, float],
    adjustments: Sequence[tuple[int, str, float, float]] = (),
) -> _Curve:
    """The period ends and the equity struck at each, from cash and marked positions.

    A period exists when it holds at least one data event, and ends at the last one it
    holds. Equity there is the capital, less what the fills paid for what they bought and
    less the costs they were charged, plus every open position marked at the last price
    published for it. That is cash plus market value: the number a drawdown, a return and
    a Sharpe are all read from.

    A split changes what is held without any fill saying so, so the adjustments the run
    applied are folded into the same running count: the quantity change is taken from the
    position's own ledger rather than recomputed from a ratio, so what the curve marks is
    exactly the share count the engine went on to trade. Without it the ex-day's price is
    marked against the pre-split count and the curve reports the whole action as return —
    measured on a one-for-ten reverse split, 190,450 against a true 99,950. What the split
    paid in lieu of the fraction it left goes into cash at the same instant, so across a
    split the curve moves only with the price.

    **A perpetual's funding is booked here, once, at its settlement instant**
    (`kanso.nautilus.costs.funding_payment`): the realised rate on the signed quantity held
    then, marked at the instrument's last print at or before the instant — of several at
    that instant, the greatest, as a period's mark is chosen — and times its multiplier,
    taken out of cash. What is held at the instant is every fill stamped before it, and no
    fill stamped at it — deliberately not the `<=` rule that books a period's fills up to
    and including its end. The realised rate is public at the settlement: the sleeve is
    handed it there, and the engine stamps the fill of an order sent in answer at that same
    instant, so under `<=` a position opened because the rate was known would collect it
    and one closed because of it would escape it. Which point of an instant an order
    answered is recorded nowhere either path can read — a stage node stamps an order by its
    live clock, not by the data — so no fill of the instant is held, a resting order placed
    earlier that fills there included: what a settlement sees was decided before its rate
    was public. A split adjustment at the instant is booked after the settlement for the
    same reason. The payment is inside the return and the equity of the period holding the
    instant, and each one is recorded (`CardRun.funding`) with what was held; a settlement
    at which nothing was held pays and records nothing. A settlement in the warmup prefix is
    not booked, since nothing is held before the open.

    A stream may begin before the window with the warmup prefix. No period ends inside it
    — the first period is the window's first — but its points are consumed for the marks,
    so a name that last printed in the prefix is marked at that print in the first period
    rather than at nothing. No fill may precede the open: the harness drops every order
    over the prefix, and one that reached the venue anyway is refused here rather than
    measured.

    **The book policy is applied here, once, at each end** (`kanso.nautilus.costs`), in
    this order. The carry first: the yearly rate on what the marked book holds above its
    equity, over the span since the previous end — or since the window's open for the
    first, or since the instant a stage restart resumed when that is later — taken out of
    cash, so it is in the period's return. Then the maintenance
    ratio, on the cash the carry left and the end's holdings valued at the period's
    adverse extreme — a long at the lowest low, a short at the highest high, printed at or
    after the open since the previous end, whether or not the position was held at that
    instant, so it is a floor like `max_hold`'s; a name that did not print is valued at
    its mark. Then the reset, at the first end of a new calendar month: the return is
    struck on the equity before the transfer, the transfer moves cash between the book
    and the cushion, and the equity recorded is the book after it. The harness settles
    the same period from the same functions when the first point of the next period is
    delivered, so a sleeve's `balance` read at a period's last point is the equity struck
    here before that end's carry and transfer, and any later read includes them.
    """
    opens, _ = request.bounds
    policy = policy_of(request.hyp.book)
    early = [fill for fill in fills if fill.ts_ns < opens]
    if early:
        raise ValidationError(
            f"fills: a fill at {early[0].ts_ns} precedes the window opening {opens}; the "
            "harness drops every order over the warmup prefix, so nothing may fill before it"
        )
    periods = marks.periods()
    settlements = tuple(item for item in marks.settlements() if item[0] >= opens)
    paid: list[FundingPayment] = []
    ends = tuple(fold.end for fold in periods)
    marked_at: dict[str, float] = {key: price for key, (_, price) in marks.prefix.marks.items()}
    held: dict[str, float] = {}
    cash = request.capital
    cushion = request.cushion
    equity: list[float] = []
    returns: list[float] = []
    cushions: list[float] = []
    carries: list[float] = []
    worsts: list[float | None] = []
    open_at: list[Held] = []
    previous_end = request.settled_ns
    previous_equity = request.capital
    carried_from = request.carried_from_ns
    fill = 0
    split = 0
    settled = 0
    for fold in periods:
        end = fold.end
        marked_at.update({key: price for key, (_, price) in fold.marks.items()})
        lows, highs = fold.lows, fold.highs
        while settled < len(settlements) and settlements[settled][0] <= end:
            ts, key, rate, mark = settlements[settled]
            cash, fill, split = _booked_to(ts - 1, fills, adjustments, held, cash, fill, split)
            qty = held.get(key, 0.0)
            if qty:
                payment = funding_payment(qty, mark, multipliers.get(key, 1.0), rate)
                cash -= payment
                paid.append(FundingPayment(ts, key, qty, rate, payment))
            settled += 1
        cash, fill, split = _booked_to(end, fills, adjustments, held, cash, fill, split)
        marked: list[float] = []
        adverse: list[float] = []
        for key in sorted(held):
            qty = held[key]
            mark = marked_at.get(key, 0.0)
            multiplier = multipliers.get(key, 1.0)
            worth = qty * mark * multiplier
            marked.append(worth)
            if qty:
                open_at.append(Held(ts_ns=end, instrument_id=key, qty=qty, notional=abs(worth)))
                floor = lows.get(key, mark) if qty > 0 else highs.get(key, mark)
                adverse.append(qty * floor * multiplier)
        value = cash + fsum(marked)
        if policy is not None:
            charged = carry(
                fsum(abs(worth) for worth in marked),
                value,
                policy.financing_rate_bps,
                end - (carried_from if previous_end is None else max(previous_end, carried_from)),
            )
            cash -= charged
            value -= charged
            worsts.append(maintenance_ratio(cash, adverse))
            carries.append(charged)
            moved = 0.0
            if policy.resets and month_turned(previous_end, end):
                moved, cushion = reset(value, request.capital, cushion)
                cash += moved
            returns.append(value - previous_equity)
            value += moved
            if policy.resets:
                cushions.append(cushion)
        else:
            returns.append(value - previous_equity)
        equity.append(value)
        previous_equity = value
        previous_end = end
    return _Curve(
        ends=ends,
        returns=tuple(returns),
        equity=tuple(equity),
        held=tuple(open_at),
        cushion=tuple(cushions),
        carry=tuple(carries),
        worst_ratio=tuple(worsts),
        funding=tuple(paid),
    )


def _booked_to(
    until: int,
    fills: Sequence[Fill],
    adjustments: Sequence[tuple[int, str, float, float]],
    held: dict[str, float],
    cash: float,
    fill: int,
    split: int,
) -> tuple[float, int, int]:
    """Book every fill and every split adjustment stamped at or before `until` into `held`
    and cash, from the two cursors given; the cash and the cursors after them."""
    while fill < len(fills) and fills[fill].ts_ns <= until:
        made = fills[fill]
        signed = made.qty if made.side == "BUY" else -made.qty
        cash -= signed * made.px * made.multiplier + made.cost
        held[made.instrument_id] = held.get(made.instrument_id, 0.0) + signed
        fill += 1
    while split < len(adjustments) and adjustments[split][0] <= until:
        _ts, key, change, paid = adjustments[split]
        held[key] = held.get(key, 0.0) + change
        cash += paid
        split += 1
    return cash, fill, split


# --- the two entry points ----------------------------------------------------


def run(request: RunRequest, catalog_path: Path) -> RunResult:
    """Run in this process, over the requested window and no other.

    The window must be one the hypothesis declares; the forward window is never
    backtested, and a window it does not declare is refused before anything is read.
    """
    stage_of(request.hyp, request.window)
    instruments, groups = window_data(request, catalog_path)
    return execute(request, instruments, groups)


def run_subprocess(
    request: RunRequest,
    catalog_path: Path,
    workdir: Path,
    extensions: Sequence[tuple[str, str]] = (),
) -> RunResult:
    """Run a card in a child of its own, supervised, with no path to any catalog.

    The window must be the research window: this is the embargo, and it is a refusal in
    code rather than a rule anyone has to remember. The parent reads that window from the
    catalog and hands the points to the child, which starts in a new session with an
    environment allow-list, so nothing in the card can reach data the run was not given.

    `extensions` are the workspace extensions this process imported, as `kanso.ext.imported`
    answered — each one's directory and module name. The child imports them before it
    unpickles a point, so a custom type an extension defines is registered there and its
    class found under the name it was pickled by (`main`). They travel in the payload, not
    the environment: the child is handed where an extension lives, never the catalog, and
    still inherits no credential.

    The payload is written to `<workdir>/.card/`, the lane's own transfer directory, as
    pickles in sequence on one file — the extensions, the request and its instruments, then
    one session's points at a time as the parent reads them — so no process holds more than
    a session of the window: the parent lets each session go once it is on disk, and the
    child takes them one at a time. The directory is emptied before the card and removed
    after it, so a lane killed mid-card leaves at most one payload behind, and its next
    card reclaims it: a payload is the whole window's points on disk, hundreds of megabytes
    for a window of minute bars and gigabytes for one of ticks.
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
    _refuse_if_interrupted()
    room = _card_room(workdir)
    try:
        _stage(request, catalog_path, room, extensions)
        gc.collect()
        return _supervised(request, room, workdir)
    finally:
        shutil.rmtree(room, ignore_errors=True)


def _stage(
    request: RunRequest, catalog_path: Path, room: Path, extensions: Sequence[tuple[str, str]]
) -> None:
    """Read the window a session at a time and write each session's points as they are read.

    Returns nothing on purpose. The points are the parent's largest allocation by a wide
    margin — a year of five-second bars is gigabytes, and a month of one name's quote
    changes and prints is ten — and the child holds them for the whole card, so neither
    process ever holds more than a session of them: the parent reads one calendar day of
    the delivered span, checks it, pickles it and lets it go, and the child unpickles them
    one at a time (`main`, `execute_chunked`). Measured before this: a lane held 1.4 GB of
    an eighteen-month five-second window beside a 2.7 GB card, six such lanes put a 16 GB
    machine into swap, and a half-month of ticks did the same beside three.

    The refusals a whole-window read made are made here, in the parent, where a refusal is
    a refusal rather than a crash the run records: a split no definition schedules, an
    overlay grain the catalog does not hold, and a window it holds nothing for.
    """
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    hyp = request.hyp
    catalog = ParquetDataCatalog(str(catalog_path))
    held = _held(catalog, hyp)
    instruments = tuple(held[name] for name in sorted(hyp.universe))
    opens, closes = request.delivered
    measured, _ = request.bounds
    scope = _scope_days(catalog, hyp, opens, closes - 1)
    loaded: dict[str, int] = {grain: 0 for grain in _bar_grains(request)}
    inside = False
    with (room / REQUEST_FILE).open("wb") as handle:
        pickle.dump(
            {"extensions": [list(source) for source in extensions]},
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
        pickle.dump(
            {"request": request.plain(), "instruments": instruments},
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
        for day_start in range(midnight_ns(day_of(opens)), closes, NS_PER_DAY):
            start = max(opens, day_start)
            end = min(closes, day_start + NS_PER_DAY) - 1
            groups, counts = _window_points(request, catalog, held, scope, start, end)
            for grain, count in counts.items():
                loaded[grain] += count
            if not groups:
                continue
            splits.unscheduled(instruments, chain.from_iterable(groups), request.span)
            inside = inside or any(
                int(point.ts_init) >= measured  # type: ignore[attr-defined]
                for point in chain.from_iterable(groups)
            )
            pickle.dump({"groups": groups}, handle, protocol=pickle.HIGHEST_PROTOCOL)
            del groups
    _refuse_missing_grain(request, loaded)
    if not inside:
        raise PreconditionError(
            f"data: the catalog holds nothing for {hyp.id} over "
            f"{request.window[0]}..{request.window[1]}",
            remedy="run `kanso data load` for the window, then take a snapshot",
        )


def _card_room(workdir: Path) -> Path:
    """The lane's one transfer directory, emptied for the card about to use it: whatever a
    card killed with its lane left there is reclaimed here, and nothing accumulates."""
    room = workdir / CARD_ROOM
    shutil.rmtree(room, ignore_errors=True)
    room.mkdir()
    return room


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
    """Start the child, watch its clock and its memory, and read back what it produced.

    The child is told which process started it, and ends itself when that process is no
    longer its parent (`end_with`): it leads its own session, so a lane killed outright
    cannot take it down, and without that it would run on with nobody watching its budget.
    """
    result_path = room / RESULT_FILE
    _refuse_if_interrupted()
    breach, peak_gb, wall_s = watched(
        [
            sys.executable,
            "-c",
            _BOOTSTRAP,
            str(room / REQUEST_FILE),
            str(result_path),
            str(os.getpid()),
        ],
        cwd=workdir,
        errors=room / "stderr.txt",
        env=child_env(),
        budget_s=request.budget_s,
        mem_cap_gb=request.mem_cap_gb,
    )
    tail = _tail((room / "stderr.txt").read_text(encoding="utf-8", errors="replace"))
    if breach == INTERRUPTED:
        raise _interrupted()
    if breach is not None:
        return _crashed(request, wall_s, peak_gb, breach, tail)
    return _reported(request, result_path, wall_s, peak_gb, tail)


def watched(
    argv: Sequence[str],
    *,
    cwd: Path,
    errors: Path,
    env: Mapping[str, str] | None,
    budget_s: float | None,
    mem_cap_gb: float | None,
) -> tuple[str | None, float, float]:
    """Start a child in a session of its own, watch it as a card is watched, and say how
    it ended: the breach that killed it (`None` when it exited by itself), the peak resident
    memory it reached in gibibytes, and its wall time.

    Whatever it writes to a stream goes to `errors`. `env` is the environment it starts
    with, the parent's own when `None`. The watch is `_watch`'s: the wall-time and
    resident-memory bounds, this process's stop, and the `wanted` checks it is inside. A
    card is one child watched this way and a stall's certification another
    (`kanso.certify.child`).
    """
    started = time.monotonic()
    with errors.open("wb") as stream:
        child = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=None if env is None else dict(env),
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
        )
        breach, peak_gb = _watch(child, budget_s, mem_cap_gb)
    return breach, peak_gb, time.monotonic() - started


def _interrupted() -> PreconditionError:
    """Why a card did not run to its end: the process running it was told to stop."""
    return PreconditionError(
        "the card was interrupted: the lane running it was told to stop",
        remedy="start the daemon again; the run resumes from its last card",
    )


def _refuse_if_interrupted() -> None:
    """Start no card in a process that has been told to stop, or for work not wanted.

    Asked before the window is read and again before the child is spawned, because a stop
    that lands while a lane is reading a window, or waiting on a model, is still a stop: a
    lane that went on to start the card would only have it killed at the watcher's first
    poll, or — killed itself in the meantime — leave it running.
    """
    if _INTERRUPT.is_set():
        raise _interrupted()
    _refuse_if_unwanted()


def _refuse_if_unwanted() -> None:
    """Ask every `wanted` check this process is inside; the first that refuses raises."""
    for check in tuple(_WANTED):
        check()


def _watch(
    child: Any, budget_s: float | None, mem_cap_gb: float | None
) -> tuple[str | None, float]:
    """Wait for the child, killing its process group when it overruns either bound, when
    this process has been told to stop, or when a `wanted` check refuses — which is raised
    once the child is reaped."""
    started = time.monotonic()
    checked = asked = started
    breach: str | None = None
    while True:
        pid, status, usage = os.wait4(child.pid, os.WNOHANG)
        if pid != 0:
            child.returncode = os.waitstatus_to_exitcode(status)
            return breach, usage.ru_maxrss * _MAXRSS_BYTES / GIB
        now = time.monotonic()
        if _INTERRUPT.is_set():
            breach = INTERRUPTED
        elif budget_s is not None and now - started > budget_s:
            breach = BUDGET
        elif mem_cap_gb is not None and now - checked >= MEMORY_POLL_S:
            checked = now
            if _resident_gb(child.pid) > mem_cap_gb:
                breach = MEMORY
        if breach is not None:
            return breach, _killed(child)
        if now - asked >= WANTED_POLL_S:
            asked = now
            try:
                _refuse_if_unwanted()
            except BaseException:  # a card never outlives the watch on it, however it ended
                _killed(child)
                raise
        time.sleep(POLL_S)


def _killed(child: Any) -> float:
    """Kill the child's process group, reap it, and return the peak it reached."""
    _kill(child.pid)
    _pid, status, usage = os.wait4(child.pid, 0)
    child.returncode = os.waitstatus_to_exitcode(status)
    return usage.ru_maxrss * _MAXRSS_BYTES / GIB


def _kill(pid: int) -> None:
    """Kill the child's whole process group; it leads one of its own."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def end_with(parent: int) -> None:
    """In a card, or a stall's certification: end the process the moment `parent` is no
    longer the one it answers to.

    A card leads its own session so that its watcher can kill it without killing the lane,
    which also means the lane cannot take it down by dying: a lane killed outright — by the
    daemon after its grace, by `research stop`'s last resort, by the kernel — leaves the
    card running, unbudgeted, with its result going nowhere. The kernel hands an orphan to
    another parent, so the card asks every `PARENT_POLL_S` whose child it is, and exits the
    moment the answer is not the lane that started it — including at once, when the lane
    was gone before the card began. It exits without cleaning up, because nothing is left
    to read what it would have written. A certification child is watched by its lane the
    same way and ends itself the same way (`kanso.certify.child`).
    """
    while os.getppid() == parent:
        time.sleep(PARENT_POLL_S)
    os._exit(ORPHANED)


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
        return _crashed(
            request, wall_s, peak_gb, EXCEPTION, reported["traceback"], reported["remedy"]
        )
    refused = reported.get("refused")
    return RunResult(
        run=reported["run"],
        wall_s=wall_s,
        peak_mem_gb=peak_gb,
        intents=reported["intents"],
        refused=None if refused is None else Refusal(**refused),
    )


def _crashed(
    request: RunRequest,
    wall_s: float,
    peak_gb: float,
    reason: str,
    tail: str | None,
    remedy: str | None = None,
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
        remedy=remedy,
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


def _chunks(handle: Any) -> Iterator[tuple[tuple[object, ...], ...]]:
    """Each session's groups as the parent pickled them, one at a time, until the file ends."""
    while True:
        try:
            yield pickle.load(handle)["groups"]
        except EOFError:
            return


def main(argv: Sequence[str]) -> int:
    """The child: read the request and its data, run it, and write the result back.

    Every failure is reported as a crash with the tail of its traceback rather than as a
    non-zero exit alone, because a card that raised is a card whose reason the loop has
    to be able to read. An order the sizing rule refused is not a crash: it comes back as
    a value, the refusal itself, and the card records it as a gate. A failure kanso itself
    raised also reports its remedy, as a value
    rather than as a line of a traceback: the parent decides what to tell the operator to
    do about a card that did not run, and it can only choose the right thing if the cause
    is what names it.

    The third argument is the pid of the process that started the card, which it outlives
    by at most `PARENT_POLL_S` (`end_with`).

    The payload is a sequence of pickles on one file. The first names the workspace
    extensions the parent imported, and they are imported here before anything else is
    unpickled, because a point of an extension's custom type is an instance of a class that
    exists only once its module has been imported, under the name it was pickled by. The
    second is the request and its instruments; every one after it is one session's points,
    read one at a time as the run consumes them (`_chunks`), so the child holds a session
    beside the marks and never the window.
    """
    from kanso.ext import reimport

    request_path, result_path = Path(argv[0]), Path(argv[1])
    threading.Thread(
        target=end_with, args=(int(argv[2]),), name="kanso-card-parent", daemon=True
    ).start()
    with request_path.open("rb") as handle:
        handed = pickle.load(handle)
        reimport((str(directory), str(name)) for directory, name in handed["extensions"])
        payload = pickle.load(handle)
        try:
            result = execute_chunked(payload["request"], payload["instruments"], _chunks(handle))
        except SizingError as refused:
            result_path.write_bytes(
                pickle.dumps(
                    {
                        "ok": True,
                        "run": _empty(payload["request"]),
                        "intents": (),
                        "refused": refused.refusal.payload(),
                    }
                )
            )
            return 0
        except Exception as failure:
            result_path.write_bytes(
                pickle.dumps(
                    {
                        "ok": False,
                        "traceback": _tail(traceback.format_exc()),
                        "remedy": failure.remedy if isinstance(failure, KansoError) else None,
                    }
                )
            )
            return 1
    result_path.write_bytes(
        pickle.dumps({"ok": True, "run": result.run, "intents": result.intents})
    )
    return 0
