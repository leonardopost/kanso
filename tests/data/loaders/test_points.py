"""Building points: the exact conversions, and what they refuse."""

from __future__ import annotations

import pytest

from kanso.data.loaders.points import (
    aggressor,
    bar_type,
    instrument_id,
    ticks_to_price,
    units_to_quantity,
    zone,
)
from kanso.errors import ValidationError


def test_ticks_reach_prices_without_a_float_in_between() -> None:
    """Integer ticks and decimal text, so the same spec is the same bytes everywhere."""
    assert str(ticks_to_price(10_050, 2)) == "100.50"
    assert str(ticks_to_price(7, 4)) == "0.0007"
    assert str(ticks_to_price(-125, 2)) == "-1.25"
    assert str(ticks_to_price(5, 0)) == "5"
    assert str(units_to_quantity(1_250, 2)) == "12.50"


def test_a_bar_type_is_external_and_last_priced() -> None:
    instrument = instrument_id("DEMO", "XNAS")
    assert str(bar_type(instrument, "15m")) == "DEMO.XNAS-15-MINUTE-LAST-EXTERNAL"
    assert str(bar_type(instrument, "1d")) == "DEMO.XNAS-1-DAY-LAST-EXTERNAL"


def test_a_resolution_that_is_not_a_bar_size_is_refused() -> None:
    instrument = instrument_id("DEMO", "XNAS")
    with pytest.raises(ValidationError, match="is not a bar size"):
        bar_type(instrument, "tick")


def test_a_bar_of_no_length_is_refused() -> None:
    instrument = instrument_id("DEMO", "XNAS")
    with pytest.raises(ValidationError, match="must be longer than zero"):
        bar_type(instrument, "0m")


def test_an_unbuildable_instrument_id_is_refused() -> None:
    with pytest.raises(ValidationError, match="is not an instrument id"):
        instrument_id("", "XNAS")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("BUY", "BUYER"), ("s", "SELLER"), ("Seller", "SELLER"), ("none", "NO_AGGRESSOR")],
)
def test_a_side_is_read_from_any_common_spelling(text: str, expected: str) -> None:
    from nautilus_trader.model.enums import AggressorSide

    assert aggressor(text) == getattr(AggressorSide, expected)


def test_an_unknown_zone_is_a_plain_value_error() -> None:
    """A spec field's failure, so the model that owns the field renders it."""
    with pytest.raises(ValueError, match="not an IANA time zone"):
        zone("Mars/Olympus")
    assert zone("UTC") is not None
