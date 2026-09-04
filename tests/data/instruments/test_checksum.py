"""Content addressing: what a snapshot pins when it pins an instrument set."""

from __future__ import annotations

import random
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.data.instruments import (
    build,
    conventions_for,
    definition_checksum,
    instruments_checksum,
)
from kanso.schemas import InstrumentEntry

from .conftest import AS_OF, CLASSES, EQUITY

SHA256 = 64


def made(spec: dict[str, Any]) -> Any:
    entry = InstrumentEntry.model_validate(spec)
    return build(entry, conventions_for(entry, AS_OF))


def universe() -> list[Any]:
    return [made(spec) for spec in CLASSES.values()]


def test_a_checksum_is_a_content_address() -> None:
    digest = instruments_checksum(universe())
    assert len(digest) == SHA256
    assert set(digest) <= set("0123456789abcdef")


def test_the_same_set_in_another_order_pins_the_same_instruments() -> None:
    made_once = universe()
    shuffled = list(made_once)
    random.Random(7).shuffle(shuffled)
    assert shuffled != made_once
    assert instruments_checksum(shuffled) == instruments_checksum(made_once)


@given(st.permutations(list(CLASSES)))
def test_no_ordering_of_the_universe_changes_the_pin(order: list[str]) -> None:
    reordered = [made(CLASSES[name]) for name in order]
    assert instruments_checksum(reordered) == instruments_checksum(universe())


def test_the_same_instrument_twice_is_the_same_set_once() -> None:
    one = made(EQUITY)
    assert instruments_checksum([one, one]) == instruments_checksum([one])


def test_a_changed_definition_changes_the_pin() -> None:
    nickel = made({**EQUITY, "override": {"currency": "USD", "price_increment": "0.05"}})
    assert definition_checksum(nickel) != definition_checksum(made(EQUITY))
    assert instruments_checksum([nickel]) != instruments_checksum([made(EQUITY)])


def test_an_added_instrument_changes_the_pin() -> None:
    assert instruments_checksum(universe()) != instruments_checksum(universe()[:-1])


def test_an_empty_universe_pins_something_stable() -> None:
    assert instruments_checksum([]) == instruments_checksum([])
    assert instruments_checksum([]) != instruments_checksum([made(EQUITY)])


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_building_the_same_entry_twice_addresses_the_same_definition(name: str) -> None:
    assert definition_checksum(made(CLASSES[name])) == definition_checksum(made(CLASSES[name]))
