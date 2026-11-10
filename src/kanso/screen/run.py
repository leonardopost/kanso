"""`kanso screen run`: validate, get the data, pin, measure, judge, record.

The order is the contract.

1. **Validate** the file (`kanso.screen.files`), and refuse a free window that meets a
   registered certification span on any instrument it reads, a signal's too
   (`kanso.screen.embargo`), before anything is read.
2. **Get the data.** A series no adapter serves is refused, naming the venue and type to
   build an adapter for; every fetchable one is fetched through the adapter that serves it
   and a snapshot taken (`kanso.screen.data`). Nothing is skipped.
3. **Pin** the newest snapshot covering every series over the window (`kanso.screen.pin`),
   taking one when what is held has not been frozen yet.
   The screen's bytes, that snapshot and the measure library's version are the result's key;
   a result already recorded under them is returned as it was, and nothing is read.
4. **Measure**, one session at a time: every leg of the session is read once through the
   runner's reader, every measure takes its cells' values from it, and the session is let go.
   What a measure fits first — a spread's hedge, a catch-up's response — is fitted before, on
   the sessions of its fit span. A condition keeps each follower's open and close of the
   session, and its signals are read afterwards in a pass of their own, a day at a time, as of
   each decision (`kanso.screen.condition`). A calendar control no calendar on file can read is
   refused once the followers' definitions are held — after step 2's fetch, which may already
   have backfilled a series and frozen a snapshot, and before the pin or any session is read.
5. **Judge** each measure's cells across sessions with its max-T null (`kanso.screen.nulls`) —
   session sign flips, or, for a condition, the null it declares — and, when the file declares
   a verdict, each cell against it. Each measure's assumption is stated in the result.
6. **Record** the result in `state.db`, render it beside the screen, and append `screened`.

No model is called anywhere here.
"""

from __future__ import annotations

import resource
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import numpy as np

from kanso.data.catalog import open_catalog
from kanso.data.types import BUILTIN_TYPES
from kanso.errors import PreconditionError
from kanso.schemas import DateWindow
from kanso.schemas.screen import (
    CatchUp,
    CatchUpStats,
    Cell,
    Condition,
    LeadLag,
    Response,
    ResponseStats,
    Screen,
    ScreenResult,
    ScreenVerdict,
    SeriesRead,
    Summary,
    span_ns,
)
from kanso.screen import (
    catch_up,
    condition,
    data,
    embargo,
    fit,
    hurdle,
    lead_lag,
    nulls,
    pin,
    records,
    response,
    sessions,
    signals,
)
from kanso.screen.files import validate
from kanso.screen.library import screen_version

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

ASSUMPTION: Final = "sessions are roughly independent of each other"
"""What a session measure's inference rests on, stated in the result; a condition states its
null's own (`kanso.screen.condition.assumption`)."""

NS_PER_SECOND: Final = 1_000_000_000

GIB: Final = 1024**3
_MAXRSS_BYTES: Final = 1 if sys.platform == "darwin" else 1024
"""`ru_maxrss` is bytes on macOS and kibibytes on Linux."""


@dataclass(frozen=True)
class Outcome:
    """What one `screen run` produced: the result, where it is rendered, what was fetched."""

    result: ScreenResult
    path: Path
    stored: bool
    fetched: tuple[data.Fetched, ...]


