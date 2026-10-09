"""The cost arithmetic: what a fill costs, and a reset, a carry and a maintenance ratio, each
a function of numbers.

Every expected value here is arithmetic a reader can check by eye, because these functions
are called from both the runner's extraction and the harness, and a test that derived its
expectation from either would prove only that the two agree with themselves.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.criteria.run import NS_PER_DAY, midnight_ns
from kanso.nautilus.costs import (
    NS_PER_YEAR,
    BookPolicy,
    carry,
    fill_cost,
    fill_rate,
    funding_payment,
    limit_at,
    maintenance_ratio,
    month_turned,
    policy_of,
    reset,
    side_rate,
    tick_slip,
)
from kanso.schemas import Book

CAPITAL = 100_000.0


# --- funding ---------------------------------------------------------------------


def test_a_long_pays_a_positive_rate_on_its_multiplied_notional() -> None:
    """Ten contracts of 0.01 BTC at 40,000 is 4,000 of notional; one basis point is 0.40."""
    assert funding_payment(10.0, 40_000.0, 0.01, 0.0001) == pytest.approx(0.4)


def test_a_short_receives_a_positive_rate_and_pays_a_negative_one() -> None:
    assert funding_payment(-10.0, 40_000.0, 0.01, 0.0001) == pytest.approx(-0.4)
    assert funding_payment(-10.0, 40_000.0, 0.01, -0.0001) == pytest.approx(0.4)
    assert funding_payment(10.0, 40_000.0, 0.01, -0.0001) == pytest.approx(-0.4)


def test_nothing_held_pays_nothing() -> None:
    assert funding_payment(0.0, 40_000.0, 0.01, 0.0003) == 0.0


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


def test_a_per_share_commission_is_paid_on_every_share_of_a_fill_that_pays_commission() -> None:
    """A hundred shares at $50 under one bp of commission, two of slippage, half a four-bp
    width and $0.0055 a share: $2.50 of rates and $0.55 of per-share commission."""
    assert fill_cost(5_000.0, 100.0, 1.0, 2.0, 0.0002, None, 0.0055, maker=False) == pytest.approx(
        3.05
    )
    assert fill_cost(5_000.0, 100.0, 1.0, 2.0, 0.0002, 0.0, 0.0055, maker=False) == pytest.approx(
        3.05
    )


def test_a_maker_under_a_stated_rate_pays_that_rate_alone_per_share_included() -> None:
    assert fill_cost(5_000.0, 100.0, 1.0, 2.0, 0.0002, 0.0, 0.0055, maker=True) == 0.0
    assert fill_cost(5_000.0, 100.0, 1.0, 2.0, 0.0002, 0.5, 0.0055, maker=True) == pytest.approx(
        0.25
    )


def test_a_maker_under_no_maker_rate_pays_the_per_share_commission_like_any_fill() -> None:
    assert fill_cost(5_000.0, 100.0, 1.0, 2.0, 0.0002, None, 0.0055, maker=True) == pytest.approx(
        3.05
    )


def test_without_a_per_share_commission_the_cost_is_the_rate_of_the_notional() -> None:
    assert fill_cost(5_000.0, 100.0, 1.0, 2.0, 0.0002, None, 0.0, maker=False) == pytest.approx(
        5_000.0 * fill_rate(1.0, 2.0, 0.0002, None, maker=False)
    )


def test_a_sale_pays_the_sell_side_fees_on_top_whoever_filled_it() -> None:
    """One bp of 10,000 is 1.00 and a cent on 100 shares is 1.00: a taker's sale pays both on
    top of its 5.00, a maker's sale under a zero rate pays exactly the two, a purchase neither."""
    taker_buy = fill_cost(10_000.0, 100.0, 1.0, 2.0, 0.0002, None, 0.0, maker=False)
    taker_sell = fill_cost(
        10_000.0,
        100.0,
        1.0,
        2.0,
        0.0002,
        None,
        0.0,
        maker=False,
        sell=True,
        sell_fee_bps=1.0,
        sell_fee_per_share=0.01,
    )
    maker_sell = fill_cost(
        10_000.0,
        100.0,
        1.0,
        2.0,
        0.0002,
        0.0,
        0.0,
        maker=True,
        sell=True,
        sell_fee_bps=1.0,
        sell_fee_per_share=0.01,
    )
    maker_buy = fill_cost(
        10_000.0,
        100.0,
        1.0,
        2.0,
        0.0002,
        0.0,
        0.0,
        maker=True,
        sell=False,
        sell_fee_bps=1.0,
        sell_fee_per_share=0.01,
    )
    assert taker_buy == pytest.approx(5.0)
    assert taker_sell == pytest.approx(7.0)
    assert maker_sell == pytest.approx(2.0)
    assert maker_buy == 0.0


