"""Sampling a session: as-of values, the grid, a derived leg's level, staleness.

**As of, never ahead.** A series' value at instant t is that of its newest point with
`ts_init` at or before t — of several at one instant, the last the catalog hands over — and
there is none before its first point. Nothing here looks past t.

**A level, so every leg differences the same way.** An instrument's level is its log price;
a basket's the weighted sum of its legs' log prices; a spread's log `long` less beta times
log `short`; a gap's `a / b - 1`. A return is a change of level, so a basket's return is the
weighted sum of its legs' returns and a gap's change is in fractions of `b`.

**A cell is live from the first instant every one of its legs has printed** and, under
`hours: overlap`, until the earliest last print among them. A derived leg's instants are
the union of its legs' instants once every one of them has printed.

**The grid** is the multiples of `clock.grid` from the epoch that fall inside a cell's live
span, so two cells sampled on one grid sample the same instants. Returns are taken between
consecutive grid instants of one session only. A leg is **stale** over a grid interval it
did not print in, and its staleness is the share of intervals in which it did not.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

from kanso.schemas.screen import Screen
from kanso.screen.sessions import Series


def asof(series: Series, times: np.ndarray, values: np.ndarray | None = None) -> np.ndarray:
    """`values` (the price by default) as of each instant; NaN before the first point."""
    data = series.price if values is None else values
    index = np.searchsorted(series.ts, times, side="right") - 1
    out = np.full(len(times), np.nan, dtype=np.float64)
    known = index >= 0
    out[known] = data[index[known]]
    return out


def live(
    legs: Sequence[str], series: Mapping[str, Series], span: tuple[int, int], overlap: bool
) -> tuple[int, int] | None:
    """The span a cell is sampled over in one session, or `None` when a leg never printed.

    It opens at the first instant every leg has printed, and never before `span` does; it
    closes where `span` does, or, under `overlap`, at the earliest last print among the legs.
    """
    if any(series[leg].empty for leg in legs):
        return None
    opens = max(span[0], *(int(series[leg].ts[0]) for leg in legs))
    closes = min(int(series[leg].ts[-1]) for leg in legs) if overlap else span[1]
    return (opens, closes) if opens < closes else None


def grid(span: tuple[int, int], step_ns: int) -> np.ndarray:
    """The multiples of the step from the epoch inside `[span]`."""
    first = -(-span[0] // step_ns) * step_ns
    return np.arange(first, span[1] + 1, step_ns, dtype=np.int64)


def instants(screen: Screen, name: str, series: Mapping[str, Series]) -> np.ndarray:
    """The instants a leg or a derived leg moves at: a derived leg's are its legs' union,
    from the first instant every one of them has printed."""
    legs = _legs(screen, name)
    if any(series[leg].empty for leg in legs):
        return np.zeros(0, dtype=np.int64)
    union = np.unique(np.concatenate([series[leg].ts for leg in legs]))
    start = max(int(series[leg].ts[0]) for leg in legs)
    return union[union >= start]


def level(
    screen: Screen,
    name: str,
    series: Mapping[str, Series],
    times: np.ndarray,
    betas: Mapping[str, float],
) -> np.ndarray:
    """A leg's or a derived leg's level as of each instant; NaN before it is defined."""
    if name in screen.legs:
        return np.log(asof(series[name], times))
    derived = screen.derived[name]
    if derived.basket is not None:
        total = np.zeros(len(times), dtype=np.float64)
        for leg, weight in derived.basket.items():
            total += weight * np.log(asof(series[leg], times))
        return total
    if derived.spread is not None:
        spread = derived.spread
        beta = spread.beta if spread.beta is not None else betas[name]
        long = np.log(asof(series[spread.long], times))
        short = np.log(asof(series[spread.short], times))
        return long - beta * short
    assert derived.gap is not None
    a = asof(series[derived.gap.a], times)
    b = asof(series[derived.gap.b], times)
    return np.asarray(a / b - 1.0, dtype=np.float64)


def staleness(series: Series, times: np.ndarray) -> float:
    """The share of grid intervals in which the series did not print; one with none."""
    if len(times) < 2:
        return 1.0
    counts = np.diff(np.searchsorted(series.ts, times, side="right"))
    return float(np.count_nonzero(counts == 0)) / float(len(counts))


def _legs(screen: Screen, name: str) -> tuple[str, ...]:
    return screen.derived[name].legs if name in screen.derived else (name,)
