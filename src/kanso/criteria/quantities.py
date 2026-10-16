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
  still reports the loss it made; on a monthly-reset book the peak starts again at each
  month's first end, so a drawdown is bounded by the month it fell in;
* **sample statistics use one degree of freedom**. A series too short for that reports a
  zero dispersion rather than raising, so a fold with nothing in it cannot crash a card;
* a **trade** is a closed position, so a per-trade quantity averages over closed positions
  only and a window that closed nothing has no edge to report;
* the **error of a mean** is read off the series the mean is taken of — the per-period
  returns, the per-trade edges — never off a handful of summaries of it, and is widened, and
  never narrowed, for the serial correlation the series shows (`long_run_factor`).

A year is 365.25 days: the average Gregorian year, which is what makes an observed count
of periods per year comparable between a calendar with weekends in it and one without.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from math import cos, floor, fsum, pi, sin, sqrt, tan

import numpy as np

from kanso.criteria.run import BPS, CardRun, day_of

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


def student_t_quantile(probability: float, df: int) -> float:
    """The `probability` quantile of a Student's t of `df` degrees of freedom, `df` at least one.

    For a whole number of degrees of freedom the chance `P(|T| ≤ √df · tan θ)` is a finite sum
    in `θ` (Abramowitz and Stegun 26.7.3 and 26.7.4), so the quantile is that sum inverted by
    bisection on `θ` in `[0, π/2)`, to the float's resolution, with nothing beyond the
    standard library: at one degree of freedom it is the Cauchy's `tan(π(p − ½))`, and as
    `df` grows it falls to the normal's. The distribution is symmetric, so a probability
    below a half is the negative of its complement's quantile, and a half is zero exactly.
    """
    if probability < 0.5:
        return -student_t_quantile(1.0 - probability, df)
    target = 2.0 * probability - 1.0
    low, high = 0.0, pi / 2
    for _ in range(64):
        middle = (low + high) / 2
        if _t_central(middle, df) < target:
            low = middle
        else:
            high = middle
    return sqrt(df) * tan(low)