def run(ws: Workspace, store: StateStore, path: Path) -> Outcome:
    """Measure the screen at `path`, or return the result its pins already hold."""
    started = time.monotonic()
    valid = validate(ws, store, path)
    screen, window = valid.screen, valid.window
    embargo.refuse_certification_data(ws, store, screen, window)
    plans = data.plan(ws, store, screen, window)
    _refuse_unresolved(plans)
    _refuse_unserved(plans)
    fetched = data.fetch(ws, store, screen, window, plans)
    catalog = open_catalog(ws)
    held = sessions.definitions(catalog, screen)
    condition.refuse_uncalendared(screen, held, window)
    snapshot = pin.covering(
        ws, store, screen, window, {**signals.definitions(catalog, screen), **held}
    )
    version = screen_version()
    stored = records.stored(store, valid.sha, snapshot.snapshot_id, version)
    if stored is not None:
        return Outcome(stored, records.render(ws, stored), True, fetched)
    store.put_blob(valid.source)
    read = [_read(item, plans) for item in data.plan(ws, store, screen, window)]
    stamps = {leg: item.timestamps for item in read for leg in item.legs}
    priced = hurdle.hurdles(ws, screen, valid.hypothesis, held) if _trades(screen) else None
    context = Context(
        screen=screen,
        sha=valid.sha,
        snapshot_id=snapshot.snapshot_id,
        draws=ws.config.screen.draws,
        folds=ws.config.research.folds,
        stamps=stamps,
        betas=(betas := _fit(ws, screen, window, catalog, held)),
        slopes=_slopes(ws, screen, window, catalog, held, betas),
        window=window,
        definitions=held,
    )
    cells = _measure(ws, context, window, catalog, held, priced)
    session, stated = _assumptions(screen)
    result = ScreenResult(
        screen=screen.id,
        sha=valid.sha,
        snapshot=snapshot.snapshot_id,
        version=version,
        window=DateWindow(start=window[0], end=window[1]),
        hyp=screen.hyp,
        hypothesis_sha=_pinned_sha(store, screen.hyp),
        draws=ws.config.screen.draws,
        folds=ws.config.research.folds,
        assumption=session,
        assumptions=stated,
        created_at=datetime.now(tz=UTC),
        wall_s=round(time.monotonic() - started, 3),
        peak_mem_gb=round(_peak_gb(), 3),
        series=read,
        cells=cells,
        summary=_summary(screen.verdict, cells),
    )
    records.record(store, result, list(screen.instruments))
    return Outcome(result, records.render(ws, result), False, fetched)


def _assumptions(screen: Screen) -> tuple[str | None, list[str]]:
    """The session measures' assumption, `None` when the screen has no session measure, and
    what each measure's inference rests on, in the order the measures are declared."""
    stated = [
        condition.assumption(measure) if isinstance(measure, Condition) else ASSUMPTION
        for measure in screen.measures
    ]
    sessions = any(not isinstance(measure, Condition) for measure in screen.measures)
    return (ASSUMPTION if sessions else None), stated


@dataclass(frozen=True)
class Context:
    """What every cell of one run is measured and judged under."""

    screen: Screen
    sha: str
    snapshot_id: str
    draws: int
    folds: int
    stamps: dict[str, str]
    betas: dict[str, float]
    slopes: dict[int, dict[catch_up.Pair, float | None]]
    window: tuple[date, date]
    definitions: dict[str, Any]


def _measure(
    ws: Workspace,
    context: Context,
    window: tuple[date, date],
    catalog: Any,
    held: dict[str, Any],
    hurdles: hurdle.Hurdles | None,
) -> list[Cell]:
    """Every cell of every measure, judged across the window's sessions.

    Every leg of a session is read once and handed to every measure; the session is let go
    before the next is read.
    """
    screen = context.screen
    days = sessions.days(screen, window)
    bounds = sessions.window_ns(window)
    overlap = screen.clock.hours == "overlap"
    names = list(screen.legs)
    columns: list[list[Any]] = [[] for _ in screen.measures]
    stale: list[list[list[dict[str, float]]]] = [[] for _ in screen.measures]
    for day in days:
        series = sessions.read(catalog, held, screen, names, day, window)
        opens, closes = sessions.hours_of(screen, day)
        span = (max(opens, bounds[0]), min(closes, bounds[1]))
        for index, measure in enumerate(screen.measures):
            if isinstance(measure, LeadLag):
                values, staleness = lead_lag.session(
                    screen, measure, series, span, overlap, context.betas
                )
                columns[index].append(values)
                stale[index].append(staleness)
            elif isinstance(measure, Condition):
                columns[index].append(condition.marks(screen, measure, series))
            elif isinstance(measure, CatchUp):
                assert hurdles is not None
                columns[index].append(
                    catch_up.session(
                        screen,
                        measure,
                        series,
                        span,
                        overlap,
                        context.betas,
                        context.slopes[index],
                        hurdles,
                    )
                )
            else:
                assert hurdles is not None
                columns[index].append(
                    response.session(screen, measure, series, span, overlap, context.betas, hurdles)
                )
        del series
    fold_of = _folds(days, window, context.folds)
    cells: list[Cell] = []
    for index, measure in enumerate(screen.measures):
        if isinstance(measure, LeadLag):
            cells += _lead_lag_cells(context, index, measure, columns[index], stale[index], fold_of)
        elif isinstance(measure, CatchUp):
            cells += _catch_up_cells(context, index, measure, columns[index], fold_of)
        elif isinstance(measure, Condition):
            assert hurdles is not None
            cells += condition.measure(
                context, index, measure, days, columns[index], fold_of, catalog, hurdles
            )
        else:
            cells += _response_cells(context, index, measure, columns[index], fold_of)
    return [_judged(screen.verdict, cell) for cell in cells]


