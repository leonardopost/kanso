"""A screen's signals: a registered custom type's numeric field, read as of declared instants.

**What a signal may be.** A `signals:` entry names a custom type, one of its fields and the
instrument the series is filed under. The type must be registered in this process — the
workspace's extensions are imported first — and the field must be one of the class's own
annotated fields, the timestamps and the instrument left out, by the rule the `csv_parquet`
loader maps a file's columns with, annotated `float`, `int` or `bool`: a number a comparison
can read. Anything else is refused by name (`check`).

**As of, never ahead.** A signal's value at an instant is its field on the point filed under
the signal's instrument with the largest `ts_init` at or before that instant — of several at
one instant, the last the catalog hands over — and there is none before the first such point.
A point's `ts_init` is when it became public, so a value read this way was known at the
instant it is read for. Nothing before the window's first midnight is read, as no leg is: a
point published before the window opened is never carried into it, and the instants before a
signal's first point inside the window hold no value, never a zero.

**One read a day per type.** The points are read one UTC day at a time through
`kanso.nautilus.backtest._custom_points`, the reader a card's custom series comes through, so
a screen is handed the points a card of the same span would be. Every signal of one type is
answered from that one read, the instruments of all of them asked for at once and each point
kept for the signals filed under its own instrument; a market-wide point, filed under no
instrument, is no signal's. The latest point is carried from one day to the next, so a
window of decades holds one day of points at a time. A field that is not a finite number is
refused (exit 2): a comparison of a NaN reads false whichever way it is asked.

**A refused point is reloaded by supersession.** A run pins a snapshot covering every series
before it reads a point, so by the time a point is refused the dataset holding it is named by
one, and a `--replace` over it is refused. The remedy names a load that supersedes it, one
dataset at a time, since `--supersedes` names one (`reload`).

NautilusTrader facts relied on (nautilus_trader 1.231.0): a custom point read back from the
catalog arrives wrapped in `CustomData`, whose `data` is the point; a query filters on
`ts_init` inclusively at both ends (`kanso.nautilus.facts`).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

import numpy as np

from kanso.data.loaders.csv_parquet import _custom_fields
from kanso.data.types import resolve_type
from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus import backtest
from kanso.schemas.screen import Screen
from kanso.screen.sessions import window_ns

DAY_NS: Final = 86_400_000_000_000
NUMBERS: Final = (float, int, bool)
"""The annotations a signal's field may carry: what a comparison reads as a number."""


@dataclass(frozen=True)
class Read:
    """A signal as of each instant asked, in the order asked: its value, and the `ts_init` and
    `ts_event` of the point it came from; NaN and -1 where no point is before the instant."""

    value: np.ndarray
    ts_init: np.ndarray
    ts_event: np.ndarray


Day = tuple[int, int, np.ndarray, np.ndarray, np.ndarray]
"""One day's points of one signal: the day's span `[start, end)`, then `ts_init`, the value and
`ts_event` of each point, in the order the catalog handed them."""


def reload(screen: Screen, name: str) -> str:
    """How to reload a signal's series once a run has pinned it: a superseding load for each of
    its datasets."""
    signal = screen.signals[name]
    return (
        f"reload {signal.type} under {signal.instrument} one dataset at a time — each `kanso data "
        "show` lists, from a spec over that dataset's span — with `kanso data load --loader "
        "csv_parquet --spec <spec> --supersedes <dataset>`: the snapshot the run pinned names "
        "them, so a `--replace` is refused"
    )


def check(screen: Screen) -> None:
    """Refuse a signal whose type is not registered or whose field is not one of its numbers."""
    for name, signal in screen.signals.items():
        try:
            data_cls = resolve_type(signal.type)
        except ValidationError as error:
            raise ValidationError(f"signals.{name}.{error.message}", remedy=error.remedy) from None
        fields = _custom_fields(data_cls)
        kind = fields.get(signal.field)
        if kind is None:
            numbers = sorted(field for field, cls in fields.items() if cls in NUMBERS)
            raise ValidationError(
                f"signals.{name}.field: {signal.field!r} is not a field of {signal.type}; its "
                f"numeric fields are {', '.join(numbers) or 'none'}",
                remedy="name one of them, as the class annotates it",
            )
        if kind not in NUMBERS:
            raise ValidationError(
                f"signals.{name}.field: {signal.type}.{signal.field} is annotated "
                f"{getattr(kind, '__name__', kind)}, which no comparison reads as a number",
                remedy="name a field annotated float, int or bool",
            )


def definitions(catalog: Any, screen: Screen) -> dict[str, Any]:
    """The store's definition of each signal's instrument that it holds; none is required."""
    wanted = sorted({signal.instrument for signal in screen.signals.values()})
    if not wanted:
        return {}
    return {str(item.id): item for item in catalog.instruments(instrument_ids=wanted)}


