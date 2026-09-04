"""The quantities every objective and gate shares, on samples with known answers."""

from __future__ import annotations

from datetime import date

import pytest

from kanso.criteria.quantities import (
    DAYS_PER_YEAR,
    correlation,
    drawdown_pct,
    edge_bps,
    mean,
    moments,
    periods_per_year,
    sharpe,
    standard_error,
    stdev,
    variance,
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