def _lead_lag_cells(
    context: Context,
    index: int,
    measure: LeadLag,
    days: list[np.ndarray],
    stale: list[list[dict[str, float]]],
    fold_of: np.ndarray,
) -> list[Cell]:
    keys = lead_lag.cells(context.screen, measure)
    matrix = np.column_stack(days) if days else np.zeros((len(keys), 0), np.float64)
    for row, key in enumerate(keys):
        if _fits(context.screen, (key.source, key.target)) & {"first_fold"}:
            matrix[row, fold_of == 0] = np.nan
    found = nulls.evidence(
        matrix, context.draws, nulls.seed(context.sha, context.snapshot_id, index)
    )
    cells: list[Cell] = []
    for row, key in enumerate(keys):
        folds = nulls.fold_means(matrix[row], fold_of, context.folds)
        cells.append(
            Cell(
                measure=index,
                id="lead_lag",
                key=key.name,
                params=key.params,
                mean=float(found.mean[row]),
                se=float(found.se[row]),
                t=float(found.t[row]),
                p=float(found.p[row]),
                sessions=int(found.sessions[row]),
                folds=folds,
                folds_same_sign=nulls.same_sign(folds, float(found.mean[row])),
                staleness=_staleness([day[row] for day in stale]),
                in_sample_fit="window" in _fits(context.screen, (key.source, key.target)),
                clock_bound=_clock_bound(context.screen, key, context.stamps),
            )
        )
    return cells


def _response_cells(
    context: Context,
    index: int,
    measure: Response,
    days: list[list[response.Tally | None]],
    fold_of: np.ndarray,
) -> list[Cell]:
    keys = response.cells(context.screen, measure)
    tallies: list[list[Tally | None]] = [[day[row] for day in days] for row in range(len(keys))]
    found: list[Traded] = []
    for row, key in enumerate(keys):
        fits = _fits(context.screen, (measure.trigger.leg, key.follower))
        if "first_fold" in fits:
            tallies[row] = _without_first_fold(tallies[row], fold_of)
        found.append(Traded(key.name, key.params, tallies[row], "window" in fits))
    return _traded_cells(context, index, "response", found, len(days), fold_of)


def _catch_up_cells(
    context: Context,
    index: int,
    measure: CatchUp,
    days: list[list[catch_up.Tally | None]],
    fold_of: np.ndarray,
) -> list[Cell]:
    """A catch-up's cells: a response's, with its pair's response and how its positions ended.

    Its own fit counts as a spread's does: fitted on the first fold, that fold holds no value;
    fitted on the window, every cell says it was judged on the data it was fitted on.
    """
    keys = catch_up.cells(context.screen, measure)
    found: list[Traded] = []
    for row, key in enumerate(keys):
        fits = _fits(context.screen, (key.leader, key.follower)) | {measure.fit}
        tallies: list[Tally | None] = [day[row] for day in days]
        if "first_fold" in fits:
            tallies = _without_first_fold(tallies, fold_of)
        live = [day for day in tallies if isinstance(day, catch_up.Tally)]
        beta = context.slopes[index].get((key.leader, key.follower))
        exits = catch_up.exits(live, beta)
        found.append(Traded(key.name, key.params, tallies, "window" in fits, exits))
    return _traded_cells(context, index, "catch_up", found, len(days), fold_of)


Tally = response.Tally | catch_up.Tally
"""A session's positions in one cell that trades."""


@dataclass(frozen=True)
class Traded:
    """One cell that trades, before it is judged: its name, its sessions, how it was fitted."""

    name: str
    params: dict[str, str | float]
    tallies: list[Tally | None]
    in_sample_fit: bool
    exits: CatchUpStats | None = None