def _t_central(theta: float, df: int) -> float:
    """`P(|T| ≤ √df · tan θ)` for a Student's t of `df` degrees of freedom: for an even `df`,
    `sin θ` times `1 + ½cos²θ + (1·3)/(2·4)cos⁴θ + …` to the power `df − 2`; for an odd one,
    `(2/π)(θ + sin θ (cos θ + ⅔cos³θ + (2·4)/(3·5)cos⁵θ + …))` to the power `df − 2`."""
    sine, cosine = sin(theta), cos(theta)
    squared = cosine * cosine
    if df % 2 == 0:
        term = total = 1.0
        for j in range(1, df // 2):
            term *= (2 * j - 1) / (2 * j) * squared
            total += term
        return sine * total
    term, total = cosine, 0.0
    for j in range((df - 1) // 2):
        total += term
        term *= (2 * j + 2) / (2 * j + 3) * squared
    return 2 / pi * (theta + sine * total)


def long_run_factor(values: Sequence[float]) -> float:
    """How much wider serial correlation makes the error of this series' mean; at least one.

    The Newey–West ratio of the long-run variance to the variance: one plus twice the
    Bartlett-weighted autocovariances over the variance, `1 + 2 Σ (1 − l/(L+1)) γ_l / γ_0`,
    at the customary lag `L = floor(4 (n/100)^(2/9))` — four at a hundred periods, eleven at
    ten thousand trades. It is floored at one: a series whose neighbours disagree more than
    chance would have them is not taken as evidence that its mean is better known than an
    independent series of that length says, so the allowance only ever widens an error.

    The error it widens replaced the spread of four fold means, which was, without saying
    so, robust to correlation inside a fold — a batch-means estimator, with three degrees of
    freedom. Read off the series, the error has the degrees of freedom the series has, and
    this factor is what keeps the robustness: a strategy whose returns persist from one
    period to the next does not have its error divided by the root of a count of periods
    that were not independent. Below three points there is nothing to estimate and the
    factor is one.
    """
    n = len(values)
    if n < 3:
        return 1.0
    deviations = np.asarray(values, dtype=float) - mean(values)
    base = float(deviations @ deviations)
    if base == 0.0:
        return 1.0
    lags = min(n - 1, floor(4 * (n / 100) ** (2 / 9)))
    weighted = fsum(
        (1 - lag / (lags + 1)) * float(deviations[lag:] @ deviations[:-lag])
        for lag in range(1, lags + 1)
    )
    return max(1.0, 1 + 2 * weighted / base)


def variances_of_means(series: Sequence[float], counts: Sequence[int]) -> list[float]:
    """The sampling variance of each fold's mean, for a series `counts` cuts into folds.

    The dispersion is the whole window's — every fold's points are draws of one series, and a
    fold of three sessions has no dispersion of its own worth the name — widened by
    `long_run_factor`, over each fold's own count. A fold that holds nothing scores the
    constant zero, which varies by nothing.
    """
    spread = variance(series) * long_run_factor(series)
    return [spread / n if n else 0.0 for n in counts]


def sharpe_variance(
    returns: Sequence[float],
    window: tuple[date, date],
    factor: float,
    shape: tuple[float, float] | None,
) -> float:
    """The sampling variance of one fold's annualised Sharpe, in annualised units squared.

    Mertens' expression for the Sharpe of non-normal returns, the one `deflated_sharpe` has
    always deflated by: `(1 − γ₃·SR + (γ₄ − 1)/4·SR²) / (n − 1)` for a per-period `SR` over `n`
    periods, `γ₃` the skewness and `γ₄` the non-excess kurtosis — `shape`, read off the whole
    window, since a fold's few periods place a fourth moment nowhere — scaled by the periods
    per year the fold observed and by `factor`, the window's `long_run_factor`. Where `shape`
    is `None` or the expression is not positive it is the normal one, `1 + SR²/2`. A fold whose
    Sharpe is the constant zero — fewer than two periods, or a return that never varied —
    varies by nothing.
    """
    dispersion = stdev(returns)
    if dispersion == 0.0:
        return 0.0
    n = len(returns)
    ratio = mean(returns) / dispersion
    spread = 1 + ratio**2 / 2
    if shape is not None:
        skewness, kurtosis = shape
        shaped = 1 - skewness * ratio + (kurtosis - 1) / 4 * ratio**2
        spread = shaped if shaped > 0 else spread
    return n / years(window) * factor * spread / (n - 1)


def sharpe_difference_variance(
    own: Sequence[float],
    against: Sequence[float],
    window: tuple[date, date],
    factor: float,
    rho: float,
) -> float:
    """The sampling variance of one fold's difference of two annualised Sharpes on paired
    returns: the same periods, one return of each run in each, both of which vary.

    Memmel's correction of the Jobson–Korkie statistic, `(2(1 − ρ) + ½(SR_a² + SR_b² −
    2·SR_a·SR_b·ρ²)) / (n − 1)` per period, `ρ` the correlation of the two series over the
    window, scaled by the periods per year the fold observed and by `factor`. The pairing is
    the point: a modifier that moves its host's Sharpe a little has a difference far better
    known than either Sharpe, because most of what varies is the host, and it varies in both.
    The expression is never negative; the floor only absorbs a correlation rounded past one.
    """
    n = len(own)
    a = mean(own) / stdev(own)
    b = mean(against) / stdev(against)
    spread = 2 * (1 - rho) + (a**2 + b**2 - 2 * a * b * rho**2) / 2
    return n / years(window) * factor * max(spread, 0.0) / (n - 1)


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


def edge_series(run: CardRun) -> tuple[float, ...]:
    """Net profit per closed trade in basis points of the position opened, in the order the
    trades closed: the series `edge_bps` is the mean of, and its error is read off."""
    closed = sorted((t for t in run.trades if t.notional > 0), key=lambda t: t.closed_ns)
    return tuple(t.pnl_net / t.notional * BPS for t in closed)


def edge_bps(run: CardRun) -> float:
    """Mean net profit per closed trade, in basis points of the position opened."""
    return mean(edge_series(run))


def contribution_series(run: CardRun) -> tuple[float, ...]:
    """Each period's net return in basis points of the capital: the series a contribution is
    the mean of, and its error is read off."""
    return tuple(r / run.capital * BPS for r in run.returns)


def contribution_bps(run: CardRun) -> float:
    """Mean net return per return period, in basis points of the capital: what the book
    earned per period on the capital it was given, the unit a P&L target is stated in.

    A period in cash contributes zero, so a strategy that trades less contributes less; a
    per-trade edge cannot say that, and a Sharpe cannot say how much of the capital was
    put to work. Not annualised: the period is the workspace's own return period. A run's
    capital is positive by construction (`CardRun`), so the quotient always exists.
    """
    return mean(run.returns) / run.capital * BPS


def drawdown_pct(run: CardRun) -> float:
    """Peak-to-trough equity over the window, as a percentage of starting capital.

    On a book a monthly reset returns to its capital — a run that carries `cushion` — a
    drawdown is bounded by the month it fell in. Each end is judged twice: on the equity
    struck before that end's transfer, against the peak so far, and on the book after it.
    At the first end of a month, and at a run's first end, which a restart may have reset,
    the peak is then taken back to the higher of the capital and the book after the
    transfer. A surplus swept into the cushion is therefore never read as a loss, a loss
    the cushion restored ends with its month, and a loss it could not restore carries into
    the next month as a drawdown from the capital.
    """
    peak = run.capital
    worst = 0.0
    if not run.cushion:
        for value in run.equity:
            peak = max(peak, value)
            worst = max(worst, peak - value)
        return worst / run.capital * 100.0
    previous_equity = run.capital
    previous_month: tuple[int, int] | None = None
    for end, made, value in zip(run.period_ends_ns, run.returns, run.equity, strict=True):
        struck = previous_equity + made
        peak = max(peak, struck)
        worst = max(worst, peak - struck)
        day = day_of(end)
        if previous_month != (day.year, day.month):
            peak = max(run.capital, value)
        peak = max(peak, value)
        worst = max(worst, peak - value)
        previous_equity, previous_month = value, (day.year, day.month)
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
