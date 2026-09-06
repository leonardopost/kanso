"""Every mapping between the broker's JSON and the engine's types, against measured rows.

The rows are the ones the live account answered with, written out in full, so what is
under test is what the broker sends rather than what this adapter hoped it would. Three
things matter more than the rest:

* the two tapes are different series — the fixtures carry both, and they disagree on the
  open, the close and the volume of the same session;
* a daily bar stamps at the same instant as the vendor already in this build, so a card
  researched on one source and traded through the other is priced at the same moment;
* a fill's identity is derived from the order and its cumulative filled quantity, so
  reading the same order row twice produces nothing the second time — which is what a
  restart with no duplicate fills is made of.

Nothing here opens a socket or resolves a credential.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import (
    AggressorSide,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.identifiers import InstrumentId

from kanso.data.adapters.massive.loaders.bars import Request, build_bar
from kanso.errors import ValidationError
from kanso.nautilus.adapters.alpaca import parsing
from kanso.nautilus.adapters.alpaca.parsing import (
    ORDER_STATUSES,
    TRADE_ID_DIGITS,
    account_id,
    asset,
    bar,
    client_order_id,
    decimal_of,
    fill_report,
    instant,
    instrument_id_of,
    market_clock,
    maybe_instant,
    order_status_report,
    position_report,
    price_of,
    quantity_of,
    quote,
    rfc3339,
    symbol_of,
    to_broker_order_type,
    to_broker_side,
    to_broker_time_in_force,
    trade,
    trade_id,
    unsupported_reason,
)

from . import ACCOUNT_NUMBER, ASSET, CLOCK, IEX_BAR, QUOTE, SIP_BAR, TRADE, order, position

AAPL = InstrumentId.from_str("AAPL.XNAS")
ACCOUNT = account_id(ACCOUNT_NUMBER)
PRICE_PRECISION = 2
SIZE_PRECISION = 0
NOW = 1_785_729_600_000_000_000

MASSIVE_MS = 1_785_729_600_000
"""The same session's window start as the vendor already in this build reports it: the
millisecond epoch the request path carries, measured against this broker's own `t`."""


def daily(row: Any, **changes: Any) -> Any:
    """One bar built from a measured row at this instrument's precisions."""
    merged = dict(row)
    merged.update(changes)
    return bar(
        merged,
        instrument=AAPL,
        resolution="1d",
        price_precision=PRICE_PRECISION,
        size_precision=SIZE_PRECISION,
    )


def report(row: Any, **kwargs: Any) -> Any:
    """One order status report at this instrument's precisions."""
    return order_status_report(
        row,
        account=ACCOUNT,
        instrument=AAPL,
        price_precision=PRICE_PRECISION,
        size_precision=SIZE_PRECISION,
        ts_init=NOW,
        **kwargs,
    )


def fill(row: Any, **kwargs: Any) -> Any:
    """One fill report at this instrument's precisions."""
    return fill_report(
        row,
        account=ACCOUNT,
        instrument=AAPL,
        price_precision=PRICE_PRECISION,
        size_precision=SIZE_PRECISION,
        ts_init=NOW,
        **kwargs,
    )


# --- timestamps ----------------------------------------------------------------


def test_a_zulu_timestamp_is_utc_nanoseconds() -> None:
    assert instant("2026-08-03T04:00:00Z") == 1_785_729_600 * 1_000_000_000


def test_an_offset_names_the_same_instant_as_the_zulu_form_of_it() -> None:
    """The clock answers with `-04:00`, so the offset is applied rather than ignored."""
    assert instant("2026-09-08T09:30:00-04:00") == instant("2026-09-08T13:30:00Z")


def test_a_positive_offset_moves_the_other_way() -> None:
    assert instant("2026-09-08T09:30:00+02:00") == instant("2026-09-08T07:30:00Z")


def test_the_digits_below_a_microsecond_survive() -> None:
    """Two prints inside one microsecond are ordered by exactly the digits a datetime drops."""
    assert instant("2026-08-03T13:30:00.123456789Z") % 1_000_000_000 == 123_456_789


def test_a_short_fraction_is_read_at_its_own_scale() -> None:
    assert instant("2026-08-03T13:30:00.5Z") % 1_000_000_000 == 500_000_000


