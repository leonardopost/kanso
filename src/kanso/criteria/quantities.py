"""The quantities every objective and gate shares, defined once.

Nothing in the toolbox is allowed its own definition of a return, an annualisation, a
drawdown or a sample statistic, because a number that means two things cannot be compared
across cards, certificates and stages. The definitions are the shipped defaults of the
workspace's research settings:

* a **return** is the mark-to-market equity change over one return period containing at
  least one data event, in the account currency. The runner produces the series; this
  module never re-derives it;
* **annualisation** is observed, not assumed: the number of return periods the window
  actually held, per year. A round-the-clock series and a five-session week are therefore
  each scaled by their own calendar, and no constant is hard-coded. A workspace that pins
  a constant instead passes it as `annualisation`;
* a **drawdown** is peak-to-trough equity over the window as a percentage of starting
  capital — the same base the hypothesis's maximum-drawdown limit is expressed in — with
  the peak seeded at the capital the window opened with, so a run that only ever loses
  still reports the loss it made;
* **sample statistics use one degree of freedom**. A series too short for that reports a
  zero dispersion rather than raising, so a fold with nothing in it cannot crash a card;
* a **trade** is a closed position, so a per-trade quantity averages over closed positions
  only and a window that closed nothing has no edge to report.

A year is 365.25 days: the average Gregorian year, which is what makes an observed count
of periods per year comparable between a calendar with weekends in it and one without.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from math import fsum, sqrt

from kanso.criteria.run import BPS, CardRun

DAYS_PER_YEAR = 365.25
AUTO = "auto"
"""Annualisation from the observed periods per year rather than a fixed constant."""


def years(window: tuple[date, date]) -> float:
    """The window's length in years, counting both end days."""
    return ((window[1] - window[0]).days + 1) / DAYS_PER_YEAR


def mean(values: Sequence[float]) -> float:
    """The arithmetic mean; an empty sample has none, and reports zero."""
    if not values:
        return 0.0
    return fsum(values) / len(values)


def stdev(values: Sequence[float]) -> float:
    """The sample standard deviation with one degree of freedom; zero below two points."""
    n = len(values)
    if n < 2:
        return 0.0
    m = mean(values)
    return sqrt(fsum((v - m) ** 2 for v in values) / (n - 1))


def variance(values: Sequence[float]) -> float:
    """The sample variance with one degree of freedom."""
    return stdev(values) ** 2


def standard_error(values: Sequence[float]) -> float:
    """The standard error of the mean: the dispersion divided by the root of the count."""
    if not values:
        return 0.0
    return stdev(values) / sqrt(len(values))


def periods_per_year(run: CardRun, annualisation: float | str = AUTO) -> float:
    """The observed number of return periods per year, or the constant that replaces it."""
    if annualisation != AUTO:
        return float(annualisation)
    return len(run.returns) / years(run.window)


def sharpe(run: CardRun, annualisation: float | str = AUTO) -> float:
    """Annualised Sharpe of the net return series at a zero risk-free rate."""
    dispersion = stdev(run.returns)
    if dispersion == 0.0:
        return 0.0
    return mean(run.returns) / dispersion * sqrt(periods_per_year(run, annualisation))


def edge_bps(run: CardRun) -> float:
    """Mean net profit per closed trade, in basis points of the position opened."""
    return mean([t.pnl_net / t.notional * BPS for t in run.trades if t.notional > 0])


def drawdown_pct(run: CardRun) -> float:
    """Peak-to-trough equity over the window, as a percentage of starting capital."""
    peak = run.capital
    worst = 0.0
    for value in run.equity:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return worst / run.capital * 100.0


def correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Pearson correlation of two paired samples; `None` when either cannot vary."""
    if len(left) < 2:
        return None
    spread_left, spread_right = stdev(left), stdev(right)
    if spread_left == 0.0 or spread_right == 0.0:
        return None
    mean_left, mean_right = mean(left), mean(right)
    covariance = fsum(
        (a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True)
    ) / (len(left) - 1)
    return covariance / (spread_left * spread_right)


def moments(values: Sequence[float]) -> tuple[float, float] | None:
    """Sample skewness and (non-excess) kurtosis; `None` when the sample cannot vary."""
    dispersion = stdev(values)
    if dispersion == 0.0:
        return None
    m = mean(values)
    third = fsum((v - m) ** 3 for v in values) / len(values)
    fourth = fsum((v - m) ** 4 for v in values) / len(values)
    return third / dispersion**3, fourth / dispersion**4
