"""The conventions table: dated facts about ticks and lots, keyed by class, venue and price."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from kanso.data import conventions
from kanso.data.conventions import ANY, Band, Schedule, lot_size, schedule, tick_size
from kanso.errors import Exit, ValidationError

TODAY = date(2024, 6, 3)


def test_a_us_equity_ticks_in_cents_at_or_above_a_dollar() -> None:
    assert tick_size("EQUITY", "XNAS", 1.00, TODAY) == Decimal("0.01")
    assert tick_size("EQUITY", "XNAS", 250.0, TODAY) == Decimal("0.01")


def test_a_us_equity_ticks_in_hundredths_of_a_cent_below_a_dollar() -> None:
    assert tick_size("EQUITY", "XNAS", 0.9999, TODAY) == Decimal("0.0001")
    assert tick_size("EQUITY", "XNAS", 0.0, TODAY) == Decimal("0.0001")


def test_the_boundary_belongs_to_the_higher_band() -> None:
    """At exactly a dollar the coarser increment applies, which is what the rule says."""
    below, at = 0.999999, 1.0
    assert tick_size("EQUITY", "XNAS", below, TODAY) == Decimal("0.0001")
    assert tick_size("EQUITY", "XNAS", at, TODAY) == Decimal("0.01")


def test_the_lot_is_one_share() -> None:
    assert lot_size("EQUITY", "XNAS", TODAY) == Decimal("1")


def test_a_national_rule_is_keyed_to_every_venue_at_once() -> None:
    """No entry names XNAS; the wildcard answers for it, and for every other US venue."""
    assert ("EQUITY", "XNAS") not in conventions.SCHEDULES
    assert tick_size("EQUITY", "XNYS", 5.0, TODAY) == tick_size("EQUITY", "ARCX", 5.0, TODAY)


def test_a_venue_of_its_own_wins_over_the_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        conventions.SCHEDULES,
        ("EQUITY", "XNAS"),
        (
            Schedule(
                effective=date(2020, 1, 1),
                authority="a venue rulebook of its own",
                bands=(Band(Decimal(0), Decimal("0.005")),),
                lot=Decimal("100"),
            ),
        ),
    )
    assert tick_size("EQUITY", "XNAS", 5.0, TODAY) == Decimal("0.005")
    assert lot_size("EQUITY", "XNAS", TODAY) == Decimal("100")
    assert tick_size("EQUITY", "XNYS", 5.0, TODAY) == Decimal("0.01")


def test_a_dated_reassignment_is_a_data_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """Appending a schedule reassigns the tick from a date, and never rewrites the past."""
    shipped = conventions.SCHEDULES[("EQUITY", ANY)]
    later = Schedule(
        effective=date(2025, 11, 3),
        authority="a half-penny increment for tick-constrained stocks",
        bands=(Band(Decimal(0), Decimal("0.0001")), Band(Decimal("1.00"), Decimal("0.005"))),
        lot=Decimal("1"),
    )
    monkeypatch.setitem(conventions.SCHEDULES, ("EQUITY", ANY), (*shipped, later))

    assert tick_size("EQUITY", "XNAS", 5.0, date(2025, 11, 2)) == Decimal("0.01")
    assert tick_size("EQUITY", "XNAS", 5.0, date(2025, 11, 3)) == Decimal("0.005")
    assert tick_size("EQUITY", "XNAS", 0.5, date(2025, 11, 3)) == Decimal("0.0001")


def test_the_schedule_in_force_names_the_rule_that_set_it() -> None:
    found = schedule("EQUITY", ANY, TODAY)
    assert found.effective == date(2005, 8, 29)
    assert "Rule 612" in found.authority


def test_an_asset_class_with_nothing_on_file_refuses() -> None:
    with pytest.raises(ValidationError) as caught:
        tick_size("COMMODITY", "XCME", 70.0, TODAY)
    assert caught.value.code is Exit.VALIDATION
    assert "COMMODITY" in caught.value.message
    assert "XCME" in caught.value.message
    assert caught.value.remedy is not None
    assert "override" in caught.value.remedy


def test_a_date_before_every_schedule_refuses_rather_than_extrapolating() -> None:
    with pytest.raises(ValidationError, match="takes effect 2005-08-29"):
        tick_size("EQUITY", "XNAS", 5.0, date(1999, 1, 4))


def test_a_negative_price_has_no_tick() -> None:
    with pytest.raises(ValidationError, match="negative price"):
        tick_size("EQUITY", "XNAS", -1.0, TODAY)


@pytest.mark.parametrize("key", list(conventions.SCHEDULES))
def test_every_shipped_schedule_covers_every_price(key: tuple[str, str]) -> None:
    """Bands ascend from zero, so no non-negative price falls outside the ladder."""
    for item in conventions.SCHEDULES[key]:
        floors = [band.at_or_above for band in item.bands]
        assert floors[0] == Decimal(0)
        assert floors == sorted(floors)
        assert len(set(floors)) == len(floors)
        assert item.lot > 0
        assert item.authority