def test_the_forms_the_grammar_accepts_are_the_ones_the_broker_writes() -> None:
    one = instant("2026-08-03T04:00:00Z")

    assert instant("2026-08-03t04:00:00z") == one
    assert instant("2026-08-03 04:00:00Z") == one
    assert instant("2026-08-03T00:00:00-0400") == one


def test_a_value_that_is_not_text_is_not_a_timestamp() -> None:
    with pytest.raises(ValidationError) as failure:
        instant(1_785_729_600, "order.created_at")

    assert "order.created_at" in str(failure.value)


def test_text_that_is_not_rfc_3339_is_refused_by_name() -> None:
    with pytest.raises(ValidationError):
        instant("3 August 2026")


def test_a_date_that_does_not_exist_is_refused() -> None:
    with pytest.raises(ValidationError) as failure:
        instant("2026-02-30T00:00:00Z")

    assert "names no instant" in str(failure.value)


def test_a_null_timestamp_is_an_absence_rather_than_a_fault() -> None:
    """Half an order row's timestamps are null until something happens to the order."""
    assert maybe_instant(None) is None
    assert maybe_instant("2026-08-03T04:00:00Z") == instant("2026-08-03T04:00:00Z")


def test_a_request_parameter_is_rendered_back_to_the_instant_it_names() -> None:
    assert rfc3339(instant("2026-08-03T04:00:00Z")) == "2026-08-03T04:00:00Z"
    assert rfc3339(instant("2026-08-03T04:00:00.000000001Z")) == "2026-08-03T04:00:00.000000001Z"


# --- numbers -------------------------------------------------------------------


def test_a_number_is_read_through_the_decimal_it_spells() -> None:
    """Money arrives as text and a bar price as a JSON number; both are exact decimals."""
    assert decimal_of("303.42") == Decimal("303.42")
    assert decimal_of(309.58) == Decimal("309.58")
    assert decimal_of(10) == Decimal(10)


@pytest.mark.parametrize("value", [True, None, "abc", {"a": 1}])
def test_something_that_is_not_a_number_is_refused(value: Any) -> None:
    with pytest.raises(ValidationError):
        decimal_of(value, "order.qty")


def test_a_price_is_quantised_to_the_instrument_and_not_to_a_float() -> None:
    assert str(price_of(309.765, 2)) == "309.77"
    assert str(price_of("303.42", 2)) == "303.42"


def test_a_negative_quantity_is_refused_where_a_sign_is_not_expected() -> None:
    """The one caller that expects one — a short position — takes the magnitude itself."""
    with pytest.raises(ValidationError) as failure:
        quantity_of("-10", 0, "quote.bs")

    assert "negative size" in str(failure.value)


# --- the clock -----------------------------------------------------------------


def test_the_clock_row_is_read_in_full() -> None:
    found = market_clock(CLOCK)

    assert found.is_open
    assert found.next_open == instant("2026-09-08T09:30:00-04:00")
    assert found.next_close == instant("2026-09-08T16:00:00-04:00")
    assert found.timestamp % 1_000_000_000 == 123_456_789


def test_a_clock_without_a_state_is_not_a_clock() -> None:
    with pytest.raises(ValidationError) as failure:
        market_clock({**CLOCK, "is_open": "yes"})

    assert "is_open" in str(failure.value)


# --- assets --------------------------------------------------------------------


def test_the_measured_equity_row_carries_no_increments_at_all() -> None:
    """Measured absence: they are crypto fields, so an overlay requiring them fails every equity."""
    found = asset(ASSET)

    assert (found.min_order_size, found.min_trade_increment, found.price_increment) == (
        None,
        None,
        None,
    )


def test_every_flag_the_overlay_reads_comes_off_the_row() -> None:
    found = asset(ASSET)

    assert (found.tradable, found.marginable, found.shortable) == (True, True, True)
    assert (found.easy_to_borrow, found.fractionable) == (True, True)
    assert (found.symbol, found.exchange, found.status) == ("AAPL", "NASDAQ", "active")
    assert found.active and found.equity


def test_a_row_that_is_not_active_or_not_tradable_is_neither() -> None:
    assert not asset({**ASSET, "status": "inactive"}).active
    assert not asset({**ASSET, "tradable": False}).active
    assert not asset({**ASSET, "class": "crypto"}).equity


