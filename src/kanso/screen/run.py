"""`kanso screen run`: validate, get the data, pin, measure, judge, record.

The order is the contract.

1. **Validate** the file (`kanso.screen.files`), and refuse a free window that meets a
   registered certification span (`kanso.screen.embargo`) before anything is read.
2. **Get the data.** A series no adapter serves is refused, naming the venue and type to
   build an adapter for; every fetchable one is fetched through the adapter that serves it
   and a snapshot taken (`kanso.screen.data`). Nothing is skipped.
3. **Pin** the newest snapshot covering every series over the window (`kanso.screen.pin`),
   taking one when what is held has not been frozen yet.
   The screen's bytes, that snapshot and the measure library's version are the result's key;
   a result already recorded under them is returned as it was, and nothing is read.
4. **Measure**, one session at a time: every leg of the session is read once through the
   runner's reader, every measure takes its cells' values from it, and the session is let go.
5. **Judge** each measure's cells across sessions with its max-T null (`kanso.screen.nulls`),
   and, when the file declares a verdict, each cell against it.
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
from typing import TYPE_CHECKING, Any, Final

import numpy as np

from kanso.data.catalog import open_catalog
from kanso.errors import PreconditionError
from kanso.schemas import DateWindow
from kanso.schemas.screen import (
    Cell,
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
)
from kanso.screen.files import validate
from kanso.screen.library import screen_version

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

ASSUMPTION: Final = "sessions are roughly independent of each other"

NS_PER_SECOND: Final = 1_000_000_000
"""What every result's inference rests on, stated in the result."""

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
    _refuse_unserved(plans)
    fetched = data.fetch(ws, store, screen, window, plans)
    catalog = open_catalog(ws)
    held = sessions.definitions(catalog, screen)
    snapshot = pin.covering(ws, store, screen, window, held)
    version = screen_version()
    stored = records.stored(store, valid.sha, snapshot.snapshot_id, version)
    if stored is not None:
        return Outcome(stored, records.render(ws, stored), True, fetched)
    store.put_blob(path.read_bytes())
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
        betas=_fit(ws, screen, window, catalog, held),
    )
    cells = _measure(ws, context, window, catalog, held, priced)
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
        assumption=ASSUMPTION,
        created_at=datetime.now(tz=UTC),
        wall_s=round(time.monotonic() - started, 3),
        peak_mem_gb=round(_peak_gb(), 3),
        series=read,
        cells=cells,
        summary=_summary(screen.verdict, cells),
    )
    records.record(store, result, sorted({leg.instrument for leg in screen.legs.values()}))
    return Outcome(result, records.render(ws, result), False, fetched)


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
        folds = _fold_means(matrix[row], fold_of, context.folds)
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
                folds_same_sign=_same_sign(folds, float(found.mean[row])),
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
    tallies = [[day[row] for day in days] for row in range(len(keys))]
    for row, key in enumerate(keys):
        if _fits(context.screen, (measure.trigger.leg, key.follower)) & {"first_fold"}:
            tallies[row] = [
                None if fold == 0 else day for day, fold in zip(tallies[row], fold_of, strict=True)
            ]
    signal = np.asarray(
        [[np.nan if day is None else day.signal for day in row] for row in tallies],
        dtype=np.float64,
    ).reshape(len(keys), len(days))
    found = nulls.evidence(
        signal, context.draws, nulls.seed(context.sha, context.snapshot_id, index)
    )
    cells: list[Cell] = []
    for row, key in enumerate(keys):
        live = [day for day in tallies[row] if day is not None]
        margins = np.asarray(
            [np.nan if day is None else day.gross - day.hurdle for day in tallies[row]],
            dtype=np.float64,
        )
        stats = _stats(live)
        folds = _fold_means(margins, fold_of, context.folds)
        cells.append(
            Cell(
                measure=index,
                id="response",
                key=key.name,
                params=key.params,
                mean=float(found.mean[row]),
                se=float(found.se[row]),
                t=float(found.t[row]),
                p=float(found.p[row]),
                sessions=int(found.sessions[row]),
                folds=folds,
                folds_same_sign=_same_sign(folds, stats.ceiling_bp_day),
                in_sample_fit="window"
                in _fits(context.screen, (measure.trigger.leg, key.follower)),
                response=stats,
            )
        )
    return cells


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


