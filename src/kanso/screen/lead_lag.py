"""`lead_lag`: whether, which way and at what delay two series move together.

A cell is an ordered pair (a, b) and a lag k; a positive k means a leads b. Its value in one
session is the correlation of a's returns with b's returns k later:

    rho_s(k) = corr(r_a(t), r_b(t + k))

**On the grid** (`estimator: grid`), returns are changes of level between consecutive grid
instants inside the cell's live span (`kanso.screen.grid`), and a lag is a whole number of
grid steps — the schema refuses one that is not. A session gives a pair a value at a lag
when at least `MIN_PAIRS` return pairs are defined and neither side is constant.

A series against itself is its autocorrelation: a spread's increments against their own past,
negative at short lags, is mean reversion measured without a fitted model.

Each cell also carries, per session, the staleness of every leg it reads on its grid: a
follower that prints once a minute sampled every second is mostly stale, and its lead is
mostly the sampling.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import numpy as np

from kanso.schemas.screen import LeadLag, Screen, pairs, span_ns
from kanso.screen import grid
from kanso.screen.sessions import Series

MIN_PAIRS: Final = 3
"""Return pairs a session needs before a correlation is a number rather than an accident."""


@dataclass(frozen=True)
class CellKey:
    """One cell: its name in a result, the pair, the lag."""

    name: str
    source: str
    target: str
    lag: str

    @property
    def params(self) -> dict[str, str | float]:
        return {"from": self.source, "to": self.target, "lag": self.lag}


def cells(screen: Screen, measure: LeadLag) -> tuple[CellKey, ...]:
    """Every cell of the measure, pair by pair and lag by lag, in the order declared."""
    return tuple(
        CellKey(f"lead_lag/{a}>{b}/{lag}", a, b, lag)
        for a, b in pairs(screen, measure)
        for lag in measure.lags
    )


def session(
    screen: Screen,
    measure: LeadLag,
    series: Mapping[str, Series],
    span: tuple[int, int],
    overlap: bool,
    betas: Mapping[str, float],
) -> tuple[np.ndarray, list[dict[str, float]]]:
    """One session's value of every cell, NaN where it has none, and each cell's staleness."""
    assert screen.clock.grid is not None
    step = span_ns(screen.clock.grid)
    values: list[float] = []
    stale: list[dict[str, float]] = []
    for a, b in pairs(screen, measure):
        legs = tuple(dict.fromkeys(screen.legs_of(a) + screen.legs_of(b)))
        live = grid.live(legs, series, span, overlap)
        times = np.zeros(0, dtype=np.int64) if live is None else grid.grid(live, step)
        returns_a = np.diff(grid.level(screen, a, series, times, betas))
        returns_b = np.diff(grid.level(screen, b, series, times, betas))
        staleness = {leg: grid.staleness(series[leg], times) for leg in legs}
        for lag in measure.lags:
            values.append(_lagged(returns_a, returns_b, span_ns(lag) // step))
            stale.append(staleness if len(times) > 1 else {})
    return np.asarray(values, dtype=np.float64), stale


def _lagged(a: np.ndarray, b: np.ndarray, steps: int) -> float:
    """corr(a(t), b(t + steps)), over the instants both are defined."""
    count = len(a)
    if steps > 0:
        x, y = a[: max(count - steps, 0)], b[steps:]
    else:
        x, y = a[-steps:], b[: max(count + steps, 0)]
    return correlation(x, y)


def correlation(x: np.ndarray, y: np.ndarray) -> float:
    """Pearson's correlation over the pairs both define; NaN when it is not a number."""
    both = ~(np.isnan(x) | np.isnan(y))
    x, y = x[both], y[both]
    if len(x) < MIN_PAIRS:
        return float("nan")
    dx = x - np.add.reduce(x) / len(x)
    dy = y - np.add.reduce(y) / len(y)
    scale = float(np.sqrt(np.add.reduce(dx * dx) * np.add.reduce(dy * dy)))
    if scale == 0.0:
        return float("nan")
    return float(np.add.reduce(dx * dy) / scale)
