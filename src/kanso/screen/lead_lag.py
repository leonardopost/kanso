"""`lead_lag`: whether, which way and at what delay two series move together.

A cell is an ordered pair (a, b) and a lag k; a positive k means a leads b. Its value in one
session is the correlation of a's returns with b's returns k later:

    rho_s(k) = corr(r_a(t), r_b(t + k))

**On the grid** (`estimator: grid`), returns are changes of level between consecutive grid
instants inside the cell's live span (`kanso.screen.grid`), and a lag is a whole number of
grid steps — the schema refuses one that is not. A session gives a pair a value at a lag
when at least `MIN_PAIRS` return pairs are defined and neither side is flat. The correlation is
the realised one, of returns that are not demeaned (`correlation`).

**Without a grid** (`estimator: hy`), each series moves at its own instants and nothing is
sampled. The value is the Hayashi–Yoshida covariance of the two sequences of returns, b's
intervals moved back by the lag, scaled to a correlation:

    HY(k) = sum over i, j of da_i db_j 1{(t_i-1, t_i] meets (u_j-1 - k, u_j - k]}

so every pair of returns whose intervals overlap once b is moved back by k counts, and none
other, over the two realised variances measured on one clock, the sparser series' instants
(`_variance`). A grid at a fine step mostly samples prices that have not moved, which shrinks a
correlation towards zero as the step shrinks (the Epps effect); the covariance has no step to
shrink.
Several points at one instant are one point, the last, because an interval of no length
holds no return. The sum is taken interval by interval with a cumulative sum, so a session
of n and m points costs n log m.

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
    if measure.estimator == "hy":
        return _asynchronous(screen, measure, series, span, overlap, betas)
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


def _asynchronous(
    screen: Screen,
    measure: LeadLag,
    series: Mapping[str, Series],
    span: tuple[int, int],
    overlap: bool,
    betas: Mapping[str, float],
) -> tuple[np.ndarray, list[dict[str, float]]]:
    """Every cell's Hayashi–Yoshida correlation in one session; no grid, so no staleness."""
    values: list[float] = []
    for a, b in pairs(screen, measure):
        legs = tuple(dict.fromkeys(screen.legs_of(a) + screen.legs_of(b)))
        live = grid.live(legs, series, span, overlap)
        moves = [_moves(screen, name, series, live, betas) for name in (a, b)]
        for lag in measure.lags:
            values.append(hayashi_yoshida(*moves[0], *moves[1], span_ns(lag)))
    return np.asarray(values, dtype=np.float64), [{} for _ in values]


def _moves(
    screen: Screen,
    name: str,
    series: Mapping[str, Series],
    live: tuple[int, int] | None,
    betas: Mapping[str, float],
) -> tuple[np.ndarray, np.ndarray]:
    """A series' instants inside the live span and its level at each, one point an instant."""
    if live is None:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float64)
    times = grid.instants(screen, name, series)
    times = times[(times >= live[0]) & (times <= live[1])]
    levels = grid.level(screen, name, series, times, betas)
    last = np.ones(len(times), dtype=bool)
    last[:-1] = times[1:] != times[:-1]
    return times[last], levels[last]


def hayashi_yoshida(
    a_times: np.ndarray, a_levels: np.ndarray, b_times: np.ndarray, b_levels: np.ndarray, lag: int
) -> float:
    """corr of a's returns with b's returns `lag` nanoseconds later, by Hayashi–Yoshida."""
    if len(a_times) <= MIN_PAIRS or len(b_times) <= MIN_PAIRS:
        return float("nan")
    da = np.diff(a_levels)
    db = np.diff(b_levels)
    shifted = b_times - lag
    cumulative = np.concatenate(([0.0], np.cumsum(db)))
    first = np.maximum(np.searchsorted(shifted, a_times[:-1], side="right"), 1)
    last = np.minimum(np.searchsorted(shifted, a_times[1:], side="left"), len(b_times) - 1)
    overlapping = np.where(last >= first, cumulative[last] - cumulative[first - 1], 0.0)
    clock = a_times if len(a_times) <= len(b_times) else b_times
    scale = float(
        np.sqrt(_variance(a_times, a_levels, clock) * _variance(b_times, b_levels, clock))
    )
    if scale == 0.0:
        return float("nan")
    return float(np.add.reduce(da * overlapping) / scale)


def _variance(times: np.ndarray, levels: np.ndarray, clock: np.ndarray) -> float:
    """A series' realised variance on `clock`: its level as of each instant, differenced.

    Both series of a Hayashi–Yoshida correlation are scaled on the sparser one's instants.
    Each one's variance on its own instants is measured at another frequency, and prints that
    come in runs inside a second carry less variance tick by tick than a second does: measured
    on 2026-09-22 on an exchange's BTC perpetual, the prints' own realised variance was 1.71e-4
    and the one-second bars' 3.75e-4, which read the prints against their own bars as a
    correlation of 1.49. On one clock the two variances measure one thing, and a series against
    itself reads one.
    """
    index = np.searchsorted(times, clock, side="right") - 1
    sampled = levels[index[index >= 0]]
    steps = np.diff(sampled)
    return float(np.add.reduce(steps * steps))


def _lagged(a: np.ndarray, b: np.ndarray, steps: int) -> float:
    """corr(a(t), b(t + steps)), over the instants both are defined."""
    count = len(a)
    if steps > 0:
        x, y = a[: max(count - steps, 0)], b[steps:]
    else:
        x, y = a[-steps:], b[: max(count + steps, 0)]
    return correlation(x, y)


def correlation(x: np.ndarray, y: np.ndarray) -> float:
    """The realised correlation over the pairs both define; NaN when it is not a number.

    Returns are not demeaned. A session's mean return is noise around zero, and taking it
    out of a few returns biases their correlation towards -1/(n-1): measured on the test
    workspace's hourly path, six bars a session and a pull of half its gap an hour — a lag-one
    reversion of -0.25 in theory — demeaning read -0.39 over 64 sessions, this reads -0.22,
    and Hayashi–Yoshida, which also counts the return a lag leaves unpaired in its variance,
    reads -0.16.
    """
    both = ~(np.isnan(x) | np.isnan(y))
    x, y = x[both], y[both]
    if len(x) < MIN_PAIRS:
        return float("nan")
    scale = float(np.sqrt(np.add.reduce(x * x) * np.add.reduce(y * y)))
    if scale == 0.0:
        return float("nan")
    return float(np.add.reduce(x * y) / scale)