# --- a maker charged per share ------------------------------------------------------


def test_a_maker_under_a_per_share_schedule_pays_exactly_that_on_each_share() -> None:
    """$0.0040 a share on 100 shares is 0.40, and nothing else: no commission in basis points
    or per share, no slippage, no half-spread — whatever the model charges a taker."""
    assert fill_cost(
        5_000.0, 100.0, 1.0, 2.0, 0.0002, None, 0.014, maker=True, maker_per_share=0.004
    ) == pytest.approx(0.4, abs=1e-15)
    assert fill_rate(1.0, 2.0, 0.0002, None, maker=True, maker_per_share=0.004) == 0.0


def test_a_taker_under_a_per_share_maker_schedule_pays_what_it_always_paid() -> None:
    assert fill_cost(
        5_000.0, 100.0, 1.0, 2.0, 0.0002, None, 0.014, maker=False, maker_per_share=0.004
    ) == fill_cost(5_000.0, 100.0, 1.0, 2.0, 0.0002, None, 0.014, maker=False)


def test_a_maker_schedule_stated_both_ways_charges_both() -> None:
    """Half a bp of 5,000 is 0.25, and $0.002 on 100 shares 0.20."""
    assert fill_cost(
        5_000.0, 100.0, 1.0, 2.0, 0.0002, 0.5, 0.0055, maker=True, maker_per_share=0.002
    ) == pytest.approx(0.45)


def test_a_negative_maker_per_share_charge_is_a_rebate() -> None:
    assert fill_cost(
        5_000.0, 100.0, 1.0, 2.0, 0.0002, None, 0.0055, maker=True, maker_per_share=-0.002
    ) == pytest.approx(-0.2)


def test_a_maker_s_sale_under_a_per_share_schedule_pays_the_sell_side_fees_on_top() -> None:
    """One bp of 10,000 is 1.00 and a cent on 100 shares 1.00, beside the maker's 0.40."""
    assert fill_cost(
        10_000.0,
        100.0,
        1.0,
        2.0,
        0.0002,
        None,
        0.014,
        maker=True,
        sell=True,
        sell_fee_bps=1.0,
        sell_fee_per_share=0.01,
        maker_per_share=0.004,
    ) == pytest.approx(2.4)


def _v0140_fill_cost(  # the arithmetic as v0.14.0 shipped it, copied, for the property below
    notional: float,
    qty: float,
    commission_bps: float,
    slippage_bps: float,
    half_spread: float,
    maker_bps: float | None,
    commission_per_share: float,
    *,
    maker: bool,
    sell: bool,
    sell_fee_bps: float,
    sell_fee_per_share: float,
) -> float:
    if maker and maker_bps is not None:
        rate = maker_bps / 10_000.0
    else:
        rate = (commission_bps + slippage_bps) / 10_000.0 + half_spread
    charged = notional * rate
    if sell:
        charged += notional * sell_fee_bps / 10_000.0 + qty * sell_fee_per_share
    if maker and maker_bps is not None:
        return charged
    return charged + qty * commission_per_share


RATES = st.floats(min_value=0.0, max_value=50.0, allow_nan=False)