@pytest.mark.parametrize("flag", ["tradable", "marginable", "shortable", "fractionable"])
def test_a_missing_flag_is_refused_rather_than_defaulted(flag: str) -> None:
    """Read as false it forbids every short; read as true it submits one the broker refuses."""
    with pytest.raises(ValidationError) as failure:
        asset({**ASSET, flag: None})

    assert flag in str(failure.value)


def test_a_missing_name_field_is_refused() -> None:
    with pytest.raises(ValidationError) as failure:
        asset({**ASSET, "exchange": ""})

    assert "asset.exchange" in str(failure.value)


def test_an_increment_the_broker_does_state_is_read() -> None:
    """A crypto row carries them; the mapping reads what is there rather than what is usual."""
    found = asset({**ASSET, "class": "crypto", "min_order_size": "0.0001"})

    assert found.min_order_size == Decimal("0.0001")


# --- bars ----------------------------------------------------------------------


def test_a_daily_bar_is_stamped_at_the_close_of_its_own_window() -> None:
    """The row's `t` opens the window; a bar is known at its close and at no instant before."""
    built = daily(SIP_BAR)

    assert built is not None
    assert built.ts_event == instant("2026-08-04T04:00:00Z")
    assert built.ts_init == built.ts_event


def test_the_measured_consolidated_prices_survive_the_mapping() -> None:
    built = daily(SIP_BAR)

    assert built is not None
    assert (str(built.open), str(built.close)) == ("309.58", "303.42")
    assert str(built.volume) == "75314280"


def test_the_two_tapes_are_two_series_and_not_two_readings_of_one() -> None:
    """A card researched on one and traded on the other is not the same strategy."""
    consolidated, one_venue = daily(SIP_BAR), daily(IEX_BAR)

    assert consolidated is not None and one_venue is not None
    assert consolidated.close != one_venue.close
    assert consolidated.volume != one_venue.volume
    assert consolidated.ts_event == one_venue.ts_event


def test_a_sub_cent_price_from_one_venue_is_quantised_to_the_instrument() -> None:
    built = daily(IEX_BAR)

    assert built is not None
    assert str(built.open) == "309.77"


def test_a_daily_bar_stamps_where_the_vendor_already_in_this_build_stamps_it() -> None:
    """Both anchor the session at the same instant, so the two sources are comparable."""
    request = Request(
        symbol="AAPL",
        venue="XNAS",
        ticker="AAPL",
        asset_class="stocks",
        dataset="bars",
        resolution="1d",
        adjusted=False,
        publication="realtime",
        publication_rule=None,
        price_precision=PRICE_PRECISION,
        size_precision=SIZE_PRECISION,
    )
    theirs = build_bar(
        request,
        {
            "t": MASSIVE_MS,
            "o": SIP_BAR["o"],
            "h": SIP_BAR["h"],
            "l": SIP_BAR["l"],
            "c": SIP_BAR["c"],
            "v": SIP_BAR["v"],
        },
    )
    ours = daily(SIP_BAR)

    assert ours is not None
    assert (ours.ts_event, ours.ts_init) == (theirs.ts_event, theirs.ts_init)
    assert ours.close == theirs.close


@pytest.mark.parametrize("missing", ["t", "o", "h", "l", "c", "v"])
def test_a_row_missing_a_field_a_bar_needs_yields_no_bar(missing: str) -> None:
    """A missing point is visible in the count; a fabricated one is not visible at all."""
    assert daily(SIP_BAR, **{missing: None}) is None


def test_a_row_whose_prices_contradict_each_other_is_not_a_bar() -> None:
    with pytest.raises(ValidationError) as failure:
        daily(SIP_BAR, h=1.0)

    assert "AAPL.XNAS" in str(failure.value)


# --- quotes and trades ---------------------------------------------------------


def test_a_quote_row_becomes_a_two_sided_quote() -> None:
    built = quote(
        QUOTE, instrument=AAPL, price_precision=PRICE_PRECISION, size_precision=SIZE_PRECISION
    )

    assert built is not None
    assert (str(built.bid_price), str(built.ask_price)) == ("309.55", "309.60")
    assert built.ts_event == instant(str(QUOTE["t"]))


@pytest.mark.parametrize("missing", ["t", "bp", "bs", "ap", "as"])
def test_a_half_quote_is_no_quote(missing: str) -> None:
    assert (
        quote(
            {**QUOTE, missing: None},
            instrument=AAPL,
            price_precision=PRICE_PRECISION,
            size_precision=SIZE_PRECISION,
        )
        is None
    )