def asof(
    catalog: Any,
    screen: Screen,
    wanted: Mapping[str, np.ndarray],
    window: tuple[date, date],
) -> dict[str, Read]:
    """Each named signal as of each of its instants, read a UTC day at a time by type."""
    bounds = window_ns(window)
    by_type: dict[str, list[str]] = {}
    for name in sorted(wanted):
        by_type.setdefault(screen.signals[name].type, []).append(name)
    found: dict[str, Read] = {}
    for type_id, names in sorted(by_type.items()):
        instants = {name: np.asarray(wanted[name], dtype=np.int64) for name in names}
        last = max((int(times.max()) for times in instants.values() if len(times)), default=-1)
        end = min(last + 1, bounds[1])
        days = _days(catalog, screen, type_id, names, bounds[0], end)
        joined = _joined(days, instants)
        found.update(joined)
    return found


def _days(
    catalog: Any, screen: Screen, type_id: str, names: Sequence[str], start: int, end: int
) -> Iterable[dict[str, Day]]:
    """Every UTC day of `[start, end)`, read once for every signal of one type."""
    data_cls = resolve_type(type_id)
    instruments = sorted({screen.signals[name].instrument for name in names})
    for day in range(start, end, DAY_NS):
        close = min(day + DAY_NS, end)
        points = backtest._custom_points(catalog, data_cls, instruments, day, close - 1)
        yield {name: _points(screen, name, points, day, close) for name in names}


def _points(screen: Screen, name: str, points: Sequence[object], start: int, end: int) -> Day:
    """One signal's points of one day, in the order the catalog handed them."""
    signal = screen.signals[name]
    stamps: list[int] = []
    values: list[float] = []
    events: list[int] = []
    for point in points:
        if backtest._instrument_of(point) != signal.instrument:
            continue
        value = float(getattr(getattr(point, "data", point), signal.field))
        stamp = int(point.ts_init)  # type: ignore[attr-defined]
        if not math.isfinite(value):
            raise PreconditionError(
                f"signals.{name}: {signal.type}.{signal.field} is {value} at ts_init {stamp} "
                f"on {signal.instrument}; a signal is a finite number",
                remedy=f"set that point's field or drop the point, and {reload(screen, name)}",
            )
        stamps.append(stamp)
        values.append(value)
        events.append(int(point.ts_event))  # type: ignore[attr-defined]
    return (
        start,
        end,
        np.asarray(stamps, dtype=np.int64),
        np.asarray(values, dtype=np.float64),
        np.asarray(events, dtype=np.int64),
    )


def _joined(days: Iterable[dict[str, Day]], instants: Mapping[str, np.ndarray]) -> dict[str, Read]:
    """Every signal's join over the same days, the days read once for all of them."""
    joins = {name: _Join(times) for name, times in instants.items()}
    for day in days:
        for name, points in day.items():
            joins[name].take(points)
    return {name: join.read() for name, join in joins.items()}


def join(days: Iterable[Day], instants: np.ndarray) -> Read:
    """One signal as of each instant, from its points handed a day at a time in time order."""
    found = _Join(np.asarray(instants, dtype=np.int64))
    for day in days:
        found.take(day)
    return found.read()


class _Join:
    """A streaming as-of join: each day answers the instants inside it, from its own points or
    from the latest point of the days before."""

    def __init__(self, instants: np.ndarray) -> None:
        self.order = np.argsort(instants, kind="stable")
        self.sorted = instants[self.order]
        self.value = np.full(len(instants), np.nan, dtype=np.float64)
        self.ts_init = np.full(len(instants), -1, dtype=np.int64)
        self.ts_event = np.full(len(instants), -1, dtype=np.int64)
        self.carry: tuple[int, float, int] | None = None

    def take(self, day: Day) -> None:
        start, end, stamps, values, events = day
        ordered = np.argsort(stamps, kind="stable")
        stamps, values, events = stamps[ordered], values[ordered], events[ordered]
        low = int(np.searchsorted(self.sorted, start, side="left"))
        high = int(np.searchsorted(self.sorted, end, side="left"))
        at = self.sorted[low:high]
        index = np.searchsorted(stamps, at, side="right") - 1
        slots = self.order[low:high]
        known = index >= 0
        self.value[slots[known]] = values[index[known]]
        self.ts_init[slots[known]] = stamps[index[known]]
        self.ts_event[slots[known]] = events[index[known]]
        if self.carry is not None:
            before = slots[~known]
            self.ts_init[before], self.value[before], self.ts_event[before] = self.carry
        if len(stamps):
            self.carry = (int(stamps[-1]), float(values[-1]), int(events[-1]))

    def read(self) -> Read:
        return Read(value=self.value, ts_init=self.ts_init, ts_event=self.ts_event)
