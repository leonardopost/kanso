"""The band a stage's realised objective is judged against, struck for its own window."""

from __future__ import annotations

from datetime import timedelta
from math import sqrt
from statistics import NormalDist
from typing import Any

import numpy as np
import pytest

from kanso.criteria.band import COVERAGE, Band, Z, prediction
from kanso.criteria.objectives import net_edge_bps, wf_contribution_bps, wf_sharpe_net
from tests.criteria.builders import START, build_run, trade

CAPITAL = 100_000.0


def expected(value: float, se: float, periods: int) -> dict[str, Any]:
    """An expectation as composition records it, reduced to what a band reads."""
    return {"objective_id": "wf_contribution_bps", "value": value, "se": se, "periods": periods}


def test_a_window_in_equal_folds_widens_the_value_s_error_by_its_own() -> None:
    """Twenty days in four folds of five against 120 certified: `se × √(1 + N/M)`."""
    run = build_run((0.0,) * 20)

    band = prediction(wf_contribution_bps, expected(4.0, 0.5, 120), run, 4)

    assert band is not None
    assert band.expected == 4.0
    assert band.high - 4.0 == pytest.approx(Z * 0.5 * sqrt(1 + 120 / 20))
    assert 4.0 - band.low == pytest.approx(band.high - 4.0)
    assert (band.periods, band.coverage) == (20, COVERAGE)
    assert pytest.approx(NormalDist().inv_cdf(0.95)) == Z


def test_a_shorter_window_strikes_a_wider_band() -> None:
    bands = [
        prediction(wf_contribution_bps, expected(4.0, 0.5, 120), build_run((0.0,) * days), 4)
        for days in (8, 20, 60)
    ]

    widths = [band.high - band.low for band in bands if band is not None]
    assert widths[0] > widths[1] > widths[2] > 2 * Z * 0.5


def test_a_fold_the_window_leaves_empty_scores_zero_and_moves_the_centre_with_it() -> None:
    """Three days in four folds: one fold holds no period, so the window realises three
    quarters of the per-fold value even when nothing has changed."""
    run = build_run((40.0, 40.0, 40.0), days=4)
    assert [len(fold.returns) for fold in run.folds(4)] == [1, 1, 1, 0]

    band = prediction(wf_contribution_bps, expected(4.0, 0.5, 120), run, 4)

    assert band is not None
    assert band.expected == pytest.approx(3.0)
    assert band.high - band.expected == pytest.approx(Z * 0.5 * sqrt(1 + 120 * 3 / 16))
    assert band.holds(wf_contribution_bps.compute(run, 4)[0])


def test_each_statistic_counts_the_folds_it_can_score() -> None:
    """A per-trade edge's fold that closed no trade scores zero, and a Sharpe's fold of one
    period does; neither moves the band as a fold that held its value would."""
    days = (0.0,) * 8
    traded = build_run(days, trades=(trade(START, pnl=10.0), trade(START + timedelta(1), 5.0)))
    edge = prediction(net_edge_bps, expected(10.0, 1.0, 120), traded, 4)
    sharpe = prediction(wf_sharpe_net, expected(1.0, 0.2, 120), build_run((0.0,) * 5), 4)
    contribution = prediction(wf_contribution_bps, expected(1.0, 0.2, 120), build_run(days), 4)

    assert edge is not None and edge.expected == pytest.approx(10.0 / 4)
    assert sharpe is not None and sharpe.expected == pytest.approx(1.0 / 4)
    assert contribution is not None and contribution.expected == pytest.approx(1.0)


@pytest.mark.parametrize(
    "recorded",
    [
        None,
        {"value": 4.0, "ci90": [3.0, 5.0]},
        {"value": 4.0, "se": 0.5},
        {"value": 4.0, "se": 0.5, "periods": 0},
        {"value": "4", "se": 0.5, "periods": 120},
        {"value": 4.0, "se": True, "periods": 120},
    ],
)
def test_an_expectation_that_records_no_dispersion_strikes_no_band(recorded: Any) -> None:
    assert prediction(wf_contribution_bps, recorded, build_run((0.0,) * 20), 4) is None


def test_a_window_of_no_period_holds_nothing() -> None:
    empty = Band(expected=0.0, low=-1.0, high=1.0, periods=0, folds=(1,))

    assert not empty.holds(0.0)
    assert Band(expected=0.0, low=-1.0, high=1.0, periods=3, folds=(3,)).holds(0.0)


def test_a_band_with_no_scoring_fold_or_a_thin_one_is_not_struck_and_holds_nothing() -> None:
    assert not Band(expected=0.0, low=-1.0, high=1.0, periods=3).struck
    assert not Band(expected=0.0, low=-1.0, high=1.0, periods=3).holds(0.0)
    thin = Band(expected=0.0, low=-1.0, high=1.0, periods=9, folds=(4, 3, 2), minimum=4)
    assert not thin.struck and not thin.holds(0.0)


def test_a_sharpe_fold_is_weighed_by_its_exact_small_sample_variance() -> None:
    """Twenty days in four folds of five: a Sharpe's fold weighs `(m − 1) / (m (m − 3))`,
    twice a mean's `1/m` at five periods."""
    run = build_run(tuple(float(day % 3) for day in range(20)))

    band = prediction(wf_sharpe_net, expected(1.5, 0.4, 250), run, 4)

    assert band is not None and band.struck
    assert band.folds == (5, 5, 5, 5) and band.minimum == 4
    weights = 4 * (5 - 1) / (5 * (5 - 3))
    assert band.high - 1.5 == pytest.approx(Z * 0.4 * sqrt(1 + 250 * weights / 16))
    mean_s = prediction(wf_contribution_bps, expected(1.5, 0.4, 250), run, 4)
    assert mean_s is not None and band.high - band.low > mean_s.high - mean_s.low