def test_a_negative_size_is_a_broken_row_rather_than_a_magnitude() -> None:
    """Read as its magnitude it would put a size in a point that nobody served."""
    with pytest.raises(ValidationError) as failure:
        quote(
            {**QUOTE, "bs": -1},
            instrument=AAPL,
            price_precision=PRICE_PRECISION,
            size_precision=SIZE_PRECISION,
        )

    assert "quote.bs" in str(failure.value)


def test_a_price_the_engine_cannot_hold_is_refused_by_the_field_that_carried_it() -> None:
    """A magnitude no fixed point holds must fail by name, not escape as a decimal error."""
    with pytest.raises(ValidationError) as failure:
        quote(
            {**QUOTE, "ap": 1e30},
            instrument=AAPL,
            price_precision=PRICE_PRECISION,
            size_precision=SIZE_PRECISION,
        )

    assert "quote.ap" in str(failure.value)


def test_a_size_the_engine_cannot_hold_is_refused_the_same_way() -> None:
    with pytest.raises(ValidationError) as failure:
        quantity_of(1e30, 2, "quote.bs")

    assert "quote.bs" in str(failure.value)


def test_a_trade_row_becomes_a_print_whose_aggressor_is_unknown() -> None:
    """The broker says which venue and which conditions, never which side crossed."""
    built = trade(
        TRADE, instrument=AAPL, price_precision=PRICE_PRECISION, size_precision=SIZE_PRECISION
    )

    assert built is not None
    assert built.aggressor_side == AggressorSide.NO_AGGRESSOR
    assert str(built.trade_id) == str(TRADE["i"])
    assert str(built.price) == "309.58"


@pytest.mark.parametrize("missing", ["t", "p", "s", "i"])
def test_a_trade_missing_what_it_is_identified_by_is_no_trade(missing: str) -> None:
    assert (
        trade(
            {**TRADE, missing: None},
            instrument=AAPL,
            price_precision=PRICE_PRECISION,
            size_precision=SIZE_PRECISION,
        )
        is None
    )


def test_a_print_of_no_size_is_refused_by_the_engine_and_reported_as_such() -> None:
    with pytest.raises(ValidationError) as failure:
        trade(
            {**TRADE, "s": 0},
            instrument=AAPL,
            price_precision=PRICE_PRECISION,
            size_precision=SIZE_PRECISION,
        )

    assert "trade" in str(failure.value)


# --- orders --------------------------------------------------------------------


def test_the_measured_order_row_reports_the_order_that_was_placed() -> None:
    found = report(order())

    assert found.order_side == OrderSide.BUY
    assert found.order_type == OrderType.MARKET
    assert found.time_in_force == TimeInForce.DAY
    assert found.order_status == OrderStatus.ACCEPTED
    assert str(found.quantity) == "10"
    assert str(found.filled_qty) == "0"
    assert str(found.client_order_id) == "O-20260905-160405-001-000-1"
    assert str(found.venue_order_id) == "61e69015-8549-4bfd-b9c3-01e75843f47d"


def test_the_two_timestamps_come_off_the_row_rather_than_off_this_process() -> None:
    """Two runs reconciling one account must agree to the nanosecond."""
    found = report(order())

    assert found.ts_accepted == instant("2026-09-05T16:04:05.200000000Z")
    assert found.ts_last == instant("2026-09-05T16:04:05.300000000Z")
    assert found.ts_init == NOW


def test_a_row_that_has_not_been_submitted_or_updated_falls_back_to_its_creation() -> None:
    found = report(order(submitted_at=None, updated_at=None))

    assert found.ts_accepted == found.ts_last == instant("2026-09-05T16:04:05.123456789Z")


def test_the_row_carries_the_order_type_under_two_names_and_either_is_read() -> None:
    """`order_type` and `type` are both present and hold the same value."""
    without = order(type="limit", limit_price="300.00")
    del without["order_type"]

    assert report(without).order_type == OrderType.LIMIT
    assert str(report(without).price) == "300.00"


@pytest.mark.parametrize("empty", [None, ""])
def test_the_fallback_name_is_read_when_the_row_carries_only_it(empty: Any) -> None:
    """Absent, null or empty: whichever of the two names holds the value is the one read."""
    missing = order(order_type="limit", type=empty, limit_price="300.00")
    absent = order(order_type="limit", limit_price="300.00")
    del absent["type"]

    assert report(missing).order_type == OrderType.LIMIT
    assert report(absent).order_type == OrderType.LIMIT