def _traded_cells(
    context: Context,
    index: int,
    kind: Literal["response", "catch_up"],
    found: Sequence[Traded],
    days: int,
    fold_of: np.ndarray,
) -> list[Cell]:
    """Cells that trade, judged across sessions on their drift-adjusted signal."""
    signal = np.asarray(
        [[np.nan if day is None else day.signal for day in item.tallies] for item in found],
        dtype=np.float64,
    ).reshape(len(found), days)
    evidence = nulls.evidence(
        signal, context.draws, nulls.seed(context.sha, context.snapshot_id, index)
    )
    cells: list[Cell] = []
    for row, item in enumerate(found):
        live = [day for day in item.tallies if day is not None]
        margins = np.asarray(
            [np.nan if day is None else day.gross - day.hurdle for day in item.tallies],
            dtype=np.float64,
        )
        stats = _stats(live)
        folds = nulls.fold_means(margins, fold_of, context.folds)
        cells.append(
            Cell(
                measure=index,
                id=kind,
                key=item.name,
                params=item.params,
                mean=float(evidence.mean[row]),
                se=float(evidence.se[row]),
                t=float(evidence.t[row]),
                p=float(evidence.p[row]),
                sessions=int(evidence.sessions[row]),
                folds=folds,
                folds_same_sign=nulls.same_sign(folds, stats.ceiling_bp_day),
                in_sample_fit=item.in_sample_fit,
                response=stats,
                catch_up=item.exits,
            )
        )
    return cells


def _without_first_fold(tallies: list[Tally | None], fold_of: np.ndarray) -> list[Tally | None]:
    """The first fold fitted what the cell reads, so its sessions hold no value for it."""
    return [None if fold == 0 else day for day, fold in zip(tallies, fold_of, strict=True)]


def _fit(
    ws: Workspace,
    screen: Screen,
    window: tuple[date, date],
    catalog: Any,
    held: dict[str, Any],
) -> dict[str, float]:
    """Every fitted spread's beta, from the sessions of its fit span (`kanso.screen.fit`)."""
    spreads = fit.fitted(screen)
    if not spreads:
        return {}
    days = sessions.days(screen, window)
    fold_of = _folds(days, window, ws.config.research.folds)
    bounds = sessions.window_ns(window)
    overlap = screen.clock.hours == "overlap"
    names = sorted({leg for spread in spreads.values() for leg in (spread.long, spread.short)})
    moments = {name: fit.Moments() for name in spreads}
    for day, fold in zip(days, fold_of.tolist(), strict=True):
        wanted = [name for name, spread in spreads.items() if spread.fit == "window" or fold == 0]
        if not wanted:
            continue
        series = sessions.read(catalog, held, screen, names, day, window)
        opens, closes = sessions.hours_of(screen, day)
        span = (max(opens, bounds[0]), min(closes, bounds[1]))
        for name in wanted:
            moments[name] = moments[name].merged(fit.session(spreads[name], series, span, overlap))
    return {name: fit.beta(name, found) for name, found in moments.items()}


def _slopes(
    ws: Workspace,
    screen: Screen,
    window: tuple[date, date],
    catalog: Any,
    held: dict[str, Any],
    betas: dict[str, float],
) -> dict[int, dict[catch_up.Pair, float | None]]:
    """Every catch-up pair's response, from the sessions of its measure's fit span."""
    measures = {
        index: measure
        for index, measure in enumerate(screen.measures)
        if isinstance(measure, CatchUp)
    }
    if not measures:
        return {}
    days = sessions.days(screen, window)
    fold_of = _folds(days, window, ws.config.research.folds)
    bounds = sessions.window_ns(window)
    overlap = screen.clock.hours == "overlap"
    gathered = {
        index: {pair: fit.Moments() for pair in catch_up.pairs(screen, measure)}
        for index, measure in measures.items()
    }
    names = sorted(
        {
            leg
            for index, measure in measures.items()
            for pair in gathered[index]
            for name in pair
            for leg in screen.legs_of(name)
        }
    )
    for day, fold in zip(days, fold_of.tolist(), strict=True):
        wanted = [
            index for index, measure in measures.items() if measure.fit == "window" or fold == 0
        ]
        if not wanted:
            continue
        series = sessions.read(catalog, held, screen, names, day, window)
        opens, closes = sessions.hours_of(screen, day)
        span = (max(opens, bounds[0]), min(closes, bounds[1]))
        for index in wanted:
            found = catch_up.moments(screen, measures[index], series, span, overlap, betas)
            for pair, moments in found.items():
                gathered[index][pair] = gathered[index][pair].merged(moments)
    return {
        index: {pair: catch_up.slope(moments) for pair, moments in found.items()}
        for index, found in gathered.items()
    }