@given(
    notional=st.floats(min_value=0.0, max_value=1e9, allow_nan=False),
    qty=st.floats(min_value=0.0, max_value=1e7, allow_nan=False),
    commission_bps=RATES,
    slippage_bps=RATES,
    half_spread=st.floats(min_value=0.0, max_value=0.01, allow_nan=False),
    maker_bps=st.none() | st.floats(min_value=-5.0, max_value=5.0, allow_nan=False),
    commission_per_share=st.floats(min_value=0.0, max_value=0.05, allow_nan=False),
    maker=st.booleans(),
    sell=st.booleans(),
    sell_fee_bps=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    sell_fee_per_share=st.floats(min_value=0.0, max_value=0.01, allow_nan=False),
)
def test_a_model_without_a_maker_per_share_charge_costs_every_fill_bit_for_bit_as_before(
    **kwargs: Any,
) -> None:
    """Leave the key out and no number moves: not one fill, not by one bit."""
    args = (
        kwargs["notional"],
        kwargs["qty"],
        kwargs["commission_bps"],
        kwargs["slippage_bps"],
        kwargs["half_spread"],
        kwargs["maker_bps"],
        kwargs["commission_per_share"],
    )
    named = {key: kwargs[key] for key in ("maker", "sell", "sell_fee_bps", "sell_fee_per_share")}
    assert fill_cost(*args, **named) == _v0140_fill_cost(*args, **named)


# --- a taker's tick ------------------------------------------------------------------


def test_a_market_order_pays_its_ticks_whole() -> None:
    """One tick of a cent; two of a sub-dollar name's hundredth of a cent."""
    assert tick_slip(1.0, 0.01, 10.0, None, sell=False) == 0.01
    assert tick_slip(2.0, 0.0001, 0.5, None, sell=True) == pytest.approx(0.0002)


def test_a_limit_is_never_charged_past_its_price() -> None:
    """A buy limited at 10.02 filled at 10.01 has a cent of room, so two ticks charge one; a
    buy taken at its own limit has none; a sale's room is below the fill."""
    assert tick_slip(2.0, 0.01, 10.01, 10.02, sell=False) == pytest.approx(0.01)
    assert tick_slip(1.0, 0.01, 10.02, 10.02, sell=False) == 0.0
    assert tick_slip(1.0, 0.01, 10.0, 9.95, sell=True) == pytest.approx(0.01)
    assert tick_slip(3.0, 0.01, 10.0, 9.99, sell=True) == pytest.approx(0.01)
    assert tick_slip(1.0, 0.01, 10.0, 10.01, sell=True) == 0.0, "a fill past its limit pays none"


def test_no_ticks_stated_charge_nothing() -> None:
    assert tick_slip(0.0, 0.01, 10.0, 10.5, sell=False) == 0.0
    assert tick_slip(1.0, 0.0, 10.0, None, sell=False) == 0.0


def test_a_taker_pays_its_ticks_per_share_and_a_maker_never_does() -> None:
    """100 shares, a cent each, over a commission of $0.004 a share: 0.40 + 1.00. A maker under
    the same model and no maker schedule pays the commission and not the tick."""
    taker = fill_cost(1_000.0, 100.0, 0.0, 0.0, 0.0, None, 0.004, maker=False, slip=0.01)
    maker = fill_cost(1_000.0, 100.0, 0.0, 0.0, 0.0, None, 0.004, maker=True, slip=0.01)
    assert taker == pytest.approx(1.4)
    assert maker == pytest.approx(0.4)


def test_a_tick_on_a_contract_is_worth_the_increment_times_the_multiplier() -> None:
    """Two contracts of a 50-times future, one tick of 0.25: 2 x 0.25 x 50 = 25.00."""
    assert fill_cost(
        200_000.0, 2.0, 0.0, 0.0, 0.0, None, 0.0, maker=False, slip=0.25, multiplier=50.0
    ) == pytest.approx(25.0)


def test_a_maker_schedule_and_a_tick_never_meet() -> None:
    assert fill_cost(
        1_000.0, 100.0, 0.0, 0.0, 0.0, None, 0.004, maker=True, maker_per_share=0.004, slip=0.01
    ) == pytest.approx(0.4)