@pytest.mark.parametrize("spelling", sorted(ORDER_STATUSES))
def test_every_state_the_broker_writes_maps_to_one_the_engine_has(spelling: str) -> None:
    assert report(order(status=spelling)).order_status == ORDER_STATUSES[spelling]


def test_a_resting_but_unworkable_order_is_still_an_accepted_one() -> None:
    """Done for the day, held, stopped: the order is with the broker and has not ended."""
    for spelling in ("done_for_day", "held", "stopped", "suspended", "calculated"):
        assert ORDER_STATUSES[spelling] is OrderStatus.ACCEPTED


def test_a_replaced_order_has_ended() -> None:
    """The engine models a replacement as one order living on, not as two."""
    assert ORDER_STATUSES["replaced"] is OrderStatus.CANCELED


def test_a_stop_limit_row_carries_both_of_its_prices() -> None:
    found = report(order(type="stop_limit", limit_price="300.00", stop_price="299.00"))

    assert (str(found.price), str(found.trigger_price)) == ("300.00", "299.00")


def test_a_filled_row_reports_the_average_it_filled_at() -> None:
    found = report(
        order(
            status="filled",
            filled_qty="10",
            filled_avg_price="303.42",
            filled_at="2026-09-05T16:04:06Z",
        )
    )

    assert found.order_status is OrderStatus.FILLED
    assert found.avg_px == Decimal("303.42")
    assert str(found.filled_qty) == "10"


def test_the_report_id_is_the_caller_s_when_it_gives_one() -> None:
    """Determinism where a test or a parity run needs it, a fresh id where it does not."""
    given = UUID4()

    assert report(order(), report_id=given).id == given
    assert report(order()).id != given


# --- what cannot be reported faithfully ----------------------------------------


def test_a_row_this_adapter_maps_has_no_reason_not_to() -> None:
    assert unsupported_reason(order()) is None
    assert unsupported_reason(order(order_class="simple")) is None


def test_a_bracket_order_is_not_mapped_rather_than_mapped_wrongly() -> None:
    """Its legs carry contingencies the engine models differently, and none was measured."""
    reason = unsupported_reason(order(order_class="bracket"))

    assert reason is not None and "legs" in reason


def test_a_notional_order_is_not_one_kanso_sizes() -> None:
    reason = unsupported_reason(order(qty=None, notional="1000"))

    assert reason is not None and "whole shares" in reason


def test_an_asset_class_kanso_does_not_trade_here_is_passed_over() -> None:
    reason = unsupported_reason(order(asset_class="crypto"))

    assert reason is not None and "crypto" in reason


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "teleported"),
        ("status", None),
        ("side", "buy_minus"),
        ("type", "bracket"),
        ("time_in_force", "gtd"),
    ],
)
def test_a_value_outside_the_table_is_refused_by_name(field: str, value: Any) -> None:
    """Guessing at an unknown state is how a working order gets reported as finished."""
    row = order(**{field: value})
    if field == "type":
        row["order_type"] = value
    reason = unsupported_reason(row)

    assert reason is not None and field in reason


def test_reporting_a_row_that_cannot_be_mapped_fails_rather_than_invents() -> None:
    with pytest.raises(ValidationError) as failure:
        report(order(order_class="oco"))

    assert "61e69015-8549-4bfd-b9c3-01e75843f47d" in str(failure.value)


def test_an_order_without_the_handle_a_restart_needs_is_refused() -> None:
    with pytest.raises(ValidationError) as failure:
        report(order(client_order_id=None))

    assert "client_order_id" in str(failure.value)


# --- the deny table, in the other direction ------------------------------------


def test_the_sides_the_broker_accepts() -> None:
    assert to_broker_side(OrderSide.BUY) == "buy"
    assert to_broker_side(OrderSide.SELL) == "sell"

    with pytest.raises(ValidationError):
        to_broker_side(OrderSide.NO_ORDER_SIDE)


