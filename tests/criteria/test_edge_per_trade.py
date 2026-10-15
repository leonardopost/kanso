"""`edge_per_trade`: a card's trades make money on average, by more than their own error.

It reads the numbers a card's digest records — the per-trade objective over the window as
one fold — so what the proposer and the operator are shown is what the gate judged.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from kanso.criteria import CardRun
from kanso.criteria.digest import digest
from kanso.criteria.gates import edge_per_trade
from kanso.schemas import GateResult
from tests.criteria.builders import START, build_run, context, trade


def run_of(*edges_bps: float) -> CardRun:
    """One trade a session on 10,000 of notional, netting each edge in turn: a profit of
    one currency unit on that notional is one basis point."""
    trades = tuple(trade(START + timedelta(days=day), edge) for day, edge in enumerate(edges_bps))
    return build_run((1.0,) * len(edges_bps), trades=trades)


def judged(run: CardRun, **params: float) -> GateResult:
    return edge_per_trade.evaluate(context(run, params=params))


WINNING = run_of(12.0, 8.0, 10.0, 11.0, 9.0, 10.0)
"""Six trades netting 10 bp on average, each within two of it."""


def test_trades_that_pay_on_average_pass_and_say_by_how_much() -> None:
    result = judged(WINNING, min_bps=0.0, k_se=1.0)

    recorded = digest(WINNING)
    assert result.passed
    assert result.evidence["net_bps_per_trade"] == recorded.net_bps_per_trade
    assert result.evidence["se"] == recorded.net_bps_per_trade_se
    assert result.evidence["lower"] == pytest.approx(
        result.evidence["net_bps_per_trade"] - result.evidence["se"]
    )
    assert (result.evidence["k_se"], result.evidence["min_bps"]) == (1.0, 0.0)
    assert result.evidence["n_trades"] == 6


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
    assert result.evidence["n_trades"] == 0
    assert (result.evidence["net_bps_per_trade"], result.evidence["se"]) == (0.0, 0.0)


@pytest.mark.parametrize("params", [{}, {"min_bps": 0.0}, {"k_se": 1.0}])
def test_without_both_thresholds_it_judges_nothing(params: dict[str, float]) -> None:
    result = judged(WINNING, **params)

    assert result.passed and result.skipped is not None
