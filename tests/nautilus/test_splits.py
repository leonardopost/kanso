"""The split schedule and the arithmetic it changes, as pure functions over numbers.

Everything here is the half of `kanso.nautilus.splits` that touches no engine: what a
schedule entry may say, what a share count becomes, and what a position's own fills and
adjustments make it. The engine half — `apply_to`, and the refusal a window without a
schedule earns — is exercised where the runner is, against a real backtest.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus.splits import (
    KEY,
    UTC_ZONE,
    ZONE_KEY,
    Move,
    Split,
    in_lieu,
    ledger,
    quantity_after,
    schedule,
    schedule_of,
    unscheduled,
)

EX = date(2024, 1, 6)
WHO = "SOXS.XNAS"


def entry(**changes: object) -> dict[str, object]:
    """One schedule entry, with these fields replaced."""
    return {"ex_date": "2024-01-06", "ratio": 0.1, **changes}


def info(*entries: object) -> dict[str, object]:
    """An instrument's `info` carrying these schedule entries."""
    return {KEY: list(entries)}


# --- what a schedule may say --------------------------------------------------


@pytest.mark.parametrize("carried", [None, {}, {"other": 1}, {KEY: None}])
def test_an_instrument_that_schedules_nothing_has_no_splits(carried: object) -> None:
    """A definition with no schedule is the normal case and is silent, not a failure."""
    assert schedule(carried, WHO) == ()  # type: ignore[arg-type]


def test_an_entry_is_read_as_the_day_and_the_ratio_it_states() -> None:
    assert schedule(info(entry()), WHO) == (Split(ex_date=EX, ratio=0.1),)


@pytest.mark.parametrize("written", ["2024-01-06", date(2024, 1, 6), datetime(2024, 1, 6, 9, 30)])
def test_an_ex_date_is_the_same_day_however_yaml_handed_it_over(written: object) -> None:
    """A YAML date, a timestamp and a string are one fact written three ways."""
    assert schedule(info(entry(ex_date=written)), WHO)[0].ex_date == EX


def test_a_schedule_is_read_in_ex_date_order_whatever_order_it_was_written_in() -> None:
    late, early = entry(ex_date="2024-06-01"), entry(ex_date="2024-01-06", ratio=4.0)

    assert [split.ex_date for split in schedule(info(late, early), WHO)] == [
        date(2024, 1, 6),
        date(2024, 6, 1),
    ]


def test_a_split_takes_effect_just_after_the_midnight_that_opens_its_ex_date() -> None:
    """After, not at: a point stamped at that midnight closes the previous session."""
    assert Split(ex_date=date(1970, 1, 2), ratio=0.5).effective_ns == 86_400_000_000_001


@pytest.mark.parametrize(
    ("ex_date", "hours_behind"), [(date(2021, 3, 2), 5), (date(2024, 7, 15), 4)]
)
def test_a_split_takes_effect_just_after_midnight_where_its_instrument_trades(
    ex_date: date, hours_behind: int
) -> None:
    """The 2021 SOXL split under UTC took effect at 19:00 New York, inside the post-market
    of a session already trading at the new price, and adjusted positions bought that day a
    second time. Dated in New York it falls between the 20:00 close and the 04:00 open, and
    New York midnight is 05:00Z in winter and 04:00Z in summer."""
    from kanso.criteria.run import NS_PER_SECOND, midnight_ns

    split = Split(ex_date=ex_date, ratio=15.0, zone="America/New_York")

    assert split.effective_ns == midnight_ns(ex_date) + hours_behind * 3_600 * NS_PER_SECOND + 1


def test_a_schedule_is_dated_in_the_zone_its_instrument_names() -> None:
    carried = {**info(entry(), entry(ex_date="2024-06-01")), ZONE_KEY: "America/New_York"}

    assert {split.zone for split in schedule(carried, WHO)} == {"America/New_York"}


def test_a_schedule_whose_instrument_names_no_zone_is_dated_in_utc() -> None:
    assert schedule(info(entry()), WHO)[0].zone == UTC_ZONE