def _order_with_a_modify(filled_at: list[float]) -> tuple[Any, list[Any]]:
    """A buy limited at 9.50, filled once, modified to 9.55, filled again: the order and its
    two fills, applied as the engine applies them."""
    from nautilus_trader.common.component import TestClock
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.model.enums import LiquiditySide, OrderSide
    from nautilus_trader.model.events import (
        OrderAccepted,
        OrderFilled,
        OrderSubmitted,
        OrderUpdated,
    )
    from nautilus_trader.model.identifiers import (
        AccountId,
        InstrumentId,
        StrategyId,
        TradeId,
        TraderId,
        VenueOrderId,
    )
    from nautilus_trader.model.objects import Currency, Money, Price, Quantity

    factory = OrderFactory(TraderId("T-1"), StrategyId("S-1"), TestClock())
    order = factory.limit(
        InstrumentId.from_str("DEMO.XNAS"), OrderSide.BUY, Quantity.from_int(200), Price(9.5, 2)
    )
    common = {
        "trader_id": order.trader_id,
        "strategy_id": order.strategy_id,
        "instrument_id": order.instrument_id,
        "client_order_id": order.client_order_id,
        "event_id": None,
        "ts_event": 0,
        "ts_init": 0,
    }
    account, venue_id = AccountId("SIM-001"), VenueOrderId("V-1")

    def made(cls: Any, **fields: Any) -> Any:
        return cls(**{**common, **fields, "event_id": UUID4()})

    order.apply(made(OrderSubmitted, account_id=account))
    order.apply(made(OrderAccepted, account_id=account, venue_order_id=venue_id))
    fills = []
    for number, (px, price) in enumerate(zip(filled_at, (None, 9.55), strict=True)):
        if price is not None:
            order.apply(
                made(
                    OrderUpdated,
                    venue_order_id=venue_id,
                    account_id=account,
                    quantity=order.quantity,
                    price=Price(price, 2),
                    trigger_price=None,
                )
            )
        fill = made(
            OrderFilled,
            account_id=account,
            venue_order_id=venue_id,
            position_id=None,
            trade_id=TradeId(f"F-{number}"),
            order_side=OrderSide.BUY,
            order_type=order.order_type,
            last_qty=Quantity.from_int(100),
            last_px=Price(px, 2),
            currency=Currency.from_str("USD"),
            commission=Money(0, Currency.from_str("USD")),
            liquidity_side=LiquiditySide.TAKER,
        )
        order.apply(fill)
        fills.append(fill)
    return order, fills


def test_a_fill_is_capped_by_the_limit_its_order_carried_when_it_filled() -> None:
    """The order holds only its last price, 9.55; the fill before the modify was taken under
    9.50, so a tick at 9.50 has no room and the one at 9.54 a cent."""
    order, (first, second) = _order_with_a_modify([9.5, 9.54])

    assert limit_at(order, first.trade_id) == 9.5
    assert limit_at(order, second.trade_id) == 9.55
    assert float(order.price) == 9.55
    assert tick_slip(1.0, 0.01, 9.5, limit_at(order, first.trade_id), sell=False) == 0.0
    assert tick_slip(1.0, 0.01, 9.54, limit_at(order, second.trade_id), sell=False) == (
        pytest.approx(0.01)
    )


def test_a_trade_id_the_order_never_took_reads_as_its_last_limit() -> None:
    from nautilus_trader.model.identifiers import TradeId

    order, _ = _order_with_a_modify([9.5, 9.54])

    assert limit_at(order, TradeId("F-9")) == 9.55


def test_the_opening_half_of_a_flipping_fill_is_capped_by_the_limit_it_filled_under() -> None:
    """The engine splits a fill that flips a net position into two events, the opening half
    under a new event id and the fill's own trade id: that half is found by its trade id, and
    reads the limit before the modify that came after it, not the order's last."""
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.model.events import OrderFilled

    order, (first, _) = _order_with_a_modify([9.5, 9.54])
    opening = OrderFilled.from_dict({**OrderFilled.to_dict(first), "event_id": UUID4().value})

    assert opening.id != first.id and opening.trade_id == first.trade_id
    assert limit_at(order, opening.trade_id) == 9.5


def test_an_order_with_no_limit_has_none_to_cap() -> None:
    from nautilus_trader.common.component import TestClock
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import InstrumentId, StrategyId, TradeId, TraderId
    from nautilus_trader.model.objects import Quantity

    factory = OrderFactory(TraderId("T-1"), StrategyId("S-1"), TestClock())
    market = factory.market(InstrumentId.from_str("DEMO.XNAS"), OrderSide.BUY, Quantity.from_int(1))
    assert limit_at(market, TradeId("F-0")) is None
    assert limit_at(None, TradeId("F-0")) is None