@pytest.mark.parametrize("days", [8, 12])
def test_a_sharpe_window_whose_folds_hold_fewer_than_four_periods_strikes_no_band(
    days: int,
) -> None:
    """Two or three periods a fold: a Sharpe of so few has no variance, so no band is struck,
    and the window holds nothing until it has grown."""
    run = build_run(tuple(float(day % 3) for day in range(days)))

    band = prediction(wf_sharpe_net, expected(1.5, 0.4, 250), run, 4)

    assert band is not None and not band.struck
    assert band.folds == (days // 4,) * 4
    assert (band.low, band.high) == (band.expected, band.expected)
    assert not band.holds(band.expected)


def test_a_per_trade_edge_window_that_closed_no_trade_strikes_no_band() -> None:
    """Such a window realises zero whatever the version is, which a band centred on the folds
    it filled — none — would hold."""
    run = build_run((0.0,) * 20)

    band = prediction(net_edge_bps, expected(10.0, 1.0, 120), run, 4)

    assert band is not None and not band.struck and band.folds == ()
    assert band.expected == 0.0 == net_edge_bps.compute(run, 4)[0]
    assert not band.holds(0.0)


def _coverage(paper_days: int, trials: int, seed: int) -> tuple[float, float]:
    """How often a version that is what was certified lands inside its prediction band, and
    inside a ninety-percent confidence band of the certified value, over `paper_days`
    against 120 certified: the per-period process is the same in both windows."""
    rng = np.random.default_rng(seed)
    inside = confident = 0
    for _ in range(trials):
        certified = build_run(tuple(rng.normal(40.0, 400.0, 120)), capital=CAPITAL)
        value, se = wf_contribution_bps.compute(certified, 4)
        paper = build_run(tuple(rng.normal(40.0, 400.0, paper_days)), capital=CAPITAL)
        realised = wf_contribution_bps.compute(paper, 4)[0]
        band = prediction(wf_contribution_bps, expected(value, se, 120), paper, 4)
        assert band is not None
        inside += band.holds(realised)
        confident += abs(realised - value) <= Z * se
    return inside / trials, confident / trials


@pytest.mark.parametrize("paper_days", [5, 20, 60])
def test_a_version_that_is_what_was_certified_lands_inside_nine_times_in_ten(
    paper_days: int,
) -> None:
    """Simulated: 600 paper windows of each length, each against its own certification.

    A confidence band of the certified value — what `paper_forward` used to judge by — holds
    the same versions `P(|Z| ≤ 1.645 / √(1 + 120/M))` of the time: 0.26 at five days, 0.47 at
    twenty, 0.66 at sixty. Measured with these seeds: 0.92, 0.90 and 0.90 inside the
    prediction band; 0.23, 0.51 and 0.67 inside the confidence band.
    """
    inside, confident = _coverage(paper_days, 600, seed=paper_days)
    old = 2 * NormalDist().cdf(Z / sqrt(1 + 120 / paper_days)) - 1

    assert inside == pytest.approx(COVERAGE, abs=0.04)
    assert confident == pytest.approx(old, abs=0.06)
    assert confident < inside


def _sharpe_coverage(paper_days: int, trials: int, seed: int) -> tuple[float, float]:
    """How often a version that is what was certified lands inside the Sharpe band struck for
    `paper_days` against 250 certified, and how often below it, as `live_drift` judges."""
    rng = np.random.default_rng(seed)
    inside = below = 0
    for _ in range(trials):
        certified = build_run(tuple(rng.normal(100.0, 1000.0, 250)), capital=CAPITAL)
        value, se = wf_sharpe_net.compute(certified, 4)
        paper = build_run(tuple(rng.normal(100.0, 1000.0, paper_days)), capital=CAPITAL)
        realised = wf_sharpe_net.compute(paper, 4)[0]
        band = prediction(wf_sharpe_net, expected(value, se, 250), paper, 4)
        assert band is not None and band.struck
        inside += band.holds(realised)
        below += realised < band.low
    return inside / trials, below / trials


@pytest.mark.parametrize("paper_days", [16, 40])
def test_a_sharpe_version_that_is_what_was_certified_lands_inside_nine_times_in_ten(
    paper_days: int,
) -> None:
    """Simulated: 600 paper windows of four and of ten days a fold. A fold's Sharpe weighed by
    `1/m`, as a mean's error is, held 0.75 and 0.87 of them and let 0.10 and 0.05 fall below
    (October 2026, these seeds); weighed by its exact variance it holds 0.91 and 0.91, and
    0.03 and 0.03 fall below."""
    inside, below = _sharpe_coverage(paper_days, 600, seed=paper_days)

    assert inside == pytest.approx(COVERAGE, abs=0.04)
    assert below <= 0.05


def test_a_version_whose_edge_is_gone_falls_below_its_band() -> None:
    """Certified at 4 bp a day at a dispersion of 4 over 120 days, the same book with its edge
    gone lands inside the band sixty paper days strike, whose lower end sits near 3 bp a day,
    in none of 400 tries with this seed."""
    rng = np.random.default_rng(7)
    passed = 0
    for _ in range(400):
        certified = build_run(tuple(rng.normal(40.0, 40.0, 120)), capital=CAPITAL)
        value, se = wf_contribution_bps.compute(certified, 4)
        dead = build_run(tuple(rng.normal(0.0, 40.0, 60)), capital=CAPITAL)
        band = prediction(wf_contribution_bps, expected(value, se, 120), dead, 4)
        assert band is not None
        passed += band.holds(wf_contribution_bps.compute(dead, 4)[0])

    assert passed / 400 < 0.10