@pytest.mark.parametrize(
    ("engine", "broker"),
    [
        (OrderType.MARKET, "market"),
        (OrderType.LIMIT, "limit"),
        (OrderType.STOP_MARKET, "stop"),
        (OrderType.STOP_LIMIT, "stop_limit"),
        (OrderType.TRAILING_STOP_MARKET, "trailing_stop"),
    ],
)
def test_each_order_type_the_broker_offers_has_one_spelling(engine: OrderType, broker: str) -> None:
    assert to_broker_order_type(engine) == broker


@pytest.mark.parametrize(
    "denied",
    [
        OrderType.MARKET_TO_LIMIT,
        OrderType.MARKET_IF_TOUCHED,
        OrderType.LIMIT_IF_TOUCHED,
        OrderType.TRAILING_STOP_LIMIT,
    ],
)
def test_an_order_type_the_broker_has_no_equivalent_for_is_refused(denied: OrderType) -> None:
    """Sending the nearest thing instead would fill a strategy at a price it never asked for."""
    with pytest.raises(ValidationError) as failure:
        to_broker_order_type(denied)

    assert denied.name in str(failure.value)
    assert "market" in str(failure.value)


@pytest.mark.parametrize(
    ("engine", "broker"),
    [
        (TimeInForce.DAY, "day"),
        (TimeInForce.GTC, "gtc"),
        (TimeInForce.AT_THE_OPEN, "opg"),
        (TimeInForce.AT_THE_CLOSE, "cls"),
        (TimeInForce.IOC, "ioc"),
        (TimeInForce.FOK, "fok"),
    ],
)
def test_each_time_in_force_the_broker_offers_has_one_spelling(
    engine: TimeInForce, broker: str
) -> None:
    assert to_broker_time_in_force(engine) == broker


def test_the_one_time_in_force_the_broker_does_not_offer_is_refused() -> None:
    """Made good-till-cancel instead, a dated order would outlive the day it was meant for."""
    with pytest.raises(ValidationError) as failure:
        to_broker_time_in_force(TimeInForce.GTD)

    assert "GTD" in str(failure.value)


# --- the deterministic trade id ------------------------------------------------


def test_the_same_order_and_the_same_fill_produce_the_same_id() -> None:
    """The whole of what makes a restart produce no duplicate fill."""
    once = trade_id("O-20260905-160405-001-000-1", Decimal("10"))
    again = trade_id("O-20260905-160405-001-000-1", Decimal("10"))

    assert once == again


def test_the_same_quantity_spelled_differently_is_the_same_fill() -> None:
    """`10`, `10.0` and `1E+1` are one quantity, and one quantity is one fill."""
    assert trade_id("O-1", Decimal("10")) == trade_id("O-1", Decimal("10.0"))
    assert trade_id("O-1", Decimal("10")) == trade_id("O-1", Decimal("1E+1"))


def test_a_further_fill_of_the_same_order_is_a_different_fill() -> None:
    assert trade_id("O-1", Decimal("10")) != trade_id("O-1", Decimal("11"))


def test_a_different_order_is_a_different_fill() -> None:
    assert trade_id("O-1", Decimal("10")) != trade_id("O-2", Decimal("10"))


def test_the_id_fits_what_the_engine_holds() -> None:
    """A client order id may be 128 characters and a `TradeId` holds 36."""
    built = trade_id("O" * 128, Decimal("10"))

    assert len(str(built)) == TRADE_ID_DIGITS <= 36


# --- fills ---------------------------------------------------------------------


def test_an_unfilled_order_reports_no_fill() -> None:
    assert fill(order()) is None


def test_a_filled_order_reports_what_it_filled() -> None:
    found = fill(
        order(
            status="filled",
            filled_qty="10",
            filled_avg_price="303.42",
            filled_at="2026-09-05T16:04:06Z",
        )
    )

    assert found is not None
    assert str(found.last_qty) == "10"
    assert str(found.last_px) == "303.42"
    assert found.commission.as_double() == 0.0
    assert str(found.commission.currency) == "USD"
    assert found.ts_event == instant("2026-09-05T16:04:06Z")


def test_a_partial_already_applied_reports_only_what_is_new() -> None:
    """The row reports a cumulative fill, so a report is the difference from what is known."""
    row = order(
        status="partially_filled",
        filled_qty="10",
        filled_avg_price="303.42",
        filled_at="2026-09-05T16:04:06Z",
    )
    found = fill(row, already_filled=Decimal("4"))

    assert found is not None
    assert str(found.last_qty) == "6"