def _fits(screen: Screen, names: Sequence[str]) -> set[str]:
    """How the spreads these names read were fitted: `window`, `first_fold`, or neither."""
    spreads = fit.fitted(screen)
    return {str(spreads[name].fit) for name in names if name in spreads}


def _stats(live: Sequence[Tally]) -> ResponseStats:
    """A cell's events, summed over the sessions it was live in."""
    events = sum(day.events for day in live)
    per = max(events, 1)
    gross = sum(day.gross for day in live)
    cost = sum(day.hurdle for day in live)
    return ResponseStats(
        events=events,
        events_per_day=round(events / len(live), 6) if live else 0.0,
        gross_bp=gross / per,
        hurdle_bp=cost / per,
        margin_bp=(gross - cost) / per,
        ceiling_bp_day=(gross - cost) / len(live) if live else 0.0,
        hit_rate=sum(day.hits for day in live) / per,
        unfilled=sum(day.unfilled for day in live),
        drift_adjusted_bp=sum(day.signal for day in live) / per,
    )


def _clock_bound(screen: Screen, key: lead_lag.CellKey, stamps: dict[str, str]) -> bool:
    """Whether a lead is shorter than a second between clocks that mean different things.

    Two sources' `ts_init` are two clocks; when one is an exchange's own instant and the
    other a tape's or a vendor's receipt, or nobody declared what one is, a sub-second lead
    between them may be the difference between the clocks, and the result says so.
    """
    if abs(span_ns(key.lag)) >= NS_PER_SECOND:
        return False
    legs = screen.legs_of(key.source) + screen.legs_of(key.target)
    kinds = {stamps.get(leg, data.UNKNOWN) for leg in legs}
    return len(kinds) > 1 or bool(kinds & {data.UNKNOWN, data.MIXED})


def _judged(verdict: ScreenVerdict | None, cell: Cell) -> Cell:
    """The cell with its judgement, when the screen declared floors to judge it by.

    Every cell needs `min_sessions` and a p at or under `alpha`; a `response` cell needs its
    margin over the hurdle and its events a day to clear their floors too. A `condition` cell
    needs those and its `mean` above zero: its p ranks |t|, so it is two-sided, and its mean is
    signed so that an effect on the side its test declares is positive. A significant mean below
    zero is the state working against that side, which the margin need not show, since over a
    hold of several sessions a round trip's gross is mostly the follower's drift. A condition
    cell set against the hold (`benchmark: hold`) is judged by that instead of its contrast: its
    p against random timing and its `p_above_hold` both at or under `alpha`. A cell under
    `min_sessions` is `thin`; otherwise every clause it misses is named in its reason, so a
    cell that fails on p and on events a day says both.
    """
    if verdict is None:
        return cell
    if cell.sessions < verdict.min_sessions:
        reason = f"{cell.sessions} session(s), under min_sessions {verdict.min_sessions}"
        return cell.model_copy(update={"judged": "thin", "reason": reason})
    missed: list[str] = []
    hold = None if cell.condition is None else cell.condition.hold
    if hold is not None:
        if hold.p > verdict.alpha:
            missed.append(f"p {hold.p:.4g} against random timing above alpha {verdict.alpha:g}")
        if hold.p_above_hold > verdict.alpha:
            missed.append(
                f"p {hold.p_above_hold:.4g} that its Sharpe {hold.sharpe:.4g} beats the hold's "
                f"{hold.sharpe_hold:.4g} above alpha {verdict.alpha:g}"
            )
    else:
        if cell.p > verdict.alpha:
            missed.append(f"p {cell.p:.4g} above alpha {verdict.alpha:g}")
        if cell.condition is not None and cell.mean <= 0:
            missed.append(f"mean {cell.mean:.4g} bp is against the declared side")
    stats = cell.response
    if stats is not None and stats.margin_bp < verdict.min_margin_bp:
        missed.append(
            f"margin {stats.margin_bp:.4g} bp under min_margin_bp {verdict.min_margin_bp:g}"
        )
    if stats is not None and stats.events_per_day < verdict.min_events_per_day:
        missed.append(
            f"{stats.events_per_day:.4g} events a day, under min_events_per_day "
            f"{verdict.min_events_per_day:g}"
        )
    if missed:
        return cell.model_copy(update={"judged": "fail", "reason": "; ".join(missed)})
    return cell.model_copy(update={"judged": "pass"})


