"""The quantities every objective and gate shares, on samples with known answers."""

from __future__ import annotations

from datetime import date
from math import acos, cos, pi, sqrt, tan
from statistics import NormalDist

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.criteria.quantities import (
    DAYS_PER_YEAR,
    contribution_bps,
    contribution_series,
    correlation,
    drawdown_pct,
    edge_bps,
    edge_series,
    long_run_factor,
    mean,
    moments,
    periods_per_year,
    sharpe,
    sharpe_difference_variance,
    sharpe_variance,
    standard_error,
    stdev,
    student_t_quantile,
    variance,
    variances_of_means,
    years,
)
from tests.criteria.builders import build_run, trade


def test_a_window_counts_both_of_its_end_days() -> None:
    assert years((date(2024, 1, 1), date(2024, 1, 1))) == 1 / DAYS_PER_YEAR
    assert years((date(2024, 1, 1), date(2024, 1, 10))) == 10 / DAYS_PER_YEAR


def test_the_sample_statistics_use_one_degree_of_freedom() -> None:
    assert mean([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert stdev([1.0, 2.0, 3.0, 4.0]) == pytest.approx((5 / 3) ** 0.5)
    assert variance([1.0, 2.0, 3.0, 4.0]) == pytest.approx(5 / 3)
    assert standard_error([1.0, 2.0, 3.0, 4.0]) == pytest.approx((5 / 3) ** 0.5 / 2)


def test_a_sample_too_short_to_vary_reports_no_dispersion() -> None:
    assert mean([]) == 0.0
    assert stdev([]) == 0.0
    assert stdev([7.0]) == 0.0
    assert standard_error([]) == 0.0


def test_annualisation_is_the_observed_periods_per_year() -> None:
    daily = build_run(tuple(1.0 for _ in range(10)), days=20)
    assert periods_per_year(daily) == pytest.approx(10 / (20 / DAYS_PER_YEAR))


def test_annualisation_can_be_pinned_to_a_constant() -> None:
    assert periods_per_year(build_run((1.0, 2.0)), annualisation=252) == 252.0


def test_sharpe_is_the_annualised_mean_over_the_dispersion() -> None:
    run = build_run((1.0, 2.0, 3.0, 4.0, 5.0))
    assert sharpe(run) == pytest.approx(3 / 2.5**0.5 * (5 / (5 / DAYS_PER_YEAR)) ** 0.5)


def test_a_series_that_cannot_vary_has_no_sharpe() -> None:
    assert sharpe(build_run((2.0, 2.0, 2.0))) == 0.0
    assert sharpe(build_run((2.0,))) == 0.0


def test_the_edge_per_trade_is_in_basis_points_of_the_position_opened() -> None:
    run = build_run(
        (0.0, 0.0),
        trades=(
            trade(date(2024, 1, 1), pnl=100.0, notional=10_000.0),
            trade(date(2024, 1, 2), pnl=-50.0, notional=10_000.0),
        ),
    )
    assert edge_bps(run) == pytest.approx((100.0 - 50.0) / 2)


def test_a_window_that_closed_nothing_has_no_edge() -> None:
    assert edge_bps(build_run((1.0, 2.0))) == 0.0


def test_drawdown_is_peak_to_trough_over_starting_capital() -> None:
    run = build_run((100.0, -200.0, 50.0), capital=1_000.0)
    assert run.equity == (1_100.0, 900.0, 950.0)
    assert drawdown_pct(run) == pytest.approx(20.0)


def test_a_run_that_only_loses_draws_down_from_its_capital() -> None:
    run = build_run((-100.0, -50.0), capital=1_000.0)
    assert drawdown_pct(run) == pytest.approx(15.0)


TURN = date(2024, 1, 30)
"""Four daily ends, January 30 to February 2: the month turns at the third."""


def test_a_surplus_swept_to_the_cushion_is_not_a_drawdown() -> None:
    """Up 150 by January 31, the first end of February moves it out and the book reads its
    capital; February's 10 is measured from the capital, not from January's 1,150."""
    run = build_run(
        (100.0, 50.0, 0.0, -10.0),
        start=TURN,
        capital=1_000.0,
        equity=(1_100.0, 1_150.0, 1_000.0, 990.0),
        cushion=(0.0, 0.0, 150.0, 150.0),
    )

    assert drawdown_pct(run) == pytest.approx(1.0)
    assert drawdown_pct(build_run((100.0, 50.0, -150.0, -10.0), capital=1_000.0)) == (
        pytest.approx(16.0)
    ), "the same curve read without the reset"


def test_a_loss_the_cushion_restores_counts_in_the_month_it_fell_in() -> None:
    """February's first end loses 100 and draws it back from the cushion: the book reads
    its capital, but the drawdown struck before the transfer is still the month's."""
    run = build_run(
        (0.0, 0.0, -100.0, 0.0),
        start=TURN,
        capital=1_000.0,
        equity=(1_000.0, 1_000.0, 1_000.0, 1_000.0),
        cushion=(500.0, 500.0, 400.0, 400.0),
    )

    assert drawdown_pct(run) == pytest.approx(10.0)


def test_a_loss_the_cushion_cannot_restore_carries_into_the_next_month() -> None:
    run = build_run((-100.0, 0.0, -50.0, -50.0), start=TURN, capital=1_000.0, cushion=(0.0,) * 4)

    assert run.equity == (900.0, 900.0, 850.0, 800.0)
    assert drawdown_pct(run) == pytest.approx(20.0)


def test_correlation_pairs_two_samples() -> None:
    assert correlation([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
    assert correlation([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_a_sample_that_cannot_vary_has_no_correlation() -> None:
    assert correlation([1.0], [2.0]) is None
    assert correlation([1.0, 1.0], [1.0, 2.0]) is None
    assert correlation([1.0, 2.0], [1.0, 1.0]) is None


def test_a_symmetric_sample_has_no_skew() -> None:
    shape = moments([-2.0, -1.0, 1.0, 2.0])
    assert shape is not None
    skewness, kurtosis = shape
    assert skewness == pytest.approx(0.0)
    assert kurtosis > 0


def test_a_sample_that_cannot_vary_has_no_moments() -> None:
    assert moments([3.0, 3.0, 3.0]) is None


def test_the_contribution_is_the_mean_return_per_period_in_basis_points_of_the_capital() -> None:
    """Ten, twenty and thirty dollars a day on $100,000: two basis points a day on average."""
    run = build_run((10.0, 20.0, 30.0), capital=100_000.0)
    assert contribution_bps(run) == pytest.approx(2.0)


def test_a_continuous_series_annualises_at_its_own_calendar() -> None:
    """A round-the-clock venue has a return period every calendar day, so a week of daily
    bars is seven periods over seven days: observed, `periods_per_year` is the year's own
    length in days, with no constant assumed."""
    from kanso.data.loaders.synthetic import SyntheticLoader

    loader = SyntheticLoader()
    ref = loader.discover(
        {
            "loader": "synthetic",
            "seed": 7,
            "instruments": ["DEMO"],
            "resolution": "1d",
            "calendar": "continuous",
            "start": "2024-03-04",
            "end": "2024-03-10",
        }
    )[0]
    ends = tuple(bar.ts_init for bar in loader.load(ref, ref.span))
    assert len(ends) == 7
    run = build_run(tuple(1.0 for _ in ends), start=ref.span[0], days=7, ends=ends)
    assert run.window == ref.span
    assert all(run.bounds[0] <= ts < run.bounds[1] for ts in ends)
    assert periods_per_year(run) == pytest.approx(DAYS_PER_YEAR, abs=1e-9)
    assert abs(periods_per_year(run) - 365) < 0.5


# -- the error of a mean ----------------------------------------------------------------


def test_the_allowance_widens_a_persistent_series_by_its_bartlett_weighted_autocovariance() -> None:
    """1, 2, 3, 4: deviations of -1.5, -0.5, 0.5 and 1.5, a lag of one at four points, an
    autocovariance of 1.25 against a variance of 5, weighted by a half: 1 + 2 x 0.125."""
    assert long_run_factor([1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.25)


def test_the_allowance_never_narrows_an_error() -> None:
    """A series whose neighbours disagree is not evidence its mean is known better."""
    assert long_run_factor([1.0, -1.0] * 50) == 1.0


def test_the_allowance_has_nothing_to_estimate_on_a_short_or_flat_series() -> None:
    assert long_run_factor([]) == long_run_factor([3.0, 9.0]) == 1.0
    assert long_run_factor([2.0, 2.0, 2.0, 2.0]) == 1.0


def test_the_allowance_reads_four_lags_at_a_hundred_and_twenty_two_sessions() -> None:
    """floor(4 (122/100)^(2/9)) is four: a series that holds each value for ten sessions is
    widened more than one that holds it for five, and one that holds it for two — positive at
    lag one, negative at two and three — is not widened at all."""
    tens = [float(i // 10 % 2) for i in range(122)]
    fives = [float(i // 5 % 2) for i in range(122)]
    twos = [float(i // 2 % 2) for i in range(122)]
    assert long_run_factor(tens) > long_run_factor(fives) > 1.0 == long_run_factor(twos)


@given(st.lists(st.floats(-1e6, 1e6, allow_nan=False), max_size=60))
def test_the_allowance_is_at_least_one_and_finite(values: list[float]) -> None:
    factor = long_run_factor(values)
    assert factor >= 1.0
    assert factor < float("inf")


def test_each_fold_mean_varies_by_the_window_s_dispersion_over_its_own_count() -> None:
    """Six points of variance 3.5 cut 3, 2, 1 and an empty fold, which varies by nothing;
    the points alternate, so the allowance leaves them alone."""
    series = [1.0, 4.0, 1.0, 4.0, 1.0, 4.0]
    spread = variance(series)
    assert variances_of_means(series, [3, 2, 1, 0]) == pytest.approx(
        [spread / 3, spread / 2, spread, 0.0]
    )


def test_the_edge_series_is_in_the_order_trades_closed_and_skips_no_notional() -> None:
    later = trade(date(2024, 1, 3), pnl=30.0)
    earlier = trade(date(2024, 1, 1), pnl=10.0)
    empty = trade(date(2024, 1, 2), pnl=5.0, notional=0.0)
    run = build_run((0.0, 0.0, 0.0), trades=(later, empty, earlier))
    assert edge_series(run) == pytest.approx((10.0, 30.0))
    assert edge_bps(run) == pytest.approx(20.0)


def test_the_contribution_series_is_each_period_on_the_capital() -> None:
    run = build_run((10.0, -20.0), capital=100_000.0)
    assert contribution_series(run) == pytest.approx((1.0, -2.0))
    assert mean(contribution_series(run)) == pytest.approx(contribution_bps(run))


WEEK = (date(2024, 1, 1), date(2024, 1, 7))


def test_a_sharpe_varies_by_mertens_over_its_periods_annualised() -> None:
    """Normal returns: (1 + SR^2/2) / (n - 1) per period, times periods per year."""
    returns = [1.0, 3.0, 2.0, 4.0]
    ratio = mean(returns) / stdev(returns)
    per_year = 4 / years(WEEK)
    expected = per_year * (1 + ratio**2 / 2) / 3
    assert sharpe_variance(returns, WEEK, 1.0, None) == pytest.approx(expected)
    assert sharpe_variance(returns, WEEK, 2.0, None) == pytest.approx(2 * expected)


def test_a_sharpe_reads_the_shape_of_the_returns_and_falls_back_where_it_is_not_positive() -> None:
    returns = [1.0, 3.0, 2.0, 4.0]
    ratio = mean(returns) / stdev(returns)
    per_year = 4 / years(WEEK)
    skewed = sharpe_variance(returns, WEEK, 1.0, (0.5, 4.0))
    assert skewed == pytest.approx(per_year * (1 - 0.5 * ratio + 0.75 * ratio**2) / 3)
    assert sharpe_variance(returns, WEEK, 1.0, (50.0, 1.0)) == pytest.approx(
        sharpe_variance(returns, WEEK, 1.0, None)
    )


def test_a_sharpe_that_cannot_vary_varies_by_nothing() -> None:
    assert sharpe_variance([2.0], WEEK, 1.0, None) == 0.0
    assert sharpe_variance([2.0, 2.0, 2.0], WEEK, 1.0, None) == 0.0


def test_a_paired_difference_of_sharpes_varies_by_memmel() -> None:
    own, against = [1.0, 3.0, 2.0, 4.0], [2.0, 2.5, 1.0, 3.0]
    a, b = mean(own) / stdev(own), mean(against) / stdev(against)
    rho = 0.6
    per_year = 4 / years(WEEK)
    expected = per_year * (2 * (1 - rho) + (a**2 + b**2 - 2 * a * b * rho**2) / 2) / 3
    assert sharpe_difference_variance(own, against, WEEK, 1.0, rho) == pytest.approx(expected)
    assert sharpe_difference_variance(own, own, WEEK, 1.0, 1.0) == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("probability", "df", "tabled"),
    [
        (0.975, 1, 12.7062),
        (0.975, 2, 4.3027),
        (0.975, 3, 3.1824),
        (0.975, 10, 2.2281),
        (0.95, 30, 1.6973),
        (0.995, 29, 2.7564),
        (0.975, 120, 1.9799),
    ],
)
def test_a_student_t_quantile_is_the_tabled_one(probability: float, df: int, tabled: float) -> None:
    """Against the printed table of the t distribution, to the four places it prints."""
    assert student_t_quantile(probability, df) == pytest.approx(tabled, abs=5e-5)


@given(st.floats(min_value=0.5, max_value=0.999999))
def test_a_student_t_quantile_has_the_closed_forms_of_one_two_and_four_degrees(
    probability: float,
) -> None:
    """One degree of freedom is the Cauchy, two and four have quantiles in closed form."""
    u = probability
    shape = 4 * u * (1 - u)
    four = 2 * sqrt(cos(acos(sqrt(shape)) / 3) / sqrt(shape) - 1)
    assert student_t_quantile(u, 1) == pytest.approx(tan(pi * (u - 0.5)), rel=1e-9, abs=1e-12)
    assert student_t_quantile(u, 2) == pytest.approx(
        (2 * u - 1) / sqrt(2 * u * (1 - u)), rel=1e-9, abs=1e-12
    )
    assert student_t_quantile(u, 4) == pytest.approx(four, rel=1e-7, abs=1e-7)


def test_a_student_t_quantile_is_symmetric_and_falls_to_the_normal_s() -> None:
    assert student_t_quantile(0.5, 7) == 0.0
    assert student_t_quantile(0.025, 3) == -student_t_quantile(0.975, 3)
    normal = NormalDist().inv_cdf(0.975)
    assert student_t_quantile(0.975, 5000) == pytest.approx(normal, abs=1e-3)
    assert student_t_quantile(0.975, 5000) > normal