@pytest.mark.parametrize("zone", ["", 5, None, "Mars/Olympus_Mons", "../etc"])
def test_a_zone_that_names_no_zone_is_refused(zone: object) -> None:
    with pytest.raises(ValidationError) as raised:
        schedule({**info(entry()), ZONE_KEY: zone}, WHO)

    assert f"info.{ZONE_KEY}" in raised.value.message
    assert "America/New_York" in (raised.value.remedy or "")


def test_a_mistyped_zone_is_refused_before_any_split_is_declared() -> None:
    with pytest.raises(ValidationError, match="is not a zone"):
        schedule({ZONE_KEY: "America/NewYork"}, WHO)


def test_a_price_printed_at_the_midnight_is_restated_and_one_printed_after_is_not() -> None:
    """The sleeve's view and the venue's agree on which side of the split a point is."""
    from kanso.nautilus.splits import restating

    split = Split(ex_date=EX, ratio=0.1, zone="America/New_York")
    midnight = split.effective_ns - 1

    assert restating((split,), midnight, midnight + 60_000_000_000) == 0.1
    assert restating((split,), midnight + 1, midnight + 60_000_000_000) == 1.0
    assert restating((split,), midnight - 1, midnight) == 1.0


def test_a_schedule_that_is_not_a_list_is_refused() -> None:
    with pytest.raises(ValidationError, match="is dict, and a split schedule is a list"):
        schedule({KEY: entry()}, WHO)


def test_an_entry_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"info.splits\[0\] is str"):
        schedule(info("2024-01-06"), WHO)


def test_a_schedule_carries_no_cash_and_says_where_cash_belongs() -> None:
    """The one refusal that names the design: money moves once, in the extraction."""
    with pytest.raises(ValidationError) as raised:
        schedule(info(entry(cash=0.25)), WHO)

    assert "names cash" in raised.value.message
    assert "a schedule carries no cash" in raised.value.message
    assert "corporate_action" in (raised.value.remedy or "")


def test_an_entry_missing_a_field_is_refused_by_the_field_it_is_missing() -> None:
    with pytest.raises(ValidationError, match="declares no ratio"):
        schedule(info({"ex_date": "2024-01-06"}), WHO)


def test_an_ex_date_that_is_not_a_day_is_refused() -> None:
    with pytest.raises(ValidationError, match="is not a calendar day"):
        schedule(info(entry(ex_date="the sixth")), WHO)


def test_a_ratio_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(ValidationError, match="is not a number"):
        schedule(info(entry(ratio="ten for one")), WHO)


@pytest.mark.parametrize("ratio", [0.0, -1.0])
def test_a_ratio_that_leaves_no_shares_is_refused(ratio: float) -> None:
    with pytest.raises(ValidationError, match="is not a positive number of shares"):
        schedule(info(entry(ratio=ratio)), WHO)


def test_a_ratio_of_one_is_not_a_split() -> None:
    """An entry that changes nothing is a mistake, and a silent no-op would hide it."""
    with pytest.raises(ValidationError, match="leaves the position exactly as it was"):
        schedule(info(entry(ratio=1.0)), WHO)


def test_two_entries_on_one_day_are_refused() -> None:
    with pytest.raises(ValidationError, match="declares 1 ex-date\\(s\\) twice"):
        schedule(info(entry(), entry(ratio=4.0)), WHO)


def test_a_schedule_is_read_off_an_instrument_by_its_own_name() -> None:
    class Definition:
        id = "SOXS.XNAS"
        info = {KEY: [entry()]}

    assert schedule_of(Definition()) == (Split(ex_date=EX, ratio=0.1),)


def test_an_instrument_class_that_carries_no_info_at_all_schedules_nothing() -> None:
    """Only an equity accepts `info`; the other four classes are asked the same question."""

    class Future:
        id = "ESZ4.XCME"

    assert schedule_of(Future()) == ()


# --- what a share count becomes ----------------------------------------------