def _summary(verdict: ScreenVerdict | None, cells: Sequence[Cell]) -> Summary:
    """The result as a whole: how many cells passed, and whether any passing one is a trade."""
    if verdict is None:
        return Summary(declared=False)
    passing = [cell for cell in cells if cell.judged == "pass"]
    ranked = sorted(passing, key=_rank)
    return Summary(
        declared=True,
        worth_a_lane=any(cell.id != "lead_lag" for cell in passing),
        passed=len(passing),
        failed=sum(cell.judged == "fail" for cell in cells),
        thin=sum(cell.judged == "thin" for cell in cells),
        best=[cell.key for cell in ranked],
    )


def _rank(cell: Cell) -> tuple[bool, float, str]:
    """Responses first, by their ceiling; then leads, by the strength of their t."""
    if cell.response is not None:
        return (False, -cell.response.ceiling_bp_day, cell.key)
    return (True, -abs(cell.t), cell.key)


def _folds(days: Sequence[date], window: tuple[date, date], folds: int) -> np.ndarray:
    """The calendar fold each session falls in: equal spans of the window's days."""
    length = (window[1] - window[0]).days + 1
    offsets = [min(max((day - window[0]).days, 0), length - 1) for day in days]
    return np.asarray([offset * folds // length for offset in offsets], dtype=np.int64)


def _staleness(sessions_seen: Sequence[dict[str, float]]) -> dict[str, float]:
    """Each leg's staleness, averaged over the sessions that sampled it."""
    totals: dict[str, list[float]] = {}
    for seen in sessions_seen:
        for leg, value in seen.items():
            totals.setdefault(leg, []).append(value)
    return {leg: round(sum(values) / len(values), 6) for leg, values in totals.items()}


def _trades(screen: Screen) -> bool:
    return any(not isinstance(measure, LeadLag) for measure in screen.measures)


def _refuse_unresolved(plans: Sequence[data.LegPlan]) -> None:
    unresolved = [item for item in plans if item.state == "unresolved"]
    if unresolved:
        reasons = "; ".join(item.reason or item.instrument for item in unresolved)
        raise PreconditionError(
            f"the screen reads instruments no definition resolves: {reasons}",
            remedy=data.resolve_remedy([item.instrument for item in unresolved]),
        )


def _refuse_unserved(plans: Sequence[data.LegPlan]) -> None:
    unserved = [item for item in plans if item.state == "unserved"]
    if unserved:
        reasons = "; ".join(item.reason or item.instrument for item in unserved)
        remedy = "build a data adapter for them as a workspace extension (docs/extensions.md)"
        if any(item.type not in BUILTIN_TYPES for item in unserved):
            remedy = (
                "load each custom series from a file with `kanso data load --loader "
                "csv_parquet --spec <spec>` over the days its reason names missing, or build a "
                "data adapter whose `serves` names its type (docs/extensions.md)"
            )
        raise PreconditionError(
            f"the screen reads series no registered adapter serves: {reasons}",
            remedy=f"{remedy}, then run the screen again",
        )


def _read(item: data.LegPlan, before: Sequence[data.LegPlan]) -> SeriesRead:
    stamps = item.timestamps
    if stamps == data.UNKNOWN:
        stamps = next((old.timestamps for old in before if old.key == item.key), stamps)
    return SeriesRead.model_validate(
        {
            "instrument": item.instrument,
            "type": item.type,
            "resolution": item.resolution,
            "legs": list(item.legs),
            "timestamps": stamps,
        }
    )


def _pinned_sha(store: StateStore, hyp_id: str | None) -> str | None:
    if hyp_id is None:
        return None
    row = store.connection.execute(
        "SELECT hypothesis_sha FROM hypotheses WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    return str(row[0])


def _peak_gb() -> float:
    """The peak resident size of this process, in gibibytes."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _MAXRSS_BYTES / GIB