def _fits(screen: Screen, names: Sequence[str]) -> set[str]:
    """How the spreads these names read were fitted: `window`, `first_fold`, or neither."""
    spreads = fit.fitted(screen)
    return {str(spreads[name].fit) for name in names if name in spreads}


def _stats(live: Sequence[response.Tally]) -> ResponseStats:
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
    margin over the hurdle and its events a day to clear their floors too. The first clause a
    cell misses is the reason it gives.
    """
    if verdict is None:
        return cell
    if cell.sessions < verdict.min_sessions:
        reason = f"{cell.sessions} session(s), under min_sessions {verdict.min_sessions}"
        return cell.model_copy(update={"judged": "thin", "reason": reason})
    if cell.p > verdict.alpha:
        reason = f"p {cell.p:.4g} above alpha {verdict.alpha:g}"
        return cell.model_copy(update={"judged": "fail", "reason": reason})
    stats = cell.response
    if stats is not None and stats.margin_bp < verdict.min_margin_bp:
        reason = f"margin {stats.margin_bp:.4g} bp under min_margin_bp {verdict.min_margin_bp:g}"
        return cell.model_copy(update={"judged": "fail", "reason": reason})
    if stats is not None and stats.events_per_day < verdict.min_events_per_day:
        reason = (
            f"{stats.events_per_day:.4g} events a day, under min_events_per_day "
            f"{verdict.min_events_per_day:g}"
        )
        return cell.model_copy(update={"judged": "fail", "reason": reason})
    return cell.model_copy(update={"judged": "pass"})


def _summary(verdict: ScreenVerdict | None, cells: Sequence[Cell]) -> Summary:
    """The result as a whole: how many cells passed, and whether any passing one is a trade."""
    if verdict is None:
        return Summary(declared=False)
    passing = [cell for cell in cells if cell.judged == "pass"]
    ranked = sorted(passing, key=_rank)
    return Summary(
        declared=True,
        worth_a_lane=any(cell.id == "response" for cell in passing),
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


def _fold_means(row: np.ndarray, fold_of: np.ndarray, folds: int) -> list[float | None]:
    means: list[float | None] = []
    for fold in range(folds):
        inside = row[(fold_of == fold) & ~np.isnan(row)]
        means.append(float(np.add.reduce(inside) / len(inside)) if len(inside) else None)
    return means


def _same_sign(folds: Sequence[float | None], mean: float) -> int:
    """How many folds' means share the sign of the whole; none when the whole is zero."""
    if mean == 0.0:
        return 0
    return sum(1 for value in folds if value is not None and value * mean > 0)


def _staleness(sessions_seen: Sequence[dict[str, float]]) -> dict[str, float]:
    """Each leg's staleness, averaged over the sessions that sampled it."""
    totals: dict[str, list[float]] = {}
    for seen in sessions_seen:
        for leg, value in seen.items():
            totals.setdefault(leg, []).append(value)
    return {leg: round(sum(values) / len(values), 6) for leg, values in totals.items()}


def _trades(screen: Screen) -> bool:
    return any(isinstance(measure, Response) for measure in screen.measures)


def _refuse_unserved(plans: Sequence[data.LegPlan]) -> None:
    unserved = [item for item in plans if item.state == "unserved"]
    if unserved:
        reasons = "; ".join(item.reason or item.instrument for item in unserved)
        raise PreconditionError(
            f"the screen reads series no registered adapter serves: {reasons}",
            remedy="build a data adapter for them as a workspace extension "
            "(docs/extensions.md), then run the screen again",
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