@pytest.mark.parametrize(
    ("held", "ratio", "expected"),
    [
        (100.0, 4.0, 400.0),
        (1000.0, 0.1, 100.0),
        (1005.0, 0.1, 100.0),
        (-1005.0, 0.1, -100.0),
        (-100.0, 4.0, -400.0),
        (9.0, 0.1, 0.0),
    ],
)
def test_a_split_leaves_whole_lots_and_drops_the_residue(
    held: float, ratio: float, expected: float
) -> None:
    """1,005 shares through a one-for-ten reverse split is 100 whole shares and a residue."""
    assert quantity_after(held, ratio, 1.0) == expected


def test_a_lot_size_larger_than_one_floors_onto_the_lot() -> None:
    assert quantity_after(1005.0, 0.1, 25.0) == 100.0


# --- what a position's own moves make it -------------------------------------


def buy(ts: int, qty: float, px: float) -> Move:
    return Move(ts_ns=ts, qty=qty, px=px)


def sell(ts: int, qty: float, px: float) -> Move:
    return Move(ts_ns=ts, qty=-qty, px=px)


def split(ts: int, change: float, ratio: float | None = None, cash: float = 0.0) -> Move:
    return Move(ts_ns=ts, qty=change, ratio=ratio, cash=cash)


def test_a_position_with_no_moves_at_all_is_empty() -> None:
    book = ledger(())

    assert (book.peak, book.avg_open, book.avg_close, book.realized) == (0.0, 0.0, 0.0, 0.0)
    assert book.basis == 0.0


def test_a_plain_round_trip_is_what_the_engine_would_have_said() -> None:
    """The reduction that lets one arithmetic serve a split and a position without one."""
    book = ledger([buy(1, 100.0, 10.0), sell(2, 100.0, 12.0)])

    assert (book.peak, book.avg_open, book.avg_close, book.realized) == (100.0, 10.0, 12.0, 200.0)


def test_scaling_in_blends_the_basis_the_way_the_engine_blends_it() -> None:
    book = ledger([buy(1, 50.0, 10.0), buy(2, 50.0, 12.0), sell(3, 100.0, 11.0)])

    assert (book.peak, book.avg_open, book.avg_close, book.realized) == (100.0, 11.0, 11.0, 0.0)


def test_a_short_round_trip_realises_the_other_way() -> None:
    book = ledger([sell(1, 100.0, 12.0), buy(2, 100.0, 10.0)])

    assert (book.peak, book.avg_open, book.avg_close, book.realized) == (100.0, 12.0, 10.0, 200.0)


def test_a_multiplier_scales_the_profit_and_not_the_prices() -> None:
    book = ledger([buy(1, 10.0, 10.0), sell(2, 10.0, 12.0)], multiplier=50.0)

    assert (book.avg_open, book.avg_close, book.realized) == (10.0, 12.0, 1_000.0)


def test_a_fill_that_flips_a_position_closes_it_and_opens_the_rest() -> None:
    """The engine splits such a fill across two positions; the arithmetic does not rely on it."""
    book = ledger([buy(1, 100.0, 10.0), sell(2, 150.0, 12.0)])

    assert book.realized == 200.0
    assert book.peak == 100.0
    assert book.avg_open == pytest.approx((100 * 10.0 + 50 * 12.0) / 150.0)


def test_a_reverse_split_leaves_the_book_value_exactly_where_it_was() -> None:
    """1,005 at ten becomes 100 at a hundred and a half: the same 10,050 of basis."""
    book = ledger([buy(1, 1005.0, 10.0), split(2, -905.0), sell(3, 100.0, 100.0)])

    assert book.peak == 1005.0
    assert book.avg_open == 10.0
    assert book.avg_close == pytest.approx(10_000.0 / 1005.0)
    assert book.realized == pytest.approx(-50.0)


