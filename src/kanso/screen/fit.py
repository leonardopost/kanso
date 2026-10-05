"""Fitting a spread's hedge: ordinary least squares of one leg's log price on the other's.

A `spread` with `hedge: ols` is `log long - beta log short`, beta the slope of `log long` on
`log short` with an intercept. It is fitted on the samples of every session of its fit span —
the whole window under `fit: window`, the window's first calendar fold under `fit: first_fold`
— at every point of whichever leg printed less, the other read as of that point.

**A fit is a choice made on data, and the result says which data.** Under `fit: window` the
beta is chosen on the sessions it is judged on, and every cell reading the spread carries
`in_sample_fit: true`. Under `fit: first_fold` the first fold fits and is scored by no cell
that reads the spread: its sessions hold no value for them, and the later folds judge it.

The moments are gathered session by session and merged by Chan's pairwise update, centred
within each session, so a log price near 4.6 that moves in its fourth decimal loses nothing
to cancellation however many sessions are merged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from kanso.errors import PreconditionError
from kanso.schemas.screen import Screen, Spread
from kanso.screen import grid
from kanso.screen.sessions import Series


@dataclass(frozen=True)
class Moments:
    """What a slope needs: a count, two means, the spread of y, the co-spread of x and y."""

    n: int = 0
    mean_x: float = 0.0
    mean_y: float = 0.0
    m2_y: float = 0.0
    c_xy: float = 0.0

    def merged(self, other: Moments) -> Moments:
        """The moments of both samples together (Chan, Golub and LeVeque's pairwise update)."""
        if other.n == 0:
            return self
        if self.n == 0:
            return other
        n = self.n + other.n
        dx = other.mean_x - self.mean_x
        dy = other.mean_y - self.mean_y
        weight = self.n * other.n / n
        return Moments(
            n=n,
            mean_x=self.mean_x + dx * other.n / n,
            mean_y=self.mean_y + dy * other.n / n,
            m2_y=self.m2_y + other.m2_y + dy * dy * weight,
            c_xy=self.c_xy + other.c_xy + dx * dy * weight,
        )


def fitted(screen: Screen) -> dict[str, Spread]:
    """Every spread whose hedge is fitted, by name."""
    return {
        name: derived.spread
        for name, derived in screen.derived.items()
        if derived.spread is not None and derived.spread.hedge == "ols"
    }


def session(
    spread: Spread, series: Mapping[str, Series], span: tuple[int, int], overlap: bool
) -> Moments:
    """One session's moments: every point of the leg that printed less, the other as of it."""
    legs = (spread.long, spread.short)
    live = grid.live(legs, series, span, overlap)
    if live is None:
        return Moments()
    sparse = min(legs, key=lambda leg: (len(series[leg].ts), leg))
    times = series[sparse].ts
    times = np.unique(times[(times >= live[0]) & (times <= live[1])])
    x = np.log(grid.asof(series[spread.long], times))
    y = np.log(grid.asof(series[spread.short], times))
    both = ~(np.isnan(x) | np.isnan(y))
    x, y = x[both], y[both]
    if not len(x):
        return Moments()
    mean_x = float(np.add.reduce(x) / len(x))
    mean_y = float(np.add.reduce(y) / len(y))
    dx, dy = x - mean_x, y - mean_y
    return Moments(
        n=len(x),
        mean_x=mean_x,
        mean_y=mean_y,
        m2_y=float(np.add.reduce(dy * dy)),
        c_xy=float(np.add.reduce(dx * dy)),
    )


def beta(name: str, moments: Moments) -> float:
    """The slope the moments give, refused when the short leg never moved."""
    if moments.n < 2 or moments.m2_y <= 0.0:
        raise PreconditionError(
            f"derived.{name}: its hedge cannot be fitted — the short leg's log price held "
            f"{moments.n} sample(s) and did not move over the fit span",
            remedy="fit it on a longer span (`fit: window`), or state a fixed beta",
        )
    return moments.c_xy / moments.m2_y
