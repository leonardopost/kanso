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
`s² Σ 1/m_k / K²`, and

    band = centre ± z₀.₉₅ × √(se² + se² × N × Σ 1/m_k / K²)

with `z₀.₉₅ = 1.645`, so a version that is what was certified lands inside it nine times in
ten whatever the window's length: the band widens as the window shortens, instead of the
window being judged at a precision it cannot have. For `K` equal folds of a window of `M`
periods the half-width is `1.645 × se × √(1 + N/M)`.

**The centre is the certified value scaled to the folds the window filled.** The objective
scores a fold that holds nothing as zero — a fold with no period, a per-trade edge's fold
that closed no trade, a Sharpe's fold of fewer than two periods — so a window that leaves one
of four folds empty realises three quarters of the per-fold value even when nothing has
changed. The centre is `value × K′/K`, `K′` the folds that held something; a certification
window is taken to have filled all of its own.

What it is not: it is not a test that the version still trades as certified fill by fill —
the stage is a replay into the same simulated venue — and its coverage is a normal
approximation, closer the more periods both windows hold. A window that holds no period at
all is judged by no band.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import fsum, sqrt
from statistics import NormalDist
from typing import Final

from kanso.criteria.objectives import Objective
from kanso.criteria.run import CardRun

__all__ = ["COVERAGE", "Z", "Band", "prediction"]

COVERAGE: Final = 0.90
"""How often a version that is what was certified lands inside its band."""

Z: Final = NormalDist().inv_cdf(0.5 + COVERAGE / 2)
"""The band's half-width in standard deviations of the difference: 1.645."""


@dataclass(frozen=True)
class Band:
    """The band one window's realised objective is judged against."""

    expected: float
    low: float
    high: float
    periods: int
    """The return periods the judged window held."""
    coverage: float = COVERAGE

    def holds(self, value: float) -> bool:
        """Whether a realised value lies inside; a window of no period holds nothing."""
        return self.periods > 0 and self.low <= value <= self.high


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
    held = [max(len(fold.returns), 1) for fold in run.folds(folds) if _scores(objective, fold)]
    expected = value * len(held) / folds
    spread = se * sqrt(1 + periods * fsum(1 / count for count in held) / folds**2)
    return Band(
        expected=expected,
        low=expected - Z * spread,
        high=expected + Z * spread,
        periods=len(run.returns),
    )


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