def test_a_forward_split_is_the_same_arithmetic_the_other_way() -> None:
    book = ledger([buy(1, 100.0, 40.0), split(2, 300.0), sell(3, 400.0, 10.0)])

    assert book.peak == 100.0
    assert book.avg_open == 40.0
    assert book.avg_close == 40.0
    assert book.realized == 0.0


def test_a_short_position_through_a_split_is_rescaled_the_same_way() -> None:
    book = ledger([sell(1, 1000.0, 10.0), split(2, 900.0), buy(3, 100.0, 100.0)])

    assert book.peak == 1000.0
    assert book.avg_open == 10.0
    assert book.realized == 0.0


def test_a_split_is_applied_before_a_fill_stamped_at_the_same_instant() -> None:
    """A sleeve adjusts before its own handler trades, and the replay must agree."""
    book = ledger([buy(1, 1000.0, 10.0), sell(2, 100.0, 100.0), split(2, -900.0)])

    assert book.realized == 0.0


@pytest.mark.parametrize("ratio", [0.001, None])
def test_a_position_paid_out_to_flat_realises_its_whole_book(ratio: float | None) -> None:
    """100 shares bought at ten, a one-for-a-thousand reverse split, 900 paid in lieu: the
    position closed at nine a share, whether or not the ledger knows the split's ratio."""
    book = ledger([buy(1, 100.0, 10.0), split(2, -100.0, ratio=ratio, cash=900.0)])

    assert book.peak == 100.0
    assert book.realized == pytest.approx(-100.0)


def test_a_fraction_paid_at_cost_realises_nothing_and_counts_as_coming_back() -> None:
    """1,005 at ten, one-for-ten, five old shares paid at ten: the payout is a close at cost,
    and what came back per opening share is still ten once the 100 are sold at a hundred."""
    book = ledger(
        [buy(1, 1_005.0, 10.0), split(2, -905.0, ratio=0.1, cash=50.0), sell(3, 100.0, 100.0)]
    )

    assert book.realized == pytest.approx(0.0)
    assert book.avg_close == pytest.approx(10.0)
    assert book.peak == pytest.approx(1_005.0)


def test_a_short_owes_the_fraction_it_cannot_deliver() -> None:
    """Short 1,005 at ten through one-for-ten: 100 new shares still owed, five old shares
    bought back in lieu at twelve — a debit of 60, and a loss of ten on those five."""
    book = ledger([sell(1, 1_005.0, 10.0), split(2, 905.0, ratio=0.1, cash=-60.0)])

    assert book.realized == pytest.approx(-10.0)
    assert book.basis == pytest.approx(100.0)


def test_without_the_ratio_a_payout_is_realised_whole_and_the_total_is_the_same() -> None:
    """The fraction's cost stays in the shares still held, and the payout is realised at
    once; once the position closes the two ways of booking it agree to the cent."""
    known = ledger(
        [buy(1, 1_005.0, 10.0), split(2, -905.0, ratio=0.1, cash=40.0), sell(3, 100.0, 90.0)]
    )
    unknown = ledger([buy(1, 1_005.0, 10.0), split(2, -905.0, cash=40.0), sell(3, 100.0, 90.0)])

    assert unknown.realized == pytest.approx(known.realized)
    assert known.realized == pytest.approx(40.0 - 50.0 + (90.0 - 100.0) * 100.0)


def test_a_float_ratio_does_not_cost_a_share_the_issuer_delivers() -> None:
    """1,200 shares through a one-for-twelve reverse split are 100, not 99: the ratio is a
    float, and 1,200 x 0.0833... is 99.999999999999996 until it is rounded."""
    assert quantity_after(1_200.0, 1.0 / 12.0, 1.0) == 100.0
    assert in_lieu(1_200.0, 1.0 / 12.0, 1.0, 5.0, 1.0) == 0.0


def test_the_fraction_is_paid_at_the_old_price_and_a_short_pays_it() -> None:
    assert in_lieu(1_005.0, 0.1, 1.0, 10.0, 1.0) == pytest.approx(50.0)
    assert in_lieu(-1_005.0, 0.1, 1.0, 10.0, 1.0) == pytest.approx(-50.0)
    assert in_lieu(5.0, 0.1, 1.0, 10.0, 100.0) == pytest.approx(5_000.0)


