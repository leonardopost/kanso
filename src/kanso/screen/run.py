"""`kanso screen run`: validate, get the data, pin, measure, judge, record.

The order is the contract.

1. **Validate** the file (`kanso.screen.files`), and refuse a free window that meets a
   registered certification span (`kanso.screen.embargo`) before anything is read.
2. **Get the data.** A series no adapter serves is refused, naming the venue and type to
   build an adapter for; every fetchable one is fetched through the adapter that serves it
   and a snapshot taken (`kanso.screen.data`). Nothing is skipped.
3. **Pin** the newest snapshot covering every series over the window (`kanso.screen.pin`).
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
    Screen,
    ScreenResult,
    ScreenVerdict,
    SeriesRead,
    Summary,
)
from kanso.screen import data, embargo, lead_lag, nulls, pin, records, sessions
from kanso.screen.files import validate
from kanso.screen.library import screen_version

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

ASSUMPTION: Final = "sessions are roughly independent of each other"
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
    _refuse_unmeasured(screen)
    plans = data.plan(ws, store, screen, window)
    _refuse_unserved(plans)
    fetched = data.fetch(ws, store, screen, window, plans)
    catalog = open_catalog(ws)
    held = sessions.definitions(catalog, screen)
    snapshot = pin.covering(ws, screen, window, held)
    version = screen_version()
    stored = records.stored(store, valid.sha, snapshot.snapshot_id, version)
    if stored is not None:
        return Outcome(stored, records.render(ws, stored), True, fetched)
    store.put_blob(path.read_bytes())
    cells = _measure(ws, screen, window, catalog, held, valid.sha, snapshot.snapshot_id)
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
        series=[_read(item, plans) for item in _held_plans(ws, store, screen, window)],
        cells=cells,
        summary=_summary(screen.verdict, cells),
    )
    records.record(store, result, sorted({leg.instrument for leg in screen.legs.values()}))
    return Outcome(result, records.render(ws, result), False, fetched)


def _measure(
    ws: Workspace,
    screen: Screen,
    window: tuple[date, date],
    catalog: Any,
    held: dict[str, Any],
    sha: str,
    snapshot_id: str,
) -> list[Cell]:
    """Every cell of every measure, judged across the window's sessions."""
    days = sessions.days(screen, window)
    bounds = sessions.window_ns(window)
    overlap = screen.clock.hours == "overlap"
    names = list(screen.legs)
    columns: list[list[np.ndarray]] = [[] for _ in screen.measures]
    stale: list[list[list[dict[str, float]]]] = [[] for _ in screen.measures]
    for day in days:
        series = sessions.read(catalog, held, screen, names, day, window)
        opens, closes = sessions.hours_of(screen, day)
        span = (max(opens, bounds[0]), min(closes, bounds[1]))
        for index, measure in enumerate(screen.measures):
            assert isinstance(measure, LeadLag)
            values, staleness = lead_lag.session(screen, measure, series, span, overlap, {})
            columns[index].append(values)
            stale[index].append(staleness)
        del series
    fold_of = _folds(days, window, ws.config.research.folds)
    cells: list[Cell] = []
    for index, measure in enumerate(screen.measures):
        assert isinstance(measure, LeadLag)
        keys = lead_lag.cells(screen, measure)
        matrix = np.column_stack(columns[index]) if days else np.zeros((len(keys), 0), np.float64)
        found = nulls.evidence(matrix, ws.config.screen.draws, nulls.seed(sha, snapshot_id, index))
        for row, key in enumerate(keys):
            folds = _fold_means(matrix[row], fold_of, ws.config.research.folds)
            cells.append(
                _judged(
                    screen.verdict,
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
                        staleness=_staleness([day[row] for day in stale[index]]),
                    ),
                )
            )
    return cells


def _judged(verdict: ScreenVerdict | None, cell: Cell) -> Cell:
    """The cell with its judgement, when the screen declared floors to judge it by."""
    if verdict is None:
        return cell
    if cell.sessions < verdict.min_sessions:
        reason = f"{cell.sessions} session(s), under min_sessions {verdict.min_sessions}"
        return cell.model_copy(update={"judged": "thin", "reason": reason})
    if cell.p > verdict.alpha:
        reason = f"p {cell.p:.4g} above alpha {verdict.alpha:g}"
        return cell.model_copy(update={"judged": "fail", "reason": reason})
    return cell.model_copy(update={"judged": "pass"})


def _summary(verdict: ScreenVerdict | None, cells: Sequence[Cell]) -> Summary:
    """The result as a whole: how many cells passed, and whether any passing one is a trade."""
    if verdict is None:
        return Summary(declared=False)
    passing = [cell for cell in cells if cell.judged == "pass"]
    ranked = sorted(passing, key=lambda cell: (cell.id != "response", -abs(cell.t), cell.key))
    return Summary(
        declared=True,
        worth_a_lane=any(cell.id == "response" for cell in passing),
        passed=len(passing),
        failed=sum(cell.judged == "fail" for cell in cells),
        thin=sum(cell.judged == "thin" for cell in cells),
        best=[cell.key for cell in ranked],
    )


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


def _refuse_unmeasured(screen: Screen) -> None:
    """What this build cannot measure yet is refused before anything is read."""
    for index, measure in enumerate(screen.measures):
        if isinstance(measure, Response):
            raise PreconditionError(
                f"measures.{index}: this build does not measure `response` yet",
                remedy="screen lead_lag alone for now",
            )
        if measure.estimator == "hy":
            raise PreconditionError(
                f"measures.{index}: this build does not measure the `hy` estimator yet",
                remedy="use `estimator: grid` for now",
            )
    for name, derived in screen.derived.items():
        if derived.spread is not None and derived.spread.hedge == "ols":
            raise PreconditionError(
                f"derived.{name}: this build does not fit an `ols` hedge yet",
                remedy="state a fixed beta for now",
            )


def _refuse_unserved(plans: Sequence[data.LegPlan]) -> None:
    unserved = [item for item in plans if item.state == "unserved"]
    if unserved:
        reasons = "; ".join(item.reason or item.instrument for item in unserved)
        raise PreconditionError(
            f"the screen reads series no registered adapter serves: {reasons}",
            remedy="build a data adapter for them as a workspace extension "
            "(docs/extensions.md), then run the screen again",
        )


def _held_plans(
    ws: Workspace, store: StateStore, screen: Screen, window: tuple[date, date]
) -> tuple[data.LegPlan, ...]:
    """The plan once the data is in: every series held, with its clock."""
    return data.plan(ws, store, screen, window)


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
