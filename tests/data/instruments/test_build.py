"""Construction: an entry plus the convention table becomes an engine instrument."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from kanso.data.instruments import (
    build,
    conventions_for,
    definition_checksum,
    read_store,
    write_store,
)
from kanso.errors import Exit, ValidationError
from kanso.schemas import InstrumentEntry
from kanso.workspace import Workspace

from .conftest import AS_OF, CLASSES, EQUITY, FUTURE, INDEX, OPTION


def built(spec: dict[str, Any], as_of: date = AS_OF) -> Any:
    item = InstrumentEntry.model_validate(spec)
    return build(item, conventions_for(item, as_of))


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_every_instrument_class_the_spec_names_is_built(name: str) -> None:
    instrument = built(CLASSES[name])
    assert type(instrument).__name__ == name
    assert instrument.id.value == CLASSES[name]["nautilus_id"]


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_every_instrument_class_round_trips_through_the_store(ws: Workspace, name: str) -> None:
    instrument = built(CLASSES[name])
    write_store(ws, [instrument])
    held = read_store(ws)
    assert definition_checksum(instrument) in held
    assert type(held[definition_checksum(instrument)]).__name__ == name


def test_the_whole_set_round_trips_at_once(ws: Workspace) -> None:
    made = [built(spec) for spec in CLASSES.values()]
    write_store(ws, made)
    assert set(read_store(ws)) == {definition_checksum(item) for item in made}


def test_an_equity_takes_its_tick_and_lot_from_the_table() -> None:
    instrument = built(EQUITY)
    assert str(instrument.price_increment) == "0.01"
    assert instrument.price_precision == 2
    assert str(instrument.lot_size) == "1"


def test_the_timestamps_are_the_date_the_definition_was_resolved_as_of() -> None:
    instrument = built(EQUITY)
    midnight = int(datetime(2024, 6, 3, tzinfo=UTC).timestamp()) * 1_000_000_000
    assert instrument.ts_event == midnight
    assert instrument.ts_init == midnight
    assert instrument.ts_init >= instrument.ts_event


def test_an_override_wins_over_the_convention_table() -> None:
    instrument = built({**EQUITY, "override": {"currency": "USD", "price_increment": "0.05"}})
    assert str(instrument.price_increment) == "0.05"
    assert instrument.price_precision == 2


def test_the_precision_follows_the_increment_it_is_told_to_use() -> None:
    instrument = built({**EQUITY, "override": {"currency": "USD", "price_increment": "0.0001"}})
    assert instrument.price_precision == 4


def test_a_stated_reference_price_picks_the_band() -> None:
    penny = InstrumentEntry.model_validate(
        {**EQUITY, "attributes": {"reference_price": 0.40}},
    )
    assert conventions_for(penny, AS_OF)["price_increment"] == Decimal("0.0001")
    assert conventions_for(penny, AS_OF, price=5.0)["price_increment"] == Decimal("0.01")


def test_an_asset_class_with_no_schedule_defaults_nothing() -> None:
    """The table is silent for an index, so only the timestamps come from kanso."""
    item = InstrumentEntry.model_validate(INDEX)
    assert set(conventions_for(item, AS_OF)) == {"ts_event", "ts_init"}


def test_a_field_the_convention_table_cannot_supply_is_named() -> None:
    with pytest.raises(ValidationError) as caught:
        built({**INDEX, "override": {"currency": "USD"}})
    assert caught.value.code is Exit.VALIDATION
    assert "SPX.XCBO" in caught.value.message
    assert "price_increment" in caught.value.message
    assert caught.value.remedy is not None
    assert "override" in caught.value.remedy


def test_an_override_the_class_does_not_accept_is_refused() -> None:
    with pytest.raises(ValidationError, match="has no field expiry_month"):
        built({**EQUITY, "override": {"currency": "USD", "expiry_month": "Z4"}})


def test_an_override_may_not_forge_an_identity() -> None:
    with pytest.raises(ValidationError, match="may not set instrument_id, raw_symbol"):
        built(
            {
                **EQUITY,
                "override": {
                    "currency": "USD",
                    "instrument_id": "MSFT.XNAS",
                    "raw_symbol": "MSFT",
                },
            }
        )


def test_the_engines_own_refusal_is_reported_as_it_is() -> None:
    with pytest.raises(ValidationError, match="rejected its fields"):
        built(
            {
                **EQUITY,
                "override": {
                    "currency": "USD",
                    "price_increment": "0.01",
                    "price_precision": 4,
                },
            }
        )


def test_a_derivative_states_its_instrument_class() -> None:
    assert type(built(OPTION)).__name__ == "OptionContract"
    assert type(built(FUTURE)).__name__ == "FuturesContract"


def test_an_instrument_class_kanso_does_not_build_is_named() -> None:
    with pytest.raises(ValidationError, match="names no instrument class"):
        built({**FUTURE, "override": {**FUTURE["override"], "instrument_class": "warrant"}})


def test_an_asset_class_implying_no_class_asks_for_one() -> None:
    with pytest.raises(ValidationError) as caught:
        built({**EQUITY, "asset_class": "COMMODITY"})
    assert "implies no instrument class" in caught.value.message
    assert caught.value.remedy is not None
    assert "instrument_class" in caught.value.remedy


def test_the_multiplier_is_never_guessed() -> None:
    """A contract size of one is a claim about the contract, so an absent one refuses."""
    without = dict(FUTURE["override"])
    del without["multiplier"]
    with pytest.raises(ValidationError, match="needs multiplier"):
        built({**FUTURE, "override": without})


def test_an_optional_engine_field_reaches_the_constructor() -> None:
    instrument = built({**EQUITY, "override": {"currency": "USD", "isin": "US0378331005"}})
    assert instrument.isin == "US0378331005"


def test_a_listing_date_is_accepted_as_a_date_or_as_nanoseconds() -> None:
    by_date = built(FUTURE)
    by_ns = built(
        {
            **FUTURE,
            "override": {
                **FUTURE["override"],
                "activation_ns": by_date.activation_ns,
                "expiration_ns": by_date.expiration_ns,
            },
        }
    )
    assert definition_checksum(by_ns) == definition_checksum(by_date)


def test_a_field_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(ValidationError, match="rejected its fields"):
        built({**EQUITY, "override": {"currency": "USD", "price_increment": "one cent"}})


def test_writing_nothing_writes_nothing(ws: Workspace) -> None:
    write_store(ws, [])
    assert read_store(ws) == {}
