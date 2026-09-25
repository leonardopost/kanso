"""The cost arithmetic: what a fill costs, and a reset, a carry and a maintenance ratio, each
a function of numbers.

Every expected value here is arithmetic a reader can check by eye, because these functions
are called from both the runner's extraction and the harness, and a test that derived its
expectation from either would prove only that the two agree with themselves.
"""

from __future__ import annotations

from datetime import date

import pytest

from kanso.criteria.run import NS_PER_DAY, midnight_ns
from kanso.nautilus.costs import (
    NS_PER_YEAR,
    BookPolicy,
    carry,
    fill_rate,
    maintenance_ratio,
    month_turned,
    policy_of,
    reset,
    side_rate,
)
from kanso.schemas import Book

CAPITAL = 100_000.0


# --- the reset ------------------------------------------------------------------


def test_a_surplus_leaves_the_book_for_the_cushion() -> None:
    assert reset(103_000.0, CAPITAL, 500.0) == (-3_000.0, 3_500.0)


def test_a_deficit_is_restored_from_the_cushion_while_it_lasts() -> None:
    assert reset(98_000.0, CAPITAL, 5_000.0) == (2_000.0, 3_000.0)


def test_a_deficit_the_cushion_cannot_cover_is_restored_only_as_far_as_it_reaches() -> None:
    """Never below zero, never borrowed: the book stays short by what the cushion lacked."""
    assert reset(90_000.0, CAPITAL, 4_000.0) == (4_000.0, 0.0)
    assert reset(90_000.0, CAPITAL, 0.0) == (0.0, 0.0)


def test_a_book_at_its_capital_moves_nothing() -> None:
    assert reset(CAPITAL, CAPITAL, 700.0) == (0.0, 700.0)


# --- the carry ------------------------------------------------------------------


def test_a_long_only_book_at_leverage_one_is_charged_nothing() -> None:
    assert carry(gross=CAPITAL, equity=CAPITAL, rate_bps=500.0, span_ns=NS_PER_DAY) == 0.0


def test_the_carry_is_the_yearly_rate_on_the_borrowed_notional_over_the_span() -> None:
    """150,000 gross on 100,000 of equity borrows 50,000; 5% a year over a Julian year."""
    assert carry(150_000.0, CAPITAL, 500.0, NS_PER_YEAR) == pytest.approx(2_500.0)
    assert carry(150_000.0, CAPITAL, 500.0, NS_PER_DAY) == pytest.approx(2_500.0 / 365.25)


def test_a_short_s_notional_is_borrowed_like_a_long_s() -> None:
    """Gross counts shorts, so a long-short book of 200,000 on 100,000 borrows 100,000."""
    assert carry(200_000.0, CAPITAL, 100.0, NS_PER_YEAR) == pytest.approx(1_000.0)


def test_a_weekend_inside_a_period_costs_the_days_it_holds() -> None:
    one_day = carry(150_000.0, CAPITAL, 500.0, NS_PER_DAY)
    assert carry(150_000.0, CAPITAL, 500.0, 3 * NS_PER_DAY) == pytest.approx(3 * one_day)


# --- the month ------------------------------------------------------------------


def test_the_month_turns_between_the_last_end_of_one_and_the_first_of_the_next() -> None:
    january = midnight_ns(date(2024, 1, 31)) + NS_PER_DAY - 1
    february = midnight_ns(date(2024, 2, 1))
    assert month_turned(january, february)
    assert not month_turned(january, january)
    assert not month_turned(midnight_ns(date(2024, 1, 2)), january)


def test_the_first_period_end_of_a_run_turns_no_month() -> None:
    assert not month_turned(None, midnight_ns(date(2024, 2, 1)))


def test_a_year_boundary_is_a_month_boundary() -> None:
    assert month_turned(midnight_ns(date(2023, 12, 31)), midnight_ns(date(2024, 1, 1)))


# --- the maintenance ratio --------------------------------------------------------


def test_a_flat_book_has_no_ratio() -> None:
    assert maintenance_ratio(CAPITAL, []) is None
    assert maintenance_ratio(CAPITAL, [0.0]) is None


def test_the_ratio_is_the_worst_equity_over_the_worst_gross() -> None:
    """Cash of 20,000 beside a long worth 90,000 at its low: 110,000 over 90,000."""
    assert maintenance_ratio(20_000.0, [90_000.0]) == pytest.approx(110_000.0 / 90_000.0)


def test_a_short_s_adverse_worth_is_negative_and_its_gross_absolute() -> None:
    """Cash of 150,000 (the capital plus the short's proceeds) against a short now worth
    -60,000 at its high: 90,000 of equity over 60,000 of gross."""
    assert maintenance_ratio(150_000.0, [-60_000.0]) == pytest.approx(1.5)


# --- the policy -----------------------------------------------------------------


def test_a_hypothesis_without_a_book_has_no_policy() -> None:
    assert policy_of(None) is None


def test_the_policy_carries_the_three_rules_and_says_which_apply() -> None:
    policy = policy_of(Book(reset="monthly", financing_rate_bps=25.0, maintenance_pct=30.0))
    assert policy == BookPolicy(reset="monthly", financing_rate_bps=25.0, maintenance_pct=30.0)
    assert policy is not None and policy.resets and policy.charges
    idle = policy_of(Book())
    assert idle is not None and not idle.resets and not idle.charges


# --- one fill -------------------------------------------------------------------


def test_a_taker_pays_commission_slippage_and_half_the_spread() -> None:
    """One bp of commission, two of slippage and half a four-bp spread: five bps."""
    assert fill_rate(1.0, 2.0, 0.0002, None, maker=False) == pytest.approx(0.0005)
    assert fill_rate(1.0, 2.0, 0.0002, -0.25, maker=False) == pytest.approx(0.0005)
    assert side_rate(1.0, 2.0, 0.0002) == pytest.approx(0.0005)


def test_a_maker_pays_its_own_rate_and_nothing_else_where_the_model_states_one() -> None:
    """No slippage and no half-spread: a resting limit filled at its own price."""
    assert fill_rate(1.0, 2.0, 0.0002, 0.5, maker=True) == 0.5 / 10_000
    assert fill_rate(1.0, 2.0, 0.0002, 0.0, maker=True) == 0.0


def test_a_negative_maker_rate_is_a_rebate() -> None:
    assert fill_rate(0.3, 0.5, 0.0001, -0.2, maker=True) == -0.2 / 10_000


def test_a_maker_under_a_model_that_states_no_maker_rate_pays_what_any_fill_pays() -> None:
    """Every fill was charged this way before the key existed, and still is without it."""
    assert fill_rate(1.0, 2.0, 0.0002, None, maker=True) == pytest.approx(0.0005)
    assert fill_rate(1.0, 2.0, 0.0002, None, maker=True) == fill_rate(
        1.0, 2.0, 0.0002, None, maker=False
    )
