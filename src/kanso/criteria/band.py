"""The band a stage's realised objective is judged against: one struck for its own window.

Composition measured the version over its sleeve's certification window and recorded the
objective's value, the error of that value as the objective reads it, and the return periods
the window held (`Expectation.value`, `se`, `periods`). A paper window and a live window are
other windows, usually much shorter, and what they realise is another estimate of the same
quantity at its own precision. Judging it against a confidence band of the certification
value — which is what the bootstrap's `ci90` is — asks a short window to be as precise as a
long one: a version behaving exactly as certified lands inside a ninety-percent band of the
certified value with probability `P(|Z| ≤ 1.645 / √(1 + N/M))` over `M` periods against
`N` — 26% over 5 sessions against 122, 46% over 20 and 76% even over 122 — and fails as
often above it as below.

**So the band is a prediction band for the window being judged.** Under the hypothesis that
the stage runs the process the certification window measured, the realised value and the
certified one are independent estimates of one quantity, and their difference has the
variance of both. Each fold value of a window of `m` periods varies as `s²/m`, `s` the
per-period dispersion the certification window showed — which its own error states:
`s² = se² × N` over its `N` periods (exact for folds of equal length, and how
`kanso.criteria.objectives` reads every error, widened for serial correlation). The stage's
value, a mean of `K` fold values over the folds that held something, then varies as
`s² Σ w(m_k) / K²`, `w(m) = 1/m` the variance of a mean over `m` periods, and

    band = centre ± z₀.₉₅ × √(se² + se² × N × Σ w(m_k) / K²)

with `z₀.₉₅ = 1.645`, so the band widens as the window shortens instead of the window being
judged at a precision it cannot have. For `K` equal folds of a window of `M` periods the
half-width is `1.645 × se × √(1 + N/M)`.

**A Sharpe's fold is weighed by its exact small-sample variance, and needs four periods.**
`1/m` is the variance of a mean over `m` periods, and of a Sharpe only asymptotically: a
Sharpe of `m` normal periods is a Student's t of `m − 1` degrees of freedom over `√m`, whose
variance is `(m − 1) / (m (m − 3))` — three times `1/m` at four periods, twice at five, and
without a variance at all at two or three, where the fold's Sharpe is a ratio of two noises.
So a Sharpe's fold weighs `w(m) = (m − 1) / (m (m − 3))`, and a window any of whose scoring
folds holds fewer than four periods strikes no band. With `1/m` instead, a version that is
what was certified landed inside the band 0.54 of the time over ten weekday sessions against
250, 0.82 over twenty and 0.87 over forty, and fell below it — what `live_drift` demotes on —
up to 0.18 of the time; with the exact weight it lands inside 0.89 to 0.94 over sixteen to
120 sessions, below it at most 0.045 (simulated October 2026 on normal and Student-t3 days,
weekday and seven-day calendars).

**The centre is the certified value scaled to the folds the window filled.** The objective
scores a fold that holds nothing as zero — a fold with no period, a per-trade edge's fold
that closed no trade, a Sharpe's fold of fewer than two periods — so a window that leaves one
of four folds empty realises three quarters of the per-fold value even when nothing has
changed. The centre is `value × K′/K`, `K′` the folds that held something; a certification
window is taken to have filled all of its own. A window that filled none — a per-trade
edge's window that closed no trade — strikes no band: it realises zero whatever the version
is, so it is evidence of nothing.

What it is not: it is not a test that the version still trades as certified fill by fill —
the stage is a replay into the same simulated venue — and its coverage is nominal: a normal
approximation, closer the more periods both windows hold. Simulated, a version that is what
was certified lands inside it 0.87 to 0.94 of the time on normal, Student-t3 and sparse books
of every family at every length that strikes one — 0.98 for a per-trade edge trading a few
times a week over five sessions — and 0.82 to 0.85 on a contribution whose periods are
autocorrelated at 0.3; it was not measured on regime-shifted ones. A window that holds no
period at all is judged by no band.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import fsum, sqrt
from statistics import NormalDist
from typing import Final

from kanso.criteria.objectives import Objective
from kanso.criteria.run import CardRun

__all__ = ["COVERAGE", "SHARPE_FOLD", "Z", "Band", "prediction"]

COVERAGE: Final = 0.90
"""The band's nominal coverage: how often a version that is what was certified lands inside."""

Z: Final = NormalDist().inv_cdf(0.5 + COVERAGE / 2)
"""The band's half-width in standard deviations of the difference: 1.645."""

SHARPE_FOLD: Final = 4
"""The fewest periods a Sharpe's scoring fold must hold for a band to be struck: below it the
fold's Sharpe has no variance to strike one from."""


@dataclass(frozen=True)
class Band:
    """The band one window's realised objective is judged against.

    `folds` holds the periods each fold the objective scores held, in window order, and
    `minimum` the fewest such a fold needs; a window with no scoring fold, or with one below
    the minimum, strikes no band — `low` and `high` are then the centre and nothing holds.
    """

    expected: float
    low: float
    high: float
    periods: int
    """The return periods the judged window held."""
    folds: tuple[int, ...] = ()
    minimum: int = 1
    coverage: float = COVERAGE

    @property
    def struck(self) -> bool:
        """Whether the window's scoring folds hold what a band needs."""
        return bool(self.folds) and min(self.folds) >= self.minimum

    def holds(self, value: float) -> bool:
        """Whether a realised value lies inside; a window of no period, or one that strikes
        no band, holds nothing."""
        return self.periods > 0 and self.struck and self.low <= value <= self.high


def prediction(
    objective: Objective,
    expectation: Mapping[str, object] | None,
    run: CardRun,
    folds: int,
) -> Band | None:
    """The band `run`'s value of `objective` over `folds` folds is judged against.

    `None` when the expectation records no value, error or period count — one composed
    before kanso recorded them — since there is then no dispersion to strike a band from.
    """
    value, se, periods = (_number(expectation, key) for key in ("value", "se", "periods"))
    if value is None or se is None or periods is None or periods <= 0:
        return None
    held = tuple(max(len(fold.returns), 1) for fold in run.folds(folds) if _scores(objective, fold))
    sharpe = objective.statistic == "sharpe"
    expected = value * len(held) / folds
    band = Band(
        expected=expected,
        low=expected,
        high=expected,
        periods=len(run.returns),
        folds=held,
        minimum=SHARPE_FOLD if sharpe else 1,
    )
    if not band.struck:
        return band
    weights = fsum((m - 1) / (m * (m - 3)) if sharpe else 1 / m for m in held)
    spread = se * sqrt(1 + periods * weights / folds**2)
    return replace(band, low=expected - Z * spread, high=expected + Z * spread)


def _scores(objective: Objective, fold: CardRun) -> bool:
    """Whether a fold holds what its statistic needs to be other than the constant zero."""
    if objective.statistic == "edge_bps":
        return any(trade.notional > 0 for trade in fold.trades)
    return len(fold.returns) >= (2 if objective.statistic == "sharpe" else 1)


def _number(expectation: Mapping[str, object] | None, key: str) -> float | None:
    """One recorded number, or `None` when the expectation does not record it."""
    found = (expectation or {}).get(key)
    if isinstance(found, bool) or not isinstance(found, int | float):
        return None
    return float(found)
