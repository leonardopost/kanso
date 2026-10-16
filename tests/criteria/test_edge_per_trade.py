"""`edge_per_trade`: a card's trades make money on average, by more than their own error.

It reads the numbers a card's digest records — the per-trade objective over the window as
one fold — so what the proposer and the operator are shown is what the gate judged.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import timedelta
from math import acos, cos, pi, sqrt, tan
from statistics import NormalDist

import pytest

from kanso.criteria import CardRun
from kanso.criteria.digest import digest
from kanso.criteria.gates import edge_per_trade, expected_maximum, periods_margin, search_margin
from kanso.schemas import GateResult
from tests.criteria.builders import START, build_run, context, trade


def run_of(*edges_bps: float) -> CardRun:
    """One trade a session on 10,000 of notional, netting each edge in turn: a profit of
    one currency unit on that notional is one basis point."""
    trades = tuple(trade(START + timedelta(days=day), edge) for day, edge in enumerate(edges_bps))
    return build_run((1.0,) * len(edges_bps), trades=trades)


def judged(run: CardRun, trials: int = 1, **params: float) -> GateResult:
    return edge_per_trade.evaluate(context(run, params=params, trials=trials))


WINNING = run_of(12.0, 8.0, 10.0, 11.0, 9.0, 10.0)
"""Six trades netting 10 bp on average, each within two of it."""


def test_trades_that_pay_on_average_pass_and_say_by_how_much() -> None:
    result = judged(WINNING, min_bps=0.0, k_se=1.0)

    recorded = digest(WINNING)
    assert result.passed
    assert result.evidence["net_bps_per_trade"] == recorded.net_bps_per_trade
    assert result.evidence["se"] == recorded.net_bps_per_trade_se
    assert result.evidence["lower"] == pytest.approx(
        result.evidence["net_bps_per_trade"] - result.evidence["margin"] * result.evidence["se"]
    )
    assert (result.evidence["k_se"], result.evidence["min_bps"]) == (1.0, 0.0)
    assert (result.evidence["n_trades"], result.evidence["periods_with_trades"]) == (6, 6)
    assert (result.evidence["trials"], result.evidence["expected_maximum"]) == (1, 0.0)
    assert result.evidence["normal_margin"] == 1.0, "one card is no search: it is k_se"
    assert result.evidence["margin"] == periods_margin(1.0, 6), "struck on six periods"


def test_trades_that_lose_on_average_fail_however_the_book_scored() -> None:
    losing = run_of(-3.0, -1.0, 2.0, -4.0, -2.0, -2.0)

    result = judged(losing, min_bps=0.0, k_se=0.0)

    assert not result.passed
    assert result.evidence["net_bps_per_trade"] == pytest.approx(-5.0 / 3.0)


def test_an_edge_inside_its_own_error_fails_at_a_k_se_that_asks_for_more() -> None:
    noisy = run_of(30.0, -25.0, 20.0, -20.0)

    assert judged(noisy, min_bps=0.0, k_se=0.0).passed, "the mean trade made money"
    assert not judged(noisy, min_bps=0.0, k_se=1.0).passed, "but not by more than its error"


def test_the_floor_is_exceeded_strictly() -> None:
    flat = run_of(5.0, 5.0, 5.0)

    assert not judged(flat, min_bps=5.0, k_se=0.0).passed
    assert judged(flat, min_bps=4.9, k_se=0.0).passed


def test_a_card_that_closed_no_trade_showed_no_edge_and_fails() -> None:
    result = judged(build_run((0.0, 0.0)), min_bps=0.0, k_se=1.0)

    assert not result.passed
    assert (result.evidence["n_trades"], result.evidence["periods_with_trades"]) == (0, 0)
    assert (result.evidence["net_bps_per_trade"], result.evidence["se"]) == (0.0, None)


@pytest.mark.parametrize("k_se", [0.0, 3.0])
def test_one_trade_that_made_money_showed_no_error_and_fails(k_se: float) -> None:
    """A single trade: the error read period by period is zero by construction, and a card
    that traded once has not shown that a trade pays, at any `k_se`."""
    once = build_run((1.0,) * 20, trades=(trade(START, 0.5),))

    result = judged(once, min_bps=0.0, k_se=k_se)

    assert not result.passed
    assert result.evidence["net_bps_per_trade"] == pytest.approx(0.5)
    assert (result.evidence["se"], result.evidence["margin"], result.evidence["lower"]) == (
        None,
        None,
        None,
    )
    assert (result.evidence["n_trades"], result.evidence["periods_with_trades"]) == (1, 1)
    assert digest(once).net_bps_per_trade_se is None, "the digest states no error either"


def test_clips_that_all_closed_in_one_session_fail_however_they_spread() -> None:
    """Five clips of one session at +200, -150, +80, -120 and +5 bp: their mean is +3 and
    their period-by-period error zero, which would pass at any `k_se`. Spread over two
    sessions the same clips have an error to read, and it is wide."""
    edges = (200.0, -150.0, 80.0, -120.0, 5.0)
    one_session = build_run((1.0,) * 20, trades=tuple(trade(START, e) for e in edges))
    two_sessions = build_run(
        (1.0,) * 20,
        trades=tuple(trade(START + timedelta(days=i % 2), e) for i, e in enumerate(edges)),
    )

    refused = judged(one_session, min_bps=0.0, k_se=3.0)
    spread = judged(two_sessions, min_bps=0.0, k_se=0.0)

    assert not refused.passed
    assert refused.evidence["periods_with_trades"] == 1 and refused.evidence["se"] is None
    assert spread.passed, "two sessions are enough to read an error off; the mean made money"
    assert spread.evidence["periods_with_trades"] == 2 and spread.evidence["se"] > 0
    assert not judged(two_sessions, min_bps=0.0, k_se=1.0).passed


SEARCHED = run_of(*(1.0 + (2.65 if day % 2 == 0 else -2.65) for day in range(60)))
"""Sixty sessions of one trade netting 1 bp on average at an error of 0.345: 2.9 errors clear."""


@pytest.mark.parametrize(
    ("trials", "expected", "passes"),
    [(1, 0.0, True), (2, 0.5198, True), (50, 2.2763, True), (500, 3.0525, False)],
)
def test_the_margin_is_k_se_or_the_search_s_expected_maximum_whichever_is_more(
    trials: int, expected: float, passes: bool
) -> None:
    """The same card at `k_se` 1, one draw of searches of 1, 2, 50 and 500 cards: the
    expected maximum of that many standard normals is 0, 0.52, 2.28 and 3.05 errors, so the
    margin is `k_se` until fifty trials have raised it — struck on sixty periods, which widen
    it by a little — and an edge 2.9 errors clear of zero that passes as one of fifty fails
    as one of five hundred."""
    result = judged(SEARCHED, trials=trials, min_bps=0.0, k_se=1.0)

    evidence = result.evidence
    assert evidence["trials"] == trials
    assert evidence["expected_maximum"] == pytest.approx(expected, abs=1e-4)
    assert evidence["normal_margin"] == max(1.0, evidence["expected_maximum"])
    assert evidence["margin"] == periods_margin(evidence["normal_margin"], 60)
    assert 0 < evidence["margin"] - evidence["normal_margin"] < 0.17
    assert evidence["lower"] == pytest.approx(
        evidence["net_bps_per_trade"] - evidence["margin"] * evidence["se"]
    )
    assert 2.85 < evidence["net_bps_per_trade"] / evidence["se"] < 2.95
    assert result.passed is passes


def test_a_k_se_above_the_search_s_expected_maximum_stands() -> None:
    """The search sets a floor under `k_se` and never lowers it: at fifty trials the
    expected maximum is 2.28 errors, and a `k_se` of 3 is still the margin."""
    result = judged(SEARCHED, trials=50, min_bps=0.0, k_se=3.0)

    assert result.evidence["normal_margin"] == 3.0
    assert result.evidence["margin"] == periods_margin(3.0, 60)
    assert not result.passed


def _cauchy(u: float) -> float:
    return tan(pi * (u - 0.5))


def _two_degrees(u: float) -> float:
    return (2 * u - 1) / sqrt(2 * u * (1 - u))


def _four_degrees(u: float) -> float:
    shape = 4 * u * (1 - u)
    return 2 * sqrt(cos(acos(sqrt(shape)) / 3) / sqrt(shape) - 1)


@pytest.mark.parametrize("trials", [1, 50, 500])
@pytest.mark.parametrize(
    ("periods", "quantile"), [(2, _cauchy), (3, _two_degrees), (5, _four_degrees)]
)
def test_on_few_periods_the_margin_is_the_t_quantile_of_their_degrees_of_freedom(
    periods: int, quantile: Callable[[float], float], trials: int
) -> None:
    """One trade on each of two, three and five sessions: the error read period by period has
    one, two and four degrees of freedom and reads low by `√((p − 1)/p)`, so the margin is
    the t's quantile at the normal's chance of `normal_margin`, in the closed form those
    three have, widened by `√(p/(p − 1))` — 2.60 errors for `k_se` 1 on two sessions, 39 as
    the fiftieth card and 397 as the five-hundredth."""
    edges = (7.0, -3.0, 5.0, 1.0, -2.0)[:periods]
    run = build_run(
        (1.0,) * 20,
        trades=tuple(trade(START + timedelta(days=3 * i), e) for i, e in enumerate(edges)),
    )

    result = judged(run, trials=trials, min_bps=0.0, k_se=1.0)

    evidence = result.evidence
    chance = NormalDist().cdf(max(1.0, search_margin(trials)))
    assert evidence["periods_with_trades"] == periods
    assert evidence["margin"] == pytest.approx(
        quantile(chance) * sqrt(periods / (periods - 1)), rel=1e-7
    )
    assert evidence["lower"] == pytest.approx(
        evidence["net_bps_per_trade"] - evidence["margin"] * evidence["se"]
    )
    if (periods, trials) == (2, 1):
        assert evidence["margin"] == pytest.approx(2.5984, abs=1e-4)


@pytest.mark.parametrize("periods", [2, 3, 5])
def test_cards_that_earn_nothing_on_few_periods_pass_at_the_normal_s_rate(periods: int) -> None:
    """Measured with a seeded draw, one trade a session on a few of twenty sessions netting a
    normal of zero mean and 10 bp spread: at `k_se` 1 the gate passes about one in six — not
    the third or more a normal margin let through on two sessions — and as the fiftieth card
    of a search about one in ninety, not one in five to one in twenty."""
    draws = random.Random(periods)
    reps, first, fiftieth = 1500, 0, 0
    for _ in range(reps):
        days = sorted(draws.sample(range(20), periods))
        run = build_run(
            (1.0,) * 20,
            trades=tuple(trade(START + timedelta(days=d), draws.gauss(0.0, 10.0)) for d in days),
        )
        first += judged(run, trials=1, min_bps=0.0, k_se=1.0).passed
        fiftieth += judged(run, trials=50, min_bps=0.0, k_se=1.0).passed

    assert abs(first / reps - (1 - NormalDist().cdf(1.0))) < 0.03
    assert fiftieth / reps < 0.022


def test_the_search_margin_is_the_deflation_s_expected_maximum_at_one_error() -> None:
    """One card is no search, and from two on the margin is the expected maximum the
    deflation gates compute, at a spread of one error, rising with the search."""
    assert search_margin(1) == 0.0
    widths = (2, 3, 10, 50, 500, 5000)
    assert [search_margin(n) for n in widths] == [expected_maximum(1.0, n) for n in widths]
    assert [search_margin(n) for n in widths] == sorted(search_margin(n) for n in widths)


@pytest.mark.parametrize(
    ("k_se", "fixed", "searched"), [(1.0, 79.33, 3.32), (2.0, 11.38, 2.26), (3.0, 0.67, 0.67)]
)
def test_zero_edge_cards_pass_with_the_log_of_the_search_not_its_length(
    k_se: float, fixed: float, searched: float
) -> None:
    """The figures the gate and `docs/concepts.md` state: over 500 cards whose edge in its
    own errors is a standard normal, the expected count that pass is each card's chance
    summed — which holds however the cards are correlated — at a fixed `k_se` and at the
    margin each card is held to as one of the cards drawn so far."""
    tail = NormalDist().cdf
    each = [1 - tail(max(k_se, search_margin(n))) for n in range(1, 501)]

    assert 500 * (1 - tail(k_se)) == pytest.approx(fixed, abs=0.01)
    assert sum(each) == pytest.approx(searched, abs=0.01)
    if k_se == 1.0:
        assert sum(each[:50]) > sum(each) / 2, "most of them early, while the search is narrow"


@pytest.mark.parametrize("params", [{}, {"min_bps": 0.0}, {"k_se": 1.0}])
def test_without_both_thresholds_it_judges_nothing(params: dict[str, float]) -> None:
    result = judged(WINNING, **params)

    assert result.passed and result.skipped is not None