def test_reading_the_same_row_again_after_a_restart_reports_nothing() -> None:
    """The first half of a restart with zero duplicate fills: nothing new is nothing at all."""
    row = order(
        status="filled",
        filled_qty="10",
        filled_avg_price="303.42",
        filled_at="2026-09-05T16:04:06Z",
    )

    assert fill(row, already_filled=Decimal("10")) is None


def test_a_fill_reported_twice_carries_the_same_identity_either_way() -> None:
    """The second half: an id the order already carries is one the engine skips."""
    row = order(
        status="filled",
        filled_qty="10",
        filled_avg_price="303.42",
        filled_at="2026-09-05T16:04:06Z",
    )
    whole, remainder = fill(row), fill(row, already_filled=Decimal("4"))

    assert whole is not None and remainder is not None
    assert whole.trade_id == remainder.trade_id == trade_id(str(whole.client_order_id), Decimal(10))


def test_a_fill_with_no_price_is_a_fill_that_cannot_be_priced() -> None:
    with pytest.raises(ValidationError) as failure:
        fill(order(status="filled", filled_qty="10", filled_at="2026-09-05T16:04:06Z"))

    assert "average price" in str(failure.value)


def test_a_fill_the_row_has_not_timestamped_falls_back_to_its_last_update() -> None:
    found = fill(order(status="filled", filled_qty="10", filled_avg_price="303.42"))

    assert found is not None
    assert found.ts_event == instant("2026-09-05T16:04:05.300000000Z")


def test_the_fill_report_id_is_the_caller_s_when_it_gives_one() -> None:
    given = UUID4()
    row = order(status="filled", filled_qty="10", filled_avg_price="303.42")

    found = fill(row, report_id=given)

    assert found is not None and found.id == given


# --- positions -----------------------------------------------------------------


def test_a_long_position_reports_the_position_and_not_what_is_free_of_it() -> None:
    """A share committed to a resting order is still a share owned."""
    found = position_report(
        position(),
        account=ACCOUNT,
        instrument=AAPL,
        size_precision=SIZE_PRECISION,
        ts_init=NOW,
    )

    assert found.position_side is PositionSide.LONG
    assert str(found.quantity) == "10"
    assert found.avg_px_open == Decimal("303.42")


def test_a_short_position_keeps_its_direction_in_the_side() -> None:
    found = position_report(
        position(side="short", qty="-10", qty_available="-10"),
        account=ACCOUNT,
        instrument=AAPL,
        size_precision=SIZE_PRECISION,
        ts_init=NOW,
    )

    assert found.position_side is PositionSide.SHORT
    assert str(found.quantity) == "10"


def test_a_side_the_broker_does_not_write_is_refused() -> None:
    with pytest.raises(ValidationError) as failure:
        position_report(
            position(side="flat"),
            account=ACCOUNT,
            instrument=AAPL,
            size_precision=SIZE_PRECISION,
            ts_init=NOW,
        )

    assert "position.side" in str(failure.value)


# --- identity ------------------------------------------------------------------


def test_an_account_id_names_its_issuer() -> None:
    assert str(ACCOUNT) == f"ALPACA-{ACCOUNT_NUMBER}"


def test_the_brokers_symbol_is_the_instrument_without_its_venue() -> None:
    assert symbol_of(AAPL) == "AAPL"


def test_an_instrument_id_is_built_from_the_listing_venue_the_row_names() -> None:
    assert instrument_id_of("AAPL", "NASDAQ") == AAPL


def test_an_exchange_this_adapter_does_not_map_yields_no_instrument() -> None:
    """One instrument's failure, rather than an invented venue that re-keys everything."""
    assert instrument_id_of("AAPL", "MOON") is None


def test_a_client_order_id_travels_verbatim() -> None:
    """It is the only handle that survives a restart on both sides."""
    assert client_order_id("O-20260905-160405-001-000-1") == "O-20260905-160405-001-000-1"


@pytest.mark.parametrize("text", ["", "O" * 129])
def test_a_client_order_id_the_broker_would_refuse_is_refused_here(text: str) -> None:
    with pytest.raises(ValidationError) as failure:
        client_order_id(text)

    assert "client_order_id" in str(failure.value)


def test_the_module_exports_what_the_slices_above_it_import() -> None:
    """The names the execution, data and overlay modules build against."""
    for name in parsing.__all__:
        assert hasattr(parsing, name), name