def test_the_basis_is_the_one_number_quoted_in_the_shares_still_held() -> None:
    """What marks a position no price has been published for, in place of `avg_px_open`.

    1,005 shares at ten cost 10,050; after a one-for-ten reverse split the same 10,050 is
    carried by 100 shares, so the basis is 100.50 while `avg_open` — the price actually
    paid, in the units the position opened in — is still 10. Marking 100 shares at 10
    would understate the position tenfold, which is exactly what reading the engine's own
    `avg_px_open` does.
    """
    book = ledger([buy(1, 1005.0, 10.0), split(2, -905.0)])

    assert book.avg_open == 10.0
    assert book.basis == pytest.approx(100.5)
    assert book.basis * 100.0 == pytest.approx(1005.0 * 10.0)


def test_a_move_knows_whether_it_was_an_execution() -> None:
    assert buy(1, 1.0, 10.0).is_fill
    assert not split(1, -1.0).is_fill


@given(
    qty=st.floats(min_value=1.0, max_value=1_000.0),
    opened=st.floats(min_value=0.01, max_value=1_000.0),
    closed=st.floats(min_value=0.01, max_value=1_000.0),
)
def test_a_round_trip_realises_the_difference_between_its_two_prices(
    qty: float, opened: float, closed: float
) -> None:
    """Whatever was paid and whatever came back, the profit is the gap times the size."""
    book = ledger([buy(1, qty, opened), sell(2, qty, closed)])

    assert book.realized == pytest.approx((closed - opened) * qty, rel=1e-9, abs=1e-6)
    assert book.avg_open == pytest.approx(opened)
    assert book.avg_close == pytest.approx(closed)


# --- what a window may hold that a definition does not -----------------------


class Definition:
    """The one thing `unscheduled` asks of an instrument: its id and its schedule."""

    def __init__(self, name: str, carried: dict[str, object] | None = None) -> None:
        self.id = name
        self.info = carried


def point(name: str = WHO, kind: str = "split", ratio: float = 0.1, ex: date = EX) -> object:
    from nautilus_trader.model.identifiers import InstrumentId

    from kanso.criteria.run import midnight_ns
    from kanso.data.types import CorporateAction

    ts = midnight_ns(ex)
    return CorporateAction(
        instrument_id=InstrumentId.from_str(name),
        kind=kind,
        ratio=ratio,
        cash=0.0,
        currency="USD",
        ex_date_ns=ts,
        ts_event=ts,
        ts_init=ts,
    )


WINDOW = (date(2024, 1, 1), date(2024, 1, 31))


def test_a_split_the_definition_schedules_is_no_refusal() -> None:
    unscheduled([Definition(WHO, info(entry()))], [point()], WINDOW)


def test_a_split_for_an_instrument_this_run_holds_no_definition_for_is_another_run_s() -> None:
    """The runner loads only what the universe names; this is the guard behind that."""
    unscheduled([Definition("AAPL.XNAS")], [point()], WINDOW)


def test_a_point_that_is_not_a_corporate_action_is_ignored() -> None:
    unscheduled([Definition(WHO)], [object()], WINDOW)


def test_a_split_effective_outside_the_window_is_another_window_s() -> None:
    unscheduled([Definition(WHO)], [point(ex=date(2024, 3, 1))], WINDOW)


def test_a_split_the_definition_does_not_schedule_is_refused() -> None:
    with pytest.raises(PreconditionError, match="its definition schedules none"):
        unscheduled([Definition(WHO)], [point()], WINDOW)


def test_a_ratio_written_two_ways_is_still_one_ratio() -> None:
    """A float is not exact; a schedule and a vendor row that agree must not be made to differ."""
    unscheduled([Definition(WHO, info(entry(ratio=0.1)))], [point(ratio=0.1 + 1e-17)], WINDOW)
